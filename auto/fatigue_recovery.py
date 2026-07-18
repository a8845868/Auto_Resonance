"""Independent daily fatigue recovery task."""

import time
from dataclasses import asdict, replace
from datetime import datetime, timedelta
from enum import Enum

from loguru import logger

from auto.module.strength import (
    execute_planned_recovery_action,
    observe_recovery_resources,
    read_strength,
)
from app.common.config import cfg
from core.control.control import connect, input_tap
from core.preset import get_station, go_outlets
from core.preset.control import go_home
from core.services.fatigue_planner import fatigue_cycle, record_fatigue_usage
from core.services.fatigue_planner import (
    FatiguePlanStatus,
    FatigueSnapshot,
    RouteLeg,
    TradeRouteContext,
    load_fatigue_usage,
    plan_fatigue_recovery,
)
from core.services.fatigue_triggers import register_deferred_fatigue_actions
from core.services.server_calendar import SERVER_CLOCK
from core.services.station_facilities import rest_area_availability
from core.services.weekly_plan_state import load_weekly_plan
from core.utils.utils import RESOURCES_PATH, read_json


MINIMUM_TRADING_FATIGUE = 80


def _route_context(current_station: str | None = None) -> TradeRouteContext | None:
    state = load_weekly_plan()
    cycle = state.get("cycle", []) if state else []
    if len(cycle) < 2:
        return None
    fatigue = read_json(RESOURCES_PATH / "goods/CityTiredData.json")
    legs = []
    for index, origin in enumerate(cycle):
        destination = cycle[(index + 1) % len(cycle)]
        availability = rest_area_availability(destination)
        amenities = (
            frozenset({"REST_AREA"})
            if availability is True
            else frozenset()
        )
        legs.append(
            RouteLeg(
                origin,
                destination,
                int(fatigue.get(f"{origin}-{destination}", 0)),
                amenities,
                "HIGH" if isinstance(availability, bool) else "UNKNOWN",
            )
        )
    current_leg_index = 0
    try:
        from core.services.trade_ledger import load_trade_week_state

        partial = load_trade_week_state().current_partial_cycle
    except Exception as error:
        logger.warning(f"无法读取跑商部分周期，疲劳路线从当前站点推导: {error}")
        partial = None
    if partial and partial.get("route_id") == "|".join(cycle):
        current_leg_index = int(partial.get("confirmed_legs", 0)) % len(legs)
    if current_station:
        station_index = next(
            (index for index, leg in enumerate(legs) if leg.origin == current_station),
            None,
        )
        if station_index is not None:
            current_leg_index = station_index
    return TradeRouteContext("|".join(cycle), tuple(legs), current_leg_index)


def _json_value(value):
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat(timespec="seconds")
    if isinstance(value, (set, frozenset, tuple)):
        return [_json_value(item) for item in value]
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    return value


def _wait_strength(timeout: float = 10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = read_strength()
        if value:
            return value
        time.sleep(0.7)
    return None


def _open_exchange_buy_page() -> bool:
    if not go_home():
        return False
    if not go_outlets("交易所"):
        return False
    time.sleep(1.5)
    input_tap((927, 321))
    time.sleep(2)
    return _wait_strength() is not None


def _next_bento_release(now: datetime) -> datetime | None:
    for hour in (12, 18):
        candidate = now.replace(hour=hour, minute=0, second=0, microsecond=0)
        if candidate > now:
            return candidate
    return SERVER_CLOCK.next_daily_reset(now)


def _snapshot_from_observation(
    station_name: str,
    strength: tuple[int, int],
    observation: dict[str, object],
    usage: dict[str, object],
) -> FatigueSnapshot:
    lunches = observation.get("lunches_remaining")
    lunch_total = observation.get("lunch_total_recovery")
    lunch_values = tuple(observation.get("lunch_recovery_values") or ())
    if (
        lunch_total is None
        and lunches is not None
        and int(lunches) > 0
        and len(lunch_values) == int(lunches)
    ):
        # Card values are usable only when every observed inventory item has a
        # corresponding value; otherwise the plan remains UNKNOWN.
        lunch_total = sum(max(0, int(value)) for value in lunch_values)
    tiers = tuple(observation.get("soda_price_tiers") or ())
    resource_known = lunches is not None and (
        int(lunches) == 0 or lunch_total is not None
    )
    return FatigueSnapshot(
        server_day_id=SERVER_CLOCK.server_day_id(),
        observed_at=SERVER_CLOCK.server_now(),
        fatigue_used=int(strength[0]),
        fatigue_cap=int(strength[1]),
        current_city_id=station_name,
        current_station_id=station_name,
        current_amenities=(
            frozenset({"REST_AREA"})
            if observation.get("rest_area_available") is True
            else frozenset()
        ),
        soda_uses_used=int(usage.get("bubble_water_uses", 0)),
        # Only the currently observed sequential price tier is executable.
        # Every successful drink forces another observation and replan.
        soda_uses_remaining=min(
            max(0, 6 - int(usage.get("bubble_water_uses", 0))), len(tiers)
        ),
        soda_reduction_per_use=50,
        soda_price_tiers=tiers,
        bento_batches_available=int(lunches) if lunches is not None else 0,
        bento_total_reduction_available=(
            int(lunch_total) if lunch_total is not None else 0
        ),
        next_bento_release_at=_next_bento_release(SERVER_CLOCK.server_now()),
        natural_recovery_at=None,
        source_confidence="HIGH" if resource_known else "UNKNOWN",
        fatigue_confidence="HIGH",
        station_confidence="HIGH" if station_name else "UNKNOWN",
        amenity_confidence=(
            "HIGH"
            if isinstance(observation.get("rest_area_available"), bool)
            else "UNKNOWN"
        ),
        soda_tier_confidence=(
            "HIGH"
            if tiers or observation.get("rest_area_available") is False
            else "UNKNOWN"
        ),
        soda_remaining_confidence=(
            "HIGH"
            if tiers or observation.get("rest_area_available") is False
            else "UNKNOWN"
        ),
        bento_inventory_confidence="HIGH" if lunches is not None else "UNKNOWN",
        bento_value_confidence=(
            "HIGH"
            if lunches is not None and (int(lunches) == 0 or lunch_total is not None)
            else "UNKNOWN"
        ),
    )


def run_daily_fatigue_recovery() -> dict:
    """Observe resources, execute the plan one action at a time, and replan."""
    if not connect():
        raise RuntimeError("疲劳规划无法连接模拟器")
    station_name = get_station()
    if not station_name:
        raise RuntimeError("疲劳规划未能确认当前站点")
    if not _open_exchange_buy_page():
        raise RuntimeError("疲劳规划未能进入交易所买入页")
    before = _wait_strength()
    if not before:
        raise RuntimeError("疲劳规划无法读取恢复前疲劳")
    logger.info(
        f"开始每日疲劳规划: {before[0]}/{before[1]}；"
        "先用气泡水，再判断全部便当是否会浪费"
    )
    usage_before = load_fatigue_usage()
    observation = observe_recovery_resources(station_name)
    snapshot = _snapshot_from_observation(
        station_name, before, observation, usage_before
    )
    route = _route_context(station_name)
    plan = plan_fatigue_recovery(
        snapshot,
        route,
        allow_premium_soda=bool(cfg.UseSilverBranch.value),
        max_iron_soda_cost=int(cfg.MaxIronSodaCost.value),
    )
    logger.info(
        f"疲劳规划状态={plan.status.value}，立即动作="
        f"{[action.kind for action in plan.immediate_actions]}，"
        f"延迟触发={plan.next_trigger}，预计浪费={plan.expected_waste}"
    )
    initial_plan = plan
    daily_usage = usage_before
    progress_made = False
    for _ in range(8):
        if plan.status is not FatiguePlanStatus.ACTION_NOW or not plan.immediate_actions:
            break
        action = plan.immediate_actions[0]
        action_result = execute_planned_recovery_action(
            action.kind,
            station_name=station_name,
        )
        if action_result.get("success") is not True:
            logger.warning(f"疲劳动作验证失败，重新规划前暂缓: {action_result}")
            plan = plan_fatigue_recovery(
                replace(
                    plan.snapshot,
                    source_confidence="UNKNOWN",
                    fatigue_confidence="UNKNOWN",
                ),
                route,
                allow_premium_soda=bool(cfg.UseSilverBranch.value),
                max_iron_soda_cost=int(cfg.MaxIronSodaCost.value),
            )
            break
        progress_made = True
        daily_usage = record_fatigue_usage(
            bubble_water_uses=int(action_result.get("bubble_water_uses", 0)),
            lunch_batches=int(action_result.get("lunch_batches", 0)),
            lunch_fatigue_restored=int(
                action_result.get("lunch_fatigue_restored", 0)
            ),
            lunches_remaining=(
                int(action_result["lunches_remaining"])
                if isinstance(action_result.get("lunches_remaining"), int)
                else None
            ),
        )
        observed_strength = _wait_strength()
        if not observed_strength:
            raise RuntimeError("疲劳动作后无法重新读取疲劳")
        observation = observe_recovery_resources(station_name)
        snapshot = _snapshot_from_observation(
            station_name, observed_strength, observation, daily_usage
        )
        plan = plan_fatigue_recovery(
            snapshot,
            route,
            allow_premium_soda=bool(cfg.UseSilverBranch.value),
            max_iron_soda_cost=int(cfg.MaxIronSodaCost.value),
        )

    after = _wait_strength() or (plan.snapshot.fatigue_used, plan.snapshot.fatigue_cap)
    deferred_actions = list(plan.deferred_actions)
    if not deferred_actions and plan.status is FatiguePlanStatus.DEFER_UNTIL_FATIGUE:
        deferred_actions = [{
            "kind": "REPLAN",
            "trigger_type": "FATIGUE_THRESHOLD",
            "fatigue_threshold": int(plan.next_trigger.get("fatigue_at_least", 0)),
        }]
    elif not deferred_actions and plan.status is FatiguePlanStatus.DEFER_UNTIL_RELEASE:
        deferred_actions = [{
            "kind": "REPLAN",
            "trigger_type": "BENTO_RELEASE_AT",
            "run_at": str(plan.next_trigger.get("at", "")),
        }]
    elif not deferred_actions and plan.status is FatiguePlanStatus.UNKNOWN:
        deferred_actions = [{
            "kind": "REPLAN",
            "trigger_type": "REOBSERVE_AT",
            "run_at": (SERVER_CLOCK.server_now() + timedelta(minutes=15)).isoformat(),
        }]
    for action in deferred_actions:
        if isinstance(action, dict):
            continue
        # Waypoint actions are dataclasses and are normalized by the trigger
        # registry; their non-empty waypoint makes the trigger unambiguous.
    revision = f"{plan.snapshot.server_day_id}:{plan.snapshot.observed_at.isoformat()}"
    register_deferred_fatigue_actions(deferred_actions, plan_revision=revision)
    go_home()
    result = {
        "success": True,
        "cycle": fatigue_cycle(),
        "station": station_name,
        "before": before[0],
        "after": after[0],
        "maximum": after[1],
        "restored": max(0, before[0] - int(after[0])),
        "available": after[1] - after[0],
        "usage": daily_usage,
        "plan": _json_value(asdict(plan)),
        "initial_plan": _json_value(asdict(initial_plan)),
        "progress_made": progress_made,
    }
    if plan.status is not FatiguePlanStatus.COMPLETE_FOR_DAY:
        result.update(deferred=True, reason=plan.reason, status=plan.status.value)
        if plan.status is FatiguePlanStatus.DEFER_UNTIL_RELEASE and plan.snapshot.next_bento_release_at is not None:
            result["next_run_at"] = plan.snapshot.next_bento_release_at.isoformat()
        elif plan.status is FatiguePlanStatus.DEFER_UNTIL_FATIGUE:
            target = plan.snapshot.natural_recovery_at or (
                SERVER_CLOCK.server_now() + timedelta(minutes=30)
            )
            result["next_run_at"] = target.isoformat()
        elif plan.status is FatiguePlanStatus.UNKNOWN:
            result["next_run_at"] = (
                SERVER_CLOCK.server_now() + timedelta(minutes=15)
            ).isoformat()
        return result
    logger.info(
        f"每日疲劳规划完成: {before[0]}/{before[1]} -> "
        f"{after[0]}/{after[1]}，恢复 {result['restored']}"
    )
    return result
