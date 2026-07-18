"""Safe, evidence-backed capabilities that can advance daily objectives."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import Callable, Iterable

from core.services.daily_rewards import DailyProgressSnapshot, RewardStrategy
from core.services.server_calendar import SERVER_CLOCK


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


class RewardSchedulingStatus(str, Enum):
    OBSERVE_FIRST = "OBSERVE_FIRST"
    RUN_ONE_DEPENDENCY = "RUN_ONE_DEPENDENCY"
    CLAIM = "CLAIM"
    COMPLETE = "COMPLETE"
    UNKNOWN_RETRY = "UNKNOWN_RETRY"


@dataclass(frozen=True)
class RewardDependencyPlan:
    status: RewardSchedulingStatus
    capabilities: tuple[DailyCapability, ...] = ()
    reason: str = ""


@dataclass(frozen=True)
class PrerequisiteEvidence:
    name: str
    status: str
    source: str
    observed_at: datetime | None
    block_reason: str = ""


@dataclass(frozen=True)
class PrerequisiteResolution:
    satisfied: frozenset[str]
    evidence: dict[str, PrerequisiteEvidence]


def _parse_observed_at(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return datetime.fromisoformat(value.strip())
        except ValueError:
            return None
    return None


def resolve_daily_capability_prerequisites(
    *,
    trade_state: dict | None = None,
    passenger_state: dict | None = None,
    now: datetime | None = None,
    fatigue_budget: int | None = None,
    purchase_books: int | None = None,
) -> PrerequisiteResolution:
    """Resolve production prerequisites from read-only, fail-closed evidence."""

    current = now or SERVER_CLOCK.server_now()
    if trade_state is None:
        from core.services.weekly_plan_state import load_weekly_plan, progress_summary

        trade_state = load_weekly_plan() or {}
        summary = progress_summary(trade_state, now=current) if trade_state else None
        if summary:
            fatigue_budget = int(summary.get("remaining_fatigue", 0))
            purchase_books = int(summary.get("remaining_books", 0))
    if passenger_state is None:
        from core.services.passenger_build_planner import load_build_monitor_plan

        passenger_state = load_build_monitor_plan() or {}

    evidence: dict[str, PrerequisiteEvidence] = {}
    trade_observed = _parse_observed_at((trade_state or {}).get("price_time"))
    if not trade_state:
        evidence["fresh_trade_plan"] = PrerequisiteEvidence(
            "fresh_trade_plan", "UNKNOWN", "weekly_plan", None, "trade_plan_missing"
        )
    else:
        try:
            from core.services.station_availability import unavailable_stations
            from core.services.trade_planning import validate_executable_trade_budget

            cycle = [str(item) for item in trade_state.get("cycle", ())]
            declared = trade_state.get("stations_available")
            if declared is not None:
                available = {str(item) for item in declared}
                closed = [item for item in cycle if item not in available]
            else:
                closed = unavailable_stations(cycle, at=current)
            if closed:
                raise ValueError(f"route stations unavailable: {closed}")
            budget_fatigue = (
                int(fatigue_budget)
                if fatigue_budget is not None
                else int(float(trade_state.get("cycle_fatigue", 0)))
            )
            budget_books = (
                int(purchase_books)
                if purchase_books is not None
                else max(
                    0,
                    int(trade_state.get("books_total", 0))
                    - int(trade_state.get("completed_books", 0)),
                )
            )
            validate_executable_trade_budget(
                trade_state,
                now=current,
                fatigue_budget=budget_fatigue,
                purchase_books=budget_books,
            )
            evidence["fresh_trade_plan"] = PrerequisiteEvidence(
                "fresh_trade_plan",
                "SATISFIED",
                "weekly_plan+station_registry",
                trade_observed,
            )
        except (TypeError, ValueError, RuntimeError) as error:
            evidence["fresh_trade_plan"] = PrerequisiteEvidence(
                "fresh_trade_plan",
                "BLOCKED",
                "weekly_plan+station_registry",
                trade_observed,
                str(error),
            )

    passenger_observed = _parse_observed_at((passenger_state or {}).get("observed_at"))
    if not passenger_state:
        evidence["safe_build_available"] = PrerequisiteEvidence(
            "safe_build_available",
            "UNKNOWN",
            "passenger_build_plan",
            None,
            "passenger_build_state_missing",
        )
    else:
        status = str(passenger_state.get("status", "UNKNOWN")).lower()
        completed = int(passenger_state.get("completed_carriages", 0))
        target = int(passenger_state.get("target_carriages", 0))
        reasons = []
        if status in {"completed", "complete"} or (target > 0 and completed >= target):
            reasons.append("nonrepeatable_build_already_complete")
        if passenger_state.get("premium_currency_required") is not False:
            reasons.append("premium_currency_safety_unknown")
        if passenger_state.get("automation_safe") is not True:
            reasons.append("automation_safety_unknown")
        evidence["safe_build_available"] = PrerequisiteEvidence(
            "safe_build_available",
            "BLOCKED" if reasons else "SATISFIED",
            "passenger_build_plan",
            passenger_observed,
            ";".join(reasons),
        )

    satisfied = frozenset(
        name for name, item in evidence.items() if item.status == "SATISFIED"
    )
    return PrerequisiteResolution(satisfied, evidence)


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
    if snapshot is None or snapshot.daily_activity_confidence.upper() == "UNKNOWN":
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
        activity_overshoot = (
            0
            if activity_gap is None
            else activity
            if activity_gap == 0
            else max(0, activity - activity_gap)
        )
        handbook_overshoot = (
            0
            if handbook_gap is None
            else handbook
            if handbook_gap == 0
            else max(0, handbook - handbook_gap)
        )
        eligible.append(
            (
                activity_overshoot + handbook_overshoot * 100,
                max(0, capability.fatigue_cost),
                max(0, capability.purchase_book_cost),
                max(0, capability.time_cost_seconds),
                1 if capability.repeatable else 0,
                capability.task_key,
                capability,
            )
        )
    if not eligible:
        return []
    return [min(eligible)[-1]]


def plan_daily_reward_dependencies(
    capabilities: Iterable[DailyCapability],
    snapshot: DailyProgressSnapshot | None,
    strategy: RewardStrategy,
    *,
    now: datetime | None = None,
    satisfied_prerequisites: frozenset[str] = frozenset(),
    post_action_observation_required: bool = False,
) -> RewardDependencyPlan:
    """Choose one scheduler action while enforcing observation-first ordering."""

    current = now or SERVER_CLOCK.server_now()
    if snapshot is None:
        return RewardDependencyPlan(
            RewardSchedulingStatus.OBSERVE_FIRST, reason="snapshot_missing"
        )
    if snapshot.server_day_id != SERVER_CLOCK.server_day_id(current):
        return RewardDependencyPlan(
            RewardSchedulingStatus.OBSERVE_FIRST, reason="snapshot_server_day_mismatch"
        )
    if (
        snapshot.observed_at.tzinfo is None
        or snapshot.observed_at.utcoffset() is None
        or not timedelta(0) <= current - snapshot.observed_at <= timedelta(minutes=15)
    ):
        return RewardDependencyPlan(
            RewardSchedulingStatus.OBSERVE_FIRST, reason="snapshot_expired"
        )
    if post_action_observation_required:
        return RewardDependencyPlan(
            RewardSchedulingStatus.UNKNOWN_RETRY,
            reason="fresh_post_action_observation_required",
        )
    if snapshot.daily_activity_confidence.upper() == "UNKNOWN":
        return RewardDependencyPlan(
            RewardSchedulingStatus.UNKNOWN_RETRY, reason="snapshot_unknown"
        )
    if (
        snapshot.daily_activity_unclaimed_tiers
        or snapshot.handbook_rewards_unclaimed
    ):
        return RewardDependencyPlan(RewardSchedulingStatus.CLAIM)
    if (
        snapshot.daily_activity_current is not None
        and snapshot.daily_activity_max is not None
        and snapshot.daily_activity_current >= snapshot.daily_activity_max
        and snapshot.handbook_daily_tasks_completed is not None
        and snapshot.handbook_daily_tasks_total is not None
        and snapshot.handbook_daily_tasks_completed
        >= snapshot.handbook_daily_tasks_total
    ):
        return RewardDependencyPlan(RewardSchedulingStatus.COMPLETE)
    selected = select_daily_capabilities(
        capabilities,
        snapshot,
        strategy,
        satisfied_prerequisites=satisfied_prerequisites,
    )
    if selected:
        return RewardDependencyPlan(
            RewardSchedulingStatus.RUN_ONE_DEPENDENCY, tuple(selected)
        )
    return RewardDependencyPlan(
        RewardSchedulingStatus.UNKNOWN_RETRY, reason="no_safe_dependency"
    )
