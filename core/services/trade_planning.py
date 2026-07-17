"""Small deterministic trade-plan optimizer for finite route candidates."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta


@dataclass(frozen=True)
class TradeCandidate:
    route_id: str
    net_profit: int
    fatigue: int
    books: int = 0


@dataclass(frozen=True)
class TradePlan:
    route_counts: dict[str, int]
    expected_total_net_profit: int
    expected_total_fatigue: int
    expected_profit_per_fatigue: float
    purchase_books_used: int


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
