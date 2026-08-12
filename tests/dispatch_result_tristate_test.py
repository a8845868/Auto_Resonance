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
    outcome_from_receipt,
    physical_input_count_from_dispatch_error,
)
from core.services.personal_action_budget import EpisodeActionBudget
from tests.action_summary_navigation_test import Clock, Frames, home, overview
from tests.city_navigation_adapter_test import _adapter, _city_detail, _home
from tests.eleventh_pass_boundaries_test import _guard, _obs, _tap_intent
from tests.inventory_assets_test import (
    Clock as InventoryClock,
    FrameProvider,
    _candidate,
    _geometry,
    _home_frame,
    _inventory_frame,
)


def _receipt(
    delivery: DeliveryStatus,
    *,
    touch_down_called: bool = False,
    release: ReleaseStatus = ReleaseStatus.UNKNOWN,
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
        release_status=release.value,
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
    evidence = []

    def tap(*_args, **_kwargs):
        calls.append(1)
        raise NemuInputDispatchError(
            _receipt(
                DeliveryStatus.UNKNOWN_AFTER_PARTIAL_DISPATCH,
                touch_down_called=True,
            )
        )

    adapter = _adapter([_home(), _home(pixel=2)], tap=tap)
    adapter.evidence_recorder = evidence.append
    result = adapter.enter_city()

    assert result.status == "BLOCKED"
    assert result.dispatch_count == 1
    assert result.physical_input_count == 1
    assert result.same_action_retry == 0
    assert len(calls) == 1
    assert evidence[0].dispatch_result == "UNKNOWN_AFTER_PARTIAL_DISPATCH"


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
    assert evidence[0].dispatch_result == "UNKNOWN_AFTER_PARTIAL_DISPATCH"


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
    assert evidence[0].dispatch_result == "UNKNOWN_AFTER_PARTIAL_DISPATCH"


class _ReceiptExecutor:
    def __init__(self, *, receipt=None, error=None):
        self.receipt = receipt
        self.error = error

    def tap(self, _point):
        if self.error is not None:
            raise self.error
        return self.receipt

    def swipe(self, _trajectory, _duration_ms):
        if self.error is not None:
            raise self.error
        return self.receipt


def test_policy_journal_carries_native_receipt_through_verified_lifecycle():
    receipt = _receipt(
        DeliveryStatus.NATIVE_ACCEPTED,
        touch_down_called=True,
        release=ReleaseStatus.CONFIRMED,
    )
    guard, issuer, _executor = _guard(executor=_ReceiptExecutor(receipt=receipt))
    permit = issuer.issue(_tap_intent("journal-native"), ((50, 40),))

    outcome = guard.authorize_coordinate((50, 40), permit=permit)

    assert outcome.status is DispatchStatus.DISPATCHED_VERIFIED
    assert outcome.receipt is receipt
    for stage in ("EXECUTED", "POSTCONDITION_VERIFIED"):
        entry = next(item for item in guard.journal if item.stage == stage)
        assert entry.delivery_status == DeliveryStatus.NATIVE_ACCEPTED.value
        assert entry.release_status == ReleaseStatus.CONFIRMED.value


def test_policy_postcondition_failed_journal_preserves_native_receipt():
    receipt = _receipt(
        DeliveryStatus.NATIVE_ACCEPTED,
        touch_down_called=True,
        release=ReleaseStatus.CONFIRMED,
    )
    guard, issuer, _executor = _guard(
        [
            _obs(oid="issue"),
            _obs(oid="consume"),
            _obs("unknown", oid="post", anchor=False),
        ],
        executor=_ReceiptExecutor(receipt=receipt),
    )
    permit = issuer.issue(_tap_intent("journal-unverified"), ((50, 40),))

    outcome = guard.authorize_coordinate((50, 40), permit=permit)

    assert outcome.status is DispatchStatus.DISPATCHED_UNVERIFIED
    failed = guard.journal[-1]
    assert failed.stage == "POSTCONDITION_FAILED"
    assert failed.delivery_status == DeliveryStatus.NATIVE_ACCEPTED.value
    assert failed.release_status == ReleaseStatus.CONFIRMED.value


@pytest.mark.parametrize(
    "delivery",
    [
        DeliveryStatus.REJECTED_BEFORE_DELIVERY,
        DeliveryStatus.UNKNOWN_AFTER_PARTIAL_DISPATCH,
    ],
)
def test_policy_execution_failed_journal_carries_exception_receipt_and_reraises(
    delivery,
):
    receipt = _receipt(
        delivery,
        touch_down_called=(
            delivery is DeliveryStatus.UNKNOWN_AFTER_PARTIAL_DISPATCH
        ),
    )
    error = NemuInputDispatchError(receipt)
    guard, issuer, _executor = _guard(executor=_ReceiptExecutor(error=error))
    permit = issuer.issue(_tap_intent(f"journal-{delivery.value}"), ((50, 40),))

    with pytest.raises(NemuInputDispatchError) as caught:
        guard.authorize_coordinate((50, 40), permit=permit)

    assert caught.value is error
    failed = guard.journal[-1]
    assert failed.stage == "EXECUTION_FAILED"
    assert failed.delivery_status == delivery.value
    assert failed.release_status == ReleaseStatus.UNKNOWN.value


def test_receiptless_backend_keeps_empty_journal_fields_and_evidence_fallback():
    guard, issuer, _executor = _guard()
    permit = issuer.issue(_tap_intent("journal-adb"), ((50, 40),))
    outcome = guard.authorize_coordinate((50, 40), permit=permit)
    assert outcome.delivery_status == outcome.release_status == ""
    assert guard.journal[-1].delivery_status == ""
    assert guard.journal[-1].release_status == ""

    evidence = []
    adapter = _adapter(
        [
            _home(),
            _home(pixel=2),
            _city_detail(pixel=3),
        ],
        tap=lambda *_args, **_kwargs: DispatchOutcome(
            DispatchStatus.DISPATCHED_VERIFIED
        ),
    )
    adapter.evidence_recorder = evidence.append
    result = adapter.enter_city()
    assert result.status == "PASS"
    assert evidence[0].dispatch_result == "call_returned"


def test_city_and_action_summary_success_evidence_use_native_delivery_status():
    receipt = _receipt(
        DeliveryStatus.NATIVE_ACCEPTED,
        touch_down_called=True,
        release=ReleaseStatus.CONFIRMED,
    )
    dispatched = outcome_from_receipt(
        DispatchStatus.DISPATCHED_VERIFIED,
        receipt=receipt,
    )

    city_evidence = []
    adapter = _adapter(
        [_home(), _home(pixel=2), _city_detail(pixel=3)],
        tap=lambda *_args, **_kwargs: dispatched,
    )
    adapter.evidence_recorder = city_evidence.append
    assert adapter.enter_city().status == "PASS"
    assert city_evidence[0].dispatch_result == "NATIVE_ACCEPTED"

    summary_evidence = []
    clock = Clock()
    navigator = ActionSummaryNavigator(
        frame_provider=Frames([home("one"), home("two"), overview("three")]),
        tap=lambda *_args, **_kwargs: dispatched,
        evidence_recorder=summary_evidence.append,
        monotonic=clock,
        sleep=clock.sleep,
        postcondition_timeout=1.0,
        poll_interval=0.2,
        stop_after_first_stage=True,
    )
    assert navigator.navigate().success
    assert summary_evidence[0].dispatch_result == "NATIVE_ACCEPTED"


def test_inventory_success_evidence_uses_native_delivery_status():
    receipt = _receipt(
        DeliveryStatus.NATIVE_ACCEPTED,
        touch_down_called=True,
        release=ReleaseStatus.CONFIRMED,
    )
    dispatched = outcome_from_receipt(
        DispatchStatus.DISPATCHED_VERIFIED,
        receipt=receipt,
    )
    provider = FrameProvider(
        [
            _home_frame(capture_id="home-1"),
            _home_frame(pixel=2, capture_id="home-2"),
            _inventory_frame(),
        ]
    )
    clock = InventoryClock()
    evidence = []

    result = inventory._open_assets_entry(
        frame_provider=provider,
        dispatcher=lambda *_args, **_kwargs: dispatched,
        candidate_resolver=lambda _image: [_candidate()],
        geometry_provider=_geometry,
        evidence_recorder=evidence.append,
        timeout=2.0,
        poll_interval=0.4,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )

    assert result is True
    assert evidence[0].dispatch_result == "NATIVE_ACCEPTED"
