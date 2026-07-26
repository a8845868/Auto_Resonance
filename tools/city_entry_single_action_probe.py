"""Run one instance-0 HOME_READY -> city -> station confirmation probe."""

from __future__ import annotations

import argparse
import json
import sys
import time
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
from core.services.navigation_evidence import CoordinateChain  # noqa: E402
from core.services.personal_action_budget import EpisodeActionBudget  # noqa: E402
from core.services.personal_runtime_episode import (  # noqa: E402
    ActionPlanner,
    RuntimeAction,
    RuntimeState,
    StateDetector,
)
from core.services.read_only_policy import ActionIntent  # noqa: E402
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
        "initial_runtime_state": "UNKNOWN",
        "daily_checkin_detected": False,
        "daily_checkin_dismiss_count": 0,
        "daily_checkin_dispatch_acknowledged": False,
        "daily_checkin_device_point": None,
        "daily_checkin_random_offset_enabled": False,
        "daily_checkin_state_sequence": [],
    }


def _inside(point: tuple[int, int], bbox: tuple[int, int, int, int]) -> bool:
    return bbox[0] <= point[0] < bbox[2] and bbox[1] <= point[1] < bbox[3]


def _prepare_home_from_claimed_daily_checkin(
    initial_frame,
    *,
    frame_provider,
    tap,
    geometry_provider,
    sleep=time.sleep,
    monotonic=time.monotonic,
    timeout_seconds: float = 10.0,
    interval_seconds: float = 0.5,
):
    """Dismiss one proven claimed check-in overlay, then observe HOME only.

    This recovery owns at most one physical action.  UNKNOWN and a still-visible
    check-in remain observation-only until the bounded deadline.
    """

    detector = StateDetector()
    planner = ActionPlanner()
    budget = EpisodeActionBudget(clock=monotonic)
    detected = detector.detect(initial_frame)
    details = {
        "status": "NOT_APPLICABLE",
        "reason": "daily_checkin_not_present",
        "initial_runtime_state": detected.state.value,
        "daily_checkin_detected": detected.state is RuntimeState.DAILY_CHECKIN,
        "daily_checkin_dismiss_count": 0,
        "daily_checkin_dispatch_acknowledged": False,
        "daily_checkin_device_point": None,
        "daily_checkin_random_offset_enabled": False,
        "daily_checkin_state_sequence": [detected.state.value],
    }
    if detected.state is not RuntimeState.DAILY_CHECKIN:
        return initial_frame, details

    first_plan = planner.plan(detected, budget=budget)
    if (
        first_plan.action is not RuntimeAction.DISMISS_DAILY_CHECKIN
        or first_plan.capture_point is None
    ):
        details.update(
            status="BLOCKED",
            reason="daily_checkin_safe_blank_region_unavailable",
        )
        return initial_frame, details

    try:
        fresh_frame = frame_provider()
        fresh = detector.detect(fresh_frame)
    except Exception:  # noqa: BLE001 - precise pre-dispatch stop class
        details.update(status="BLOCKED", reason="daily_checkin_fresh_capture_failed")
        return initial_frame, details
    details["daily_checkin_state_sequence"].append(fresh.state.value)
    fresh_plan = planner.plan(fresh, budget=budget)
    if (
        fresh.state is not RuntimeState.DAILY_CHECKIN
        or fresh_plan.action is not RuntimeAction.DISMISS_DAILY_CHECKIN
        or fresh_plan.capture_point is None
    ):
        details.update(status="BLOCKED", reason="daily_checkin_fresh_confirmation_failed")
        return fresh_frame, details

    capture_point = fresh_plan.capture_point
    if (
        fresh.dialog_bbox is None
        or _inside(capture_point, fresh.dialog_bbox)
        or any(_inside(capture_point, bbox) for bbox in fresh.ocr_bboxes)
    ):
        details.update(status="BLOCKED", reason="daily_checkin_safe_point_invalid")
        return fresh_frame, details

    geometry = geometry_provider()
    chain = CoordinateChain.from_capture_point(
        capture_point,
        capture_size=fresh.frame_dimensions,
        render_client_size=(
            int(getattr(geometry, "physical_width")),
            int(getattr(geometry, "physical_height")),
        ),
        device_size=(
            int(getattr(geometry, "physical_width")),
            int(getattr(geometry, "physical_height")),
        ),
        source_coordinate_space="CAPTURE_PIXELS",
    )
    decision = budget.authorize(
        state=RuntimeState.DAILY_CHECKIN.value,
        action_type=RuntimeAction.DISMISS_DAILY_CHECKIN.value,
        normalized_point=chain.render_client_point,
    )
    if not decision.allowed:
        details.update(status="BLOCKED", reason=decision.reason_code)
        return fresh_frame, details

    try:
        acknowledged = tap(
            chain.device_point,
            random_offset=False,
            intent=ActionIntent(
                "dialog_cancel",
                "claimed_daily_checkin_blank_region",
                "CITY-ENTRY-SINGLE-ACTION",
            ),
        )
    except Exception:  # noqa: BLE001 - delivery may have happened; never retry
        budget.record_dispatch(decision)
        budget.record_result(decision, "DELIVERY_UNKNOWN")
        details.update(
            status="BLOCKED",
            reason="daily_checkin_delivery_unknown",
            daily_checkin_dismiss_count=1,
            daily_checkin_device_point=chain.device_point,
        )
        return fresh_frame, details
    if acknowledged is False:
        budget.record_result(decision, "DISPATCH_REJECTED")
        details.update(status="BLOCKED", reason="daily_checkin_dispatch_rejected")
        return fresh_frame, details

    budget.record_dispatch(decision)
    budget.record_result(decision, "DISPATCHED")
    details.update(
        daily_checkin_dismiss_count=1,
        daily_checkin_dispatch_acknowledged=True,
        daily_checkin_device_point=chain.device_point,
    )
    deadline = monotonic() + max(0.0, float(timeout_seconds))
    interval = max(0.001, float(interval_seconds))
    while monotonic() < deadline:
        sleep(min(interval, max(0.0, deadline - monotonic())))
        if monotonic() >= deadline:
            break
        try:
            observed_frame = frame_provider()
            observed = detector.detect(observed_frame)
        except Exception:  # noqa: BLE001 - no second action after delivery
            details.update(status="BLOCKED", reason="daily_checkin_post_capture_failed")
            return fresh_frame, details
        details["daily_checkin_state_sequence"].append(observed.state.value)
        if observed.state is RuntimeState.HOME_READY:
            details.update(status="PASS", reason="home_ready_after_daily_checkin")
            return observed_frame, details
        if observed.state in {RuntimeState.DAILY_CHECKIN, RuntimeState.UNKNOWN}:
            continue
        details.update(
            status="BLOCKED",
            reason=f"daily_checkin_unexpected_post_state:{observed.state.value}",
        )
        return observed_frame, details

    details.update(status="BLOCKED", reason="daily_checkin_postcondition_timeout")
    return fresh_frame, details


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

    initial_frame = screenshot()
    prepared_frame, checkin = _prepare_home_from_claimed_daily_checkin(
        initial_frame,
        frame_provider=screenshot,
        tap=input_tap,
        geometry_provider=current_display_geometry,
    )
    result.update(checkin)
    if checkin["status"] == "BLOCKED":
        result["reason"] = checkin["reason"]
        result["real_ui_actions"] = checkin["daily_checkin_dismiss_count"]
        result["post_state_sequence"] = checkin["daily_checkin_state_sequence"]
        return result

    before = observe_city_frame(prepared_frame)
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
            "real_ui_actions": dispatches + int(checkin["daily_checkin_dismiss_count"]),
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
