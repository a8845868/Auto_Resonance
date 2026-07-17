from datetime import datetime, timedelta

from core.services.daily_rewards import (
    DailyProgressSnapshot,
    RewardRunStatus,
    RewardStrategy,
    decide_reward_run,
    schedule_debounced_reward_recheck,
)
from core.services.server_calendar import GameServerClock
from core.services.task_schedule_state import task_timing
from core.services.task_schedule_state import record_task_execution


def _now():
    clock = GameServerClock()
    return datetime(2026, 7, 17, 12, 38, 34, tzinfo=clock.timezone)


def _snapshot(**overrides):
    values = {
        "server_day_id": "2026-07-17",
        "daily_activity_current": 300,
        "daily_activity_max": 600,
        "daily_activity_source": "GAME_OBSERVED",
        "daily_activity_confidence": "HIGH",
        "daily_activity_claimable_tiers": 0,
        "daily_activity_unclaimed_tiers": 0,
        "handbook_daily_tasks_total": 5,
        "handbook_daily_tasks_completed": 2,
        "handbook_rewards_claimable": 0,
        "handbook_rewards_unclaimed": 0,
        "observed_at": _now(),
    }
    values.update(overrides)
    return DailyProgressSnapshot(**values)


def test_incomplete_reward_run_schedules_retry_before_reset():
    result = decide_reward_run(_snapshot(), now=_now())

    assert result.status is RewardRunStatus.INCOMPLETE
    assert result.deferred is True
    assert result.next_run_at == _now() + timedelta(minutes=30)
    assert result.next_run_at < GameServerClock().next_daily_reset(_now())


def test_complete_reward_run_schedules_next_daily_reset():
    snapshot = _snapshot(
        daily_activity_current=600,
        handbook_daily_tasks_completed=5,
    )
    result = decide_reward_run(snapshot, now=_now())

    assert result.status is RewardRunStatus.COMPLETE
    assert result.all_tracked_objectives_complete is True
    assert result.next_run_at == GameServerClock().next_daily_reset(_now())


def test_no_claim_button_does_not_mean_daily_complete():
    result = decide_reward_run(_snapshot(daily_activity_claimable_tiers=0), now=_now())
    assert result.status is RewardRunStatus.INCOMPLETE
    assert result.all_tracked_objectives_complete is False


def test_handbook_pending_prevents_full_completion():
    result = decide_reward_run(
        _snapshot(daily_activity_current=600, handbook_daily_tasks_completed=4),
        now=_now(),
    )
    assert result.status is RewardRunStatus.INCOMPLETE


def test_unknown_reward_state_retries_in_same_server_day():
    result = decide_reward_run(
        _snapshot(daily_activity_current=None, daily_activity_confidence="UNKNOWN"),
        now=_now(),
    )
    assert result.status is RewardRunStatus.UNKNOWN
    assert result.next_run_at == _now() + timedelta(minutes=5)


def test_transient_reward_error_uses_bounded_backoff():
    first = decide_reward_run(None, now=_now(), transient_error=True, attempt=1)
    later = decide_reward_run(None, now=_now(), transient_error=True, attempt=99)
    assert first.next_run_at == _now() + timedelta(minutes=2)
    assert later.next_run_at == _now() + timedelta(minutes=30)


def test_daily_strategy_claim_only_remains_available():
    result = decide_reward_run(
        _snapshot(), now=_now(), strategy=RewardStrategy.CLAIM_ONLY
    )
    assert result.strategy is RewardStrategy.CLAIM_ONLY


def test_progress_task_completion_triggers_debounced_reward_recheck(tmp_path):
    path = tmp_path / "schedule.json"
    first = schedule_debounced_reward_recheck(_now(), path=path)
    second = schedule_debounced_reward_recheck(
        _now() + timedelta(seconds=1), path=path
    )

    assert first is True
    assert second is False
    assert task_timing("reward_collection", path)["next_run"].startswith(
        "2026-07-17T12:38:"
    )


def test_near_reset_claims_current_rewards_even_when_tasks_blocked():
    clock = GameServerClock()
    near_reset = datetime(2026, 7, 18, 4, 59, 50, tzinfo=clock.timezone)
    result = decide_reward_run(
        _snapshot(daily_activity_unclaimed_tiers=1),
        now=near_reset,
        blocked_reasons=("dependency_blocked",),
    )
    assert result.status is RewardRunStatus.CLAIMABLE
    assert result.next_action == "claim_current_rewards"
    assert near_reset < result.next_run_at < clock.next_daily_reset(near_reset)


def test_reward_attempt_progress_and_full_completion_are_persisted_separately(tmp_path):
    path = tmp_path / "schedule.json"
    attempt = _now()
    record_task_execution(
        "reward_collection",
        "领取任务奖励",
        True,
        attempt + timedelta(minutes=30),
        {"success": True, "deferred": True, "progress_made": True},
        attempt,
        path,
        deferred=True,
    )
    timing = task_timing("reward_collection", path)
    assert timing["last_attempt"] == attempt.isoformat(timespec="seconds")
    assert timing["progress_at"] == attempt.isoformat(timespec="seconds")
    assert timing["completed_at"] == ""
