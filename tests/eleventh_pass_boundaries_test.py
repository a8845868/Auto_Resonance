from __future__ import annotations

import copy
import json
import threading
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

import auto.reward_collection as rewards
import core.control.control as control_module
from core.services import fatigue_triggers as triggers
from core.services.read_only_policy import (
    ActionIntent,
    ActionKind,
    AnchorResolver,
    ObservedAnchor,
    PageObservation,
    PageObserver,
    ReadOnlyActionGuard,
    ReadOnlySafetySession,
    ReadOnlyPermitHandle,
    ReadOnlyPermitIssuer,
    ReadOnlyPolicySpec,
)
from core.services.server_calendar import SERVER_CLOCK
from core.services.task_schedule_state import load_task_schedule, request_immediate_run


NOW = datetime(2026, 7, 19, 12, 0, tzinfo=timezone(timedelta(hours=8)))


def _obs(page="daily_activity", *, oid="obs", anchor=True):
    anchors = (
        ObservedAnchor("top_left_back", "返回", (20, 10, 130, 85)),
        ObservedAnchor("daily_content", "任务列表", (150, 180, 1180, 650)),
    ) if anchor else ()
    return PageObservation(
        oid, oid * 32, page, (page,), anchors, NOW,
    )


class Executor:
    def __init__(self, fail=False, entered=None, release=None):
        self.fail, self.entered, self.release = fail, entered, release
        self.taps, self.swipes = [], []

    def tap(self, point):
        self.taps.append(point)
        if self.entered:
            self.entered.set()
        if self.release:
            self.release.wait(5)
        if self.fail:
            raise RuntimeError("device write failed")

    def swipe(self, trajectory, duration_ms):
        self.swipes.append((trajectory, duration_ms))
        if self.fail:
            raise RuntimeError("device write failed")


def _guard(values=None, *, executor=None, policies=None, now=lambda: NOW, limit=256):
    values = list(values or [_obs(), _obs(), _obs("home", anchor=False)])
    state = {"index": 0}

    def observe():
        value = values[min(state["index"], len(values) - 1)]
        state["index"] += 1
        sequence = state["index"]
        return replace(
            value,
            captured_at=now() + timedelta(microseconds=sequence),
            capture_sequence=sequence,
            source_capture_id=f"eleventh-boundary-{id(state)}-{sequence}",
            source_monotonic_sequence=sequence,
            backend_generation=1,
            instance_id="test-instance-0",
            adb_serial="test-adb-0",
        )

    issuer = ReadOnlyPermitIssuer(
        PageObserver(observe), AnchorResolver(), policies=policies,
        now=now, registry_limit=limit,
    )
    executor = executor or Executor()
    return ReadOnlySafetySession(executor, permit_issuer=issuer, now=now), issuer, executor


def _tap_intent(correlation="tap"):
    return ActionIntent("reward_back", "top_left_back", correlation)


def _card(status=None):
    def box(text, x, y):
        return {"text": text, "position": [[x-5,y-5],[x+5,y-5],[x+5,y+5],[x-5,y+5]]}
    items = [box("1/2", 300, 350), box("安全运输", 300, 400), box("+10", 300, 550)]
    if status is not None:
        items.append(box(status, 300, 620))
    return items


def test_tap_policy_cannot_be_consumed_by_swipe_api():
    guard, _issuer, _executor = _guard()
    assert guard.authorize_swipe((50, 40), (100, 40), intent=_tap_intent(), duration_ms=300) is False
    assert guard.journal[-1].reason == "action_kind_mismatch"


def test_zero_length_scroll_is_rejected():
    guard, _issuer, _executor = _guard()
    intent = ActionIntent("daily_horizontal_scroll", "daily_content", "zero")
    assert guard.authorize_swipe((500, 350), (500, 350), intent=intent, duration_ms=300) is False
    assert "distinct" in guard.journal[-1].reason


def test_scroll_direction_and_minimum_displacement_are_enforced():
    spec = ReadOnlyPolicySpec(
        "left_scroll", frozenset({"daily_activity"}), "daily_content",
        action_kind=ActionKind.SWIPE, allowed_region=(150, 180, 1180, 650),
        allowed_swipe_directions=frozenset({"LEFT"}),
        postcondition="daily_anchor_remains_valid",
    )
    for start, end, reason in (
        ((500, 350), (520, 350), "minimum"),
        ((500, 350), (600, 350), "direction"),
    ):
        guard, _issuer, _executor = _guard(policies={"left_scroll": spec})
        assert guard.authorize_swipe(start, end, intent=ActionIntent("left_scroll", "daily_content"), duration_ms=300) is False
        assert reason in guard.journal[-1].reason


def test_trusted_executor_cannot_receive_a_different_trajectory(monkeypatch):
    calls = []
    monkeypatch.setattr(control_module, "control", type("D", (), {
        "ratio": 1.0,
        "input_swipe": staticmethod(lambda x1,y1,x2,y2,d: calls.append((x1,y1,x2,y2,d))),
    })())
    executor = control_module._create_bound_input_executor(control_module.control)
    captures = [
        replace(_obs(), content_marker_hash="before"),
        replace(_obs(), content_marker_hash="before"),
        replace(_obs(), content_marker_hash="after"),
    ]
    guard, _issuer, _unused = _guard(values=captures, executor=executor)
    owner = control_module.activate_action_policy(guard)
    try:
        assert control_module.input_swipe(
            (900, 350), (400, 350), 650,
            intent=ActionIntent("daily_horizontal_scroll", "daily_content"),
        ) is True
    finally:
        control_module.remove_action_policy(owner)
    assert calls == [(900, 350, 400, 350, 650)]
    executed = next(entry for entry in reversed(guard.journal) if entry.stage == "POSTCONDITION_VERIFIED")
    assert executed.physical_trajectory[0] == (900, 350)
    assert executed.physical_trajectory[-1] == (400, 350)


def test_permit_handle_mutation_cannot_change_registry_record():
    guard, issuer, _executor = _guard()
    handle = issuer.issue(_tap_intent("mutation"), ((50, 40),))
    token = handle.opaque_token
    object.__setattr__(handle, "opaque_token", "mutated")
    assert token in issuer._registry
    assert guard.authorize_coordinate((50, 40), permit=ReadOnlyPermitHandle(token)) is True


def test_direct_handle_copy_remains_single_use():
    guard, issuer, executor = _guard()
    handle = issuer.issue(_tap_intent("copy"), ((50, 40),))
    copied = copy.copy(handle)
    assert guard.authorize_coordinate((50, 40), permit=handle) is True
    assert guard.authorize_coordinate((50, 40), permit=copied) is False
    assert executor.taps == [(50, 40)]


def test_postcondition_failure_stops_followup_actions():
    guard, issuer, executor = _guard([_obs(oid="issue"), _obs(oid="consume"), _obs("unknown", oid="post", anchor=False)])
    handle = issuer.issue(_tap_intent("post-fail"), ((50, 40),))
    assert guard.authorize_coordinate((50, 40), permit=handle) is False
    assert guard.journal[-1].stage == "POSTCONDITION_FAILED"
    assert guard.authorize_coordinate((50, 40), permit=handle) is False
    assert executor.taps == [(50, 40)]


def test_issue_and_consume_use_independent_capture_sequences():
    guard, issuer, _executor = _guard()
    handle = issuer.issue(_tap_intent("captures"), ((50, 40),))
    entry = issuer._registry[handle.opaque_token]
    issue_sequence = entry.record.issue_capture_sequence
    assert guard.authorize_coordinate((50, 40), permit=handle) is True
    assert entry.consume_capture_sequence > issue_sequence


def test_page_change_inside_old_cache_window_is_rejected():
    guard, issuer, executor = _guard([_obs(oid="issue"), _obs("account_settings", oid="changed", anchor=False)])
    handle = issuer.issue(_tap_intent("page-change"), ((50, 40),))
    assert guard.authorize_coordinate((50, 40), permit=handle) is False
    assert executor.taps == []


def test_replacement_inherits_frozen_cycle_server_day(tmp_path):
    path = tmp_path / "fatigue.json"
    day = (datetime.fromisoformat(SERVER_CLOCK.server_day_id()) - timedelta(days=1)).date().isoformat()
    now = SERVER_CLOCK.server_now()
    path.write_text(json.dumps({"server_day_id": SERVER_CLOCK.server_day_id(), "actions": [{
        "id":"B", "waypoint_id":"B", "state":"CLAIMED", "schedule_status":"CLAIMED",
        "cycle_id":"B|C", "cycle_server_day":day, "owner_id":"o", "lease_token":"l",
        "lease_expires_at":(now+timedelta(minutes=5)).isoformat(),
    }]}), encoding="utf-8")
    result = {"success":True,"deferred":True,"status":"DEFER_UNTIL_WAYPOINT","transfer_intent":{
        "target_waypoint":"C","trigger_type":"WAYPOINT","action_payload":{"waypoint_id":"C"},
        "source_plan_revision":"r","cycle_id":"B|C","cycle_server_day":day,"reason":"x"}}
    done = triggers.complete_fatigue_checkpoint_processing("B", result, owner_id="o", lease_token="l", path=path)
    assert done["replacement"]["cycle_server_day"] == day
    assert done["replacement"]["parent_checkpoint_id"] == "B"


def test_empty_inventory_requires_authoritative_no_task_evidence():
    scanner = rewards.DailyCardScanner()
    scanner.add_page([]); scanner.add_page([]); scanner.add_page([])
    assert scanner.complete is True and scanner.claim_states_complete is False
    scanner.mark_authoritative_no_tasks()
    assert scanner.claim_states_complete is True


def test_missing_claim_text_remains_unknown():
    card = rewards._card_items(_card(None), manual=False)[0]
    assert card.claimable is None and card.claimed is None


def _handoff_state(path):
    path.write_text(json.dumps({"server_day_id":SERVER_CLOCK.server_day_id(),"actions":[{
        "id":"x","state":"ACKNOWLEDGED","scheduler_handoff":{"state":"FAILED_RETRYABLE","attempts":1}
    }]}), encoding="utf-8")


def test_failed_business_handoff_retries_without_process_restart(tmp_path):
    path = tmp_path / "fatigue.json"; _handoff_state(path)
    attempts = {"n":0}
    def schedule(_key):
        attempts["n"] += 1
        if attempts["n"] == 1: raise OSError("locked")
    assert triggers.deliver_pending_business_handoffs(path=path, schedule=schedule) is False
    assert triggers.deliver_pending_business_handoffs(path=path, schedule=schedule) is True
    assert attempts["n"] == 2


def test_handoff_retry_preserves_all_other_task_entries(tmp_path):
    checkpoint = tmp_path / "fatigue.json"; schedule = tmp_path / "schedule.json"
    _handoff_state(checkpoint)
    schedule.write_text(json.dumps({"tasks":{"other":{"next_run":"2030-01-01T00:00:00"}},"completed":[{"key":"other"}]}), encoding="utf-8")
    assert triggers.deliver_pending_business_handoffs(
        path=checkpoint, schedule=lambda key: request_immediate_run(key, schedule)
    ) is True
    state = load_task_schedule(schedule)
    assert state["tasks"]["other"]["next_run"] == "2030-01-01T00:00:00"
    assert state["completed"] == [{"key":"other"}]


def test_journal_does_not_mark_execution_success_before_hardware_returns():
    entered, release = threading.Event(), threading.Event()
    executor = Executor(entered=entered, release=release)
    guard, _issuer, _unused = _guard(executor=executor)
    result = []
    thread = threading.Thread(target=lambda: result.append(guard.authorize_coordinate((50,40), intent=_tap_intent("blocking"))))
    thread.start(); assert entered.wait(5)
    assert "EXECUTED" not in [entry.stage for entry in guard.journal]
    release.set(); thread.join(5)
    assert result == [True]
    assert guard.journal[-1].stage == "POSTCONDITION_VERIFIED"


def test_registry_expiry_cleanup_and_revoke_are_bounded():
    clock = {"now": NOW}
    guard, issuer, _executor = _guard(now=lambda: clock["now"], limit=1)
    expired = issuer.issue(_tap_intent("expired"), ((50,40),))
    clock["now"] += timedelta(seconds=6)
    assert issuer.cleanup_expired() == 1
    sequence = {"value": 100}
    def fresh():
        sequence["value"] += 1
        return replace(
            _obs(), captured_at=clock["now"] + timedelta(microseconds=sequence["value"]),
            capture_sequence=sequence["value"],
            source_capture_id=f"expiry-{sequence['value']}",
            source_monotonic_sequence=sequence["value"],
            backend_generation=1, instance_id="test-instance-0", adb_serial="test-adb-0",
        )
    issuer.observer._observe = fresh
    active = issuer.issue(_tap_intent("active"), ((50,40),))
    with pytest.raises(PermissionError, match="capacity"):
        issuer.issue(_tap_intent("overflow"), ((50,40),))
    assert issuer.revoke(active) is True
    assert issuer.registry_contains(active) is False
