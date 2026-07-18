from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

import auto.run_business.main as business
from core.model.city_goods import RouteModel, RoutesModel
from core.services.server_calendar import GameServerClock
from core.services.trade_ledger import (
    TradeEvent,
    TradeEventType,
    append_trade_event,
    stable_trade_event_id,
)
from core.services.trade_planning import recommend_max_feasible_runs_today
from core.services.weekly_plan_state import progress_summary


CLOCK = GameServerClock()
NOW = datetime(2026, 7, 18, 12, 0, tzinfo=CLOCK.timezone)


def _recommend(**updates):
    values = {
        "remaining_runs": 2,
        "cycle_fatigue": 100,
        "available_fatigue": 200,
        "recoverable_fatigue": 0,
        "purchase_books": 10,
        "books_per_cycle": 0,
        "partial_cycle": None,
        "price_fresh": True,
        "cycle": ("A", "B"),
        "leg_fatigue_schedule": (70, 30),
        "remaining_run_book_schedule": ({"A": 0, "B": 0}, {"A": 0, "B": 0}),
        "current_run_index": 0,
        "current_city": "A",
        "price_revision": "r1",
        "current_price_revision": "r1",
    }
    values.update(updates)
    return recommend_max_feasible_runs_today(**values)


def test_today_recommendation_uses_partial_leg_remaining_cost():
    assert _recommend(
        available_fatigue=60,
        partial_cycle={"confirmed_legs": 1, "last_destination": "B"},
        current_city="B",
    ) == 1


def test_today_recommendation_uses_exact_next_run_book_schedule():
    assert _recommend(
        purchase_books=1,
        remaining_run_book_schedule=(
            {"A": 0, "B": 1},
            {"A": 2, "B": 0},
        ),
    ) == 1


def test_today_recommendation_changes_at_batch_boundary():
    schedule = ({"A": 1, "B": 0}, {"A": 2, "B": 0})
    assert _recommend(purchase_books=2, remaining_run_book_schedule=schedule) == 1
    assert _recommend(purchase_books=3, remaining_run_book_schedule=schedule) == 2


def _summary_state():
    return {
        "cycle": ["A", "B"],
        "total_runs": 3,
        "runs": [{"A": 0, "B": 0}] * 3,
        "completed_runs": 1,
        "completed_books": 0,
        "books_total": 0,
        "cycle_fatigue": 100,
        "leg_fatigue_schedule": [70, 30],
        "expected_profit": 600,
        "run_expected_profits": [100, 200, 300],
        "optimizer_config": {"weekly_fatigue": 300},
        "price_time": NOW.isoformat(),
        "price_source": "game_observed",
        "price_revision": "r1",
    }


def test_remaining_profit_excludes_confirmed_runs(tmp_path):
    summary = progress_summary(
        _summary_state(), ledger_path=tmp_path / "ledger.json", now=NOW
    )
    assert summary["remaining_expected_profit"] == 500
    assert summary["expected_total_net_profit"] == 600


def test_required_fatigue_is_not_labeled_available_fatigue(tmp_path):
    summary = progress_summary(
        _summary_state(), ledger_path=tmp_path / "ledger.json", now=NOW
    )
    assert summary["remaining_required_fatigue"] == 200
    assert summary["confirmed_available_fatigue"] is None


def test_unknown_current_resources_produce_unknown_recommendation(tmp_path):
    summary = progress_summary(
        _summary_state(), ledger_path=tmp_path / "ledger.json", now=NOW
    )
    assert summary["today_suggested_runs"] is None
    assert summary["today_recommendation_reason"] == "current_resources_unknown"


def test_multi_run_guard_cannot_reuse_initial_book_budget():
    with pytest.raises(ValueError, match="one complete round trip"):
        business.two_city_weekly_run(
            "A", "B", [{"runs": 2, "books": {"A": 1, "B": 1}}],
            max_runs=2, available_books=2,
        )


def test_single_run_production_contract_is_explicit():
    assert business.MAX_WEEKLY_RUNS_PER_INVOCATION == 1


def _active_routes():
    return RoutesModel(city_data=[
        RouteModel(buy_city_name="A", sell_city_name="B", goods_data={}),
        RouteModel(buy_city_name="B", sell_city_name="A", goods_data={}),
    ])


def _active_context(tmp_path):
    ledger = tmp_path / "ledger.json"
    event = TradeEvent(
        event_id=stable_trade_event_id(
            "2026-07-13", "A|B", "cycle-active", "A|B", TradeEventType.LEG_STARTED
        ),
        server_week_id="2026-07-13",
        route_id="A|B",
        cycle_id="cycle-active",
        leg_id="A|B",
        event_type=TradeEventType.LEG_STARTED,
        origin="A",
        destination="B",
        observed_at=NOW,
        confirmed_by="GAME_OBSERVED",
    )
    append_trade_event(ledger, event)
    return {
        "ledger_path": ledger,
        "server_week_id": "2026-07-13",
        "route_id": "A|B",
        "cycle_id": "cycle-active",
        "origin": "A",
        "completed_legs": 0,
    }


def test_active_leg_off_route_city_fails_closed_without_generic_navigation(tmp_path):
    image = MagicMock()
    image.ocr.return_value = []
    with patch.object(business, "connect", return_value=True), patch.object(
        business, "is_game_running", return_value=True
    ), patch.object(business, "is_sell_page", return_value=False), patch.object(
        business, "_normalize_trade_startup_screen", return_value=True
    ), patch.object(business, "get_station", return_value="C"), patch.object(
        business, "screenshot", return_value=image
    ), patch.object(business, "click_station") as navigate, patch.object(
        business, "go_business"
    ) as exchange:
        assert business.run(_active_routes(), ledger_context=_active_context(tmp_path)) is False
    navigate.assert_not_called()
    exchange.assert_not_called()


def test_active_leg_unknown_city_never_buys_or_sells(tmp_path):
    with patch.object(business, "connect", return_value=True), patch.object(
        business, "is_game_running", return_value=True
    ), patch.object(business, "is_sell_page", return_value=False), patch.object(
        business, "_normalize_trade_startup_screen", return_value=True
    ), patch.object(business, "get_station", return_value=None), patch.object(
        business, "buy_business"
    ) as buy, patch.object(business, "sell_business") as sell:
        assert business.run(_active_routes(), ledger_context=_active_context(tmp_path)) is False
    buy.assert_not_called()
    sell.assert_not_called()
