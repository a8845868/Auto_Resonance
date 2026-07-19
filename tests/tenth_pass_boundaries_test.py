from __future__ import annotations

import json
import threading
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from types import MappingProxyType
from unittest.mock import Mock

import cv2 as cv
import numpy as np
import pytest

import auto.fatigue_recovery as recovery
import auto.reward_collection as rewards
import core.control.control as control_module
import core.services.fatigue_triggers as triggers
import tools.sixth_read_only_probe as probe
import tools.audit_export as audit
from app.utils.task_queue import QueuedTask, TaskQueueWorker
from core.services.fatigue_planner import (
    FatigueAction,
    FatiguePlan,
    FatiguePlanStatus,
    FatigueSnapshot,
    RouteLeg,
    TradeRouteContext,
)
from core.services.read_only_policy import (
    ActionIntent,
    AnchorResolver,
    DisplayGeometry,
    ObservedAnchor,
    PageObservation,
    PageObserver,
    ReadOnlyActionGuard,
    ReadOnlySafetySession,
    ReadOnlyPermitIssuer,
)
from core.services.server_calendar import SERVER_CLOCK
from core.services.task_schedule_state import (
    TASK_KEY_RUN_BUSINESS,
    is_task_due,
    load_task_schedule,
    request_immediate_run,
    set_next_run,
)


NOW = datetime(2026, 7, 19, 12, 0, tzinfo=timezone(timedelta(hours=8)))


def _observation(
    *,
    page_type: str = "daily_activity",
    anchors: tuple[ObservedAnchor, ...] | None = None,
    geometry: DisplayGeometry | None = None,
) -> PageObservation:
    return PageObservation(
        observation_id="obs-tenth-boundary",
        screenshot_hash="b" * 64,
        page_type=page_type,
        markers=(page_type,),
        anchors=anchors or (
            ObservedAnchor("top_left_back", "返回", (20, 10, 130, 85)),
            ObservedAnchor("daily_content", "任务列表", (150, 180, 1180, 650)),
        ),
        captured_at=NOW,
        display_geometry=geometry,
    )


def _guard(
    observation: PageObservation | None = None,
    *,
    hardware_tap=None,
):
    initial = observation or _observation()
    state = {"value": initial, "post_value": None, "sequence": 0}
    def observe():
        state["sequence"] += 1
        sequence = state["sequence"]
        if sequence % 3 == 0:
            base = state["post_value"] or PageObservation(
                "post-home", "c" * 64, "home", ("home",), (), NOW
            )
        else:
            base = state["value"]
        return replace(
            base,
            captured_at=NOW + timedelta(microseconds=sequence),
            capture_sequence=sequence,
            source_capture_id=f"tenth-boundary-{id(state)}-{sequence}",
            source_monotonic_sequence=sequence,
            backend_generation=1,
            instance_id="test-instance-0",
            adb_serial="test-adb-0",
        )
    issuer = ReadOnlyPermitIssuer(
        PageObserver(observe), AnchorResolver(), now=lambda: NOW
    )
    guard = ReadOnlySafetySession(
        hardware_tap or (lambda _point: None), permit_issuer=issuer, now=lambda: NOW
    )
    return guard, issuer, state


def test_mutated_permit_fields_invalidate_signature_or_registry_record():
    guard, issuer, _state = _guard()
    permit = issuer.issue(
        ActionIntent("reward_back", "top_left_back", "mutate"), ((50, 40),)
    )
    mutated = type(permit)("mutated-token")

    assert guard.authorize_coordinate((50, 40), permit=mutated) is False
    assert guard.journal[-1].reason == "permit_not_issued_by_registry"
    assert issuer.permit_status(permit)["uses"] == 0


def test_permit_signature_covers_action_region_trajectory_and_observation():
    guard, issuer, _state = _guard()
    permit = issuer.issue(
        ActionIntent("reward_back", "top_left_back", "coverage"), ((50, 40),)
    )
    mutations = tuple(type(permit)(f"mutated-{index}") for index in range(4))

    for mutated in mutations:
        assert guard.authorize_coordinate(
            (50, 40), permit=mutated
        ) is False
        assert guard.journal[-1].reason == "permit_not_issued_by_registry"
    assert issuer.permit_status(permit)["uses"] == 0


def test_safe_action_key_cannot_authorize_dangerous_coordinate():
    _guard_value, issuer, _state = _guard()
    with pytest.raises(PermissionError, match="anchor|coordinate|region"):
        issuer.issue(
            ActionIntent("reward_back", "top_left_back", "dangerous-coordinate"),
            ((1000, 650),),
        )


def test_permit_consume_rechecks_current_static_policy():
    guard, issuer, _state = _guard()
    permit = issuer.issue(
        ActionIntent("reward_back", "top_left_back", "policy-recheck"), ((50, 40),)
    )
    issuer._policies = MappingProxyType({})

    assert guard.authorize_coordinate((50, 40), permit=permit) is False
    assert guard.journal[-1].reason == "policy_no_longer_allows_action"


def test_concurrent_permit_use_causes_exactly_one_hardware_tap(monkeypatch):
    taps: list[tuple[int, int]] = []
    backend = SimpleNamespace(ratio=1.0, input_tap=lambda x, y: taps.append((x, y)))
    monkeypatch.setattr(control_module, "control", backend)
    guard, issuer, _state = _guard(
        hardware_tap=control_module._create_bound_input_executor(backend)
    )
    permit = issuer.issue(
        ActionIntent("reward_back", "top_left_back", "hardware-race"), ((50, 40),)
    )
    results: list[bool] = []
    threads = [
        threading.Thread(
            target=lambda: results.append(
                guard.authorize_coordinate((50, 40), permit=permit)
            )
        )
        for _ in range(2)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert sorted(results) == [False, True]
    assert taps == [(50, 40)]
    assert [entry.stage for entry in guard.journal].count("POSTCONDITION_VERIFIED") == 1
    assert [entry.reason for entry in guard.journal].count("permit_already_consumed") == 1


def test_ratio_greater_than_one_preserves_valid_logical_tap(monkeypatch):
    for ratio in (1.0, 1.25, 1.5, 2.0):
        taps: list[tuple[int, int]] = []
        backend = SimpleNamespace(ratio=ratio, input_tap=lambda x, y: taps.append((x, y)))
        monkeypatch.setattr(control_module, "control", backend)
        guard, _issuer, _state = _guard(
            hardware_tap=control_module._create_bound_input_executor(backend)
        )
        owner = control_module.activate_action_policy(guard)
        try:
            assert control_module.input_tap(
                (50, 40),
                intent=ActionIntent("reward_back", "top_left_back", f"ratio-{ratio}"),
            ) is True
        finally:
            control_module.remove_action_policy(owner)
        assert taps == [(round(50 * ratio), round(40 * ratio))]
        executed = next(
            entry for entry in reversed(guard.journal)
            if entry.stage == "POSTCONDITION_VERIFIED"
        )
        assert executed.logical_trajectory == ((50, 40),)
        assert executed.physical_trajectory == tuple(taps)


def test_swipe_full_trajectory_uses_one_coordinate_space(monkeypatch):
    swipes: list[tuple[int, int, int, int, int]] = []
    ratio = 1.25
    backend = SimpleNamespace(
            ratio=ratio,
            input_swipe=lambda x1, y1, x2, y2, duration: swipes.append(
                (x1, y1, x2, y2, duration)
            ),
        )
    monkeypatch.setattr(control_module, "control", backend)
    guard, _issuer, state = _guard(
        hardware_tap=control_module._create_bound_input_executor(backend)
    )
    state["value"] = replace(state["value"], content_marker_hash="before")
    state["post_value"] = replace(state["value"], content_marker_hash="after")
    owner = control_module.activate_action_policy(guard)
    try:
        assert control_module.input_swipe(
            (900, 350),
            (400, 350),
            swipe_time=650,
            intent=ActionIntent(
                "daily_horizontal_scroll", "daily_content", "one-space"
            ),
        ) is True
    finally:
        control_module.remove_action_policy(owner)

    assert swipes == [(1125, 438, 500, 438, 650)]
    entry = guard.journal[-1]
    assert len(entry.logical_trajectory) == len(entry.physical_trajectory)
    assert entry.physical_trajectory == tuple(
        (round(x * ratio), round(y * ratio)) for x, y in entry.logical_trajectory
    )


def test_geometry_change_invalidates_existing_permit():
    original = DisplayGeometry.from_ratio(1.0, geometry_revision="geometry-1")
    changed = DisplayGeometry.from_ratio(1.25, geometry_revision="geometry-2")
    guard, issuer, _state = _guard(_observation(geometry=original))
    permit = issuer.issue(
        ActionIntent("reward_back", "top_left_back", "geometry"),
        ((50, 40),),
        geometry=original,
    )

    assert guard.authorize_coordinate(
        (50, 40), permit=permit, geometry=changed
    ) is False
    assert guard.journal[-1].reason == "display_geometry_revision_changed"


def _ocr(text: str, x: int, y: int) -> dict:
    return {
        "text": text,
        "position": [[x - 5, y - 5], [x + 5, y - 5], [x + 5, y + 5], [x - 5, y + 5]],
    }


def _probe_observation(monkeypatch, texts: list[str]) -> PageObservation:
    items = [_ocr(text, 200 + index * 100, 200 + index * 50) for index, text in enumerate(texts)]
    frame = SimpleNamespace(
        image=np.zeros((720, 1280, 3), dtype=np.uint8), ocr=lambda: items
    )
    monkeypatch.setattr(probe, "capture_envelope", lambda: SimpleNamespace(
        frame=frame.image,
        raw_frame_hash="a" * 64,
        backend_monotonic_sequence=1,
        backend_capture_id="test-capture-boundary",
        captured_at=datetime.now().astimezone(),
        backend_generation=1,
        instance_id="test-instance-0",
        adb_serial="test-adb-0",
    ))
    monkeypatch.setattr(probe, "Image", lambda _image: frame)
    monkeypatch.setattr(probe, "current_display_geometry", DisplayGeometry)
    return probe._trusted_observation().as_observation()


def test_exchange_sell_classifier_precedes_generic_exchange_menu(monkeypatch):
    observed = _probe_observation(
        monkeypatch,
        ["我要卖", "预计卖出", "卖出总价", "载货量", "卖出"],
    )
    assert observed.page_type == "exchange_sell"


def test_conflicting_exchange_markers_are_unknown(monkeypatch):
    observed = _probe_observation(
        monkeypatch,
        ["预计买入", "买入总价", "预计卖出", "卖出总价", "载货量", "买入", "卖出"],
    )
    assert observed.page_type == "unknown"
    assert "conflicting_exchange_markers" in observed.markers


def test_static_region_is_not_reported_as_ocr_observed_anchor(monkeypatch):
    observed = _probe_observation(monkeypatch, ["每日活跃", "完成进度", "活跃度"])
    ocr_ids = {anchor.anchor_id for anchor in observed.anchors}
    static_ids = {region.anchor_id for region in observed.static_regions}

    assert "top_left_back" not in ocr_ids
    assert "daily_content" not in ocr_ids
    assert {"top_left_back", "daily_content"} <= static_ids


def _scheduled_checkpoint(path, *, transfer: bool = False):
    day = SERVER_CLOCK.server_day_id()
    payload = {"kind": "REOBSERVE_RECOVERY_AT_WAYPOINT", "waypoint_id": "B"}
    if transfer:
        payload.update({"cycle_id": "B|C", "cycle_server_day": day})
    triggers.register_deferred_fatigue_actions(
        [payload], plan_revision="rev-old", path=path
    )
    triggers.notify_fatigue_event("arrival", "B", path=path, schedule=Mock())
    return day


def _install_real_impl_transfer_contract(monkeypatch):
    day = SERVER_CLOCK.server_day_id()
    snapshot = FatigueSnapshot(
        server_day_id=day,
        observed_at=SERVER_CLOCK.server_now(),
        fatigue_used=100,
        fatigue_cap=800,
        current_city_id="B",
        current_station_id="B",
        current_amenities=frozenset(),
        soda_uses_used=0,
        soda_uses_remaining=0,
        soda_reduction_per_use=0,
        soda_price_tiers=(),
        bento_batches_available=0,
        bento_total_reduction_available=0,
        next_bento_release_at=None,
        natural_recovery_at=None,
        source_confidence="HIGH",
    )
    plan = FatiguePlan(
        snapshot=snapshot,
        immediate_actions=(),
        deferred_actions=(
            FatigueAction("REOBSERVE_RECOVERY_AT_WAYPOINT", waypoint_id="C"),
        ),
        expected_fatigue_by_waypoint={"C": 150},
        expected_resource_usage={},
        expected_waste=0,
        next_trigger={"waypoint_id": "C"},
        status=FatiguePlanStatus.DEFER_UNTIL_WAYPOINT,
        reason="recover_at_future_waypoint",
    )
    route = TradeRouteContext(
        "B|C", (RouteLeg("B", "C", 50, frozenset()),), 0
    )
    monkeypatch.setattr(recovery, "connect", lambda: True)
    monkeypatch.setattr(recovery, "get_station", lambda: "B")
    monkeypatch.setattr(recovery, "_open_exchange_buy_page", lambda: True)
    monkeypatch.setattr(recovery, "_wait_strength", lambda *a, **kw: (100, 800))
    monkeypatch.setattr(recovery, "load_fatigue_usage", lambda: {})
    monkeypatch.setattr(recovery, "observe_recovery_resources", lambda _station: {})
    monkeypatch.setattr(recovery, "_snapshot_from_observation", lambda *a, **kw: snapshot)
    monkeypatch.setattr(recovery, "_route_context", lambda _station=None: route)
    monkeypatch.setattr(recovery, "plan_fatigue_recovery", lambda *a, **kw: plan)
    monkeypatch.setattr(recovery, "go_home", lambda: None)
    monkeypatch.setattr(recovery, "register_deferred_fatigue_actions", Mock())


def test_checkpoint_transfer_never_creates_business_phantom_key(tmp_path, monkeypatch):
    checkpoint_path = tmp_path / "fatigue.json"
    schedule_path = tmp_path / "schedule.json"
    _scheduled_checkpoint(checkpoint_path)
    monkeypatch.setattr(
        recovery,
        "_run_daily_fatigue_recovery_impl",
        lambda **_kw: {"success": True, "status": "COMPLETE_FOR_DAY"},
    )
    monkeypatch.setattr(
        "core.services.task_schedule_state.request_immediate_run",
        lambda key: request_immediate_run(key, schedule_path),
    )

    recovery.run_daily_fatigue_recovery(
        expected_waypoint="B", checkpoint_path=checkpoint_path
    )

    tasks = load_task_schedule(schedule_path)["tasks"]
    assert TASK_KEY_RUN_BUSINESS in tasks
    assert "business" not in tasks


def test_scheduler_handoff_failure_is_recovered_on_startup(tmp_path, monkeypatch):
    checkpoint_path = tmp_path / "fatigue.json"
    schedule_path = tmp_path / "schedule.json"
    _scheduled_checkpoint(checkpoint_path)
    monkeypatch.setattr(
        recovery,
        "_run_daily_fatigue_recovery_impl",
        lambda **_kw: {"success": True, "status": "COMPLETE_FOR_DAY"},
    )
    monkeypatch.setattr(
        "core.services.task_schedule_state.request_immediate_run",
        Mock(side_effect=OSError("schedule locked")),
    )

    result = recovery.run_daily_fatigue_recovery(
        expected_waypoint="B", checkpoint_path=checkpoint_path
    )
    persisted = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    assert result["checkpoint_acknowledged"] is True
    assert result["scheduler_handoff_delivered"] is False
    assert persisted["actions"][0]["state"] == "ACKNOWLEDGED"
    assert persisted["actions"][0]["scheduler_handoff"]["state"] == "FAILED_RETRYABLE"

    monkeypatch.setattr(
        "core.services.task_schedule_state.request_immediate_run",
        lambda key: request_immediate_run(key, schedule_path),
    )
    assert triggers.recover_pending_fatigue_schedules(path=checkpoint_path) is True
    assert is_task_due(TASK_KEY_RUN_BUSINESS, path=schedule_path) is True
    recovered = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    assert recovered["actions"][0]["scheduler_handoff"]["state"] == "DELIVERED"


def test_task_queue_worker_transfer_makes_run_business_immediately_due(tmp_path, monkeypatch):
    checkpoint_path = tmp_path / "fatigue.json"
    schedule_path = tmp_path / "schedule.json"
    day = _scheduled_checkpoint(checkpoint_path, transfer=True)
    _install_real_impl_transfer_contract(monkeypatch)
    set_next_run(
        TASK_KEY_RUN_BUSINESS,
        SERVER_CLOCK.server_now() + timedelta(minutes=10),
        schedule_path,
    )
    monkeypatch.setattr(
        "core.services.task_schedule_state.request_immediate_run",
        lambda key: request_immediate_run(key, schedule_path),
    )
    results: list[dict] = []

    def run_fatigue():
        result = recovery.run_daily_fatigue_recovery(
            expected_waypoint="B", checkpoint_path=checkpoint_path
        )
        results.append(result)
        return result

    worker = TaskQueueWorker([
        QueuedTask("fatigue checkpoint", run_fatigue, key="fatigue_recovery")
    ])
    worker.run()

    assert results[0]["checkpoint_outcome"] == "TRANSFER_TO_NEW_CHECKPOINT"
    assert results[0]["scheduler_handoff_delivered"] is True
    assert is_task_due(TASK_KEY_RUN_BUSINESS, path=schedule_path) is True
    persisted = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    old = next(item for item in persisted["actions"] if item["waypoint_id"] == "B")
    replacement = next(item for item in persisted["actions"] if item["waypoint_id"] == "C")
    assert old["state"] == "SUPERSEDED"
    assert replacement["state"] == "ACTIVE"
    assert replacement["cycle_server_day"] == day

    triggers.notify_fatigue_event("arrival", "C", path=checkpoint_path, schedule=Mock())
    claimed_c = triggers.claim_fatigue_checkpoint(
        replacement["id"], owner_id="worker-c", lease_token="lease-c", path=checkpoint_path
    )
    assert claimed_c["waypoint_id"] == "C"


def _digit_image(digit: str, *, scale: float = 1.0, value: int = 255) -> np.ndarray:
    image = np.zeros((260, 320, 3), dtype=np.uint8)
    cv.putText(
        image,
        digit,
        (205, 215),
        cv.FONT_HERSHEY_SIMPLEX,
        scale,
        (value, value, 0),
        3,
        cv.LINE_AA,
    )
    return image


def _daily_frame() -> list[dict]:
    return [
        _ocr("每日活跃", 400, 60),
        _ocr("100", 500, 120),
        _ocr("200", 600, 120),
    ]


def test_ambiguous_digit_remains_unknown():
    frames = [_daily_frame(), _daily_frame(), _daily_frame()]
    assert rewards._stable_visual_daily_current(
        frames,
        maximum=200,
        images=[_digit_image("0"), _digit_image("0"), _digit_image("6")],
    ) is None
    assert rewards._stable_visual_daily_current(
        frames,
        maximum=200,
        images=[_digit_image("0"), _digit_image("0"), _digit_image("0")],
    ) == 0


@pytest.mark.parametrize("value,scale", [(255, 1.0), (180, 1.0), (255, 0.9), (255, 1.1)])
def test_image_zero_accepts_antialias_brightness_and_small_scale(value, scale):
    assert rewards._image_daily_zero(
        _digit_image("0", scale=scale, value=value)
    ) == 0


def test_non_uniform_geometry_fails_closed():
    non_uniform = DisplayGeometry(
        physical_width=1280,
        physical_height=800,
        geometry_revision="non-uniform",
    )
    guard, issuer, _state = _guard()
    with pytest.raises(PermissionError, match="non_uniform"):
        issuer.issue(
            ActionIntent("reward_back", "top_left_back", "non-uniform"),
            ((50, 40),),
            geometry=non_uniform,
        )


def test_shareable_journal_exports_only_permit_digest_and_geometry():
    minimized = audit.minimize_audit_journal({
        "action_key": "page_back",
        "permit_token": "REAL-TOKEN-MUST-NOT-EXPORT",
        "registry_contents": {"REAL-TOKEN-MUST-NOT-EXPORT": {"uses": 1}},
        "permit_digest": "0123456789abcdef",
        "logical_trajectory": [[82, 36]],
        "physical_trajectory": [[103, 45]],
        "geometry_revision": "geometry-1",
        "anchor_source": "CALIBRATED_STATIC",
        "registry_registered": True,
        "permit_uses": 1,
        "permit_max_uses": 1,
        "allowed": True,
        "reason": "valid_trusted_read_only_permit",
    })
    encoded = json.dumps(minimized, sort_keys=True)
    assert "REAL-TOKEN-MUST-NOT-EXPORT" not in encoded
    assert minimized["permit_digest"] == "0123456789abcdef"
    assert minimized["logical_trajectory"] == [[82, 36]]
    assert minimized["physical_trajectory"] == [[103, 45]]
