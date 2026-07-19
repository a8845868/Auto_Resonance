from __future__ import annotations

import threading
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import cv2 as cv
import numpy as np

import auto.fatigue_recovery as recovery
import auto.reward_collection as rewards
import core.control.control as control_module
import core.services.fatigue_triggers as triggers
import tools.sixth_read_only_probe as probe
from core.services.server_calendar import SERVER_CLOCK
from core.services.read_only_policy import (
    ActionIntent,
    AnchorResolver,
    ObservedAnchor,
    PageObservation,
    PageObserver,
    ReadOnlyActionGuard,
    ReadOnlySafetySession,
    ReadOnlyPermit,
    ReadOnlyPermitIssuer,
)


NOW = datetime(2026, 7, 19, 12, 0, tzinfo=timezone(timedelta(hours=8)))


def _observation(*, anchor_bbox=(20, 10, 130, 85)) -> PageObservation:
    return PageObservation(
        observation_id="obs-tenth-red",
        screenshot_hash="a" * 64,
        page_type="daily_activity",
        markers=("daily_activity",),
        anchors=(ObservedAnchor("top_left_back", "返回", anchor_bbox),),
        captured_at=NOW,
    )


def _guard(observation: PageObservation | None = None):
    value = observation or _observation()
    state = {"sequence": 0}
    def observe():
        state["sequence"] += 1
        sequence = state["sequence"]
        base = (
            PageObservation(
                "post-home", "c" * 64, "home", ("home",), (), NOW
            ) if sequence % 3 == 0 else value
        )
        return replace(
            base,
            captured_at=NOW + timedelta(microseconds=sequence),
            capture_sequence=sequence,
            source_capture_id=f"tenth-red-{id(state)}-{sequence}",
            source_monotonic_sequence=sequence,
            backend_generation=1,
            instance_id="test-instance-0",
            adb_serial="test-adb-0",
        )
    issuer = ReadOnlyPermitIssuer(
        PageObserver(observe), AnchorResolver(), now=lambda: NOW
    )
    return ReadOnlySafetySession(lambda _point: None, permit_issuer=issuer, now=lambda: NOW), issuer


def test_directly_constructed_permit_is_rejected():
    observation = _observation()
    guard, _issuer = _guard(observation)
    forged = ReadOnlyPermit("forged-permit")

    assert guard.authorize_coordinate((1000, 650), permit=forged) is False
    assert guard.journal[-1].reason == "permit_not_issued_by_registry"


def test_one_use_permit_is_consumed_atomically_by_concurrent_callers(monkeypatch):
    guard, issuer = _guard()
    permit = issuer.issue(
        ActionIntent("reward_back", "top_left_back", "concurrent"), ((50, 40),)
    )
    barrier = threading.Barrier(2)

    def synchronized_revalidate(_permit):
        barrier.wait(timeout=5)
        return True

    monkeypatch.setattr(issuer, "revalidate", synchronized_revalidate)
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
    denied = [entry.reason for entry in guard.journal if entry.stage == "CONSUME_DENIED"]
    assert denied == ["permit_already_consumed"]


def test_ratio_half_does_not_map_outside_logical_point_into_anchor(monkeypatch):
    guard, _issuer = _guard(_observation(anchor_bbox=(20, 10, 130, 85)))
    taps: list[tuple[int, int]] = []
    monkeypatch.setattr(
        control_module,
        "control",
        SimpleNamespace(ratio=0.5, input_tap=lambda x, y: taps.append((x, y))),
    )
    owner = control_module.activate_action_policy(guard)
    try:
        allowed = control_module.input_tap(
            (150, 100),
            intent=ActionIntent("reward_back", "top_left_back", "ratio-half"),
        )
    finally:
        control_module.remove_action_policy(owner)

    assert allowed is False
    assert taps == []


def test_checkpoint_ack_schedules_run_business_key(tmp_path, monkeypatch):
    path = tmp_path / "fatigue.json"
    triggers.register_deferred_fatigue_actions(
        [{"kind": "REOBSERVE", "waypoint_id": "B"}],
        plan_revision="rev-ack",
        path=path,
    )
    triggers.notify_fatigue_event("arrival", "B", path=path, schedule=Mock())
    requested = Mock()
    monkeypatch.setattr(
        recovery,
        "_run_daily_fatigue_recovery_impl",
        lambda **_kw: {"success": True, "status": "COMPLETE_FOR_DAY"},
    )
    monkeypatch.setattr(
        "core.services.task_schedule_state.request_immediate_run", requested
    )

    recovery.run_daily_fatigue_recovery(
        expected_waypoint="B", checkpoint_path=path
    )

    requested.assert_called_once_with("run_business")


def test_checkpoint_transfer_schedules_run_business_immediately(tmp_path, monkeypatch):
    path = tmp_path / "fatigue.json"
    day = SERVER_CLOCK.server_day_id()
    triggers.register_deferred_fatigue_actions(
        [{
            "kind": "REOBSERVE_RECOVERY_AT_WAYPOINT",
            "waypoint_id": "B",
            "cycle_id": "B|C",
            "cycle_server_day": day,
        }],
        plan_revision="rev-old",
        path=path,
    )
    triggers.notify_fatigue_event("arrival", "B", path=path, schedule=Mock())
    requested = Mock()
    monkeypatch.setattr(
        recovery,
        "_run_daily_fatigue_recovery_impl",
        lambda **_kw: {
            "success": True,
            "deferred": True,
            "status": "DEFER_UNTIL_WAYPOINT",
            "transfer_intent": {
                "target_waypoint": "C",
                "trigger_type": "WAYPOINT",
                "action_payload": {
                    "kind": "REOBSERVE_RECOVERY_AT_WAYPOINT",
                    "waypoint_id": "C",
                },
                "source_plan_revision": "rev-new",
                "cycle_id": "B|C",
                "cycle_server_day": day,
                "reason": "recover_at_future_waypoint",
            },
        },
    )
    monkeypatch.setattr(
        "core.services.task_schedule_state.request_immediate_run", requested
    )

    recovery.run_daily_fatigue_recovery(
        expected_waypoint="B", checkpoint_path=path
    )

    requested.assert_called_once_with("run_business")


def _ocr(text: str, x: int, y: int) -> dict:
    return {
        "text": text,
        "position": [
            [x - 10, y - 5],
            [x + 10, y - 5],
            [x + 10, y + 5],
            [x - 10, y + 5],
        ],
    }


def test_exchange_buy_classifier_precedes_generic_exchange_menu(monkeypatch):
    items = [
        _ocr("我要买", 700, 300),
        _ocr("预计买入", 850, 400),
        _ocr("买入总价", 900, 500),
        _ocr("载货量", 750, 600),
        _ocr("买入", 1050, 650),
    ]
    frame = SimpleNamespace(
        image=np.zeros((720, 1280, 3), dtype=np.uint8), ocr=lambda: items
    )
    monkeypatch.setattr(probe, "capture_envelope", lambda: SimpleNamespace(
        frame=frame.image,
        raw_frame_hash="b" * 64,
        backend_monotonic_sequence=1,
        backend_capture_id="test-capture-red",
        captured_at=datetime.now().astimezone(),
        backend_generation=1,
        instance_id="test-instance-0",
        adb_serial="test-adb-0",
    ))
    monkeypatch.setattr(probe, "Image", lambda _image: frame)

    assert probe._trusted_observation().as_observation().page_type == "exchange_buy"


def _digit_image(digit: str) -> np.ndarray:
    image = np.zeros((260, 320, 3), dtype=np.uint8)
    cv.putText(
        image,
        digit,
        (205, 215),
        cv.FONT_HERSHEY_SIMPLEX,
        1.0,
        (255, 255, 0),
        3,
        cv.LINE_AA,
    )
    return image


def test_image_zero_rejects_six():
    assert rewards._image_daily_zero(_digit_image("6")) is None


def test_image_zero_rejects_eight():
    assert rewards._image_daily_zero(_digit_image("8")) is None


def test_image_zero_rejects_nine():
    assert rewards._image_daily_zero(_digit_image("9")) is None
