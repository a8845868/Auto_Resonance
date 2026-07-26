"""One-action probe from ACTIVITY_OVERVIEW_VISIBLE through Global Prep only."""

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

from core.control.control import (  # noqa: E402
    connect_adb,
    current_display_geometry,
    get_runtime_device,
    input_tap,
    screenshot,
)
import core.control.control as control_module  # noqa: E402
from core.services.action_summary_navigation import ActionSummaryNavigator  # noqa: E402
from core.services.emulator_lifecycle import GAME_PACKAGE, MuMuManagerClient  # noqa: E402


DEFAULT_OUTPUT_DIR = ROOT / "dist" / "debug_private" / "global-prep-single-stage-v1"


def _blocked(reason: str, *, final_state: str = "NOT_RUN") -> dict:
    return {
        "status": "BLOCKED",
        "reason": reason,
        "activity_overview_before_probe": False,
        "final_state": final_state,
        "global_prep_stage_result": "NOT_RUN",
        "global_prep_dispatches": 0,
        "action_summary_total_dispatches": 0,
        "real_ui_actions": 0,
    }


def run(*, adb_port: int = 16384) -> dict:
    device = get_runtime_device()
    if int(device.index) != 0:
        return _blocked("instance_index_mismatch")
    manager = MuMuManagerClient(device, correlation_id="GLOBAL-PREP-SINGLE-STAGE")
    info = manager.info()
    game = manager.game_info(GAME_PACKAGE)
    if not bool(info.get("is_process_started")) or not bool(info.get("is_android_started")):
        return _blocked("instance_zero_not_running")
    if str(game.get("state", "")).casefold() != "running":
        return _blocked("target_package_not_running")
    if not connect_adb(adb_port):
        return _blocked("instance_zero_backend_connect_failed")

    recorded = []
    result = ActionSummaryNavigator(
        frame_provider=screenshot,
        tap=input_tap,
        geometry_provider=current_display_geometry,
        evidence_recorder=recorded.append,
        dispatch_backend=type(control_module.control).__name__,
        stop_after_global_prep_stage=True,
    ).navigate()
    candidates = [item.to_dict() for item in result.global_prep_candidate_resolutions]
    hit_targets = [item.to_dict() for item in result.global_prep_hit_target_resolutions]
    final_candidate = candidates[-1] if candidates else None
    final_hit = hit_targets[-1].get("target") if hit_targets else None
    attempt = result.evidences[0] if result.evidences else None
    dispatches = sum(
        evidence.entry_name == "open_activity_overview" and evidence.dispatch_requested
        for evidence in result.evidences
    )
    first_stable_page = (
        result.state.value
        if result.global_prep_stage_result in {
            "PASS", "OPTIONAL_OVERLAY_REACHED", "ACTION_SUMMARY_REACHED",
            "STABLE_CHANGED_UNKNOWN",
        }
        else None
    )
    status = "PASS" if result.global_prep_stage_result in {
        "PASS", "OPTIONAL_OVERLAY_REACHED", "ACTION_SUMMARY_REACHED",
    } else "BLOCKED"
    return {
        "status": status,
        "probe": "global_prep_single_stage_v1",
        "created_at": datetime.now().astimezone().isoformat(timespec="milliseconds"),
        "instance_index": 0,
        "package_id": GAME_PACKAGE,
        "activity_overview_before_probe": bool(
            attempt and attempt.pre_state == "ACTIVITY_OVERVIEW_VISIBLE"
        ),
        "final_state": result.state.value,
        "reason": result.reason,
        "global_prep_stage_result": result.global_prep_stage_result,
        "first_stable_page": first_stable_page,
        "global_prep_label_candidate_count": final_candidate.get("safe_candidate_count", 0) if final_candidate else 0,
        "global_prep_label_bbox": final_candidate.get("candidate_bbox") if final_candidate else None,
        "global_prep_parent_container_count": hit_targets[-1].get("visual_parent_count", 0) if hit_targets else 0,
        "global_prep_hit_target_bbox": final_hit.get("hit_target_bbox") if final_hit else None,
        "global_prep_hit_target_point": final_hit.get("hit_target_point") if final_hit else None,
        "global_prep_hit_target_method": final_hit.get("container_detection_method") if final_hit else None,
        "global_prep_target_occluded": final_hit.get("occlusion_detected") if final_hit else None,
        "coordinate_chain_complete": bool(attempt and attempt.coordinate_chain.complete),
        "random_offset_enabled": False,
        "global_prep_dispatches": dispatches,
        "action_summary_total_dispatches": result.dispatch_count,
        "dispatch_acknowledged_semantics": "COMMAND_RETURN_ONLY",
        "dispatch_command_returned": bool(attempt and attempt.dispatch_command_returned),
        "dispatch_backend_error": attempt.dispatch_backend_error if attempt else None,
        "touch_effect_observed": bool(attempt and attempt.touch_effect_observed),
        "post_frame_changed": bool(attempt and attempt.post_frame_changed),
        "target_page_changed": bool(attempt and attempt.target_page_changed),
        "post_state_sequence": [item["post_state"] for item in result.timeline if item["transition_classification"] != "STALE"],
        "post_frame_hash_sequence": [item["post_frame_sha256"] for item in result.timeline if item["transition_classification"] != "STALE"],
        "candidate_resolution_evidence": candidates,
        "hit_target_evidence": hit_targets,
        "coordinate_chain": attempt.to_dict() if attempt else None,
        "post_effect_timeline": result.timeline,
        "real_ui_actions": result.dispatch_count,
        "action_terminal_dispatches": 0,
        "optional_overlay_dispatches": 0,
        "action_summary_entry_dispatches": 0,
        "sweep_executed": False,
        "battle_actions": 0,
        "reward_actions": 0,
        "fatigue_item_actions": 0,
        "trade_actions": 0,
        "purchase_actions": 0,
        "departure_actions": 0,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--adb-port", type=int, default=16384)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    result_path = args.output.resolve() / "REAL_PROBE_RESULT.json"
    if not args.execute:
        document = _blocked("explicit_execute_flag_required")
    else:
        logger.disable("core.image.ocr")
        try:
            document = run(adb_port=args.adb_port)
        except Exception as error:  # noqa: BLE001 - sanitized runtime blocker
            document = _blocked(f"runtime_error:{type(error).__name__}")
        finally:
            try:
                control_module.control.kill()
            except Exception:
                pass
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    print(json.dumps({
        "output": str(result_path),
        "sha256": hashlib.sha256(result_path.read_bytes()).hexdigest(),
        "status": document.get("status"),
        "global_prep_stage_result": document.get("global_prep_stage_result"),
        "dispatches": document.get("global_prep_dispatches", 0),
    }, ensure_ascii=False))
    return 0 if document.get("status") == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
