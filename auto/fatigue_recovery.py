"""Independent daily fatigue recovery task."""

import time
from dataclasses import asdict
from datetime import datetime
from enum import Enum

from loguru import logger

from auto.module.strength import read_strength, recover_strength
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
from core.services.server_calendar import SERVER_CLOCK
from core.services.station_facilities import rest_area_availability
from core.services.weekly_plan_state import load_weekly_plan
from core.utils.utils import RESOURCES_PATH, read_json


MINIMUM_TRADING_FATIGUE = 80


def _route_context() -> TradeRouteContext | None:
    state = load_weekly_plan()
    cycle = state.get("cycle", []) if state else []
    if len(cycle) < 2:
        return None
    fatigue = read_json(RESOURCES_PATH / "goods/CityTiredData.json")
    legs = []
    for index, origin in enumerate(cycle):
        destination = cycle[(index + 1) % len(cycle)]
        amenities = (
            frozenset({"REST_AREA"})
            if rest_area_availability(destination) is True
            else frozenset()
        )
        legs.append(
            RouteLeg(
                origin,
                destination,
                int(fatigue.get(f"{origin}-{destination}", 0)),
                amenities,
            )
        )
    return TradeRouteContext("|".join(cycle), tuple(legs))


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


def run_daily_fatigue_recovery() -> dict:
    """Use safe recovery now and keep the plan pending until drinks are checked."""
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
    lunches_remaining = usage_before.get("lunches_remaining")
    lunch_count = int(lunches_remaining) if isinstance(lunches_remaining, int) else 0
    snapshot = FatigueSnapshot(
        server_day_id=SERVER_CLOCK.server_day_id(),
        observed_at=SERVER_CLOCK.server_now(),
        fatigue_used=before[0],
        fatigue_cap=before[1],
        current_city_id=station_name,
        current_station_id=station_name,
        current_amenities=(
            frozenset({"REST_AREA"})
            if rest_area_availability(station_name) is True
            else frozenset()
        ),
        soda_uses_used=int(usage_before.get("bubble_water_uses", 0)),
        soda_uses_remaining=max(
            0, 6 - int(usage_before.get("bubble_water_uses", 0))
        ),
        soda_reduction_per_use=50,
        soda_price_tiers=("FREE", "IRON"),
        bento_batches_available=lunch_count,
        bento_total_reduction_available=lunch_count * 24,
        next_bento_release_at=None,
        natural_recovery_at=None,
        source_confidence="HIGH",
    )
    plan = plan_fatigue_recovery(snapshot, _route_context())
    logger.info(
        f"疲劳规划状态={plan.status.value}，立即动作="
        f"{[action.kind for action in plan.immediate_actions]}，"
        f"延迟触发={plan.next_trigger}，预计浪费={plan.expected_waste}"
    )
    # A low-fatigue observation is not a completed daily plan.  Leave the task
    # pending until a route/fatigue event or a release time makes an action safe.
    # When available headroom is already unsafe, keep the existing executor's
    # cabinet inspection so it can discover an unobserved bento batch.
    if (
        plan.status is not FatiguePlanStatus.ACTION_NOW
        and before[1] - before[0] >= MINIMUM_TRADING_FATIGUE
    ):
        go_home()
        return {
            "success": True,
            "deferred": True,
            "progress_made": False,
            "reason": plan.reason,
            "status": plan.status.value,
            "station": station_name,
            "before": before[0],
            "maximum": before[1],
            "plan": _json_value(asdict(plan)),
        }
    recovery_usage: dict[str, object] = {}
    recovered = recover_strength(
        "buy",
        min_available=MINIMUM_TRADING_FATIGUE,
        station_name=station_name,
        usage=recovery_usage,
    )
    daily_usage = record_fatigue_usage(**recovery_usage)
    if not recovered:
        logger.warning("疲劳恢复条件尚未满足，本次暂缓且不更新完成时间")
        go_home()
        return {
            "success": True,
            "deferred": True,
            "reason": "recovery_conditions_not_met",
            "station": station_name,
            "before": before[0],
            "maximum": before[1],
            "usage": daily_usage,
            "plan": _json_value(asdict(plan)),
        }
    after = _wait_strength()
    if not after:
        raise RuntimeError("疲劳规划无法读取恢复后疲劳")
    if not go_home():
        raise RuntimeError("疲劳恢复完成，但未能安全返回主界面")
    result = {
        "success": True,
        "cycle": fatigue_cycle(),
        "station": station_name,
        "before": before[0],
        "after": after[0],
        "maximum": after[1],
        "restored": max(0, before[0] - after[0]),
        "available": after[1] - after[0],
        "usage": daily_usage,
        "plan": _json_value(asdict(plan)),
        "progress_made": max(0, before[0] - after[0]) > 0,
    }
    if rest_area_availability(station_name) is False and after[0] >= 50:
        result.update(
            deferred=True,
            reason="lunch_only_waiting_for_rest_area",
        )
        logger.info(
            "当前站点无休息区，便当仅完成安全保底；保留疲劳规划，"
            "抵达有休息区站点后继续使用气泡水并重新判断便当"
        )
        return result
    logger.info(
        f"每日疲劳规划完成: {before[0]}/{before[1]} -> "
        f"{after[0]}/{after[1]}，恢复 {result['restored']}"
    )
    return result
