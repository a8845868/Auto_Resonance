from __future__ import annotations

import json
import os
import shutil
import threading
import time
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

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


class WeeklyStateCorrupt(RuntimeError):
    """The weekly state cannot be trusted and must not be partially replaced."""


def current_week_start(today: date | None = None) -> str:
    today = today or SERVER_CLOCK.server_week_date()
    return (today - timedelta(days=today.weekday())).isoformat()


def _flatten_batches(batches: list[dict]) -> list[dict[str, int]]:
    if not isinstance(batches, list) or not batches:
        raise ValueError("weekly_plan_execution_batches_required")
    runs: list[dict[str, int]] = []
    for batch in batches:
        if not isinstance(batch, dict):
            raise ValueError("weekly_plan_batch_invalid")
        try:
            run_count = int(batch["runs"])
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("weekly_plan_batch_runs_invalid") from error
        if run_count <= 0:
            raise ValueError("weekly_plan_batch_runs_must_be_positive")
        raw_books = batch.get("books")
        if not isinstance(raw_books, dict):
            raise ValueError("weekly_plan_batch_books_required")
        books = {str(city): int(count) for city, count in raw_books.items()}
        runs.extend(dict(books) for _ in range(run_count))
    return runs


def _compress_runs(runs: list[dict[str, int]]) -> list[dict]:
    batches: list[dict] = []
    for books in runs:
        if batches and batches[-1]["books"] == books:
            batches[-1]["runs"] += 1
        else:
            batches.append({"runs": 1, "books": dict(books)})
    return batches


def load_weekly_plan(
    include_expired: bool = False,
    *,
    path: Path | None = None,
) -> dict[str, Any] | None:
    target = path or STATE_PATH
    if not target.exists():
        return None
    with _STATE_LOCK:
        try:
            state = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError) as error:
            _preserve_corrupt_weekly_state(target)
            raise WeeklyStateCorrupt(
                f"weekly state unreadable: {type(error).__name__}"
            ) from error
        if not isinstance(state, dict):
            _preserve_corrupt_weekly_state(target)
            raise WeeklyStateCorrupt("weekly state has invalid schema")
    if not include_expired and state.get("week_start") != current_week_start():
        return None
    return state


def roll_weekly_plan_forward(*, path: Path | None = None) -> dict[str, Any] | None:
    """Start a new week from the most recent configured plan.

    A Monday reset must clear progress, not make an enabled trading task look
    successfully finished.  The saved cycle and execution batches remain the
    user's active plan until they explicitly apply a replacement.
    """
    target = path or STATE_PATH
    if not target.exists():
        return None
    def mutate(state: dict[str, Any]) -> None:
        week = current_week_start()
        if state.get("week_start") == week:
            return
        if not state or str(state.get("week_start", "")) > week:
            return
        state["week_start"] = week
        state["completed_runs"] = 0
        state["completed_books"] = 0
        state["committed_event_ids"] = []
        state["needs_reoptimization"] = True
        state["updated_at"] = datetime.now().isoformat(timespec="seconds")
    return update_weekly_state(mutate, path=target)


def build_weekly_plan_state(result: dict) -> dict[str, Any]:
    """Build the executable plan schema without writing it to disk."""

    cycle = result.get("cycle")
    if not isinstance(cycle, (list, tuple)) or len(cycle) != 2:
        raise ValueError("weekly_plan_cycle_invalid")
    runs = _flatten_batches(result["execution_batches"])
    return {
        "version": 3,
        "schema_version": 3,
        "week_start": current_week_start(),
        "cycle": list(cycle),
        "total_runs": len(runs),
        "runs": runs,
        "completed_runs": 0,
        "completed_books": 0,
        "books_total": int(result.get("books_used", 0)),
        "cycle_fatigue": float(result.get("cycle_fatigue", 0)),
        "leg_fatigue_schedule": [
            float(item.get("fatigue", 0))
            for item in result.get("legs", ())
            if isinstance(item, dict)
        ],
        "run_expected_profits": [
            int(value) for value in result.get("run_expected_profits", ())
        ],
        "expected_profit": int(result.get("combined_profit", result.get("profit", 0))),
        "cargo_profit": int(result.get("cargo_profit", result.get("profit", 0))),
        "passenger_profit": int(result.get("passenger_profit", 0)),
        "passenger_plan": result.get("passenger_plan", {}),
        "price_time": result.get("price_time", ""),
        "price_source": result.get("price_source", ""),
        "price_revision": result.get("price_revision", result.get("price_time", "")),
        "current_price_evidence": result.get("current_price_evidence"),
        "optimizer_config": result.get("optimizer_config", {}),
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }


def save_weekly_plan(result: dict, *, path: Path | None = None) -> dict[str, Any]:
    planned = build_weekly_plan_state(result)
    runs = planned["runs"]
    def mutate(state: dict[str, Any]) -> None:
        same_plan = state.get("cycle") == result["cycle"] and state.get("runs") == runs
        completed_runs = min(int(state.get("completed_runs", 0)), len(runs)) if same_plan else 0
        completed_books = int(state.get("completed_books", 0)) if same_plan else 0
        preserved = {
            key: state[key]
            for key in ("current_resources", "recovery_resources", "current_city_evidence")
            if key in state
        }
        state.update(planned)
        state.update(preserved)
        state["completed_runs"] = completed_runs
        state["completed_books"] = completed_books
    return update_weekly_state(mutate, path=path)


def _write_state_unlocked(state: dict[str, Any], target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temp_path = target.with_name(
        f"{target.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    )
    with temp_path.open("w", encoding="utf-8") as stream:
        json.dump(state, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    for attempt in range(5):
        try:
            os.replace(temp_path, target)
            break
        except PermissionError:
            if attempt == 4:
                temp_path.unlink(missing_ok=True)
                raise
            time.sleep(0.02 * (attempt + 1))


def _write_state(state: dict[str, Any], *, path: Path | None = None) -> None:
    target = path or STATE_PATH
    with _STATE_LOCK:
        _write_state_unlocked(state, target)


def _preserve_corrupt_weekly_state(target: Path) -> Path | None:
    if not target.is_file():
        return None
    stamp = SERVER_CLOCK.server_now().strftime("%Y%m%dT%H%M%S%f")
    backup = target.with_name(
        f"{target.name}.corrupt.{stamp}.{uuid.uuid4().hex[:8]}"
    )
    try:
        shutil.copy2(target, backup)
    except OSError:
        return None
    return backup


def update_weekly_state(
    mutator: Callable[[dict[str, Any]], object],
    *,
    path: Path | None = None,
) -> dict[str, Any]:
    """Perform read, mutation and atomic replace under one lock scope."""

    target = path or STATE_PATH
    with _STATE_LOCK:
        if target.exists():
            try:
                state = json.loads(target.read_text(encoding="utf-8"))
            except (OSError, TypeError, ValueError) as error:
                _preserve_corrupt_weekly_state(target)
                raise WeeklyStateCorrupt(
                    f"weekly state unreadable: {type(error).__name__}"
                ) from error
            if not isinstance(state, dict):
                _preserve_corrupt_weekly_state(target)
                raise WeeklyStateCorrupt("weekly state has invalid schema")
        else:
            state = {}
        replacement = mutator(state)
        if isinstance(replacement, dict) and replacement is not state:
            state = replacement
        _write_state_unlocked(state, target)
        return json.loads(json.dumps(state))


def save_current_resource_evidence(evidence, *, path: Path | None = None) -> dict[str, Any]:
    """Persist fresh observed inventory separately from plan requirements."""

    from core.services.daily_capabilities import CurrentResourceEvidence

    parsed = CurrentResourceEvidence.from_value(evidence)
    if parsed is None:
        raise ValueError("invalid current resource evidence")
    def mutate(state: dict[str, Any]) -> None:
        state["current_resources"] = parsed.to_dict()
        state["recovery_resources"] = {
            "recoverable_fatigue_today": parsed.recoverable_fatigue_today,
            "source": parsed.source,
            "observed_at": parsed.observed_at.isoformat(),
            "valid_until": parsed.valid_until.isoformat(),
            "server_day_id": parsed.server_day_id,
            "revision": parsed.revision,
        }
        state["updated_at"] = SERVER_CLOCK.server_now().isoformat(timespec="seconds")

    return update_weekly_state(mutate, path=path)


def save_current_city_evidence(
    city: str,
    *,
    source: str,
    observed_at: datetime,
    valid_until: datetime,
    revision: str,
    path: Path | None = None,
) -> dict[str, Any]:
    if not city or source not in {"game_observed", "user_calibrated"}:
        raise ValueError("current city requires observed or calibrated evidence")
    def mutate(state: dict[str, Any]) -> None:
        state["current_city_evidence"] = {
            "city": city,
            "source": source,
            "observed_at": observed_at.isoformat(),
            "valid_until": valid_until.isoformat(),
            "server_day_id": SERVER_CLOCK.server_day_id(observed_at),
            "revision": revision,
        }

    return update_weekly_state(mutate, path=path)


def record_completed_run(
    books: dict[str, int],
    *,
    ledger_path: Path = LEDGER_PATH,
    cycle_id: str | None = None,
    confirmed_books: int = 0,
    server_week_id: str | None = None,
    path: Path | None = None,
) -> dict[str, Any] | None:
    state = load_weekly_plan(path=path)
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
    expected_route = route
    def mutate(current: dict[str, Any]) -> None:
        if [str(city) for city in current.get("cycle", [])] != expected_route:
            raise RuntimeError("weekly plan changed while committing completed run")
        committed_event_ids = list(current.get("committed_event_ids", []))
        already_recorded = event_id in committed_event_ids
        current["completed_runs"] = min(
            max(int(current.get("completed_runs", 0)), int(fact_completed or 0)),
            int(current.get("total_runs", 0)),
        )
        if not already_recorded:
            current["completed_books"] = int(current.get("completed_books", 0)) + max(0, int(confirmed_books))
            committed_event_ids.append(event_id)
        current["committed_event_ids"] = committed_event_ids[-500:]
        current["updated_at"] = datetime.now().isoformat(timespec="seconds")
    return update_weekly_state(mutate, path=path)


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
    from core.services.trade_planning import (
        current_price_revision,
        plan_price_is_fresh,
        recommend_today_runs,
    )

    leg_costs = [max(0, float(value)) for value in state.get("leg_fatigue_schedule", ())]
    partial = facts.current_partial_cycle or {}
    confirmed_legs = min(
        len(leg_costs), max(0, int(partial.get("confirmed_legs", 0)))
    )
    if leg_costs and remaining:
        remaining_required_fatigue = sum(leg_costs[confirmed_legs:]) + max(
            0, remaining - 1
        ) * sum(leg_costs)
    else:
        remaining_required_fatigue = remaining * float(state.get("cycle_fatigue", 0))
    run_profits = [int(value) for value in state.get("run_expected_profits", ())]
    remaining_expected_profit = (
        sum(run_profits[completed:])
        if len(run_profits) == total
        else round(int(state.get("expected_profit", 0)) * remaining / total)
        if total
        else 0
    )
    from core.services.daily_capabilities import CurrentResourceEvidence

    current = now or SERVER_CLOCK.server_now()
    resource_observation = CurrentResourceEvidence.from_value(state.get("current_resources"))
    resource_error = (
        resource_observation.freshness_error(current)
        if resource_observation is not None
        else "current_resources_missing"
    )
    confirmed_available_fatigue = (
        resource_observation.available_fatigue if not resource_error else None
    )
    recoverable_fatigue_today = (
        resource_observation.recoverable_fatigue_today if not resource_error else None
    )
    purchase_books_available = (
        resource_observation.purchase_books_available if not resource_error else None
    )
    location = state.get("current_city_evidence") or {}
    location_observed = None
    location_valid_until = None
    try:
        location_observed = datetime.fromisoformat(str(location.get("observed_at", "")))
        location_valid_until = datetime.fromisoformat(str(location.get("valid_until", "")))
    except ValueError:
        pass
    location_fresh = bool(
        location.get("city")
        and location.get("source") in {"game_observed", "user_calibrated"}
        and location_observed is not None
        and location_valid_until is not None
        and location_observed.tzinfo is not None
        and location_valid_until.tzinfo is not None
        and location_observed <= current <= location_valid_until
        and location.get("server_day_id") == SERVER_CLOCK.server_day_id(current)
        and location.get("revision")
    )
    ledger_city = (
        str(partial.get("last_destination", ""))
        if partial and int(partial.get("confirmed_legs", 0)) > 0
        else ""
    )
    current_city = ledger_city or (str(location.get("city")) if location_fresh else None)
    remaining_profit_per_fatigue = (
        round(remaining_expected_profit / remaining_required_fatigue, 2)
        if remaining_required_fatigue
        else 0.0
    )
    price_fresh = plan_price_is_fresh(state, now=now)
    observed_price_revision = current_price_revision(state)
    suggested_today = recommend_today_runs(
        remaining_runs=remaining,
        cycle_fatigue=float(state.get("cycle_fatigue", 0)),
        available_fatigue=confirmed_available_fatigue,
        recoverable_fatigue=recoverable_fatigue_today,
        purchase_books=purchase_books_available,
        books_per_cycle=0,
        partial_cycle=facts.current_partial_cycle,
        price_fresh=price_fresh,
        cycle=state.get("cycle", ()),
        leg_fatigue_schedule=leg_costs,
        remaining_run_book_schedule=state.get("runs", ()),
        current_run_index=completed,
        current_city=current_city,
        price_revision=str(state.get("price_revision", "")),
        current_price_revision=observed_price_revision,
    )
    missing_evidence = []
    if resource_error:
        missing_evidence.append(resource_error)
    if current_city is None:
        missing_evidence.append("current_city_missing")
    if missing_evidence:
        suggested_today = None
        recommendation_reason = (
            "current_resources_unknown"
            if resource_error
            else "current_city_unknown"
        )
    elif not price_fresh:
        recommendation_reason = "price_snapshot_not_fresh"
    elif suggested_today is None:
        recommendation_reason = "exact_schedule_evidence_unknown"
    else:
        recommendation_reason = "exact_remaining_schedule"
    return {
        **state,
        "remaining_runs": remaining,
        "remaining_books": max(0, books_total - books_used),
        "remaining_fatigue": round(remaining_required_fatigue, 2),
        "remaining_required_fatigue": round(remaining_required_fatigue, 2),
        "confirmed_available_fatigue": confirmed_available_fatigue,
        "recoverable_fatigue_today": recoverable_fatigue_today,
        "purchase_books_available": purchase_books_available,
        "current_city": current_city,
        "recommendation_missing_evidence": missing_evidence,
        "remaining_expected_profit": remaining_expected_profit,
        "remaining_expected_fatigue": round(remaining_required_fatigue, 2),
        "remaining_profit_per_fatigue": remaining_profit_per_fatigue,
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
        "feasible_round_trips_by_fatigue": suggested_today,
        "expected_total_net_profit": expected_profit,
        "expected_total_fatigue": round(expected_fatigue, 2),
        "expected_profit_per_fatigue": profit_per_fatigue,
        "price_snapshot_fresh": price_fresh,
        "current_price_revision": observed_price_revision,
        "today_suggested_runs": suggested_today,
        "today_recommendation_reason": recommendation_reason,
    }
