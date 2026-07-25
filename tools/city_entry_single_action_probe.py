"""Run one instance-0 HOME_READY -> city -> station confirmation probe."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

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
from core.services.city_navigation import (  # noqa: E402
    CityNavigationAdapter,
    CityNavigationState,
    observe_city_frame,
)
from core.services.emulator_lifecycle import (  # noqa: E402
    GAME_PACKAGE,
    MuMuManagerClient,
)
from core.utils.utils import RESOURCES_PATH, read_json  # noqa: E402


def _blocked(reason: str) -> dict[str, object]:
    return {
        "status": "BLOCKED",
        "reason": reason,
        "home_ready_before_probe": False,
        "city_entry_candidate_count": 0,
        "city_entry_dispatches": 0,
        "dispatch_acknowledged": False,
        "post_state_sequence": [],
        "transition_observation_count": 0,
        "transition_elapsed_seconds": 0.0,
        "transition_result": "NOT_RUN",
        "last_observed_state": "",
        "transition_timeline": [],
        "city_entry_postcondition": "NOT_RUN",
        "station_confirmed": False,
        "station_id": None,
        "station_defaulted_to_lanxin": False,
        "real_ui_actions": 0,
        "unknown_state_actions": 0,
    }


def run(output: Path, *, adb_port: int = 16384) -> dict[str, object]:
    output.mkdir(parents=True, exist_ok=True)
    logger.disable("core.image.ocr")
    result = _blocked("preflight_not_run")
    device = get_runtime_device()
    if int(device.index) != 0:
        return _blocked("instance_index_mismatch")
    manager = MuMuManagerClient(device, correlation_id="CITY-ENTRY-SINGLE-ACTION")
    info = manager.info()
    game = manager.game_info(GAME_PACKAGE)
    if not bool(info.get("is_process_started")) or not bool(info.get("is_android_started")):
        return _blocked("instance_zero_not_running")
    if str(game.get("state", "")).casefold() != "running":
        return _blocked("target_package_not_running")
    if not connect_adb(adb_port):
        return _blocked("instance_zero_backend_connect_failed")

    before = observe_city_frame(screenshot())
    result["city_entry_candidate_count"] = before.city_entry.candidate_count
    if before.state is not CityNavigationState.CITY_ENTRY_VISIBLE:
        result["reason"] = f"home_ready_unavailable:{before.state.value}"
        result["post_state_sequence"] = [before.state.value]
        return result
    result["home_ready_before_probe"] = True

    station_map = read_json(RESOURCES_PATH / "stations/name2id.json")
    recorded = []
    navigation = CityNavigationAdapter(
        frame_provider=screenshot,
        tap=input_tap,
        timeout=30.0,
        max_attempts=90,
        stall_frames=12,
        correlation_id="CITY-ENTRY-SINGLE-ACTION",
        geometry_provider=current_display_geometry,
        evidence_recorder=recorded.append,
        station_ids=tuple(station_map),
        require_station_confirmation=True,
        dispatch_backend=type(control_module.control).__name__,
    ).enter_city()
    dispatches = sum(
        event.reason == "city_entry_action_executed" for event in navigation.trace
    )
    state_sequence = [event.state.value for event in navigation.trace]
    evidence = navigation.evidence
    if recorded and evidence is not recorded[0]:
        raise RuntimeError("navigation_evidence_identity_mismatch")
    post_events = [
        event
        for event in navigation.trace
        if event.elapsed_since_dispatch_seconds is not None
        and event.action != "enter_city"
    ]
    post_evidence = list(evidence.post_observations) if evidence else []
    transition_timeline = []
    for index, event in enumerate(post_events, start=1):
        matched = next(
            (
                item
                for item in post_evidence
                if item.post_frame_sha256 == event.screenshot_hash
            ),
            None,
        )
        transition_timeline.append(
            {
                "post_observation_index": index,
                "elapsed_since_dispatch_seconds": event.elapsed_since_dispatch_seconds,
                "frame_sha256": event.screenshot_hash,
                "detected_state": matched.post_state if matched else event.state.value,
                "transition_classification": event.transition_classification,
                "positive_cues": list(matched.positive_cues) if matched else [],
                "negative_cues": list(matched.negative_cues) if matched else [],
                "reason_codes": list(matched.reason_codes) if matched else [event.reason],
            }
        )
    result.update(
        {
            "status": "PASS" if navigation.status == "PASS" else navigation.status,
            "reason": navigation.reason,
            "city_entry_dispatches": dispatches,
            "dispatch_acknowledged": bool(evidence and evidence.dispatch_acknowledged),
            "post_state_sequence": state_sequence,
            "transition_observation_count": navigation.post_observation_count,
            "transition_elapsed_seconds": navigation.transition_elapsed_seconds,
            "transition_result": navigation.transition_result,
            "last_observed_state": navigation.last_observed_state,
            "transition_timeline": transition_timeline,
            "city_entry_postcondition": "PASS" if navigation.entry_opened else "FAIL",
            "station_confirmed": navigation.station_confirmed,
            "station_id": navigation.station_id,
            "station_defaulted_to_lanxin": False,
            "real_ui_actions": dispatches,
            "unknown_state_actions": 0,
            "coordinate_chain_complete": bool(
                evidence and evidence.coordinate_chain.complete
            ),
            "random_offset_enabled": bool(
                evidence and evidence.random_offset_enabled
            ),
            "evidence_attempt_id": navigation.evidence_attempt_id,
            "navigation_result": {
                "state": navigation.state.value,
                "status": navigation.status,
                "reason": navigation.reason,
                "attempt_count": navigation.attempt_count,
                "dispatch_count": navigation.dispatch_count,
                "post_observation_count": navigation.post_observation_count,
                "transition_elapsed_seconds": navigation.transition_elapsed_seconds,
                "transition_result": navigation.transition_result,
                "last_observed_state": navigation.last_observed_state,
                "entry_opened": navigation.entry_opened,
                "station_confirmed": navigation.station_confirmed,
                "station_id": navigation.station_id,
            },
            "evidence": evidence.to_dict() if evidence else None,
        }
    )
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--adb-port", type=int, default=16384)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    if not args.execute:
        result = _blocked("explicit_execute_flag_required")
    else:
        try:
            result = run(args.output.resolve(), adb_port=args.adb_port)
        except Exception as error:  # noqa: BLE001 - persist the exact stop class
            result = _blocked(f"runtime_error:{type(error).__name__}")
        finally:
            try:
                control_module.control.kill()
            except Exception:
                pass
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "REAL_PROBE_RESULT.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("status") == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
