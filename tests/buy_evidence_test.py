from unittest.mock import patch

import auto.run_business.buy as buy


def _successful_buy(**extra):
    with patch.object(buy, "get_boatload", return_value=50), patch.object(
        buy, "buy_good", return_value=(True, 0)
    ), patch.object(buy, "is_empty_goods", return_value=False), patch.object(
        buy, "click_bargain_button", return_value=True
    ), patch.object(buy, "click_buy_button", return_value=True), patch.object(
        buy, "_buy_tap"
    ), patch.object(buy.time, "sleep"):
        return buy.buy_business(["good-a"], [], num=1, **extra)


def test_buy_flow_evidence_preserves_leg_and_transition_order(monkeypatch):
    events = []

    def capture(name, **kwargs):
        events.append((name, kwargs))

    monkeypatch.setattr(buy, "capture_session_evidence", capture)
    ledger = {"cycle_id": "cycle-1"}

    assert _successful_buy(ledger_context=ledger, leg_id="A->B#1") is True

    names = [name for name, _kwargs in events]
    assert names == [
        "BUY_FLOW_START",
        "BUY_GOOD_BEFORE",
        "BUY_GOOD_AFTER",
        "BUY_BARGAIN_BEFORE",
        "BUY_BARGAIN_AFTER",
        "BUY_CONFIRM_BEFORE",
        "BUY_CONFIRM_AFTER",
        "BUY_FLOW_COMPLETE",
    ]
    assert all(kwargs["ledger_context"] is ledger for _name, kwargs in events)
    assert all(kwargs["leg_id"] == "A->B#1" for _name, kwargs in events)
    assert events[-2][1]["current_page_classification"] == "PURCHASE_CONFIRMED"


def test_buy_confirmation_failure_records_precise_boundary(monkeypatch):
    events = []
    monkeypatch.setattr(
        buy,
        "capture_session_evidence",
        lambda name, **kwargs: events.append(
            (name, kwargs["current_page_classification"])
        ),
    )
    with patch.object(buy, "get_boatload", return_value=50), patch.object(
        buy, "buy_good", return_value=(True, 0)
    ), patch.object(buy, "is_empty_goods", return_value=False), patch.object(
        buy, "click_bargain_button", return_value=True
    ), patch.object(buy, "click_buy_button", return_value=False):
        assert buy.buy_business(["good-a"], []) is False

    assert events[-2:] == [
        ("BUY_CONFIRM_BEFORE", "PURCHASE_CONFIRMATION_PENDING"),
        ("BUY_CONFIRM_AFTER", "PURCHASE_NOT_CONFIRMED"),
    ]


def test_buy_evidence_failure_does_not_change_success(monkeypatch):
    def fail_capture(*_args, **_kwargs):
        raise OSError("evidence unavailable")

    monkeypatch.setattr(buy, "capture_session_evidence", fail_capture)

    assert _successful_buy() is True
