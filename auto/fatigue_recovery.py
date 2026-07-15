"""Independent daily fatigue recovery task."""

import time

from loguru import logger

from auto.module.strength import read_strength, recover_strength
from core.control.control import connect, input_tap
from core.preset import get_station, go_outlets
from core.preset.control import go_home
from core.services.fatigue_planner import fatigue_cycle, record_fatigue_usage
from core.services.station_facilities import rest_area_availability


MINIMUM_TRADING_FATIGUE = 80


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
