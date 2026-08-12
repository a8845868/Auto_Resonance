"""Safe, evidence-backed capabilities that can advance daily objectives."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import Callable, Iterable

from core.services.daily_rewards import (
    DailyProgressSnapshot,
    RewardStrategy,
    snapshot_unknown_for_enabled_channels,
)
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


@dataclass(frozen=True)
class RemainingRequirements:
    required_fatigue: int
    required_books: int


@dataclass(frozen=True)
class CurrentResourceEvidence:
    fatigue_used: int
    fatigue_cap: int
    available_fatigue: int
    recoverable_fatigue_today: int
    purchase_books_available: int
    source: str
    observed_at: datetime
    valid_until: datetime
    server_day_id: str
    revision: str

    def to_dict(self) -> dict[str, object]:
        return {
            "fatigue_used": int(self.fatigue_used),
            "fatigue_cap": int(self.fatigue_cap),
            "available_fatigue": int(self.available_fatigue),
            "recoverable_fatigue_today": int(self.recoverable_fatigue_today),
            "purchase_books_available": int(self.purchase_books_available),
            "source": self.source,
            "observed_at": self.observed_at.isoformat(),
            "valid_until": self.valid_until.isoformat(),
            "server_day_id": self.server_day_id,
            "revision": self.revision,
        }

    @classmethod
    def from_value(cls, value: object) -> "CurrentResourceEvidence | None":
        if isinstance(value, cls):
            return value
        if not isinstance(value, dict):
            return None
        try:
            observed_at = _parse_observed_at(value.get("observed_at"))
            valid_until = _parse_observed_at(value.get("valid_until"))
            if observed_at is None or valid_until is None:
                return None
            return cls(
                fatigue_used=int(value.get("fatigue_used", 0)),
                fatigue_cap=int(value.get("fatigue_cap", 0)),
                available_fatigue=int(value["available_fatigue"]),
                recoverable_fatigue_today=int(value.get("recoverable_fatigue_today", 0)),
                purchase_books_available=int(value["purchase_books_available"]),
                source=str(value.get("source", "")),
                observed_at=observed_at,
                valid_until=valid_until,
                server_day_id=str(value.get("server_day_id", "")),
                revision=str(value.get("revision", "")),
            )
        except (KeyError, TypeError, ValueError):
            return None

    def freshness_error(self, now: datetime) -> str:
        if self.source not in {"game_observed", "user_calibrated", "caller_observed"}:
            return "current_resources_source_unknown"
        if not _aware(self.observed_at) or not _aware(self.valid_until):
            return "current_resources_timestamp_unknown"
        if self.server_day_id != SERVER_CLOCK.server_day_id(now):
            return "current_resources_server_day_mismatch"
        if not self.revision:
            return "current_resources_revision_missing"
        if not self.observed_at <= now <= self.valid_until:
            return "current_resources_stale"
        if self.available_fatigue < 0 or self.recoverable_fatigue_today < 0 or self.purchase_books_available < 0:
            return "current_resources_invalid"
        return ""


def _parse_observed_at(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return datetime.fromisoformat(value.strip())
        except ValueError:
            return None
    return None


def _aware(value: datetime | None) -> bool:
    return bool(value is not None and value.tzinfo is not None and value.utcoffset() is not None)


def _station_evidence_is_fresh(
    evidence: object, cycle: list[str], current: datetime
) -> bool:
    if not isinstance(evidence, dict):
        return False
    source = str(evidence.get("source", ""))
    observed = _parse_observed_at(evidence.get("observed_at"))
    valid_until = _parse_observed_at(evidence.get("valid_until"))
    covered = {
        str(item)
        for key in ("available", "closed", "unknown")
        for item in evidence.get(key, ())
    }
    return bool(
        source in {"station_registry", "game_observed"}
        and _aware(observed)
        and _aware(valid_until)
        and observed <= current <= valid_until
        and set(cycle) <= covered
    )


def resolve_daily_capability_prerequisites(
    *,
    trade_state: dict | None = None,
    passenger_state: dict | None = None,
    now: datetime | None = None,
    fatigue_budget: int | None = None,
    purchase_books: int | None = None,
    resource_evidence: CurrentResourceEvidence | dict | None = None,
) -> PrerequisiteResolution:
    """Resolve production prerequisites from read-only, fail-closed evidence."""

    current = now or SERVER_CLOCK.server_now()
    if trade_state is None:
        from core.services.weekly_plan_state import load_weekly_plan

        trade_state = load_weekly_plan() or {}
    resources = CurrentResourceEvidence.from_value(resource_evidence)
    if resources is None:
        resources = CurrentResourceEvidence.from_value((trade_state or {}).get("current_resources"))
    # Compatibility for callers that supply independently observed values.
    # Production dashboard code no longer derives these from remaining plan requirements.
    if resources is None and fatigue_budget is not None and purchase_books is not None:
        resources = CurrentResourceEvidence(
            fatigue_used=0,
            fatigue_cap=max(0, int(fatigue_budget)),
            available_fatigue=max(0, int(fatigue_budget)),
            recoverable_fatigue_today=0,
            purchase_books_available=max(0, int(purchase_books)),
            source="caller_observed",
            observed_at=current,
            valid_until=current + timedelta(minutes=15),
            server_day_id=SERVER_CLOCK.server_day_id(current),
            revision=f"caller:{current.isoformat()}",
        )
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
            from core.services import station_availability
            from core.services.trade_planning import validate_executable_trade_budget

            cycle = [str(item) for item in trade_state.get("cycle", ())]
            availability = trade_state.get("station_availability_evidence")
            if not _station_evidence_is_fresh(availability, cycle, current):
                availability = station_availability.station_availability_evidence(
                    cycle, at=current
                )
            closed = [str(item) for item in availability.get("closed", ())]
            unknown = [str(item) for item in availability.get("unknown", ())]
            if closed:
                raise ValueError(f"route stations unavailable: {closed}")
            if unknown:
                raise ValueError(f"route station availability unknown: {unknown}")
            if resources is None:
                evidence["fresh_trade_plan"] = PrerequisiteEvidence(
                    "fresh_trade_plan", "UNKNOWN", "weekly_plan+current_resources",
                    trade_observed, "current_resources_missing",
                )
                raise LookupError
            freshness_error = resources.freshness_error(current)
            if freshness_error:
                evidence["fresh_trade_plan"] = PrerequisiteEvidence(
                    "fresh_trade_plan", "UNKNOWN",
                    "weekly_plan+" + (resources.source or "current_resources"),
                    resources.observed_at, freshness_error,
                )
                raise LookupError
            budget_fatigue = int(resources.available_fatigue) + int(resources.recoverable_fatigue_today)
            budget_books = int(resources.purchase_books_available)
            validate_executable_trade_budget(
                trade_state,
                now=current,
                fatigue_budget=budget_fatigue,
                purchase_books=budget_books,
            )
            evidence["fresh_trade_plan"] = PrerequisiteEvidence(
                "fresh_trade_plan",
                "SATISFIED",
                "weekly_plan+" + str(availability.get("source", "UNKNOWN"))
                + ("" if resources.source == "caller_observed" else "+" + resources.source),
                resources.observed_at,
            )
        except LookupError:
            pass
        except (TypeError, ValueError, RuntimeError) as error:
            availability_source = (
                str(availability.get("source", "UNKNOWN"))
                if isinstance(locals().get("availability"), dict)
                else "UNKNOWN"
            )
            evidence["fresh_trade_plan"] = PrerequisiteEvidence(
                "fresh_trade_plan",
                "BLOCKED",
                "weekly_plan+" + availability_source,
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
        evidence_status = "BLOCKED"
        server_day_id = str(passenger_state.get("server_day_id", ""))
        if not _aware(passenger_observed):
            reasons.append("passenger_observation_timestamp_unknown")
            evidence_status = "UNKNOWN"
        elif server_day_id != SERVER_CLOCK.server_day_id(current):
            reasons.append("passenger_observation_server_day_mismatch")
            evidence_status = "UNKNOWN"
        elif not timedelta(0) <= current - passenger_observed <= timedelta(minutes=15):
            reasons.append("passenger_observation_stale")
        if status in {"completed", "complete"} or (target > 0 and completed >= target):
            reasons.append("nonrepeatable_build_already_complete")
        if passenger_state.get("premium_currency_required") is not False:
            reasons.append("premium_currency_safety_unknown")
        if passenger_state.get("automation_safe") is not True:
            reasons.append("automation_safety_unknown")
        safety_source = str(passenger_state.get("automation_safety_source", ""))
        safety_reason = str(passenger_state.get("automation_safety_reason", ""))
        if safety_source not in {"game_observed", "verified_config"} or not safety_reason:
            reasons.append("automation_safety_evidence_missing")
        if (
            passenger_state.get("evidence_config_revision")
            != passenger_state.get("config_revision")
            or passenger_state.get("evidence_target_carriages") != target
            or passenger_state.get("evidence_completed_carriages") != completed
            or str(passenger_state.get("evidence_status", "")).lower() != status
        ):
            reasons.append("passenger_safety_evidence_revision_mismatch")
        evidence["safe_build_available"] = PrerequisiteEvidence(
            "safe_build_available",
            evidence_status if reasons else "SATISFIED",
            "passenger_build_plan+" + (safety_source or "UNKNOWN"),
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
    daily_activity_enabled: bool = True,
    travel_manual_enabled: bool = True,
) -> list[DailyCapability]:
    """Select one minimum-cost safe action, then require a fresh observation."""

    if strategy is RewardStrategy.CLAIM_ONLY:
        return []
    if snapshot is None or snapshot_unknown_for_enabled_channels(
        snapshot, daily_activity_enabled, travel_manual_enabled
    ):
        return []
    activity_gap = 0 if not daily_activity_enabled else None
    handbook_gap = 0 if not travel_manual_enabled else None
    if snapshot is not None:
        if daily_activity_enabled and snapshot.daily_activity_current is not None and snapshot.daily_activity_max is not None:
            activity_gap = max(0, snapshot.daily_activity_max - snapshot.daily_activity_current)
        if travel_manual_enabled and snapshot.handbook_daily_tasks_completed is not None and snapshot.handbook_daily_tasks_total is not None:
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
    daily_activity_enabled: bool = True,
    travel_manual_enabled: bool = True,
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
    if snapshot_unknown_for_enabled_channels(
        snapshot, daily_activity_enabled, travel_manual_enabled
    ):
        return RewardDependencyPlan(
            RewardSchedulingStatus.UNKNOWN_RETRY, reason="snapshot_unknown"
        )
    if (
        (daily_activity_enabled and snapshot.daily_activity_unclaimed_tiers)
        or (travel_manual_enabled and snapshot.handbook_rewards_unclaimed)
    ):
        return RewardDependencyPlan(RewardSchedulingStatus.CLAIM)
    if (
        not daily_activity_enabled
        or (
            snapshot.daily_activity_current is not None
            and snapshot.daily_activity_max is not None
            and snapshot.daily_activity_current >= snapshot.daily_activity_max
        )
    ) and (
        not travel_manual_enabled
        or (
            snapshot.handbook_daily_tasks_completed is not None
            and snapshot.handbook_daily_tasks_total is not None
            and snapshot.handbook_daily_tasks_completed
            >= snapshot.handbook_daily_tasks_total
        )
    ):
        return RewardDependencyPlan(RewardSchedulingStatus.COMPLETE)
    selected = select_daily_capabilities(
        capabilities,
        snapshot,
        strategy,
        satisfied_prerequisites=satisfied_prerequisites,
        daily_activity_enabled=daily_activity_enabled,
        travel_manual_enabled=travel_manual_enabled,
    )
    if selected:
        return RewardDependencyPlan(
            RewardSchedulingStatus.RUN_ONE_DEPENDENCY, tuple(selected)
        )
    return RewardDependencyPlan(
        RewardSchedulingStatus.UNKNOWN_RETRY, reason="no_safe_dependency"
    )
