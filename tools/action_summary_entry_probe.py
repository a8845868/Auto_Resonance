"""One bounded trusted-state probe that stops at ACTION_SUMMARY_VISIBLE.

The probe accepts the already-proven HOME/activity/global-prep/summary states
and may dismiss one *claimed* daily check-in through the existing safe helper.
UNKNOWN and every other page are zero-input stops. It never imports or calls
resident sweep, battle, reward, fatigue, trade, or departure operations.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime
from pathlib import Path
import sys

from loguru import logger

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.control.control import (
    connect_adb,
    current_display_geometry,
    get_runtime_device,
    input_tap,
    screenshot,
)
import core.control.control as control_module
from core.services.action_summary_navigation import (
    ActionSummaryNavigator,
    ActionSummaryState,
    observe_action_summary,
)
from core.services.emulator_lifecycle import GAME_PACKAGE, MuMuManagerClient
from core.services.personal_runtime_episode import RuntimeState, StateDetector
from tools.city_entry_single_action_probe import (
    _prepare_home_from_claimed_daily_checkin,
)


DEFAULT_OUTPUT_DIR = (
    ROOT
    / "dist"
    / "debug_private"
    / "runtime-navigation-kernel-v1-live-gate"
)


class _PrefetchedFrameProvider:
    def __init__(self, first_frame, provider):
        self.first_frame = first_frame
        self.provider = provider

    def __call__(self):
        if self.first_frame is not None:
            frame, self.first_frame = self.first_frame, None
            return frame
        return self.provider()


def _write_json(path: Path, document: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True)
    path.write_text(payload, encoding="utf-8")


def blocked(reason: str) -> dict:
    return {
        "status": "BLOCKED",
        "reason": reason,
        "home_ready_before_probe": False,
        "success": False,
        "final_state": "NOT_RUN",
        "action_summary_total_dispatches": 0,
        "action_summary_stage_count": 0,
        "real_ui_actions": 0,
        "unknown_state_actions": 0,
        "daily_checkin_dismisses": 0,
        "action_terminal_dispatches": 0,
        "global_prep_dispatches": 0,
        "optional_overlay_dispatches": 0,
        "action_summary_entry_dispatches": 0,
    }


def _top_level_stage_points(evidences) -> dict[str, list[int] | None]:
    def point_for(entry_name: str):
        evidence = next(
            (item for item in evidences if item.entry_name == entry_name), None
        )
        point = evidence.actual_dispatched_point if evidence else None
        return list(point) if point else None

    last = evidences[-1] if evidences else None
    actual = last.actual_dispatched_point if last else None
    return {
        "actual_dispatched_point": list(actual) if actual else None,
        "action_terminal_device_point": point_for("open_action_entry"),
        "action_summary_entry_device_point": point_for("open_action_summary"),
    }


def _evidence_for_entry(evidences, entry_name: str):
    return next(
        (evidence for evidence in evidences if evidence.entry_name == entry_name),
        None,
    )


def run(*, adb_port: int = 16384) -> dict:
    device = get_runtime_device()
    if int(device.index) != 0:
        return blocked("instance_index_mismatch")
    manager = MuMuManagerClient(device, correlation_id="ACTION-SUMMARY-ENTRY")
    info = manager.info()
    game = manager.game_info(GAME_PACKAGE)
    if not bool(info.get("is_process_started")) or not bool(info.get("is_android_started")):
        return blocked("instance_zero_not_running")
    if str(game.get("state", "")).casefold() != "running":
        return blocked("target_package_not_running")
    if not connect_adb(adb_port):
        return blocked("instance_zero_backend_connect_failed")

    try:
        initial_frame = screenshot()
        detected = StateDetector().detect(initial_frame)
    except Exception as error:  # noqa: BLE001 - sanitized zero-input blocker
        return blocked(f"initial_state_detection_failed:{type(error).__name__}")
    daily_details = {
        "status": "NOT_APPLICABLE",
        "initial_runtime_state": detected.state.value,
        "daily_checkin_dismiss_count": 0,
        "daily_checkin_state_sequence": [detected.state.value],
    }
    if detected.state is RuntimeState.DAILY_CHECKIN:
        initial_frame, daily_details = _prepare_home_from_claimed_daily_checkin(
            initial_frame,
            frame_provider=screenshot,
            tap=input_tap,
            geometry_provider=current_display_geometry,
        )
        if daily_details.get("status") != "PASS":
            document = blocked(str(daily_details.get("reason", "daily_checkin_blocked")))
            document.update(daily_details)
            document["real_ui_actions"] = int(
                daily_details.get("daily_checkin_dismiss_count", 0)
            )
            return document
    initial_observation = observe_action_summary(initial_frame)
    trusted_starts = {
        ActionSummaryState.HOME_READY,
        ActionSummaryState.ACTIVITY_OVERVIEW_VISIBLE,
        ActionSummaryState.ACTION_SUMMARY_ENTRY_VISIBLE,
        ActionSummaryState.ACTION_SUMMARY_VISIBLE,
    }
    if initial_observation.state not in trusted_starts:
        document = blocked(
            f"untrusted_initial_navigation_state:{initial_observation.state.value}"
        )
        document.update(
            initial_runtime_state=detected.state.value,
            actual_navigation_start=initial_observation.state.value,
        )
        return document

    recorded = []
    result = ActionSummaryNavigator(
        frame_provider=_PrefetchedFrameProvider(initial_frame, screenshot),
        tap=input_tap,
        geometry_provider=current_display_geometry,
        evidence_recorder=recorded.append,
        dispatch_backend=type(control_module.control).__name__,
    ).navigate()
    resolutions = [item.to_dict() for item in result.candidate_resolutions]
    fresh_resolution = next(
        (item for item in resolutions if item["phase"] == "fresh"),
        resolutions[-1] if resolutions else None,
    )
    action_terminal_evidence = _evidence_for_entry(
        result.evidences,
        "open_action_entry",
    )
    stage_points = _top_level_stage_points(result.evidences)
    terminal_dispatches = sum(
        evidence.entry_name == "open_action_entry" and evidence.dispatch_requested
        for evidence in result.evidences
    )
    global_prep_dispatches = sum(
        evidence.entry_name == "open_activity_overview" and evidence.dispatch_requested
        for evidence in result.evidences
    )
    optional_overlay_dispatches = sum(
        evidence.entry_name == "dismiss_known_optional_overlay"
        and evidence.dispatch_requested
        for evidence in result.evidences
    )
    action_summary_entry_dispatches = sum(
        evidence.entry_name == "open_action_summary" and evidence.dispatch_requested
        for evidence in result.evidences
    )
    daily_checkin_dismisses = int(
        daily_details.get("daily_checkin_dismiss_count", 0)
    )
    real_ui_actions = daily_checkin_dismisses + result.dispatch_count
    if real_ui_actions > 5:
        return blocked("real_ui_action_budget_exceeded")
    terminal_postcondition = "NOT_RUN"
    if action_terminal_evidence and action_terminal_evidence.post_observations:
        terminal_postcondition = (
            action_terminal_evidence.post_observations[-1].postcondition_result
        )
    return {
        "status": "PASS" if result.success else "BLOCKED",
        "probe": "action_summary_entry_isolation_v1",
        "created_at": datetime.now().astimezone().isoformat(timespec="milliseconds"),
        "instance_index": 0,
        "package_id": GAME_PACKAGE,
        "initial_runtime_state": detected.state.value,
        "actual_navigation_start": initial_observation.state.value,
        "home_ready_before_probe": bool(
            result.state.value == "HOME_READY"
            or (result.evidences and result.evidences[0].pre_state == "HOME_READY")
        ),
        "success": result.success,
        "final_state": result.state.value,
        "reason": result.reason,
        "action_summary_total_dispatches": result.dispatch_count,
        "action_summary_stage_count": result.stage_count,
        "action_terminal_candidate_count": (
            fresh_resolution["safe_candidate_count"] if fresh_resolution else 0
        ),
        "action_terminal_candidate_type": (
            fresh_resolution["candidate_type"] if fresh_resolution else None
        ),
        "action_terminal_candidate_bbox": (
            fresh_resolution["candidate_bbox"] if fresh_resolution else None
        ),
        "evidence_schema_version": "2.0",
        **stage_points,
        "action_terminal_dispatches": terminal_dispatches,
        "action_terminal_dispatch_acknowledged": (
            bool(action_terminal_evidence.dispatch_acknowledged)
            if action_terminal_evidence
            else None
        ),
        "action_terminal_postcondition": terminal_postcondition,
        "candidate_resolution_evidence": resolutions,
        "random_offset_enabled": False,
        "post_state_sequence": [
            item["post_state"] for item in result.timeline
            if item["transition_classification"] != "STALE"
        ],
        "action_summary_postcondition": "PASS" if result.success else (
            "FAIL" if result.dispatch_count else "NOT_RUN"
        ),
        "action_summary_visible": result.success,
        "daily_checkin_dismisses": daily_checkin_dismisses,
        "daily_checkin_state_sequence": daily_details.get(
            "daily_checkin_state_sequence", []
        ),
        "global_prep_dispatches": global_prep_dispatches,
        "optional_overlay_dispatches": optional_overlay_dispatches,
        "action_summary_entry_dispatches": action_summary_entry_dispatches,
        "action_summary_entry_resolutions": [
            item.to_dict() for item in result.action_summary_entry_resolutions
        ],
        "real_ui_actions": real_ui_actions,
        "unknown_state_actions": 0,
        "sweep_executed": False,
        "real_sweep_actions": 0,
        "challenge_actions": 0,
        "battle_actions": 0,
        "reward_actions": 0,
        "fatigue_item_actions": 0,
        "trade_actions": 0,
        "purchase_actions": 0,
        "sell_actions": 0,
        "departure_actions": 0,
        "stage_evidence": [evidence.to_dict() for evidence in result.evidences],
        "transition_timeline": result.timeline,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--adb-port", type=int, default=16384)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    output_dir = args.output.resolve()
    result_path = output_dir / "RUN_RESULT.json"
    if not args.execute:
        document = blocked("explicit_execute_flag_required")
    else:
        logger.disable("core.image.ocr")
        try:
            document = run(adb_port=args.adb_port)
        except Exception as error:  # noqa: BLE001 - persist exact blocker class
            document = blocked(f"runtime_error:{type(error).__name__}")
        finally:
            try:
                control_module.control.kill()
            except Exception:
                pass
    _write_json(result_path, document)
    _write_json(
        output_dir / "EVENT_TIMELINE.json",
        {"events": document.get("transition_timeline", [])},
    )
    frame_index = []
    seen_hashes = set()
    for attempt in document.get("stage_evidence", []):
        for state_key, hash_key in (
            ("pre_state", "pre_frame_sha256"),
            ("post_state", "post_frame_sha256"),
        ):
            frame_hash = str(attempt.get(hash_key) or "")
            if frame_hash and frame_hash not in seen_hashes:
                seen_hashes.add(frame_hash)
                frame_index.append({"frame_hash": frame_hash, "state": attempt.get(state_key)})
    _write_json(output_dir / "FRAME_INDEX.json", {"frames": frame_index})
    digest = hashlib.sha256(result_path.read_bytes()).hexdigest()
    print(json.dumps({
        "output": str(result_path),
        "sha256": digest,
        "success": document.get("success", False),
        "final_state": document.get("final_state", "NOT_RUN"),
        "dispatches": document.get("action_summary_total_dispatches", 0),
    }, ensure_ascii=False))
    return 0 if document.get("status") == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
