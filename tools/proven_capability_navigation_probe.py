"""Bounded product-path probe for the V2A proven-edge capability planner."""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
import sys

from loguru import logger

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from auto.resident_activity import ResidentActivityAutomation, ScreenDriver
from core.control.control import connect_adb, get_runtime_device
import core.control.control as control_module
from core.services.emulator_lifecycle import GAME_PACKAGE, MuMuManagerClient


DEFAULT_OUTPUT_DIR = (
    ROOT
    / "dist"
    / "debug_private"
    / "runtime-navigation-kernel-v2a-live-gate"
)


def _write_json(path: Path, document: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def _blocked(reason: str) -> dict[str, object]:
    return {
        "status": "BLOCKED",
        "success": False,
        "reason": reason,
        "target_capability": "ACTION_SUMMARY_VISIBLE",
        "initial_state": "NOT_RUN",
        "final_state": "NOT_RUN",
        "planned_edge_ids": [],
        "completed_edge_ids": [],
        "failed_edge_id": None,
        "physical_dispatches": 0,
        "real_ui_actions": 0,
        "unknown_state_actions": 0,
        "irreversible_actions": 0,
        "sweep_actions": 0,
        "challenge_actions": 0,
        "battle_actions": 0,
        "reward_actions": 0,
        "fatigue_item_actions": 0,
        "trade_actions": 0,
        "purchase_actions": 0,
        "sell_actions": 0,
        "departure_actions": 0,
    }


def run(*, adb_port: int = 16384) -> dict[str, object]:
    device = get_runtime_device()
    if int(device.index) != 0:
        return _blocked("instance_index_mismatch")
    manager = MuMuManagerClient(device, correlation_id="PROVEN-CAPABILITY-V2A")
    info = manager.info()
    game = manager.game_info(GAME_PACKAGE)
    if not bool(info.get("is_process_started")) or not bool(
        info.get("is_android_started")
    ):
        return _blocked("instance_zero_not_running")
    if str(game.get("state", "")).casefold() != "running":
        return _blocked("target_package_not_running")
    if not connect_adb(adb_port):
        return _blocked("instance_zero_backend_connect_failed")

    automation = ResidentActivityAutomation(
        ScreenDriver(), use_proven_edge_planner=True
    )
    success = automation.open_action_summary()
    result = automation.last_capability_navigation_result
    if result is None:
        return _blocked("capability_navigation_result_missing")
    document = result.to_dict()
    document.update(
        status="PASS" if success else "BLOCKED",
        success=success,
        created_at=datetime.now().astimezone().isoformat(timespec="milliseconds"),
        probe="proven_capability_navigation_v2a",
        instance_index=0,
        package_id=GAME_PACKAGE,
        final_live_navigation=(
            "PASS"
            if success and result.final_state == "ACTION_SUMMARY_VISIBLE"
            else "BLOCKED"
        ),
        final_ui_state=result.final_state,
        target_capability_reached=bool(
            success and result.final_state == "ACTION_SUMMARY_VISIBLE"
        ),
        real_ui_actions=result.physical_dispatches,
        sweep_actions=0,
        challenge_actions=0,
        battle_actions=0,
        reward_actions=0,
        fatigue_item_actions=0,
        trade_actions=0,
        purchase_actions=0,
        sell_actions=0,
        departure_actions=0,
    )
    if result.physical_dispatches > 4:
        document.update(
            status="BLOCKED", success=False,
            reason="real_ui_action_budget_exceeded",
            final_live_navigation="BLOCKED",
        )
    return document


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--adb-port", type=int, default=16384)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    output = args.output.resolve()
    if not args.execute:
        document = _blocked("explicit_execute_flag_required")
    else:
        logger.disable("core.image.ocr")
        try:
            document = run(adb_port=args.adb_port)
        except Exception as error:
            document = _blocked(f"runtime_error:{type(error).__name__}")
        finally:
            try:
                control_module.control.kill()
            except Exception:
                pass
    _write_json(output / "RUN_RESULT.json", document)
    _write_json(
        output / "EVENT_TIMELINE.json",
        {"steps": document.get("step_results", [])},
    )
    return 0 if document.get("status") == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
