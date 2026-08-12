from unittest.mock import MagicMock, patch

import auto.run_business.main as business
from core.model.city_goods import RouteModel, RoutesModel


def _routes():
    return RoutesModel(
        city_data=[
            RouteModel(buy_city_name="A", sell_city_name="B", goods_data={"good": {}}),
            RouteModel(buy_city_name="B", sell_city_name="A", goods_data={"good": {}}),
        ]
    )


def _run_to_purchase_boundary(tmp_path, buy_result):
    context = {
        "ledger_path": tmp_path / "trade-ledger.json",
        "server_week_id": "2026-08-10",
        "route_id": "A|B",
        "cycle_id": "cycle-purchase-proof",
        "origin": "A",
        "completed_legs": 0,
        "require_purchase_guard": True,
        "purchase_validator": lambda *_args, **_kwargs: True,
    }
    image = MagicMock()
    image.ocr.return_value = []
    recorded = []

    def record(_context, event_type, **_kwargs):
        recorded.append(event_type)
        return True

    travel = object()
    with patch.object(business, "connect", return_value=True), patch.object(
        business, "is_game_running", return_value=True
    ), patch.object(business, "is_sell_page", return_value=False), patch.object(
        business, "_normalize_trade_startup_screen", return_value=True
    ), patch.object(business, "get_station", return_value="A"), patch.object(
        business, "screenshot", return_value=image
    ), patch.object(business, "is_train_in_transit", return_value=False), patch.object(
        business, "go_business", return_value=True
    ), patch.object(business, "_clear_residual_cargo", return_value=True), patch.object(
        business, "prepare_negotiation", return_value=2
    ), patch.object(
        business, "buy_business", return_value=buy_result
    ) as buy, patch.object(
        business, "_record_ledger_event", side_effect=record
    ), patch.object(
        business, "_begin_departure", return_value=travel
    ) as departure, patch.object(
        business, "_wait_for_arrival_with_evidence", return_value=False
    ), patch.object(
        business, "read_strength", return_value=(100, 816)
    ), patch(
        "core.services.fatigue_triggers.notify_fatigue_event"
    ):
        result = business.run(_routes(), ledger_context=context)

    return result, recorded, buy, departure


def _event_names(events):
    return [event.value if hasattr(event, "value") else str(event) for event in events]


def test_bare_truthy_buy_result_never_commits_purchase_or_departs(tmp_path):
    result, events, buy, departure = _run_to_purchase_boundary(tmp_path, True)

    assert result is False
    assert "PURCHASE_CONFIRMED" not in _event_names(events)
    departure.assert_not_called()
    assert buy.call_args.kwargs.get("on_purchase_confirmed") is None


def test_affirmative_structured_buy_result_commits_once_before_departure(tmp_path):
    result, events, _buy, departure = _run_to_purchase_boundary(
        tmp_path,
        {
            "success": True,
            "confirmed_books": 0,
            "purchase_confirmed": True,
            "cargo_already_full": False,
        },
    )

    assert result is False
    assert _event_names(events).count("PURCHASE_CONFIRMED") == 1
    departure.assert_called_once()


def test_already_full_cargo_can_continue_without_forging_purchase_event(tmp_path):
    result, events, _buy, departure = _run_to_purchase_boundary(
        tmp_path,
        {
            "success": True,
            "confirmed_books": 0,
            "purchase_confirmed": False,
            "cargo_already_full": True,
        },
    )

    assert result is False
    assert "PURCHASE_CONFIRMED" not in _event_names(events)
    departure.assert_called_once()
