from datetime import datetime, timedelta

from core.services.server_calendar import GameServerClock
from core.services.trade_ledger import (
    ProgressSource,
    TradeEvent,
    TradeEventType,
    append_trade_event,
    load_trade_week_state,
    reconcile_trade_baseline,
)
from core.services.trade_planning import (
    TradeCandidate,
    choose_trade_plan,
    price_snapshot_is_fresh,
)


def _at(minutes=0):
    clock = GameServerClock()
    return datetime(2026, 7, 17, 12, minutes, tzinfo=clock.timezone)


def _event(event_id, event_type, *, cycle="cycle-1", books=0, observed_at=None):
    return TradeEvent(
        event_id=event_id,
        server_week_id="2026-07-13",
        route_id="岚心城|汇流塔",
        cycle_id=cycle,
        leg_id="岚心城|汇流塔",
        event_type=event_type,
        origin="岚心城",
        destination="汇流塔",
        observed_at=observed_at or _at(),
        confirmed_by="GAME_OBSERVED",
        purchase_book_delta=books,
    )


def test_unknown_weekly_progress_is_not_rendered_as_zero(tmp_path):
    state = load_trade_week_state(tmp_path / "ledger.json", now=_at())
    assert state.source is ProgressSource.UNKNOWN
    assert state.confirmed_round_trips is None


def test_round_trip_is_committed_only_after_verified_cycle_completion(tmp_path):
    path = tmp_path / "ledger.json"
    append_trade_event(path, _event("leg", TradeEventType.LEG_COMPLETED))
    state = load_trade_week_state(path, now=_at())
    assert state.confirmed_round_trips is None
    assert state.confirmed_delta_since_baseline == 0
    append_trade_event(path, _event("round", TradeEventType.ROUND_TRIP_COMPLETED))
    state = load_trade_week_state(path, now=_at())
    assert state.confirmed_round_trips is None
    assert state.confirmed_delta_since_baseline == 1


def test_crash_after_outbound_resumes_partial_cycle(tmp_path):
    path = tmp_path / "ledger.json"
    append_trade_event(path, _event("leg", TradeEventType.LEG_COMPLETED))
    state = load_trade_week_state(path, now=_at())
    assert state.current_partial_cycle["cycle_id"] == "cycle-1"
    assert state.current_partial_cycle["confirmed_legs"] == 1


def test_duplicate_round_trip_event_is_idempotent(tmp_path):
    path = tmp_path / "ledger.json"
    event = _event("same", TradeEventType.ROUND_TRIP_COMPLETED)
    assert append_trade_event(path, event) is True
    assert append_trade_event(path, event) is False
    state = load_trade_week_state(path, now=_at())
    assert state.confirmed_round_trips is None
    assert state.confirmed_delta_since_baseline == 1


def test_purchase_book_is_counted_only_after_confirmed_use(tmp_path):
    path = tmp_path / "ledger.json"
    append_trade_event(path, _event("planned", TradeEventType.PURCHASE_CONFIRMED, books=0))
    append_trade_event(path, _event("book", TradeEventType.PURCHASE_BOOK_CONFIRMED, books=2))
    assert load_trade_week_state(path, now=_at()).purchase_books_used == 2


def test_manual_reconciliation_records_source_and_timestamp(tmp_path):
    path = tmp_path / "ledger.json"
    reconcile_trade_baseline(path, 4, observed_at=_at())
    state = load_trade_week_state(path, now=_at())
    assert state.source is ProgressSource.USER_CORRECTED
    assert state.confirmed_round_trips == 4
    assert state.last_reconciled_at == _at()


def test_manual_reconciliation_replaces_earlier_ledger_total(tmp_path):
    path = tmp_path / "ledger.json"
    append_trade_event(
        path,
        _event(
            "before-correction",
            TradeEventType.ROUND_TRIP_COMPLETED,
            observed_at=_at() - timedelta(minutes=10),
        ),
    )
    reconcile_trade_baseline(path, 4, observed_at=_at())
    append_trade_event(
        path,
        _event(
            "after-correction",
            TradeEventType.ROUND_TRIP_COMPLETED,
            cycle="cycle-2",
            observed_at=_at() + timedelta(minutes=10),
        ),
    )
    assert load_trade_week_state(path, now=_at()).confirmed_round_trips == 5


def test_server_week_reset_rotates_state_once(tmp_path):
    path = tmp_path / "ledger.json"
    append_trade_event(path, _event("round", TradeEventType.ROUND_TRIP_COMPLETED))
    next_week = _at() + timedelta(days=7)
    state = load_trade_week_state(path, now=next_week)
    assert state.server_week_id == "2026-07-20"
    assert state.confirmed_round_trips is None


def test_optimizer_maximizes_total_net_profit_under_fatigue_budget():
    candidates = (
        TradeCandidate("high", net_profit=1000, fatigue=100, books=0),
        TradeCandidate("efficient", net_profit=600, fatigue=50, books=0),
    )
    plan = choose_trade_plan(candidates, fatigue_budget=100, purchase_books=0)
    assert plan.expected_total_net_profit == 1200
    assert plan.route_counts == {"efficient": 2}


def test_optimizer_prefers_higher_profit_per_fatigue_on_tie():
    candidates = (
        TradeCandidate("wasteful", net_profit=1000, fatigue=100, books=0),
        TradeCandidate("efficient", net_profit=1000, fatigue=80, books=0),
    )
    plan = choose_trade_plan(candidates, fatigue_budget=100, purchase_books=0)
    assert plan.route_counts == {"efficient": 1}
    assert plan.expected_profit_per_fatigue == 12.5


def test_stale_price_snapshot_invalidates_plan():
    assert price_snapshot_is_fresh(_at(), now=_at() + timedelta(minutes=29)) is True
    assert price_snapshot_is_fresh(_at(), now=_at() + timedelta(minutes=31)) is False
