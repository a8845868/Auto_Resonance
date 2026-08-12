from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from core.services.daily_capabilities import (
    DailyCapability,
    RewardSchedulingStatus,
    plan_daily_reward_dependencies,
    resolve_daily_capability_prerequisites,
    select_daily_capabilities,
)
from core.services.daily_rewards import DailyProgressSnapshot, RewardStrategy
from core.services.trade_planning import (
    StalePriceSnapshot,
    recommend_max_feasible_runs_today,
    validate_executable_trade_budget,
)


NOW = datetime(2026, 7, 18, 12, 0, tzinfo=timezone(timedelta(hours=8)))


def _snapshot(*, activity=300, maximum=600, handbook=4, total=5, observed_at=NOW):
    return DailyProgressSnapshot(
        server_day_id="2026-07-18",
        daily_activity_current=activity,
        daily_activity_max=maximum,
        daily_activity_source="game_observed",
        daily_activity_confidence="HIGH",
        daily_activity_claimable_tiers=0,
        daily_activity_unclaimed_tiers=0,
        handbook_daily_tasks_total=total,
        handbook_daily_tasks_completed=handbook,
        handbook_rewards_claimable=0,
        handbook_rewards_unclaimed=0,
        observed_at=observed_at,
    )


def _capability(key, activity, handbook, **kwargs):
    return DailyCapability(
        task_key=key,
        enabled=True,
        automation_available=True,
        activity_contribution_min=activity,
        activity_contribution_max=activity,
        handbook_contribution=handbook,
        **kwargs,
    )


def _trade_state(**updates):
    state = {
        "schema_version": 2,
        "cycle": ["A", "B"],
        "total_runs": 2,
        "completed_runs": 0,
        "runs": [{"A": 1, "B": 0}, {"A": 1, "B": 1}],
        "books_total": 3,
        "cycle_fatigue": 100,
        "expected_profit": 2000,
        "price_time": NOW.isoformat(),
        "price_source": "game_observed",
        "price_revision": "r1",
        "stations_available": ["A", "B"],
    }
    state.update(updates)
    return state


def test_no_snapshot_never_enqueues_progress_dependency():
    cap = _capability("safe", 100, 1)
    assert select_daily_capabilities([cap], None, RewardStrategy.MAXIMIZE_PROGRESS) == []
    decision = plan_daily_reward_dependencies(
        [cap], None, RewardStrategy.MAXIMIZE_PROGRESS, now=NOW
    )
    assert decision.status is RewardSchedulingStatus.OBSERVE_FIRST
    assert decision.capabilities == ()


def test_expired_snapshot_never_enqueues_progress_dependency():
    decision = plan_daily_reward_dependencies(
        [_capability("safe", 100, 1)],
        _snapshot(observed_at=NOW - timedelta(minutes=16)),
        RewardStrategy.MAXIMIZE_PROGRESS,
        now=NOW,
    )
    assert decision.status is RewardSchedulingStatus.OBSERVE_FIRST
    assert decision.capabilities == ()


def test_incomplete_retry_reobserves_before_dependency():
    decision = plan_daily_reward_dependencies(
        [_capability("safe", 100, 1)],
        _snapshot(),
        RewardStrategy.MAXIMIZE_PROGRESS,
        now=NOW,
        post_action_observation_required=True,
    )
    assert decision.status is RewardSchedulingStatus.UNKNOWN_RETRY
    assert decision.capabilities == ()


def test_dependency_completion_requires_fresh_post_action_snapshot():
    before = plan_daily_reward_dependencies(
        [_capability("safe", 100, 1)],
        _snapshot(),
        RewardStrategy.MAXIMIZE_PROGRESS,
        now=NOW,
    )
    after = plan_daily_reward_dependencies(
        [_capability("safe", 100, 1)],
        _snapshot(),
        RewardStrategy.MAXIMIZE_PROGRESS,
        now=NOW,
        post_action_observation_required=True,
    )
    assert before.status is RewardSchedulingStatus.RUN_ONE_DEPENDENCY
    assert after.status is RewardSchedulingStatus.UNKNOWN_RETRY


def test_prerequisite_resolver_allows_fresh_safe_trade():
    result = resolve_daily_capability_prerequisites(
        trade_state=_trade_state(),
        passenger_state={},
        now=NOW,
        fatigue_budget=100,
        purchase_books=1,
    )
    assert "fresh_trade_plan" in result.satisfied
    assert result.evidence["fresh_trade_plan"].status == "SATISFIED"


def test_prerequisite_resolver_blocks_stale_trade():
    result = resolve_daily_capability_prerequisites(
        trade_state=_trade_state(price_time=(NOW - timedelta(hours=1)).isoformat()),
        passenger_state={},
        now=NOW,
        fatigue_budget=100,
        purchase_books=1,
    )
    assert "fresh_trade_plan" not in result.satisfied
    assert result.evidence["fresh_trade_plan"].block_reason


def test_prerequisite_resolver_allows_safe_passenger():
    result = resolve_daily_capability_prerequisites(
        trade_state={},
        passenger_state={
            "status": "pending",
            "completed_carriages": 2,
            "target_carriages": 8,
            "premium_currency_required": False,
            "automation_safe": True,
            "automation_safety_source": "game_observed",
            "automation_safety_reason": "build_button_and_currency_observed",
            "server_day_id": "2026-07-18",
            "config_revision": "cfg-1",
            "evidence_config_revision": "cfg-1",
            "evidence_target_carriages": 8,
            "evidence_completed_carriages": 2,
            "evidence_status": "pending",
            "observed_at": NOW.isoformat(),
        },
        now=NOW,
    )
    assert "safe_build_available" in result.satisfied


def test_unknown_prerequisite_is_not_treated_as_satisfied():
    result = resolve_daily_capability_prerequisites(
        trade_state={}, passenger_state={}, now=NOW
    )
    assert result.satisfied == frozenset()
    assert all(item.status == "UNKNOWN" for item in result.evidence.values())


def test_unknown_prerequisite_is_not_satisfied():
    test_unknown_prerequisite_is_not_treated_as_satisfied()


def test_zero_activity_gap_penalizes_extra_activity():
    selected = select_daily_capabilities(
        [_capability("wasteful", 100, 1), _capability("precise", 0, 1)],
        _snapshot(activity=600, handbook=4),
        RewardStrategy.MAXIMIZE_PROGRESS,
    )
    assert [item.task_key for item in selected] == ["precise"]


def test_zero_handbook_gap_penalizes_extra_handbook_work():
    selected = select_daily_capabilities(
        [_capability("wasteful", 100, 1), _capability("precise", 100, 0)],
        _snapshot(activity=500, handbook=5),
        RewardStrategy.MAXIMIZE_PROGRESS,
    )
    assert [item.task_key for item in selected] == ["precise"]


def test_unknown_gap_does_not_equal_zero_gap():
    unknown = _snapshot(activity=None, maximum=None, handbook=4)
    selected = select_daily_capabilities(
        [_capability("known_work", 100, 0)],
        unknown,
        RewardStrategy.MAXIMIZE_PROGRESS,
    )
    # An unknown enabled channel is an observation gate, not an invitation to
    # spend resources merely because the gap is not known to be zero.
    assert selected == []


def test_missing_price_source_is_not_executable():
    state = _trade_state()
    state.pop("price_source")
    with pytest.raises(StalePriceSnapshot):
        validate_executable_trade_budget(
            state, now=NOW, fatigue_budget=100, purchase_books=1
        )


def test_unknown_price_source_is_not_executable():
    with pytest.raises(StalePriceSnapshot):
        validate_executable_trade_budget(
            _trade_state(price_source="UNKNOWN"),
            now=NOW,
            fatigue_budget=100,
            purchase_books=1,
        )


def test_nondivisible_book_budget_uses_exact_requirement():
    plan = validate_executable_trade_budget(
        _trade_state(), now=NOW, fatigue_budget=100, purchase_books=1
    )
    assert plan.purchase_books_used == 1


def test_per_leg_book_schedule_blocks_underfunded_second_leg():
    with pytest.raises(ValueError, match="book"):
        validate_executable_trade_budget(
            _trade_state(
                completed_runs=1,
                current_partial_cycle={"confirmed_legs": 1},
            ),
            now=NOW,
            fatigue_budget=100,
            purchase_books=0,
        )


def test_partial_cycle_without_remaining_resources_is_not_recommended():
    assert recommend_max_feasible_runs_today(
        remaining_runs=1,
        cycle_fatigue=100,
        available_fatigue=0,
        recoverable_fatigue=0,
        purchase_books=0,
        books_per_cycle=1,
        partial_cycle={"confirmed_legs": 1},
        price_fresh=True,
    ) == 0
