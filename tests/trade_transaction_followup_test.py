import json
from datetime import datetime, timedelta
from unittest.mock import Mock, patch

import pytest

import auto.run_business.buy as buy
import auto.run_business.main as business
import core.services.weekly_plan_state as weekly_state
from core.services.server_calendar import GameServerClock
from core.services.trade_ledger import (
    LedgerMigrationRequired,
    ProgressSource,
    TradeEvent,
    TradeEventType,
    append_trade_event,
    append_trade_events,
    load_trade_cycle_state,
    load_trade_week_state,
    migrate_trade_ledger,
    reconcile_trade_baseline,
    stable_trade_event_id,
)
from core.services.weekly_plan_state import progress_summary


CLOCK = GameServerClock()
WEEK = "2026-07-13"
ROUTE = "岚心城|武林源"
CYCLE = "cycle-a"


def _at(minute=0, *, days=0):
    return datetime(2026, 7, 17, 12, minute, tzinfo=CLOCK.timezone) + timedelta(days=days)


def _event(event_type, leg_id="", *, minute=0, days=0, cycle=CYCLE, route=ROUTE):
    observed = _at(minute, days=days)
    return TradeEvent(
        event_id=stable_trade_event_id(WEEK, route, cycle, leg_id, event_type, 0),
        server_week_id=WEEK,
        route_id=route,
        cycle_id=cycle,
        leg_id=leg_id,
        event_type=event_type,
        origin=leg_id.split("|")[0] if "|" in leg_id else "岚心城",
        destination=leg_id.split("|")[-1] if "|" in leg_id else "武林源",
        observed_at=observed,
        confirmed_by="GAME_OBSERVED",
    )


def _context(path):
    return {
        "ledger_path": path,
        "server_week_id": WEEK,
        "route_id": ROUTE,
        "cycle_id": CYCLE,
        "origin": "岚心城",
    }


def test_crash_after_second_leg_finalizes_without_rerunning_route(tmp_path):
    path = tmp_path / "ledger.json"
    append_trade_events(
        path,
        [
            _event(TradeEventType.CYCLE_STARTED),
            _event(TradeEventType.LEG_COMPLETED, "岚心城|武林源", minute=1),
            _event(TradeEventType.LEG_COMPLETED, "武林源|岚心城", minute=2),
        ],
    )
    runner = Mock(return_value=False)

    result = business.execute_weekly_cycle(object(), _context(path), runner=runner)

    assert result["success"] is True
    assert result["finalized_without_route_rerun"] is True
    runner.assert_not_called()
    assert load_trade_cycle_state(path, CYCLE).phase == "CYCLE_COMPLETED"


def test_crash_after_book_confirmation_does_not_consume_second_book():
    sold_out = Mock()
    sold_out.get_bgr.return_value = Mock(r=14)
    refreshed = Mock()
    refreshed.get_bgr.return_value = Mock(r=60)
    confirmed = Mock()

    with patch.object(buy, "find_text", side_effect=[((700, 300), sold_out), ((700, 300), refreshed)]), patch.object(
        buy, "use_book", return_value=True
    ) as use_book, patch.object(buy, "click"):
        result, count = buy.buy_good(
            "货物", 0, 1, on_book_confirmed=confirmed
        )

    assert result is True
    assert count == 1
    use_book.assert_called_once()
    confirmed.assert_called_once_with(1)

    with patch.object(buy, "find_text", return_value=((700, 300), sold_out)), patch.object(
        buy, "use_book", return_value=True
    ) as repeated:
        result, count = buy.buy_good(
            "货物", 1, 1, on_book_confirmed=confirmed
        )
    assert result is None
    assert count == 1
    repeated.assert_not_called()


def test_purchase_confirmed_resume_does_not_buy_again(tmp_path):
    path = tmp_path / "ledger.json"
    leg = "岚心城|武林源"
    append_trade_events(
        path,
        [
            _event(TradeEventType.LEG_STARTED, leg),
            _event(TradeEventType.PURCHASE_CONFIRMED, leg, minute=1),
        ],
    )
    assert business.resume_action_for_leg(_context(path), leg) == "DEPART"


def test_departure_confirmed_resume_never_issues_departure_again(tmp_path):
    path = tmp_path / "ledger.json"
    leg = "岚心城|武林源"
    append_trade_events(
        path,
        [
            _event(TradeEventType.PURCHASE_CONFIRMED, leg),
            _event(TradeEventType.DEPARTURE_CONFIRMED, leg, minute=1),
        ],
    )
    action = business.resume_action_for_leg(_context(path), leg)
    assert action == "WAIT_ARRIVAL"
    assert business.should_issue_departure(action, "岚心城", "武林源") is False
    assert business.should_issue_departure(action, "武林源", "武林源") is False


def test_sale_confirmed_resume_only_finalizes_leg(tmp_path):
    path = tmp_path / "ledger.json"
    leg = "岚心城|武林源"
    append_trade_events(
        path,
        [
            _event(TradeEventType.ARRIVAL_CONFIRMED, leg),
            _event(TradeEventType.SALE_CONFIRMED, leg, minute=1),
        ],
    )
    assert business.resume_action_for_leg(_context(path), leg) == "FINALIZE_LEG"


def test_cycle_crossing_week_reset_uses_one_week_id(tmp_path):
    path = tmp_path / "ledger.json"
    append_trade_events(
        path,
        [
            _event(TradeEventType.CYCLE_STARTED),
            _event(TradeEventType.LEG_COMPLETED, "岚心城|武林源", days=2),
            _event(TradeEventType.LEG_COMPLETED, "武林源|岚心城", days=3),
        ],
    )
    business.execute_weekly_cycle(
        object(), _context(path), runner=Mock(), now=_at(days=4)
    )
    state = load_trade_cycle_state(path, CYCLE)
    assert state.server_week_id == WEEK
    assert {event["server_week_id"] for event in state.events} == {WEEK}


def test_unknown_baseline_remains_unknown_after_first_new_event(tmp_path):
    path = tmp_path / "ledger.json"
    append_trade_event(path, _event(TradeEventType.CYCLE_COMPLETED))
    state = load_trade_week_state(path, now=_at())
    assert state.baseline_known is False
    assert state.confirmed_delta_since_baseline == 1
    assert state.full_week_total is None


def test_manual_baseline_changes_actual_remaining_execution(tmp_path):
    path = tmp_path / "ledger.json"
    reconcile_trade_baseline(path, 3, observed_at=_at())
    state = {
        "cycle": ["岚心城", "武林源"],
        "total_runs": 5,
        "runs": [{}, {}, {}, {}, {}],
        "completed_runs": 0,
        "completed_books": 0,
        "books_total": 0,
        "cycle_fatigue": 100,
        "expected_profit": 500,
        "optimizer_config": {"weekly_fatigue": 500},
    }
    summary = progress_summary(state, ledger_path=path, now=_at())
    assert summary["remaining_runs"] == 2
    assert summary["finished"] is False


def test_manual_correction_discards_pre_correction_partial_cycle(tmp_path):
    path = tmp_path / "ledger.json"
    append_trade_event(path, _event(TradeEventType.LEG_COMPLETED, "岚心城|武林源"))
    reconcile_trade_baseline(path, 2, observed_at=_at(10))
    state = load_trade_week_state(path, now=_at(11))
    assert state.current_partial_cycle is None


def test_last_transaction_uses_max_observed_at(tmp_path):
    path = tmp_path / "ledger.json"
    append_trade_events(
        path,
        [
            _event(TradeEventType.PURCHASE_CONFIRMED, minute=20, cycle="later"),
            _event(TradeEventType.SALE_CONFIRMED, minute=5, cycle="earlier"),
        ],
    )
    assert load_trade_week_state(path, now=_at()).last_confirmed_transaction_at == _at(20)


def test_unknown_schema_is_preserved_until_explicit_migration(tmp_path):
    path = tmp_path / "ledger.json"
    original = {"schema_version": 999, "events": [{"opaque": True}]}
    path.write_text(json.dumps(original), encoding="utf-8")
    with pytest.raises(LedgerMigrationRequired):
        append_trade_event(path, _event(TradeEventType.CYCLE_STARTED))
    assert json.loads(path.read_text(encoding="utf-8")) == original
    with pytest.raises(LedgerMigrationRequired):
        migrate_trade_ledger(path)


def test_plan_progress_rejects_boolean_without_two_leg_facts(tmp_path):
    plan = {
        "cycle": ["岚心城", "武林源"],
        "total_runs": 1,
        "completed_runs": 0,
        "committed_event_ids": [],
    }
    with patch.object(weekly_state, "load_weekly_plan", return_value=plan), patch.object(
        weekly_state, "_write_state"
    ) as write:
        result = weekly_state.record_completed_run(
            {}, ledger_path=tmp_path / "ledger.json", cycle_id="unverified"
        )

    assert result == plan
    assert result["completed_runs"] == 0
    write.assert_not_called()
