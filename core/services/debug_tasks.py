"""Task registry shared by the persistent headless debug runtime."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable

from app.common.config import cfg
from core.services.task_schedule_state import next_daily_reset


@dataclass(frozen=True)
class DebugTask:
    key: str
    name: str
    run: Callable[[], object]
    enabled: Callable[[], bool] = lambda: True
    next_run_factory: Callable[[datetime], datetime] = next_daily_reset
    failure_retry_seconds: int = 600

    def next_run_after(self, succeeded: bool, now: datetime | None = None) -> datetime:
        now = now or datetime.now()
        if succeeded:
            return self.next_run_factory(now)
        return now + timedelta(seconds=self.failure_retry_seconds)


def _screen_state() -> dict:
    from core.control.control import connect, kill, screenshot
    from core.services.screen_state import is_train_in_transit

    if not connect():
        raise RuntimeError("无法连接模拟器")
    try:
        items = screenshot().ocr()
        texts = [item["text"] for item in items]
        return {
            "in_transit": is_train_in_transit(items),
            "texts": texts,
        }
    finally:
        kill()


def _station_state() -> dict:
    from core.control.control import connect, kill
    from core.preset import get_station

    if not connect():
        raise RuntimeError("无法连接模拟器")
    try:
        station = get_station()
        if not station:
            raise RuntimeError("未能确认当前站点")
        return {"station": station}
    finally:
        kill()


def _fatigue() -> object:
    from auto.fatigue_recovery import run_daily_fatigue_recovery

    return run_daily_fatigue_recovery()


def _passenger_build() -> object:
    from auto.passenger_carriage_build import run_build_monitor

    return run_build_monitor()


def _resident_activity() -> object:
    from auto.resident_activity import run_resident_activity

    return run_resident_activity(
        cfg.residentActivityTask.value,
        cfg.residentActivityFullRealmReward.value,
    )


def _rewards() -> object:
    from auto.module.dispatch import collect_dispatch_rewards
    from auto.reward_collection import collect_rewards

    rewards = collect_rewards(
        bool(cfg.autoCollectDailyActivity.value),
        bool(cfg.autoCollectTravelManual.value),
    )
    return {
        "task_rewards": rewards,
        "dispatch_collected": collect_dispatch_rewards(),
    }


def _run_business() -> object:
    from auto.run_business import adaptive_weekly_run, two_city_run
    from core.services.weekly_plan_state import load_weekly_plan, progress_summary

    saved_plan = load_weekly_plan()
    route_plan = saved_plan or load_weekly_plan(include_expired=True)
    if not route_plan or len(route_plan.get("cycle", [])) != 2:
        raise RuntimeError("后台跑商需要先在 GUI 中保存一条双城周计划")
    summary = progress_summary(saved_plan) if saved_plan else None
    if summary and summary["finished"]:
        return {"already_completed": True, "cycle": route_plan["cycle"]}
    if saved_plan:
        return adaptive_weekly_run()
    return two_city_run(
        buy_city_name=route_plan["cycle"][0],
        sell_city_name=route_plan["cycle"][1],
    )


def _shop_probe() -> object:
    """Read-only catalog walk: every product dialog is cancelled."""
    from auto.shop_purchase import probe_shop_catalog

    return probe_shop_catalog(capture_evidence=True)


def _shop_dialog_probe() -> object:
    """Open one multi-buy dialog, select max, OCR-check it, then cancel."""
    from auto.shop_purchase import probe_shop_quantity_dialog

    return probe_shop_quantity_dialog(capture_evidence=True)


def _shop_purchase() -> object:
    from auto.shop_purchase import run_shop_purchase

    return run_shop_purchase()


def _next_fatigue(now: datetime) -> datetime:
    from core.services.fatigue_planner import next_fatigue_refresh

    return next_fatigue_refresh(now)


def _next_passenger_build(now: datetime) -> datetime:
    from core.services.passenger_build_planner import load_build_monitor_plan

    latest = load_build_monitor_plan() or {}
    due = latest.get("active_due_at")
    if due:
        try:
            return datetime.fromisoformat(due).replace(tzinfo=None)
        except ValueError:
            pass
    return now + timedelta(minutes=5)


def _passenger_build_enabled() -> bool:
    if not bool(cfg.enablePassengerBuildMonitor.value):
        return False
    from core.services.passenger_build_planner import (
        build_monitor_summary,
        load_build_monitor_plan,
    )

    return bool(build_monitor_summary(load_build_monitor_plan()).get("active"))


def _business_enabled() -> bool:
    if not bool(cfg.enableRunBusiness.value) or int(cfg.BuyCount.value) <= 0:
        return False
    from core.services.weekly_plan_state import load_weekly_plan

    plan = load_weekly_plan() or load_weekly_plan(include_expired=True)
    return bool(plan and len(plan.get("cycle", [])) == 2)


def _next_business(now: datetime) -> datetime:
    from core.services.weekly_plan_state import load_weekly_plan, progress_summary

    state = load_weekly_plan()
    summary = progress_summary(state)
    if state and summary and not summary["finished"]:
        return now + timedelta(seconds=5)
    return next_daily_reset(now)


def _shop_purchase_enabled() -> bool:
    from core.services.shop_catalog import shop_plan_enabled

    return shop_plan_enabled()


def _next_shop_purchase(now: datetime) -> datetime:
    from core.services.shop_catalog import next_shop_reset

    return next_shop_reset(now)


def task_registry() -> dict[str, DebugTask]:
    tasks = [
        DebugTask("screen", "只读画面识别", _screen_state),
        DebugTask("station", "识别当前站点", _station_state),
        DebugTask("shop_probe", "只读扫描总部黑月商店", _shop_probe),
        DebugTask(
            "shop_dialog_probe",
            "只读校验商店数量弹窗",
            _shop_dialog_probe,
        ),
        DebugTask(
            "fatigue_recovery",
            "疲劳规划",
            _fatigue,
            enabled=lambda: bool(cfg.enableFatiguePlanner.value),
            next_run_factory=_next_fatigue,
        ),
        DebugTask(
            "resident_activity",
            "扫荡与全域整备",
            _resident_activity,
            enabled=lambda: bool(cfg.enableResidentActivity.value),
        ),
        DebugTask(
            "reward_collection",
            "领取任务奖励",
            _rewards,
            enabled=lambda: bool(cfg.enableRewardCollection.value)
            and (
                bool(cfg.autoCollectDailyActivity.value)
                or bool(cfg.autoCollectTravelManual.value)
            ),
        ),
        DebugTask(
            "run_business",
            "端点跑商",
            _run_business,
            enabled=_business_enabled,
            next_run_factory=_next_business,
        ),
        DebugTask(
            "passenger_build_monitor",
            "客厢连续建造监控",
            _passenger_build,
            enabled=_passenger_build_enabled,
            next_run_factory=_next_passenger_build,
        ),
        DebugTask(
            "shop_purchase",
            "商店自动购买",
            _shop_purchase,
            enabled=_shop_purchase_enabled,
            next_run_factory=_next_shop_purchase,
        ),
    ]
    return {task.key: task for task in tasks}


TASK_ALIASES = {
    "probe": "screen",
    "state": "screen",
    "疲劳": "fatigue_recovery",
    "fatigue": "fatigue_recovery",
    "trade": "run_business",
    "business": "run_business",
    "跑商": "run_business",
    "passenger": "passenger_build_monitor",
    "passenger-build": "passenger_build_monitor",
    "客厢": "passenger_build_monitor",
    "resident": "resident_activity",
    "rewards": "reward_collection",
    "shop": "shop_purchase",
    "store": "shop_purchase",
    "商店": "shop_purchase",
    "shop-probe": "shop_probe",
    "probe-shop": "shop_probe",
    "商店探测": "shop_probe",
    "shop-dry": "shop_dialog_probe",
    "shop-dialog": "shop_dialog_probe",
    "商店干跑": "shop_dialog_probe",
}


def resolve_task(name: str) -> DebugTask:
    registry = task_registry()
    key = TASK_ALIASES.get(str(name).strip(), str(name).strip())
    try:
        return registry[key]
    except KeyError as error:
        choices = ", ".join(registry)
        raise KeyError(f"未知后台任务 {name!r}；可用任务: {choices}") from error
