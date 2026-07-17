"""Safe, evidence-backed capabilities that can advance daily objectives."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Iterable

from core.services.daily_rewards import DailyProgressSnapshot, RewardStrategy


@dataclass(frozen=True)
class DailyCapability:
    task_key: str
    enabled: bool
    automation_available: bool
    activity_contribution: int = 0
    handbook_contribution: int = 0
    premium_currency_risk: bool = False
    prerequisites: tuple[str, ...] = ()
    run_factory: Callable[[], object] = lambda: None
    activity_contribution_min: int | None = None
    activity_contribution_max: int | None = None
    completion_known: bool = True
    completed: bool = False
    fatigue_cost: int = 0
    time_cost_seconds: int = 0
    purchase_book_cost: int = 0
    repeatable: bool = False
    next_safe_run_at: datetime | None = None

    @property
    def known_activity_min(self) -> int:
        value = self.activity_contribution_min
        return max(0, int(self.activity_contribution if value is None else value))

    @property
    def known_activity_max(self) -> int:
        value = self.activity_contribution_max
        return max(self.known_activity_min, int(self.activity_contribution if value is None else value))


# Production metadata lives with the capability model rather than being
# invented by the dashboard. Unknown or risky tasks intentionally contribute 0.
PRODUCTION_CAPABILITY_METADATA: dict[str, dict[str, object]] = {
    "resident_activity": {
        "activity_contribution_min": 300,
        "activity_contribution_max": 300,
        "handbook_contribution": 1,
        "completion_known": True,
        "fatigue_cost": 0,
        "time_cost_seconds": 180,
        "purchase_book_cost": 0,
        "premium_currency_risk": False,
        "prerequisites": (),
        "repeatable": False,
    },
    "run_business": {
        "activity_contribution_min": 100,
        "activity_contribution_max": 100,
        "handbook_contribution": 1,
        "completion_known": True,
        "fatigue_cost": 160,
        "time_cost_seconds": 900,
        "purchase_book_cost": 0,
        "premium_currency_risk": False,
        "prerequisites": ("fresh_trade_plan",),
        "repeatable": True,
    },
    "passenger_build": {
        "activity_contribution_min": 100,
        "activity_contribution_max": 100,
        "handbook_contribution": 1,
        "completion_known": True,
        "fatigue_cost": 0,
        "time_cost_seconds": 120,
        "purchase_book_cost": 0,
        "premium_currency_risk": False,
        "prerequisites": ("safe_build_available",),
        "repeatable": False,
    },
}


def select_daily_capabilities(
    capabilities: Iterable[DailyCapability],
    snapshot: DailyProgressSnapshot | None,
    strategy: RewardStrategy,
    *,
    satisfied_prerequisites: frozenset[str] = frozenset(),
) -> list[DailyCapability]:
    """Select one minimum-cost safe action, then require a fresh observation."""

    if strategy is RewardStrategy.CLAIM_ONLY:
        return []
    activity_gap = None
    handbook_gap = None
    if snapshot is not None:
        if snapshot.daily_activity_current is not None and snapshot.daily_activity_max is not None:
            activity_gap = max(0, snapshot.daily_activity_max - snapshot.daily_activity_current)
        if snapshot.handbook_daily_tasks_completed is not None and snapshot.handbook_daily_tasks_total is not None:
            handbook_gap = max(
                0,
                snapshot.handbook_daily_tasks_total - snapshot.handbook_daily_tasks_completed,
            )
    if activity_gap == 0 and handbook_gap == 0:
        return []

    eligible = []
    for capability in capabilities:
        if not capability.enabled or not capability.automation_available:
            continue
        if capability.premium_currency_risk or not capability.completion_known:
            continue
        if capability.completed and not capability.repeatable:
            continue
        if any(item not in satisfied_prerequisites for item in capability.prerequisites):
            continue
        activity = capability.known_activity_min
        handbook = max(0, int(capability.handbook_contribution))
        if not ((activity_gap is None or activity_gap > 0) and activity > 0) and not (
            (handbook_gap is None or handbook_gap > 0) and handbook > 0
        ):
            continue
        activity_overshoot = max(0, activity - (activity_gap or activity))
        handbook_overshoot = max(0, handbook - (handbook_gap or handbook))
        eligible.append(
            (
                activity_overshoot + handbook_overshoot * 100,
                max(0, capability.fatigue_cost),
                max(0, capability.purchase_book_cost),
                max(0, capability.time_cost_seconds),
                capability.task_key,
                capability,
            )
        )
    if not eligible:
        return []
    return [min(eligible)[-1]]
