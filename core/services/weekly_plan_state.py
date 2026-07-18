from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from core.utils.utils import ROOT_PATH
from core.services.server_calendar import SERVER_CLOCK
from core.services.trade_ledger import (
    LEDGER_PATH,
    TradeEventType,
    finalize_trade_cycle,
    load_trade_cycle_state,
    load_trade_week_state,
    stable_trade_event_id,
)


STATE_PATH = Path(ROOT_PATH).resolve() / "config" / "weekly_plan.json"
_STATE_LOCK = threading.RLock()


def current_week_start(today: date | None = None) -> str:
    today = today or SERVER_CLOCK.server_week_date()
    return (today - timedelta(days=today.weekday())).isoformat()


def _flatten_batches(batches: list[dict]) -> list[dict[str, int]]:
    runs: list[dict[str, int]] = []
    for batch in batches:
        books = {str(city): int(count) for city, count in batch.get("books", {}).items()}
        runs.extend(dict(books) for _ in range(int(batch.get("runs", 0))))
    return runs


def _compress_runs(runs: list[dict[str, int]]) -> list[dict]:
    batches: list[dict] = []
    for books in runs:
        if batches and batches[-1]["books"] == books:
            batches[-1]["runs"] += 1
        else:
            batches.append({"runs": 1, "books": dict(books)})
    return batches


def load_weekly_plan(include_expired: bool = False) -> dict[str, Any] | None:
    if not STATE_PATH.exists():
        return None
    try:
        state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    if not include_expired and state.get("week_start") != current_week_start():
        return None
    return state


def roll_weekly_plan_forward() -> dict[str, Any] | None:
    """Start a new week from the most recent configured plan.

    A Monday reset must clear progress, not make an enabled trading task look
    successfully finished.  The saved cycle and execution batches remain the
    user's active plan until they explicitly apply a replacement.
    """
    current = load_weekly_plan()
    if current:
        return current
    previous = load_weekly_plan(include_expired=True)
    if not previous or previous.get("week_start", "") > current_week_start():
        return None
    state = dict(previous)
    state["week_start"] = current_week_start()
    state["completed_runs"] = 0
    state["completed_books"] = 0
    # A new week can have a different restock-book inventory.  Keep the route
    # as the safe default, but force the live runner to verify inventory and
    # rebuild the execution batches before treating last week's book allocation
    # as current.
    state["needs_reoptimization"] = True
    state["updated_at"] = datetime.now().isoformat(timespec="seconds")
    _write_state(state)
    return state


def save_weekly_plan(result: dict) -> dict[str, Any]:
    runs = _flatten_batches(result["execution_batches"])
    existing = load_weekly_plan()
    completed_runs = 0
    completed_books = 0
    if existing and existing.get("cycle") == result["cycle"] and existing.get("runs") == runs:
        completed_runs = min(int(existing.get("completed_runs", 0)), len(runs))
        completed_books = int(existing.get("completed_books", 0))
    state: dict[str, Any] = {
        "version": 2,
        "schema_version": 2,
        "week_start": current_week_start(),
        "cycle": result["cycle"],
        "total_runs": len(runs),
        "runs": runs,
        "completed_runs": completed_runs,
        "completed_books": completed_books,
        "books_total": int(result.get("books_used", 0)),
        "cycle_fatigue": float(result.get("cycle_fatigue", 0)),
        "expected_profit": int(result.get("combined_profit", result.get("profit", 0))),
        "cargo_profit": int(result.get("cargo_profit", result.get("profit", 0))),
        "passenger_profit": int(result.get("passenger_profit", 0)),
        "passenger_plan": result.get("passenger_plan", {}),
        "price_time": result.get("price_time", ""),
        "price_source": result.get("price_source", ""),
        "price_revision": result.get("price_revision", result.get("price_time", "")),
        "stations_available": list(result.get("cycle", ())),
        "optimizer_config": result.get("optimizer_config", {}),
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }
    _write_state(state)
    return state


def _write_state(state: dict[str, Any]) -> None:
    with _STATE_LOCK:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        temp_path = STATE_PATH.with_name(
            f"{STATE_PATH.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
        )
        with temp_path.open("w", encoding="utf-8") as stream:
            json.dump(state, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, STATE_PATH)


def record_completed_run(
    books: dict[str, int],
    *,
    ledger_path: Path = LEDGER_PATH,
    cycle_id: str | None = None,
    confirmed_books: int = 0,
    server_week_id: str | None = None,
) -> dict[str, Any] | None:
    state = load_weekly_plan()
    if not state:
        return None
    route = [str(city) for city in state.get("cycle", [])]
    now = SERVER_CLOCK.server_now()
    next_sequence = int(state.get("completed_runs", 0)) + 1
    cycle_id = cycle_id or f"{SERVER_CLOCK.server_week_id(now)}:{'|'.join(route)}:{next_sequence}"
    frozen_week_id = server_week_id or SERVER_CLOCK.server_week_id(now)
    event_id = stable_trade_event_id(
        frozen_week_id,
        "|".join(route),
        cycle_id,
        "|".join(route),
        TradeEventType.CYCLE_COMPLETED,
        0,
    )
    committed_event_ids = list(state.get("committed_event_ids", []))
    already_recorded_in_plan = event_id in committed_event_ids
    cycle = load_trade_cycle_state(ledger_path, cycle_id)
    if cycle.ready_to_finalize:
        finalize_trade_cycle(ledger_path, cycle_id, observed_at=now)
        cycle = load_trade_cycle_state(ledger_path, cycle_id)
    if cycle.phase != "CYCLE_COMPLETED":
        # Never advance the plan from a caller's boolean alone. Both leg facts
        # must already exist in the ledger before a cycle can affect progress.
        return state
    facts = load_trade_week_state(ledger_path, now=now)
    fact_completed = (
        facts.full_week_total
        if facts.baseline_known
        else facts.confirmed_delta_since_baseline
    )
    state["completed_runs"] = min(
        max(int(state.get("completed_runs", 0)), int(fact_completed or 0)),
        int(state.get("total_runs", 0)),
    )
    if not already_recorded_in_plan:
        state["completed_books"] = int(state.get("completed_books", 0)) + max(
            0, int(confirmed_books)
        )
    if event_id not in committed_event_ids:
        committed_event_ids.append(event_id)
    state["committed_event_ids"] = committed_event_ids[-500:]
    state["updated_at"] = datetime.now().isoformat(timespec="seconds")
    _write_state(state)
    return state


def _effective_completed_runs(
    state: dict[str, Any],
    *,
    ledger_path: Path = LEDGER_PATH,
    now: datetime | None = None,
) -> int:
    facts = load_trade_week_state(ledger_path, now=now)
    fact_completed = (
        facts.full_week_total
        if facts.baseline_known
        else facts.confirmed_delta_since_baseline
    )
    return min(
        max(int(state.get("completed_runs", 0)), int(fact_completed or 0)),
        int(state.get("total_runs", 0)),
    )


def remaining_batches(
    state: dict[str, Any] | None = None,
    *,
    ledger_path: Path = LEDGER_PATH,
    now: datetime | None = None,
) -> list[dict]:
    state = state or load_weekly_plan()
    if not state:
        return []
    completed = _effective_completed_runs(state, ledger_path=ledger_path, now=now)
    return _compress_runs(state.get("runs", [])[completed:])


def progress_summary(
    state: dict[str, Any] | None = None,
    *,
    ledger_path: Path = LEDGER_PATH,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    state = state or load_weekly_plan()
    if not state:
        return None
    completed = _effective_completed_runs(state, ledger_path=ledger_path, now=now)
    total = int(state.get("total_runs", 0))
    books_used = int(state.get("completed_books", 0))
    books_total = int(state.get("books_total", 0))
    remaining = max(0, total - completed)
    batches = remaining_batches(state, ledger_path=ledger_path, now=now)
    current = batches[0] if batches else None
    facts = load_trade_week_state(ledger_path, now=now)
    expected_profit = int(state.get("expected_profit", 0))
    expected_fatigue = float(state.get("cycle_fatigue", 0)) * total
    profit_per_fatigue = (
        round(expected_profit / expected_fatigue, 2) if expected_fatigue else 0.0
    )
    from core.services.trade_planning import plan_price_is_fresh, recommend_today_runs

    available_weekly_fatigue = max(
        0,
        int(float(state.get("optimizer_config", {}).get("weekly_fatigue", 0)))
        - int(facts.fatigue_used_for_trade),
    )
    current_books_per_cycle = (
        sum(int(value) for value in current.get("books", {}).values())
        if current
        else 0
    )
    price_fresh = plan_price_is_fresh(state, now=now)
    suggested_today = recommend_today_runs(
        remaining_runs=remaining,
        cycle_fatigue=float(state.get("cycle_fatigue", 0)),
        available_fatigue=available_weekly_fatigue,
        recoverable_fatigue=int(
            (state.get("recovery_resources") or {}).get("confirmed_available", 0)
        ),
        purchase_books=max(0, books_total - books_used),
        books_per_cycle=current_books_per_cycle,
        partial_cycle=facts.current_partial_cycle,
        price_fresh=price_fresh,
    )
    return {
        **state,
        "remaining_runs": remaining,
        "remaining_books": max(0, books_total - books_used),
        "remaining_fatigue": round(remaining * float(state.get("cycle_fatigue", 0)), 2),
        "current_batch": current,
        "remaining_batches": batches,
        "finished": remaining == 0,
        "server_week_id": facts.server_week_id,
        "progress_source": facts.source.value,
        "progress_confidence": facts.confidence,
        "baseline_known": facts.baseline_known,
        "baseline_round_trips": facts.baseline_round_trips,
        "tracking_started_at": (
            facts.tracking_started_at.isoformat(timespec="seconds")
            if facts.tracking_started_at
            else ""
        ),
        "confirmed_delta_since_baseline": facts.confirmed_delta_since_baseline,
        "full_week_total": facts.full_week_total,
        "confirmed_round_trips": facts.confirmed_round_trips,
        "confirmed_legs": facts.confirmed_legs,
        "current_partial_cycle": facts.current_partial_cycle,
        "confirmed_books_used": facts.purchase_books_used,
        "confirmed_trade_profit": facts.confirmed_profit,
        "last_reconciled_at": (
            facts.last_reconciled_at.isoformat(timespec="seconds")
            if facts.last_reconciled_at
            else ""
        ),
        "last_confirmed_transaction_at": (
            facts.last_confirmed_transaction_at.isoformat(timespec="seconds")
            if facts.last_confirmed_transaction_at
            else ""
        ),
        "planned_round_trips_remaining": remaining,
        "feasible_round_trips_by_fatigue": (
            int(float(state.get("optimizer_config", {}).get("weekly_fatigue", 0)) // float(state.get("cycle_fatigue", 1)))
            if float(state.get("cycle_fatigue", 0)) > 0
            else 0
        ),
        "expected_total_net_profit": expected_profit,
        "expected_total_fatigue": round(expected_fatigue, 2),
        "expected_profit_per_fatigue": profit_per_fatigue,
        "price_snapshot_fresh": price_fresh,
        "today_suggested_runs": suggested_today,
    }
