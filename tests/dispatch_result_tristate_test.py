from __future__ import annotations

import pytest

import auto.inventory as inventory
from core.control.nemu_receipt import (
    DeliveryStatus,
    NemuInputDispatchError,
    NemuTouchReceipt,
    ReleaseStatus,
)
from core.services.action_summary_navigation import ActionSummaryNavigator
from core.services.dispatch_outcome import (
    DispatchOutcome,
    DispatchStatus,
    physical_input_count_from_dispatch_error,
)
from core.services.personal_action_budget import EpisodeActionBudget
from tests.action_summary_navigation_test import Clock, Frames, home
from tests.city_navigation_adapter_test import _adapter, _home
from tests.eleventh_pass_boundaries_test import _guard, _obs, _tap_intent
from tests.inventory_assets_test import (
    Clock as InventoryClock,
    FrameProvider,
    _candidate,
    _geometry,
    _home_frame,
)


def _receipt(
    delivery: DeliveryStatus,
    *,
    touch_down_called: bool = False,
) -> NemuTouchReceipt:
    return NemuTouchReceipt(
        schema_version="1.0",
        attempt_id="attempt",
        dispatch_id="dispatch",
        instance_id="0",
        display_id=0,
        session_generation=1,
        capture_width=1280,
        capture_height=720,
        display_width=1280,
        display_height=720,
        rotation=0,
        capture_point=(100, 100),
        mapped_nemu_point=(100, 100),
        touch_down_called=touch_down_called,
        touch_down_return_code=0 if touch_down_called else None,
        touch_down_status="ACCEPTED" if touch_down_called else "NOT_CALLED",
        touch_up_called=False,
        touch_up_return_code=None,
        touch_up_status="NOT_CALLED",
        python_call_returned=False,
        delivery_status=delivery.value,
        release_status=ReleaseStatus.UNKNOWN.value,
        started_at="2026-08-09T00:00:00+08:00",
        finished_at="2026-08-09T00:00:01+08:00",
    )


def test_policy_returns_verified_and_unverified_truthy_outcomes_but_reuse_denial_is_false():
    verified, issuer, _executor = _guard()
    permit = issuer.issue(_tap_intent("verified"), ((50, 40),))
    verified_outcome = verified.authorize_coordinate((50, 40), permit=permit)
    assert verified_outcome
    assert verified_outcome.status is DispatchStatus.DISPATCHED_VERIFIED
    assert verified_outcome.receipt is None
    assert verified.authorize_coordinate((50, 40), permit=permit) is False

    unverified, issuer, executor = _guard(
        [_obs(oid="issue"), _obs(oid="consume"), _obs("unknown", oid="post", anchor=False)]
    )
    permit = issuer.issue(_tap_intent("unverified"), ((50, 40),))
    unverified_outcome = unverified.authorize_coordinate((50, 40), permit=permit)
    assert unverified_outcome
    assert unverified_outcome.status is DispatchStatus.DISPATCHED_UNVERIFIED
    assert unverified_outcome.receipt is None
    assert unverified.journal[-1].stage == "POSTCONDITION_FAILED"
    assert executor.taps == [(50, 40)]


def test_consumer_truth_styles_and_budget_count_unverified_as_dispatched():
    denied = False
    verified = DispatchOutcome(DispatchStatus.DISPATCHED_VERIFIED)
    unverified = DispatchOutcome(DispatchStatus.DISPATCHED_UNVERIFIED)

    assert denied is False
    assert verified is not False and bool(verified)
    assert unverified is not False and bool(unverified)

    budget = EpisodeActionBudget(clock=lambda: 1.0)
    decision = budget.authorize(
        state="HOME_READY",
        action_type="ENTER_CITY",
        normalized_point=(100, 100),
    )
    if unverified is not False:
        budget.record_dispatch(decision)
    assert budget.total_actions == 1
    assert budget.actions_by_action_type["ENTER_CITY"] == 1


@pytest.mark.parametrize(
    ("delivery", "touch_down_called", "expected"),
    [
        (DeliveryStatus.REJECTED_BEFORE_DELIVERY, False, 0),
        (DeliveryStatus.UNKNOWN_AFTER_PARTIAL_DISPATCH, True, 1),
        (DeliveryStatus.UNKNOWN_AFTER_EXCEPTION, True, 1),
        (DeliveryStatus.UNKNOWN_AFTER_EXCEPTION, False, 0),
    ],
)
def test_exception_receipt_physical_input_accounting(
    delivery: DeliveryStatus,
    touch_down_called: bool,
    expected: int,
):
    error = NemuInputDispatchError(
        _receipt(delivery, touch_down_called=touch_down_called)
    )
    assert physical_input_count_from_dispatch_error(error) == expected


def test_receiptless_exception_preserves_zero_physical_input_accounting():
    assert physical_input_count_from_dispatch_error(RuntimeError("offline")) == 0


def test_city_partial_dispatch_exception_counts_once_and_never_redispatches():
    calls = []

    def tap(*_args, **_kwargs):
        calls.append(1)
        raise NemuInputDispatchError(
            _receipt(
                DeliveryStatus.UNKNOWN_AFTER_PARTIAL_DISPATCH,
                touch_down_called=True,
            )
        )

    result = _adapter([_home(), _home(pixel=2)], tap=tap).enter_city()

    assert result.status == "BLOCKED"
    assert result.dispatch_count == 1
    assert result.physical_input_count == 1
    assert result.same_action_retry == 0
    assert len(calls) == 1


def test_action_summary_partial_dispatch_exception_counts_budget_once():
    calls = []
    evidence = []
    clock = Clock()

    def tap(*_args, **_kwargs):
        calls.append(1)
        raise NemuInputDispatchError(
            _receipt(
                DeliveryStatus.UNKNOWN_AFTER_PARTIAL_DISPATCH,
                touch_down_called=True,
            )
        )

    navigator = ActionSummaryNavigator(
        frame_provider=Frames([home("one"), home("two")]),
        tap=tap,
        evidence_recorder=evidence.append,
        monotonic=clock,
        sleep=clock.sleep,
        postcondition_timeout=1.0,
        poll_interval=0.2,
    )
    result = navigator.navigate()

    assert not result.success
    assert result.reason == "dispatch_failure"
    assert result.dispatch_count == 1
    assert navigator.budget.total_actions == 1
    assert len(calls) == len(evidence) == 1


def test_inventory_partial_dispatch_exception_is_accounted_and_not_retried(
    monkeypatch,
):
    provider = FrameProvider(
        [
            _home_frame(capture_id="home-1"),
            _home_frame(pixel=2, capture_id="home-2"),
        ]
    )
    clock = InventoryClock()
    calls = []
    evidence = []
    accounted = []
    original = inventory.physical_input_count_from_dispatch_error

    def account(error):
        value = original(error)
        accounted.append(value)
        return value

    def dispatch(*_args, **_kwargs):
        calls.append(1)
        raise NemuInputDispatchError(
            _receipt(
                DeliveryStatus.UNKNOWN_AFTER_PARTIAL_DISPATCH,
                touch_down_called=True,
            )
        )

    monkeypatch.setattr(
        inventory, "physical_input_count_from_dispatch_error", account
    )
    result = inventory._open_assets_entry(
        frame_provider=provider,
        dispatcher=dispatch,
        candidate_resolver=lambda _image: [_candidate()],
        geometry_provider=_geometry,
        evidence_recorder=evidence.append,
        timeout=0.8,
        poll_interval=0.2,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )

    assert result is False
    assert accounted == [1]
    assert len(calls) == len(evidence) == 1
