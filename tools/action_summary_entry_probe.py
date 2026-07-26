"""One bounded live probe that stops at ACTION_SUMMARY_VISIBLE.

This tool performs no home recovery.  A non-HOME start is a zero-input block.
It never imports or calls resident sweep, battle, reward, fatigue, trade, or
departure operations.
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
from core.services.action_summary_navigation import ActionSummaryNavigator
from core.services.emulator_lifecycle import GAME_PACKAGE, MuMuManagerClient


DEFAULT_OUTPUT_DIR = (
    ROOT
    / "dist"
    / "debug_private"
    / "action-summary-entry-isolation-v1"
)


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
    }


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

    recorded = []
    result = ActionSummaryNavigator(
        frame_provider=screenshot,
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
    first_evidence = result.evidences[0] if result.evidences else None
    terminal_dispatches = sum(
        evidence.entry_name == "open_action_entry" and evidence.dispatch_requested
        for evidence in result.evidences
    )
    terminal_postcondition = "NOT_RUN"
    if first_evidence and first_evidence.post_observations:
        terminal_postcondition = first_evidence.post_observations[-1].postcondition_result
    return {
        "status": "PASS" if result.success else "BLOCKED",
        "probe": "action_summary_entry_isolation_v1",
        "created_at": datetime.now().astimezone().isoformat(timespec="milliseconds"),
        "instance_index": 0,
        "package_id": GAME_PACKAGE,
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
        "action_terminal_device_point": (
            list(first_evidence.coordinate_chain.device_point)
            if first_evidence else None
        ),
        "action_terminal_dispatches": terminal_dispatches,
        "action_terminal_dispatch_acknowledged": (
            bool(first_evidence.dispatch_acknowledged) if first_evidence else None
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
        "real_ui_actions": result.dispatch_count,
        "sweep_executed": False,
        "real_sweep_actions": 0,
        "battle_actions": 0,
        "reward_actions": 0,
        "fatigue_item_actions": 0,
        "trade_actions": 0,
        "purchase_actions": 0,
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
    result_path = output_dir / "REAL_PROBE_RESULT.json"
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
