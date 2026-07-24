"""Run one bounded instance-0 personal automation episode.

Without ``--execute`` this command performs lifecycle, identity, capture, OCR,
classification, and planning only. With ``--execute`` it may also confirm one
strictly detected resource update, observe its download, recover the package
once, dismiss known overlays, and enter the city before stopping at CITY_DETAIL.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import sys
import time
import uuid
from pathlib import Path

import cv2 as cv
import psutil


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.control.adb import ADB  # noqa: E402
from core.control.adb_port import EmulatorInfo, EmulatorType  # noqa: E402
from core.image.image import Image  # noqa: E402
from core.services.emulator_lifecycle import (  # noqa: E402
    GAME_PACKAGE,
    EmulatorLifecycle,
    LifecycleOptions,
    MuMuManagerClient,
)
from core.services.personal_action_budget import EpisodeActionBudget  # noqa: E402
from core.services.personal_automation_runtime import (  # noqa: E402
    GameWindowCandidate,
    PersonalAutomationRuntime,
)
from core.services.personal_runtime_episode import (  # noqa: E402
    ActionExecutor,
    ActionPlanner,
    CoordinateTransform,
    EpisodePolicy,
    PersonalAutomationEpisode,
    ResourceUpdateHandler,
    ResourceUpdatePolicy,
    RunRecorder,
    StateDetector,
)


DEFAULT_OUTPUT = ROOT / "dist" / "debug_private" / "personal-fast-loop-v1-20260723"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--install-path", type=Path, required=True)
    parser.add_argument("--instance-id", type=int, default=0)
    parser.add_argument("--package-id", default=GAME_PACKAGE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--resource-update-timeout", type=float, default=600.0)
    parser.add_argument("--resource-update-stall-timeout", type=float, default=120.0)
    parser.add_argument("--observation-interval", type=float, default=0.75)
    parser.add_argument("--resource-update-minimum-mb", type=float, default=0.01)
    parser.add_argument("--resource-update-maximum-mb", type=float, default=2048.0)
    parser.add_argument("--resource-update-exact-mb", type=float, action="append", default=[])
    parser.add_argument(
        "--disable-resource-update-auto-confirm", action="store_true"
    )
    parser.add_argument("--execute", action="store_true")
    return parser.parse_args()


def require_exact_target(args: argparse.Namespace) -> None:
    if args.instance_id != 0:
        raise PermissionError("personal_episode_instance_must_be_zero")
    if args.package_id != GAME_PACKAGE:
        raise PermissionError("personal_episode_package_mismatch")


def window_identity(info: dict) -> dict:
    if not sys.platform.startswith("win"):
        raise RuntimeError("windows_window_identity_required")
    main_handle = int(str(info.get("main_wnd") or "0"), 16)
    render_handle = int(str(info.get("render_wnd") or "0"), 16)
    manager_pid = int(info.get("pid") or 0)
    if not main_handle or not render_handle or manager_pid <= 0:
        raise RuntimeError("instance_zero_window_identity_incomplete")
    user32 = ctypes.windll.user32
    main_pid = ctypes.c_ulong()
    render_pid = ctypes.c_ulong()
    user32.GetWindowThreadProcessId(main_handle, ctypes.byref(main_pid))
    user32.GetWindowThreadProcessId(render_handle, ctypes.byref(render_pid))
    buffer = ctypes.create_unicode_buffer(512)
    user32.GetWindowTextW(main_handle, buffer, len(buffer))
    process_path = psutil.Process(manager_pid).exe()
    if int(main_pid.value) != manager_pid or int(render_pid.value) != manager_pid:
        raise RuntimeError("manager_window_pid_mismatch")
    if Path(process_path).name.casefold() != "mumunxdevice.exe":
        raise RuntimeError("manager_process_not_mumuv5")
    return {
        "title": buffer.value,
        "pid": manager_pid,
        "main_handle": f"0x{main_handle:08X}",
        "render_handle": f"0x{render_handle:08X}",
        "process_path": process_path,
        "runtime_family": "MuMuV5",
        "instance_index": 0,
        "package_id": GAME_PACKAGE,
    }


def foreground_window(candidate: GameWindowCandidate) -> bool:
    handle = int(candidate.window_handle, 16)
    user32 = ctypes.windll.user32
    user32.ShowWindow(handle, 9)
    return bool(user32.SetForegroundWindow(handle)) or user32.GetForegroundWindow() == handle


def stable_other_instances(before: dict, after: dict) -> bool:
    stable_keys = ("index", "pid", "created_timestamp", "is_process_started", "is_android_started", "adb_port")
    for key, value in before.items():
        if key == "0" or not isinstance(value, dict):
            continue
        current = after.get(key)
        if not isinstance(current, dict):
            return False
        if any(value.get(field) != current.get(field) for field in stable_keys):
            return False
    return True


def main() -> int:
    args = parse_args()
    require_exact_target(args)
    args.output.mkdir(parents=True, exist_ok=True)
    correlation_id = uuid.uuid4().hex
    device = EmulatorInfo(
        name="雷索纳斯 instance 0",
        port=None,
        path=str(args.install_path),
        type=EmulatorType.MUMUV5,
        index=0,
    )
    options = LifecycleOptions(
        emulator_start_timeout=args.timeout,
        stopping_settle_timeout=min(60.0, args.timeout),
        android_boot_timeout=args.timeout,
        game_start_timeout=args.timeout,
        close_game_when_idle=False,
        close_emulator_when_idle=False,
        poll_interval=1.0,
    )
    manager = MuMuManagerClient(device, timeout=10.0, correlation_id=correlation_id)
    lifecycle = EmulatorLifecycle(
        device,
        options=options,
        manager=manager,
        correlation_id=correlation_id,
    )
    budget = EpisodeActionBudget()

    def candidates() -> list[GameWindowCandidate]:
        info = manager.info()
        identity = window_identity(info)
        return [
            GameWindowCandidate(
                window_handle=str(identity["main_handle"]),
                process_id=int(identity["pid"]),
                instance_index=0,
                package_id=GAME_PACKAGE,
                runtime_family=str(identity["runtime_family"]),
                title=str(identity["title"]),
                render_child_handle=str(identity["render_handle"]),
            )
        ]

    runtime = PersonalAutomationRuntime(
        lifecycle=lifecycle,
        window_candidates=candidates,
        foreground_window=foreground_window,
        budget=budget,
    )
    adb = None
    result_payload: dict = {
        "task": "RESOURCE_UPDATE_COMPLETE_AND_CONTINUE_TO_CITY_DETAIL_V1",
        "correlation_id": correlation_id,
        "execute_authorized": bool(args.execute),
        "instance_index": 0,
        "package_id": GAME_PACKAGE,
    }
    physical_action_count = 0
    try:
        before_all = manager.all_info()
        runtime.ensure_mumu_instance_running()
        runtime.ensure_package_running()
        (
            runtime.ensure_game_window_foreground()
            if args.execute
            else runtime.ensure_game_window_available()
        )
        ready = lifecycle.device
        target_info = manager.info()
        game_info = manager.game_info(GAME_PACKAGE)
        identity = window_identity(target_info)
        if str(game_info.get("state", "")).casefold() != "running":
            raise RuntimeError("target_game_not_running")
        foregrounded = bool(args.execute)

        adb = ADB()
        if not adb.connect(int(ready.port)):
            raise RuntimeError("instance_zero_adb_connect_failed")
        capture_sequence = 0
        recorder = RunRecorder()

        def frame_provider():
            nonlocal capture_sequence
            raw = adb.screenshot()
            capture_sequence += 1
            cv.imwrite(str(args.output / f"frame-{capture_sequence:03d}.png"), raw)
            return Image(raw)

        detector = StateDetector()
        resource_policy = ResourceUpdatePolicy(
            auto_confirm_enabled=not args.disable_resource_update_auto_confirm,
            minimum_size_mb=args.resource_update_minimum_mb,
            maximum_size_mb=args.resource_update_maximum_mb,
            allowed_exact_sizes_mb=tuple(args.resource_update_exact_mb),
            timeout_seconds=args.resource_update_timeout,
            stall_timeout_seconds=args.resource_update_stall_timeout,
        )
        resource_policy.validate()
        planner = ActionPlanner(ResourceUpdateHandler(resource_policy))
        first = detector.detect(frame_provider())
        first_plan = planner.plan(first, budget=budget)
        result_payload["preflight"] = {
            "state": first.state.value,
            "planned_action": first_plan.action.value,
            "capture_point": first_plan.capture_point,
            "evidence": first.evidence,
            "frame_hash": first.frame_hash,
            "frame_dimensions": first.frame_dimensions,
            "announcement_candidates": [candidate.point for candidate in first.announcement_candidates],
            "resource_size_mb": first.resource_size_mb,
            "resource_confirm_bbox": first.resource_confirm_bbox,
            "resource_progress_percent": first.resource_progress_percent,
            "download_complete_text": first.download_complete_text,
            "tap_to_enter_text": first.tap_to_enter_text,
            "confidence": first.confidence,
            "reason_codes": first.reason_codes,
            "authorized_window_match_count": 1,
        }
        if not args.execute:
            result_payload.update(
                {
                    "status": "DRY_RUN_PASS",
                    "final_state": first.state.value,
                    "real_ui_actions": budget.total_actions,
                    "input_dispatch_count": 0,
                    "window": identity,
                    "window_foregrounded": False,
                }
            )
        else:
            pending = [Image(cv.imread(str(args.output / "frame-001.png"), cv.IMREAD_COLOR))]

            def episode_frame_provider():
                return pending.pop(0) if pending else frame_provider()

            policy = EpisodePolicy(
                episode_timeout_seconds=args.timeout,
                resource_update_timeout_seconds=args.resource_update_timeout,
                resource_update_stall_timeout_seconds=args.resource_update_stall_timeout,
                minimum_action_interval_seconds=args.observation_interval,
                observation_interval_seconds=args.observation_interval,
            )

            def dispatch_tap(point: tuple[int, int]) -> bool:
                nonlocal physical_action_count
                adb.input_tap(*point)
                physical_action_count += 1
                return True

            def exact_target_still_active() -> bool:
                current_info = manager.info()
                current_identity = window_identity(current_info)
                current_game = manager.game_info(GAME_PACKAGE)
                return (
                    int(current_identity["instance_index"]) == 0
                    and str(current_identity["package_id"]) == GAME_PACKAGE
                    and str(current_identity["runtime_family"]).casefold()
                    == "MuMuV5".casefold()
                    and str(current_game.get("state", "")).casefold() == "running"
                )

            def recover_resource_package() -> None:
                runtime.ensure_package_running()
                runtime.ensure_game_window_foreground()

            episode = PersonalAutomationEpisode(
                frame_provider=episode_frame_provider,
                detector=detector,
                planner=planner,
                executor=ActionExecutor(
                    dispatch_tap,
                    pre_dispatch_guard=exact_target_still_active,
                ),
                transform_provider=lambda detected: CoordinateTransform(
                    detected.frame_dimensions,
                    (853, 480),
                    detected.frame_dimensions,
                    (0, 0),
                ),
                budget=budget,
                recorder=recorder,
                policy=policy,
                resource_update_recover_package=recover_resource_package,
                resource_update_package_running=lambda: str(
                    manager.game_info(GAME_PACKAGE).get("state", "")
                ).casefold()
                in {"running", "starting"},
            )
            episode_result = episode.run()
            result_payload.update(
                {
                    "status": episode_result.status,
                    "final_state": episode_result.final_state.value,
                    "reason": episode_result.reason,
                    "real_ui_actions": budget.total_actions,
                    "input_dispatch_count": physical_action_count,
                    "round_total_ui_actions": budget.total_actions,
                    "observation_count": episode_result.observation_count,
                    "events": list(episode_result.events),
                    "window": identity,
                    "window_foregrounded": foregrounded,
                }
            )
        after_all = manager.all_info()
        result_payload["other_instances_unchanged"] = stable_other_instances(before_all, after_all)
        result_payload["capture_count"] = capture_sequence
        result_payload["action_budget"] = budget.snapshot()
    except Exception as exc:  # noqa: BLE001 - persist exact live failure
        result_payload.update(
            {
                "status": "FAIL",
                "final_state": "UNKNOWN",
                "reason": f"{type(exc).__name__}: {exc}",
                "real_ui_actions": budget.total_actions,
                "input_dispatch_count": physical_action_count,
            }
        )
    finally:
        if adb is not None:
            try:
                adb.kill()
            except Exception:
                pass
        result_payload["lifecycle_state_history"] = lifecycle.instance_state_history
        result_payload["instance_launch_dispatches"] = lifecycle.emulator_launch_dispatches
        result_payload["package_launch_dispatches"] = budget.actions_by_action_type[
            "START_PACKAGE"
        ]
        result_payload["resource_confirm_attempts"] = budget.actions_by_action_type[
            "CONFIRM_RESOURCE_UPDATE"
        ]
        result_payload["resource_complete_entry_attempts"] = budget.actions_by_action_type[
            "ENTER_AFTER_RESOURCE_UPDATE"
        ]
        result_payload["completed_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        (args.output / "PERSONAL_AUTOMATION_EPISODE_RESULT.json").write_text(
            json.dumps(result_payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(json.dumps(result_payload, ensure_ascii=False, indent=2))
    return 0 if result_payload.get("status") in {"PASS", "DRY_RUN_PASS"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
