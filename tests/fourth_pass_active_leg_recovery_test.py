from datetime import datetime, timedelta
from unittest.mock import MagicMock, Mock, patch

import auto.run_business.main as business
from core.model.city_goods import RouteModel, RoutesModel
from core.services.server_calendar import GameServerClock
from core.services.trade_ledger import (
    TradeEvent,
    TradeEventType,
    append_trade_events,
    stable_trade_event_id,
)


CLOCK = GameServerClock()
NOW = datetime(2026, 7, 18, 12, 0, tzinfo=CLOCK.timezone)


def _event(kind, minute=0):
    leg_id = "A|B"
    return TradeEvent(
        event_id=stable_trade_event_id(
            "2026-07-13", "A|B", "cycle-active", leg_id, kind
        ),
        server_week_id="2026-07-13",
        route_id="A|B",
        cycle_id="cycle-active",
        leg_id=leg_id,
        event_type=kind,
        origin="A",
        destination="B",
        observed_at=NOW + timedelta(minutes=minute),
        confirmed_by="GAME_OBSERVED",
    )


def _routes():
    return RoutesModel(
        city_data=[
            RouteModel(buy_city_name="A", sell_city_name="B", goods_data={"A-good": {}}),
            RouteModel(buy_city_name="B", sell_city_name="A", goods_data={"B-good": {}}),
        ]
    )


def _run_arrived_active_leg(tmp_path, *, confirmed, sale_succeeds=True):
    ledger = tmp_path / "trade-ledger.json"
    events = [
        _event(TradeEventType.LEG_STARTED),
        _event(TradeEventType.PURCHASE_CONFIRMED, 1),
        _event(TradeEventType.DEPARTURE_REQUESTED, 2),
    ]
    if confirmed:
        events.append(_event(TradeEventType.DEPARTURE_CONFIRMED, 3))
    append_trade_events(ledger, events)
    context = {
        "ledger_path": ledger,
        "server_week_id": "2026-07-13",
        "route_id": "A|B",
        "cycle_id": "cycle-active",
        "origin": "A",
        "completed_legs": 0,
    }
    route = _routes()
    actions = []
    travel = MagicMock()
    travel.__bool__.return_value = True
    travel.wait.return_value = True

    def navigate(_destination, cur_station=None, on_departure_requested=None):
        actions.append(("depart", cur_station))
        if on_departure_requested:
            on_departure_requested()
        return travel

    def buy(*_args, **_kwargs):
        actions.append(("buy",))
        return False

    def sell(*_args, **_kwargs):
        actions.append(("sell",))
        return sale_succeeds

    image = MagicMock()
    image.ocr.return_value = []
    with patch.object(business, "connect", return_value=True), patch.object(
        business, "is_game_running", return_value=True
    ), patch.object(business, "is_sell_page", return_value=False), patch.object(
        business, "_normalize_trade_startup_screen", return_value=True
    ), patch.object(business, "get_station", return_value="B"), patch.object(
        business, "screenshot", return_value=image
    ), patch.object(business, "is_train_in_transit", return_value=False), patch.object(
        business, "go_business", return_value=True
    ), patch.object(business, "click_station", side_effect=navigate), patch.object(
        business, "prepare_negotiation", return_value=2
    ), patch.object(business, "buy_business", side_effect=buy) as buy_mock, patch.object(
        business, "_prepare_max_sell_haggle", return_value=2
    ), patch.object(business, "sell_business", side_effect=sell) as sell_mock, patch.object(
        business, "read_strength", return_value=(100, 816)
    ), patch("core.services.fatigue_triggers.notify_fatigue_event"):
        result = business.run(route, ledger_context=context)
    return route, actions, result, buy_mock, sell_mock


def test_arrived_requested_leg_resumes_sale_before_reverse_leg(tmp_path):
    route, actions, _result, _buy, _sell = _run_arrived_active_leg(
        tmp_path, confirmed=False
    )
    assert actions[0] == ("sell",)
    assert [(item.buy_city_name, item.sell_city_name) for item in route.city_data] == [
        ("A", "B"),
        ("B", "A"),
    ]


def test_arrived_confirmed_leg_resumes_sale_before_reverse_leg(tmp_path):
    _route, actions, _result, _buy, _sell = _run_arrived_active_leg(
        tmp_path, confirmed=True
    )
    assert actions[0] == ("sell",)


def test_active_leg_recovery_never_buys_before_pending_sale(tmp_path):
    _route, actions, _result, buy, sell = _run_arrived_active_leg(
        tmp_path, confirmed=True, sale_succeeds=False
    )
    sell.assert_called_once()
    buy.assert_not_called()
    assert actions == [("sell",)]


def test_active_leg_completion_then_selects_reverse_leg(tmp_path):
    _route, actions, _result, buy, sell = _run_arrived_active_leg(
        tmp_path, confirmed=True
    )
    sell.assert_called_once()
    buy.assert_called_once()
    assert actions[:2] == [("sell",), ("buy",)]


def test_price_revalidated_before_each_purchase(tmp_path):
    ledger = tmp_path / "trade-ledger.json"
    deferred = {
        "success": True,
        "deferred": True,
        "reason": "price_revision_changed_before_purchase",
        "next_run_at": (NOW + timedelta(minutes=15)).isoformat(),
    }
    validator = Mock(side_effect=[True, deferred])
    context = {
        "ledger_path": ledger,
        "server_week_id": "2026-07-13",
        "route_id": "A|B",
        "cycle_id": "cycle-guarded",
        "origin": "A",
        "completed_legs": 0,
        "require_purchase_guard": True,
        "purchase_validator": validator,
    }
    travel = MagicMock()
    travel.__bool__.return_value = True
    travel.wait.return_value = True
    image = MagicMock()
    image.ocr.return_value = []
    with patch.object(business, "connect", return_value=True), patch.object(
        business, "is_game_running", return_value=True
    ), patch.object(business, "is_sell_page", return_value=False), patch.object(
        business, "_normalize_trade_startup_screen", return_value=True
    ), patch.object(business, "get_station", return_value="A"), patch.object(
        business, "screenshot", return_value=image
    ), patch.object(business, "is_train_in_transit", return_value=False), patch.object(
        business, "go_business", return_value=True
    ), patch.object(business, "_clear_residual_cargo", return_value=True), patch.object(
        business, "click_station", return_value=travel
    ), patch.object(business, "prepare_negotiation", return_value=2), patch.object(
        business, "buy_business", return_value=True
    ) as buy, patch.object(
        business, "_prepare_max_sell_haggle", return_value=2
    ), patch.object(business, "sell_business", return_value=True), patch.object(
        business, "read_strength", return_value=(100, 816)
    ), patch("core.services.fatigue_triggers.notify_fatigue_event"):
        result = business.run(_routes(), ledger_context=context)
    assert result == deferred
    assert validator.call_count == 2
    assert buy.call_count == 1
