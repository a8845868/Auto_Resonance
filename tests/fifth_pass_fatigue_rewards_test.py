from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

from auto.fatigue_recovery import _snapshot_from_observation, run_daily_fatigue_recovery
from core.services.daily_capabilities import (
    DailyCapability,
    RewardSchedulingStatus,
    plan_daily_reward_dependencies,
)
from core.services.daily_rewards import (
    DailyProgressSnapshot,
    RewardRunStatus,
    RewardStrategy,
    decide_reward_run,
    snapshot_unknown_for_enabled_channels,
)
from core.services.fatigue_planner import (
    FatiguePlanStatus,
    RouteLeg,
    SodaPriceTier,
    TradeRouteContext,
    plan_fatigue_recovery,
)
from core.services.fatigue_triggers import (
    notify_fatigue_event,
    register_deferred_fatigue_actions,
)


NOW = datetime(2026, 7, 18, 12, 0, tzinfo=timezone(timedelta(hours=8)))


def _capability() -> DailyCapability:
    return DailyCapability(
        task_key="safe_dependency",
        enabled=True,
        automation_available=True,
        activity_contribution_min=100,
        activity_contribution_max=100,
        handbook_contribution=1,
    )


def _unknown_handbook_snapshot() -> DailyProgressSnapshot:
    return DailyProgressSnapshot(
        server_day_id="2026-07-18",
        daily_activity_current=600,
        daily_activity_max=600,
        daily_activity_source="game_observed",
        daily_activity_confidence="HIGH",
        daily_activity_claimable_tiers=0,
        daily_activity_unclaimed_tiers=0,
        handbook_daily_tasks_total=None,
        handbook_daily_tasks_completed=None,
        handbook_rewards_claimable=None,
        handbook_rewards_unclaimed=None,
        observed_at=NOW,
    )


def test_no_current_rest_area_keeps_soda_remaining_unknown():
    snapshot = _snapshot_from_observation(
        "A",
        (10, 800),
        {
            "rest_area_available": False,
            "soda_price_tiers": (),
            "lunches_remaining": 0,
            "lunch_total_recovery": 0,
        },
        {"bubble_water_uses": 1},
    )

    assert snapshot.soda_uses_remaining is None
    assert snapshot.soda_remaining_confidence == "UNKNOWN"
    assert snapshot.soda_tier_confidence == "UNKNOWN"


def test_future_rest_area_creates_reobserve_waypoint_action():
    snapshot = _snapshot_from_observation(
        "A",
        (10, 800),
        {
            "rest_area_available": False,
            "soda_price_tiers": (),
            "lunches_remaining": 0,
            "lunch_total_recovery": 0,
        },
        {"bubble_water_uses": 1},
    )
    route = TradeRouteContext(
        "A|B",
        (RouteLeg("A", "B", 50, frozenset({"REST_AREA"}), "HIGH"),),
    )

    plan = plan_fatigue_recovery(snapshot, route)

    assert plan.status is FatiguePlanStatus.DEFER_UNTIL_WAYPOINT
    assert plan.deferred_actions[0].kind == "REOBSERVE_RECOVERY_AT_WAYPOINT"
    assert plan.deferred_actions[0].waypoint_id == "B"


def test_handbook_unknown_snapshot_never_enqueues_dependency():
    plan = plan_daily_reward_dependencies(
        [_capability()],
        _unknown_handbook_snapshot(),
        RewardStrategy.MAXIMIZE_PROGRESS,
        now=NOW,
    )

    assert plan.status is RewardSchedulingStatus.UNKNOWN_RETRY
    assert plan.capabilities == ()


def test_unknown_snapshot_reobserves_before_any_resource_task():
    plan = plan_daily_reward_dependencies(
        [_capability()],
        _unknown_handbook_snapshot(),
        RewardStrategy.MAXIMIZE_PROGRESS,
        now=NOW,
    )

    assert plan.reason == "snapshot_unknown"
    assert plan.capabilities == ()


def test_waypoint_reobserve_discovers_tiers_before_drinking(tmp_path):
    route = TradeRouteContext(
        "A|B", (RouteLeg("A", "B", 50, frozenset({"REST_AREA"}), "HIGH"),)
    )
    path = tmp_path / "fatigue-waypoint.json"
    register_deferred_fatigue_actions(
        [{"kind": "REOBSERVE_RECOVERY_AT_WAYPOINT", "waypoint_id": "B"}],
        path=path,
    )
    sequence = []
    free = SodaPriceTier(1, "FREE", 0, True)

    def observe(_station):
        sequence.append("observe")
        if sequence.count("observe") > 1:
            return {
                "rest_area_available": False,
                "soda_price_tiers": (),
                "lunches_remaining": 0,
                "lunch_total_recovery": 0,
            }
        return {
            "rest_area_available": True,
            "soda_price_tiers": (free,),
            "lunches_remaining": 0,
            "lunch_total_recovery": 0,
        }

    def execute(kind, **_kwargs):
        sequence.append(kind)
        return {"success": True, "bubble_water_uses": 1}

    with patch("auto.fatigue_recovery.connect", return_value=True), patch(
        "auto.fatigue_recovery.get_station", return_value="B"
    ), patch("auto.fatigue_recovery._open_exchange_buy_page", return_value=True), patch(
        "auto.fatigue_recovery._wait_strength", side_effect=[(60, 800), (110, 800), (110, 800)]
    ), patch("auto.fatigue_recovery._route_context", return_value=route), patch(
        "auto.fatigue_recovery.observe_recovery_resources", side_effect=observe
    ), patch("auto.fatigue_recovery.execute_planned_recovery_action", side_effect=execute), patch(
        "auto.fatigue_recovery.load_fatigue_usage", return_value={"bubble_water_uses": 0}
    ), patch("auto.fatigue_recovery.record_fatigue_usage", return_value={"bubble_water_uses": 1}), patch(
        "auto.fatigue_recovery.register_deferred_fatigue_actions"
    ), patch("auto.fatigue_recovery.go_home", return_value=True):
        assert notify_fatigue_event(
            "arrival", "B", path=path, schedule=run_daily_fatigue_recovery
        ) is True

    assert sequence[0:2] == ["observe", "DRINK_SODA"]


def test_future_waypoint_never_preapproves_premium_tier():
    snapshot = _snapshot_from_observation(
        "A", (10, 800),
        {"rest_area_available": False, "soda_price_tiers": (), "lunches_remaining": 0, "lunch_total_recovery": 0},
        {"bubble_water_uses": 0},
    )
    route = TradeRouteContext(
        "A|B", (RouteLeg("A", "B", 50, frozenset({"REST_AREA"}), "HIGH"),)
    )
    plan = plan_fatigue_recovery(snapshot, route, allow_premium_soda=True)
    assert [action.kind for action in plan.deferred_actions] == [
        "REOBSERVE_RECOVERY_AT_WAYPOINT"
    ]
    assert not plan.immediate_actions


def test_route_without_rest_area_reports_explicit_blocker():
    snapshot = _snapshot_from_observation(
        "A", (60, 800),
        {"rest_area_available": False, "soda_price_tiers": (), "lunches_remaining": 0, "lunch_total_recovery": 0},
        {"bubble_water_uses": 0},
    )
    route = TradeRouteContext(
        "A|B", (RouteLeg("A", "B", 10, frozenset(), "HIGH"),)
    )
    plan = plan_fatigue_recovery(snapshot, route)
    assert plan.status is FatiguePlanStatus.BLOCKED
    assert plan.reason == "route_has_no_recovery_waypoint"


def test_daily_unknown_snapshot_never_enqueues_handbook_dependency():
    snapshot = _unknown_handbook_snapshot()
    snapshot = DailyProgressSnapshot(
        **{
            **snapshot.__dict__,
            "daily_activity_current": None,
            "daily_activity_max": None,
            "daily_activity_confidence": "UNKNOWN",
            "handbook_daily_tasks_total": 5,
            "handbook_daily_tasks_completed": 4,
            "handbook_rewards_unclaimed": 0,
        }
    )
    plan = plan_daily_reward_dependencies(
        [_capability()], snapshot, RewardStrategy.MAXIMIZE_PROGRESS, now=NOW
    )
    assert plan.status is RewardSchedulingStatus.UNKNOWN_RETRY
    assert plan.capabilities == ()


def test_dependency_planner_and_reward_decider_share_unknown_predicate():
    snapshot = _unknown_handbook_snapshot()
    assert snapshot_unknown_for_enabled_channels(snapshot, True, True)
    dependency = plan_daily_reward_dependencies(
        [_capability()], snapshot, RewardStrategy.MAXIMIZE_PROGRESS, now=NOW
    )
    reward = decide_reward_run(snapshot, now=NOW)
    assert dependency.status is RewardSchedulingStatus.UNKNOWN_RETRY
    assert reward.status is RewardRunStatus.UNKNOWN


def test_disabled_manual_channel_does_not_require_manual_fields():
    snapshot = _unknown_handbook_snapshot()
    assert not snapshot_unknown_for_enabled_channels(snapshot, True, False)
    dependency = plan_daily_reward_dependencies(
        [_capability()], snapshot, RewardStrategy.MAXIMIZE_PROGRESS, now=NOW,
        travel_manual_enabled=False,
    )
    reward = decide_reward_run(
        snapshot, now=NOW, travel_manual_enabled=False
    )
    assert dependency.status is RewardSchedulingStatus.COMPLETE
    assert reward.status is RewardRunStatus.COMPLETE
