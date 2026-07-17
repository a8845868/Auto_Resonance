from datetime import datetime, timedelta
from unittest.mock import Mock, patch

from app.utils.task_queue import QueuedTask
from app.view.dashboard_interface import select_reward_dependency_tasks
from auto.reward_collection import (
    _daily_activity_progress,
    _manual_daily_progress,
    collect_scheduled_rewards,
)
from core.services.daily_capabilities import DailyCapability
from core.services.daily_rewards import (
    DailyProgressSnapshot,
    RewardRunStatus,
    RewardStrategy,
    decide_reward_run,
)
from core.services.server_calendar import GameServerClock


CLOCK = GameServerClock()


def _now(hour=12, minute=0):
    return datetime(2026, 7, 17, hour, minute, tzinfo=CLOCK.timezone)


def _snapshot(**updates):
    values = dict(
        server_day_id="2026-07-17",
        daily_activity_current=300,
        daily_activity_max=600,
        daily_activity_source="OCR",
        daily_activity_confidence="HIGH",
        daily_activity_claimable_tiers=0,
        daily_activity_unclaimed_tiers=0,
        handbook_daily_tasks_total=5,
        handbook_daily_tasks_completed=2,
        handbook_rewards_claimable=0,
        handbook_rewards_unclaimed=0,
        observed_at=_now(),
    )
    values.update(updates)
    return DailyProgressSnapshot(**values)


def _capability(task):
    return DailyCapability(
        task_key=task.key,
        enabled=True,
        automation_available=True,
        activity_contribution=100,
        handbook_contribution=1,
        premium_currency_risk=False,
        prerequisites=(),
        run_factory=lambda: task,
    )


def test_claim_only_never_enqueues_progress_tasks():
    task = QueuedTask("扫荡", lambda: True, key="resident_activity")
    selected = select_reward_dependency_tasks(
        [_capability(task)], _snapshot(), RewardStrategy.CLAIM_ONLY
    )
    assert selected == []


def test_maximize_progress_enqueues_selected_dependency():
    task = QueuedTask("扫荡", lambda: True, key="resident_activity")
    selected = select_reward_dependency_tasks(
        [_capability(task)], _snapshot(), RewardStrategy.MAXIMIZE_PROGRESS
    )
    assert selected == [task]


def test_incomplete_daily_activity_is_observed_as_known_value():
    items = [
        {"text": "每日活跃", "position": ((100, 100), (200, 100), (200, 130), (100, 130))},
        {"text": "300/600", "position": ((160, 170), (260, 170), (260, 210), (160, 210))},
    ]
    assert _daily_activity_progress(items) == (300, 600)


def test_handbook_parser_ignores_unrelated_ratios():
    unrelated = [
        {"text": "背包 3/20", "position": ((900, 100), (1000, 100), (1000, 130), (900, 130))}
    ]
    assert _manual_daily_progress(unrelated) is None


def test_tasks_complete_but_unclaimed_reward_is_not_complete():
    result = decide_reward_run(
        _snapshot(
            daily_activity_current=600,
            handbook_daily_tasks_completed=5,
            handbook_rewards_unclaimed=1,
        ),
        now=_now(),
    )
    assert result.status is RewardRunStatus.CLAIMABLE
    assert result.all_tracked_objectives_complete is False


def test_unknown_near_reset_runs_final_check_before_reset():
    now = _now(4, 58)
    result = decide_reward_run(None, now=now)
    reset = CLOCK.next_daily_reset(now)
    assert result.next_action == "final_check_then_claim_before_reset"
    assert result.next_run_at < reset


def test_production_exception_uses_bounded_transient_backoff():
    fixed_clock = Mock(wraps=CLOCK)
    fixed_clock.server_now.return_value = _now()
    with patch("auto.reward_collection.RewardCollector.run", side_effect=OSError("frame lost")), patch(
        "auto.reward_collection.SERVER_CLOCK", fixed_clock
    ):
        result = collect_scheduled_rewards()
    assert result["status"] == RewardRunStatus.TRANSIENT_ERROR.value
    assert datetime.fromisoformat(result["next_run_at"]) <= _now() + timedelta(minutes=30)
