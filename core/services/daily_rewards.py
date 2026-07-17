"""Structured daily reward state and state-based scheduling decisions."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any

from core.services.server_calendar import SERVER_CLOCK
from core.services.task_schedule_state import STATE_PATH, set_next_run, task_timing


class RewardRunStatus(str, Enum):
    UNKNOWN = "UNKNOWN"
    INCOMPLETE = "INCOMPLETE"
    ACTIONABLE = "ACTIONABLE"
    RUNNING_DEPENDENCIES = "RUNNING_DEPENDENCIES"
    CLAIMABLE = "CLAIMABLE"
    COMPLETE = "COMPLETE"
    BLOCKED = "BLOCKED"
    TRANSIENT_ERROR = "TRANSIENT_ERROR"
    CANCELLED = "CANCELLED"


class RewardStrategy(str, Enum):
    MAXIMIZE_PROGRESS = "maximize_progress"
    CLAIM_ONLY = "claim_only"


@dataclass(frozen=True)
class DailyProgressSnapshot:
    server_day_id: str
    daily_activity_current: int | None
    daily_activity_max: int | None
    daily_activity_source: str
    daily_activity_confidence: str
    daily_activity_claimable_tiers: int | None
    daily_activity_unclaimed_tiers: int | None
    handbook_daily_tasks_total: int | None
    handbook_daily_tasks_completed: int | None
    handbook_rewards_claimable: int | None
    handbook_rewards_unclaimed: int | None
    observed_at: datetime


@dataclass(frozen=True)
class DailyRewardRunResult:
    status: RewardRunStatus
    strategy: RewardStrategy
    snapshot_after: DailyProgressSnapshot | None
    all_tracked_objectives_complete: bool
    blocked_reasons: tuple[str, ...]
    next_action: str
    next_run_at: datetime
    next_run_reason: str
    deferred: bool = True
    success: bool = True

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["status"] = self.status.value
        payload["strategy"] = self.strategy.value
        payload["next_run_at"] = self.next_run_at.isoformat(timespec="seconds")
        if self.snapshot_after is not None:
            payload["snapshot_after"]["observed_at"] = (
                self.snapshot_after.observed_at.isoformat(timespec="seconds")
            )
        return payload


def _snapshot_unknown(snapshot: DailyProgressSnapshot) -> bool:
    required = (
        snapshot.daily_activity_current,
        snapshot.daily_activity_max,
        snapshot.daily_activity_unclaimed_tiers,
        snapshot.handbook_daily_tasks_total,
        snapshot.handbook_daily_tasks_completed,
        snapshot.handbook_rewards_unclaimed,
    )
    return snapshot.daily_activity_confidence.upper() == "UNKNOWN" or any(
        value is None for value in required
    )


def _snapshot_complete(snapshot: DailyProgressSnapshot) -> bool:
    return bool(
        snapshot.daily_activity_current is not None
        and snapshot.daily_activity_max is not None
        and snapshot.daily_activity_current >= snapshot.daily_activity_max
        and snapshot.daily_activity_unclaimed_tiers == 0
        and snapshot.handbook_daily_tasks_total is not None
        and snapshot.handbook_daily_tasks_completed is not None
        and snapshot.handbook_daily_tasks_completed
        >= snapshot.handbook_daily_tasks_total
        and snapshot.handbook_rewards_unclaimed == 0
    )


def decide_reward_run(
    snapshot: DailyProgressSnapshot | None,
    *,
    now: datetime | None = None,
    strategy: RewardStrategy = RewardStrategy.MAXIMIZE_PROGRESS,
    running_dependencies: bool = False,
    blocked_reasons: tuple[str, ...] = (),
    transient_error: bool = False,
    attempt: int = 1,
) -> DailyRewardRunResult:
    current = now or SERVER_CLOCK.server_now()
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("reward scheduling requires a timezone-aware timestamp")

    if transient_error:
        delay_minutes = min(30, 2 ** max(1, min(int(attempt), 5)))
        next_run = current + timedelta(minutes=delay_minutes)
        reset = SERVER_CLOCK.next_daily_reset(current)
        if reset - current <= timedelta(minutes=10):
            next_run = max(current, min(next_run, reset - timedelta(seconds=5)))
        return DailyRewardRunResult(
            RewardRunStatus.TRANSIENT_ERROR,
            strategy,
            snapshot,
            False,
            blocked_reasons,
            "retry_observation",
            next_run,
            "bounded_transient_backoff",
        )
    if snapshot is None or _snapshot_unknown(snapshot):
        reset = SERVER_CLOCK.next_daily_reset(current)
        near_reset = reset - current <= timedelta(minutes=10)
        next_run = current + timedelta(minutes=5)
        if near_reset:
            next_run = max(
                current,
                min(current + timedelta(seconds=30), reset - timedelta(seconds=5)),
            )
        return DailyRewardRunResult(
            RewardRunStatus.UNKNOWN,
            strategy,
            snapshot,
            False,
            blocked_reasons or ("state_not_confirmed",),
            (
                "final_check_then_claim_before_reset"
                if near_reset
                else "reobserve_daily_progress"
            ),
            next_run,
            (
                "unknown_state_final_pre_reset_check"
                if near_reset
                else "unknown_state_same_server_day_retry"
            ),
        )
    if running_dependencies:
        return DailyRewardRunResult(
            RewardRunStatus.RUNNING_DEPENDENCIES,
            strategy,
            snapshot,
            False,
            blocked_reasons,
            "wait_for_progress_event",
            current + timedelta(minutes=10),
            "progress_dependency_running",
        )
    if _snapshot_complete(snapshot):
        return DailyRewardRunResult(
            RewardRunStatus.COMPLETE,
            strategy,
            snapshot,
            True,
            (),
            "wait_for_next_server_day",
            SERVER_CLOCK.next_daily_reset(current),
            "all_tracked_daily_objectives_complete",
            deferred=False,
        )
    claimable = bool(
        (snapshot.daily_activity_unclaimed_tiers or 0)
        or (snapshot.handbook_rewards_unclaimed or 0)
    )
    status = RewardRunStatus.CLAIMABLE if claimable else RewardRunStatus.INCOMPLETE
    if claimable:
        reset = SERVER_CLOCK.next_daily_reset(current)
        remaining = reset - current
        delay = timedelta(seconds=2) if remaining <= timedelta(minutes=10) else timedelta(minutes=5)
        next_run = current + delay
    else:
        next_run = current + timedelta(minutes=30)
    next_action = (
        "claim_current_rewards"
        if claimable
        else "claim_only_reobserve"
        if strategy is RewardStrategy.CLAIM_ONLY
        else "enqueue_safe_progress_dependencies"
    )
    return DailyRewardRunResult(
        status,
        strategy,
        snapshot,
        False,
        blocked_reasons,
        next_action,
        next_run,
        "claimable_rewards_pending" if claimable else "daily_objectives_incomplete",
    )


def schedule_debounced_reward_recheck(
    now: datetime | None = None,
    *,
    path: Path = STATE_PATH,
    debounce_seconds: int = 10,
) -> bool:
    current = now or SERVER_CLOCK.server_now()
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("reward scheduling requires a timezone-aware timestamp")
    target = current + timedelta(seconds=max(1, int(debounce_seconds)))
    existing_text = task_timing("reward_collection", path).get("next_run", "")
    if existing_text:
        try:
            existing = datetime.fromisoformat(existing_text)
            if existing.tzinfo is None:
                existing = existing.replace(
                    tzinfo=datetime.now().astimezone().tzinfo
                ).astimezone(SERVER_CLOCK.timezone)
            if existing <= target:
                return False
        except (TypeError, ValueError):
            pass
    set_next_run("reward_collection", target, path)
    return True
