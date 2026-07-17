from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

import pytest

import core.control.control as control
from core.services.server_calendar import GameServerClock
from core.services.task_schedule_state import next_daily_reset
from core.services.trade_planning import (
    StalePriceSnapshot,
    build_executable_trade_plan,
)


def test_stale_price_blocks_real_business_execution():
    clock = GameServerClock()
    state = {
        "cycle": ["岚心城", "武林源"],
        "price_time": "2026-07-17T10:00:00+08:00",
        "cycle_fatigue": 100,
        "expected_profit": 1000,
        "books_total": 0,
        "optimizer_config": {"cargo": 100},
    }
    with pytest.raises(StalePriceSnapshot):
        build_executable_trade_plan(
            state,
            now=datetime(2026, 7, 17, 11, 0, tzinfo=clock.timezone),
            fatigue_budget=100,
            purchase_books=0,
        )


def test_optimizer_is_referenced_by_production_calculation():
    clock = GameServerClock()
    state = {
        "cycle": ["岚心城", "武林源"],
        "price_time": "2026-07-17T10:50:00+08:00",
        "cycle_fatigue": 100,
        "expected_profit": 1000,
        "books_total": 0,
        "optimizer_config": {"cargo": 100},
    }
    with patch("core.services.trade_planning.choose_trade_plan", wraps=__import__(
        "core.services.trade_planning", fromlist=["choose_trade_plan"]
    ).choose_trade_plan) as optimizer:
        plan = build_executable_trade_plan(
            state,
            now=datetime(2026, 7, 17, 11, 0, tzinfo=clock.timezone),
            fatigue_budget=100,
            purchase_books=0,
        )
    assert plan.expected_total_net_profit == 1000
    optimizer.assert_called_once()


def test_nemu_programming_error_is_not_silently_downgraded(monkeypatch):
    device = Mock(is_mumu=True, port=16384)
    monkeypatch.setattr(control, "get_runtime_device", lambda: device)
    monkeypatch.setattr(control, "NEMU", Mock(side_effect=RuntimeError("programming bug")))
    with pytest.raises(RuntimeError, match="programming bug"):
        control.connect()


def test_naive_datetime_compatibility_converts_from_system_timezone():
    local = timezone(timedelta(hours=-5))
    naive = datetime(2026, 7, 17, 4, 30)
    with patch("core.services.task_schedule_state._system_local_timezone", return_value=local):
        result = next_daily_reset(naive)
    assert result == datetime(2026, 7, 18, 5, 0)
