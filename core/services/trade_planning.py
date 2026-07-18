"""Small deterministic trade-plan optimizer for finite route candidates."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from core.services.server_calendar import SERVER_CLOCK


@dataclass(frozen=True)
class TradeCandidate:
    route_id: str
    net_profit: int
    fatigue: int
    books: int = 0
    current_city: str = ""
    partial_cycle: dict[str, Any] | None = None
    cargo: int = 0
    passenger_profit: int = 0
    tax: float = 0.0
    haggle: float = 0.0
    markup: float = 0.0
    return_cost: int = 0
    recovery_resources: dict[str, Any] | None = None


@dataclass(frozen=True)
class TradePlan:
    route_counts: dict[str, int]
    expected_total_net_profit: int
    expected_total_fatigue: int
    expected_profit_per_fatigue: float
    purchase_books_used: int


class StalePriceSnapshot(RuntimeError):
    """Raised when an executable plan is based on an expired price snapshot."""


def choose_trade_plan(
    candidates: tuple[TradeCandidate, ...],
    *,
    fatigue_budget: int,
    purchase_books: int,
) -> TradePlan:
    budget = max(0, int(fatigue_budget))
    book_budget = max(0, int(purchase_books))
    usable = tuple(
        candidate
        for candidate in candidates
        if candidate.fatigue > 0
        and candidate.net_profit > 0
        and candidate.books >= 0
    )
    states: dict[tuple[int, int], tuple[int, dict[str, int]]] = {(0, 0): (0, {})}
    for fatigue in range(budget + 1):
        for books in range(book_budget + 1):
            current = states.get((fatigue, books))
            if current is None:
                continue
            profit, counts = current
            for candidate in usable:
                next_fatigue = fatigue + candidate.fatigue
                next_books = books + candidate.books
                if next_fatigue > budget or next_books > book_budget:
                    continue
                next_profit = profit + candidate.net_profit
                key = (next_fatigue, next_books)
                previous = states.get(key)
                if previous is None or next_profit > previous[0]:
                    updated = dict(counts)
                    updated[candidate.route_id] = updated.get(candidate.route_id, 0) + 1
                    states[key] = (next_profit, updated)

    best_key, (best_profit, best_counts) = max(
        states.items(),
        key=lambda item: (item[1][0], -item[0][0], -item[0][1]),
    )
    fatigue, books = best_key
    ratio = round(best_profit / fatigue, 2) if fatigue else 0.0
    return TradePlan(best_counts, best_profit, fatigue, ratio, books)


def price_snapshot_is_fresh(
    calculated_at: datetime,
    *,
    now: datetime,
    max_age: timedelta = timedelta(minutes=30),
) -> bool:
    if (
        calculated_at.tzinfo is None
        or calculated_at.utcoffset() is None
        or now.tzinfo is None
        or now.utcoffset() is None
    ):
        raise ValueError("price snapshot timestamps must be timezone-aware")
    age = now - calculated_at.astimezone(now.tzinfo)
    return timedelta(0) <= age <= max_age


def _parse_price_time(value: object) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        parsed = datetime.fromisoformat(value.strip())
    else:
        raise StalePriceSnapshot("trade plan has no confirmed price timestamp")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        local_zone = datetime.now().astimezone().tzinfo
        parsed = parsed.replace(tzinfo=local_zone).astimezone(SERVER_CLOCK.timezone)
    return parsed


def validate_executable_trade_budget(
    state: dict[str, Any],
    *,
    now: datetime | None = None,
    fatigue_budget: int,
    purchase_books: int,
) -> TradePlan:
    """Validate one persisted route against fresh prices and finite budgets."""

    current = now or SERVER_CLOCK.server_now()
    calculated_at = _parse_price_time(state.get("price_time"))
    if not price_snapshot_is_fresh(calculated_at, now=current):
        raise StalePriceSnapshot(
            f"price snapshot expired: calculated_at={calculated_at.isoformat()} now={current.isoformat()}"
        )
    price_source = str(state.get("price_source", "")).strip().lower()
    if price_source not in {"live_exchange", "game_observed"}:
        raise StalePriceSnapshot(
            f"price source {price_source!r} is not approved for real execution"
        )
    cycle = [str(city) for city in state.get("cycle", [])]
    if len(cycle) < 2 or len(set(cycle)) < 2:
        raise ValueError("executable trade plan requires two distinct stations")
    total_runs = max(1, int(state.get("total_runs", 1)))
    per_cycle_profit = int(state.get("expected_profit", state.get("profit", 0)))
    if int(state.get("total_runs", 0)) > 0:
        per_cycle_profit //= total_runs
    if per_cycle_profit <= 0:
        raise ValueError("executable trade plan must have positive net profit")
    cycle_fatigue = int(round(float(state.get("cycle_fatigue", 0))))
    if cycle_fatigue <= 0:
        raise ValueError("executable trade plan must have positive fatigue cost")
    runs = state.get("runs")
    if not isinstance(runs, list) or len(runs) != total_runs:
        raise ValueError("executable trade plan requires an exact per-run book schedule")
    normalized_runs: list[dict[str, int]] = []
    for run in runs:
        if not isinstance(run, dict):
            raise ValueError("book schedule contains an invalid run")
        normalized_runs.append({str(city): max(0, int(value)) for city, value in run.items()})
    scheduled_total = sum(sum(run.values()) for run in normalized_runs)
    if scheduled_total != max(0, int(state.get("books_total", scheduled_total))):
        raise ValueError("book schedule total does not match books_total")
    run_index = min(max(0, int(state.get("completed_runs", 0))), total_runs - 1)
    current_schedule = normalized_runs[run_index]
    confirmed_legs = max(
        0, int((state.get("current_partial_cycle") or {}).get("confirmed_legs", 0))
    )
    remaining_origins = cycle[min(confirmed_legs, len(cycle)) :]
    per_cycle_books = sum(current_schedule.get(city, 0) for city in remaining_origins)
    candidate = TradeCandidate(
        route_id="|".join(cycle),
        current_city=str(state.get("current_city", cycle[0] if cycle else "")),
        partial_cycle=state.get("current_partial_cycle"),
        net_profit=per_cycle_profit,
        fatigue=cycle_fatigue,
        books=per_cycle_books,
        cargo=int((state.get("optimizer_config") or {}).get("cargo", 0)),
        passenger_profit=int(state.get("passenger_profit", 0)),
        tax=float(state.get("tax", 0.0)),
        haggle=float(state.get("haggle", 0.0)),
        markup=float(state.get("markup", 0.0)),
        return_cost=int(state.get("return_cost", 0)),
        recovery_resources=dict(state.get("recovery_resources") or {}),
    )
    plan = choose_trade_plan(
        (candidate,),
        fatigue_budget=max(0, int(fatigue_budget)),
        purchase_books=max(0, int(purchase_books)),
    )
    if plan.expected_total_net_profit <= 0:
        raise ValueError("no profitable route fits the current fatigue and book budgets")
    return plan


# Compatibility for external callers. Production code uses the precise name
# above; this alias can be removed after downstream integrations migrate.
build_executable_trade_plan = validate_executable_trade_budget


def plan_price_is_fresh(
    state: dict[str, Any], *, now: datetime | None = None
) -> bool:
    try:
        calculated_at = _parse_price_time(state.get("price_time"))
    except (StalePriceSnapshot, TypeError, ValueError):
        return False
    return price_snapshot_is_fresh(
        calculated_at,
        now=now or SERVER_CLOCK.server_now(),
    )


def recommend_max_feasible_runs_today(
    *,
    remaining_runs: int,
    cycle_fatigue: float,
    available_fatigue: int | None,
    recoverable_fatigue: int | None,
    purchase_books: int | None,
    books_per_cycle: int,
    partial_cycle: dict[str, Any] | None,
    price_fresh: bool,
    cycle: tuple[str, ...] | list[str] | None = None,
    leg_fatigue_schedule: tuple[int | float, ...] | list[int | float] | None = None,
    remaining_run_book_schedule: tuple[dict[str, int], ...] | list[dict[str, int]] | None = None,
    current_run_index: int = 0,
    current_city: str | None = None,
    price_revision: str | None = None,
    current_price_revision: str | None = None,
) -> int | None:
    """Return exact completable runs, or ``None`` when current facts are unknown."""

    if not price_fresh or remaining_runs <= 0 or cycle_fatigue <= 0:
        return 0
    if (
        available_fatigue is None
        or recoverable_fatigue is None
        or purchase_books is None
    ):
        return None
    if (
        price_revision is not None
        and current_price_revision is not None
        and price_revision != current_price_revision
    ):
        return 0
    precise = bool(cycle and leg_fatigue_schedule and remaining_run_book_schedule)
    if precise:
        route = tuple(str(item) for item in cycle or ())
        leg_costs = tuple(max(0, int(round(value))) for value in leg_fatigue_schedule or ())
        schedules = tuple(remaining_run_book_schedule or ())
        if len(route) != len(leg_costs) or len(schedules) < current_run_index + remaining_runs:
            return None
        confirmed_legs = max(0, int((partial_cycle or {}).get("confirmed_legs", 0)))
        confirmed_legs = min(confirmed_legs, len(route))
        expected_city = route[confirmed_legs % len(route)]
        if current_city and current_city != expected_city:
            return None
        fatigue_budget = max(0, int(available_fatigue)) + max(0, int(recoverable_fatigue))
        book_budget = max(0, int(purchase_books))
        completed_today = 0
        for offset in range(max(0, int(remaining_runs))):
            schedule = schedules[current_run_index + offset]
            start_leg = confirmed_legs if offset == 0 else 0
            fatigue_cost = sum(leg_costs[start_leg:])
            book_cost = sum(
                max(0, int(schedule.get(origin, 0)))
                for origin in route[start_leg:]
            )
            if fatigue_cost > fatigue_budget or book_cost > book_budget:
                break
            fatigue_budget -= fatigue_cost
            book_budget -= book_cost
            completed_today += 1
        return completed_today
    fatigue_runs = int(
        max(0, available_fatigue + recoverable_fatigue) // max(1, int(cycle_fatigue))
    )
    book_runs = (
        remaining_runs
        if books_per_cycle <= 0
        else max(0, int(purchase_books)) // int(books_per_cycle)
    )
    return min(max(0, int(remaining_runs)), min(fatigue_runs, book_runs))


recommend_today_runs = recommend_max_feasible_runs_today
