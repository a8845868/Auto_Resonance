from unittest.mock import patch

import auto.run_business.sell as sell


def _successful_sale(**extra):
    with patch.object(sell, "is_all_cargo_selected", side_effect=[False, False]), patch.object(
        sell, "cargo_contains_expected_goods", return_value=True
    ), patch.object(sell, "read_raise_percent", return_value=20.0), patch.object(
        sell, "select_all_sellable_cargo", return_value=True
    ), patch.object(sell, "read_selected_sell_quote", return_value=(123, 456)), patch.object(
        sell, "click_sell_button", return_value=True
    ), patch.object(sell, "_sell_tap"), patch.object(sell.time, "sleep"):
        return sell.sell_business(expected_goods=["good-a"], **extra)


def test_sell_flow_evidence_preserves_leg_and_boundaries(monkeypatch):
    events = []
    monkeypatch.setattr(
        sell,
        "capture_session_evidence",
        lambda name, **kwargs: events.append((name, kwargs)),
    )
    ledger = {"cycle_id": "cycle-1"}

    assert _successful_sale(ledger_context=ledger, leg_id="A->B#1") is True

    names = [name for name, _kwargs in events]
    assert names == [
        "SELL_FLOW_START",
        "SELL_CARGO_VALIDATE",
        "SELL_RAISE_OBSERVE",
        "SELL_SELECT_ALL_BEFORE",
        "SELL_SELECT_ALL_AFTER",
        "SELL_QUOTE_OBSERVE",
        "SELL_CONFIRM_BEFORE",
        "SELL_CONFIRM_AFTER",
        "SELL_FLOW_COMPLETE",
    ]
    assert all(kwargs["ledger_context"] is ledger for _name, kwargs in events)
    assert all(kwargs["leg_id"] == "A->B#1" for _name, kwargs in events)
    assert events[-2][1]["current_page_classification"] == "SALE_CONFIRMED"


def test_sell_confirmation_failure_records_precise_boundary(monkeypatch):
    events = []
    monkeypatch.setattr(
        sell,
        "capture_session_evidence",
        lambda name, **kwargs: events.append(
            (name, kwargs["current_page_classification"])
        ),
    )
    with patch.object(sell, "is_all_cargo_selected", side_effect=[True, True]), patch.object(
        sell, "read_raise_percent", return_value=20.0
    ), patch.object(sell, "read_selected_sell_quote", return_value=(123, 456)), patch.object(
        sell, "click_sell_button", return_value=False
    ):
        assert sell.sell_business(expected_goods=["good-a"]) is False

    assert events[-2:] == [
        ("SELL_CONFIRM_BEFORE", "SALE_CONFIRMATION_PENDING"),
        ("SELL_CONFIRM_AFTER", "SALE_NOT_CONFIRMED"),
    ]


def test_sell_evidence_failure_does_not_change_success(monkeypatch):
    monkeypatch.setattr(
        sell,
        "capture_session_evidence",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("no evidence")),
    )

    assert _successful_sale() is True
