from datetime import datetime

from core.services.fatigue_planner import (
    FatiguePlanStatus,
    FatigueSnapshot,
    RouteLeg,
    TradeRouteContext,
    plan_fatigue_recovery,
)
from core.services.server_calendar import GameServerClock


def _snapshot(**overrides):
    clock = GameServerClock()
    values = {
        "server_day_id": "2026-07-17",
        "observed_at": datetime(2026, 7, 17, 12, 0, tzinfo=clock.timezone),
        "fatigue_used": 44,
        "fatigue_cap": 816,
        "current_city_id": "岚心城",
        "current_station_id": "岚心城",
        "current_amenities": frozenset({"REST_AREA"}),
        "soda_uses_used": 0,
        "soda_uses_remaining": 6,
        "soda_reduction_per_use": 50,
        "soda_price_tiers": ("FREE", "IRON"),
        "bento_batches_available": 3,
        "bento_total_reduction_available": 72,
        "next_bento_release_at": None,
        "natural_recovery_at": None,
        "source_confidence": "HIGH",
    }
    values.update(overrides)
    return FatigueSnapshot(**values)


def test_44_fatigue_defers_soda_instead_of_finishing_day():
    plan = plan_fatigue_recovery(_snapshot())
    assert plan.status is FatiguePlanStatus.DEFER_UNTIL_FATIGUE
    assert plan.immediate_actions == ()
    assert plan.next_trigger["fatigue_at_least"] == 50


def test_drinks_maximum_non_wasting_sodas_at_current_station():
    plan = plan_fatigue_recovery(
        _snapshot(fatigue_used=300, bento_total_reduction_available=0)
    )
    assert plan.status is FatiguePlanStatus.ACTION_NOW
    assert plan.immediate_actions[0].kind == "DRINK_SODA"
    assert plan.immediate_actions[0].count == 6
    assert plan.immediate_actions[0].reobserve_after_each is True


def test_275_fatigue_uses_five_sodas_not_six():
    plan = plan_fatigue_recovery(
        _snapshot(fatigue_used=275, bento_total_reduction_available=0)
    )
    assert plan.immediate_actions[0].count == 5


def test_route_inserts_soda_action_at_first_eligible_waypoint():
    route = TradeRouteContext(
        route_id="route-1",
        legs=(
            RouteLeg("岚心城", "汇流塔", 20, frozenset()),
            RouteLeg("汇流塔", "修格里城", 40, frozenset({"REST_AREA"})),
        ),
    )
    plan = plan_fatigue_recovery(
        _snapshot(current_amenities=frozenset(), current_city_id="岚心城"), route
    )
    assert plan.status is FatiguePlanStatus.DEFER_UNTIL_WAYPOINT
    assert plan.deferred_actions[0].waypoint_id == "修格里城"
    assert plan.expected_fatigue_by_waypoint["修格里城"] == 104


def test_route_without_bar_is_reported_as_deferred():
    route = TradeRouteContext(
        route_id="route-2",
        legs=(RouteLeg("岚心城", "汇流塔", 60, frozenset()),),
    )
    plan = plan_fatigue_recovery(
        _snapshot(current_amenities=frozenset(), current_city_id="岚心城"), route
    )
    assert plan.status in {
        FatiguePlanStatus.DEFER_UNTIL_FATIGUE,
        FatiguePlanStatus.DEFER_UNTIL_WAYPOINT,
        FatiguePlanStatus.BLOCKED,
    }
    assert all(action.kind != "DRINK_SODA" for action in plan.deferred_actions)


def test_bento_all_use_waits_until_no_waste():
    plan = plan_fatigue_recovery(_snapshot(soda_uses_remaining=0))
    assert plan.immediate_actions == ()
    assert plan.next_trigger["fatigue_at_least"] == 72


def test_soda_and_bento_order_maximizes_free_reduction_without_waste():
    plan = plan_fatigue_recovery(_snapshot(fatigue_used=172, soda_uses_remaining=2))
    assert [action.kind for action in plan.immediate_actions] == [
        "USE_ALL_BENTOS",
        "DRINK_SODA",
    ]
    assert plan.expected_waste == 0


def test_premium_soda_is_never_planned_when_disabled():
    plan = plan_fatigue_recovery(
        _snapshot(fatigue_used=100, soda_price_tiers=("PREMIUM",)),
        allow_premium_soda=False,
    )
    assert all(action.kind != "DRINK_SODA" for action in plan.immediate_actions)
