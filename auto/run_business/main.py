"""
Author: Night-stars-1 nujj1042633805@gmail.com
Date: 2024-04-05 17:14:29
LastEditTime: 2025-02-11 19:26:08
LastEditors: Night-stars-1 nujj1042633805@gmail.com
"""

import time
from typing import Any, Dict, Literal

from loguru import logger

from auto.module.strength import can_afford_fatigue, prepare_negotiation
from auto.module.dispatch import collect_dispatch_rewards
from auto.run_business.buy import buy_business
from auto.run_business.sell import sell_business
from core.control.control import connect, input_tap, screenshot
from core.control.control import is_stopped, stop as stop_control
from core.exception.exceptions import StopExecution
from core.model import app
from core.model.city_goods import RouteModel, RoutesModel
from core.module.bgr import BGR
from core.preset import click_station, get_station, go_outlets, wait_gbr
from core.preset.control import click
from core.utils.utils import read_json, RESOURCES_PATH
from core.services.game_recovery import is_game_running, recover_game

_city_sell_data: Any = read_json(RESOURCES_PATH / "goods/CityGoodsSellData.json")
_city_tired_data: Dict[str, int] = read_json(RESOURCES_PATH / "goods/CityTiredData.json")
city_sell_data = {
    city: dict(sorted(goods.items(), key=lambda item: item[1]["price"], reverse=True))
    for city, goods in _city_sell_data.items()
}


def _inspect_recovered_station():
    """Recreate the display controller before inspecting a restarted game."""
    if not connect():
        return None
    return get_station()


def show(routes: RoutesModel):
    route = routes.city_data
    message = f"""{route[0].buy_city_name}<->{route[0].sell_city_name}:
{route[0].buy_city_name}:
    商品顺序: {"->".join(route[0].goods_data.keys())}
    议价次数: {route[0].haggle_num}
    书本数量: {route[0].book}
{route[0].sell_city_name}:
    商品顺序: {"->".join(route[1].goods_data.keys())}
    议价次数: {route[1].haggle_num}
    书本数量: {route[1].book}"""

    return message


def go_business(type: Literal["buy", "sell"] = "buy"):
    logger.info("前往交易所")
    result = go_outlets("交易所")
    is_join = wait_gbr(
        pos=(286, 35),
        min_gbr=BGR(250, 250, 250),
        max_gbr=BGR(255, 255, 255),
        cropped_pos1=(242, 11),
        cropped_pos2=(414, 66),
    )
    if result and is_join:
        if type == "buy":
            input_tap((927, 321))
        elif type == "sell":
            input_tap((932, 404))
        time.sleep(1.0)
        bgr = screenshot().get_bgr((1175, 460))
        logger.debug(f"进入交易所颜色检查: {bgr}")
        if (
            BGR(0, 123, 240) <= bgr <= BGR(2, 133, 255)
            or BGR(225, 225, 225) == bgr
            or BGR(0, 170, 240) <= bgr <= BGR(5, 185, 255)
        ):
            return True
        else:
            logger.error("进入交易所失败")
            return False
    else:
        logger.error("进入交易所失败")
        return False


def run(routes: RoutesModel, recovery_attempts: int = 2):
    logger.info(show(routes))
    status = connect()
    if not status:
        logger.error("ADB连接失败")
        return False
    try:
        game_running = is_game_running()
    except Exception as exc:
        logger.warning(f"无法检查游戏进程，继续使用画面识别: {exc}")
        game_running = True
    if not game_running:
        if recovery_attempts <= 0:
            logger.error("游戏仍未运行，恢复次数已用尽")
            return False
        state = recover_game(
            inspect_station=_inspect_recovered_station,
            expected_cities={item.buy_city_name for item in routes.city_data},
        )
        if not state:
            return False
        # Reconnect the normal controller after Android recreated the display.
        if not connect():
            return False
    # Dispatch rewards are opportunistic: no reminder means a fast no-op and
    # recognition failure must never block the trading route.
    try:
        collect_dispatch_rewards()
    except StopExecution:
        raise
    except Exception:
        logger.exception("自动领取委派奖励失败，跳过并继续跑商")
    city_name = get_station()
    if not city_name:
        logger.error("无法确定当前城市，已安全停止而非抛出异常")
        return False
    if routes.city_data[0].sell_city_name == city_name:
        routes.city_data = [routes.city_data[1], routes.city_data[0]]
    for city in routes.city_data:
        logger.info(f"{city.buy_city_name}->{city.sell_city_name}")
        if not click_station(city.buy_city_name, cur_station=city_name).wait():
            logger.error(f"无法到达买货城市 {city.buy_city_name}，停止本次跑商")
            return False
        if not go_business("buy"):
            return False
        buy_haggle = prepare_negotiation("buy", min(city.haggle_num, 2))
        travel_cost = int(
            _city_tired_data.get(f"{city.buy_city_name}-{city.sell_city_name}", 0)
        )
        if buy_haggle == 0 and not can_afford_fatigue(travel_cost):
            logger.warning(
                "恢复资源已用完，剩余疲劳不足以到达下一城市，本轮不进货并暂停"
            )
            return False
        goods_data = list(city.goods_data.keys())
        buy_business(
            goods_data[:1],
            goods_data[1:],
            buy_haggle,
            max_book=city.book,
        )
        if not click_station(city.sell_city_name, cur_station=city_name).wait():
            logger.error(f"无法到达卖货城市 {city.sell_city_name}，停止本次跑商")
            return False
        if not go_business("sell"):
            return False
        sell_haggle = prepare_negotiation("sell", min(city.haggle_num, 2))
        if sell_haggle == 0:
            logger.warning("疲劳不足，本次不抬价，直接卖出以保证货物结算")
        if not sell_business(sell_haggle):
            logger.error("卖货未完成，不将本轮记为完成")
            return False
        # 流程跑完，更改站点名称为当前出售商品的站点
        city_name = city.sell_city_name
    logger.info("运行完成")
    return True


def run_with_recovery(routes: RoutesModel, recovery_attempts: int = 2):
    """Run one round, restarting a crashed client and re-checking its station."""
    for attempt in range(recovery_attempts + 1):
        try:
            return run(routes, recovery_attempts=recovery_attempts - attempt)
        except Exception:
            if is_stopped():
                raise
            logger.exception("跑商操作中断，检查游戏是否意外退出")
            try:
                running = is_game_running()
            except Exception:
                running = True
            if running or attempt >= recovery_attempts:
                logger.error("游戏仍在运行或恢复次数已用尽，安全停止本轮")
                return False
            state = recover_game(
                inspect_station=_inspect_recovered_station,
                expected_cities={item.buy_city_name for item in routes.city_data},
            )
            if not state:
                return False
            logger.info(f"已确认当前城市 {state.city}，重新核对路线后继续本轮")
    return False


def two_city_run(buy_city_name: str, sell_city_name: str):
    global STOP
    STOP = False
    count = app.RunBuy.BuyCount
    buy_haggle_num = app.CityHaggle[buy_city_name]
    sell_haggle_num = app.CityHaggle[sell_city_name]
    buy_book_num = app.CityBook[buy_city_name]
    sell_book_num = app.CityBook[sell_city_name]
    routes = RoutesModel(
        city_data=[
            RouteModel(
                buy_city_name=buy_city_name,
                sell_city_name=sell_city_name,
                haggle_num=buy_haggle_num,
                book=buy_book_num,
                goods_data=city_sell_data[buy_city_name],
            ),
            RouteModel(
                buy_city_name=sell_city_name,
                sell_city_name=buy_city_name,
                haggle_num=sell_haggle_num,
                book=sell_book_num,
                goods_data=city_sell_data[sell_city_name],
            ),
        ],
    )
    logger.info(f"准备运行端点跑商，运行次数: {count}")
    for i in range(count):
        if not run_with_recovery(routes) or is_stopped():
            break


def two_city_weekly_run(buy_city_name: str, sell_city_name: str, execution_batches: list[dict]):
    """Execute an optimizer plan and change restock-book counts between batches."""
    from core.services import record_completed_run

    global STOP
    STOP = False
    buy_haggle_num = app.CityHaggle[buy_city_name]
    sell_haggle_num = app.CityHaggle[sell_city_name]
    total_runs = sum(int(batch["runs"]) for batch in execution_batches)
    logger.info(f"准备运行周计划，共 {total_runs} 次完整往返，{len(execution_batches)} 个阶段")
    completed = 0
    for batch_index, batch in enumerate(execution_batches, start=1):
        books = batch.get("books", {})
        batch_runs = int(batch.get("runs", 0))
        logger.info(f"周计划阶段 {batch_index}/{len(execution_batches)}: {batch_runs} 次完整往返，进货书 {books}")
        for _ in range(batch_runs):
            routes = RoutesModel(
                city_data=[
                    RouteModel(
                        buy_city_name=buy_city_name,
                        sell_city_name=sell_city_name,
                        haggle_num=buy_haggle_num,
                        book=int(books.get(buy_city_name, 0)),
                        goods_data=city_sell_data[buy_city_name],
                    ),
                    RouteModel(
                        buy_city_name=sell_city_name,
                        sell_city_name=buy_city_name,
                        haggle_num=sell_haggle_num,
                        book=int(books.get(sell_city_name, 0)),
                        goods_data=city_sell_data[sell_city_name],
                    ),
                ]
            )
            if not run_with_recovery(routes) or is_stopped():
                logger.info(f"周计划停止，本次已完成 {completed}/{total_runs} 次完整往返")
                return
            completed += 1
            record_completed_run(books)
    logger.info(f"周计划完成，共 {completed} 次完整往返")


def adaptive_weekly_run():
    """Verify actual books, then keep or replace the remaining live-price plan."""
    from app.common.config import cfg
    from auto.inventory import read_restock_book_count
    from core.services import (
        OptimizationConfig,
        load_weekly_plan,
        optimize_live_routes,
        progress_summary,
        remaining_batches,
        save_weekly_plan,
    )

    state = load_weekly_plan()
    summary = progress_summary(state)
    if not state or not summary or summary["finished"]:
        logger.info("没有待执行的本周跑商计划")
        return
    fallback = int(cfg.InventoryBooks.value)
    actual = read_restock_book_count() if bool(cfg.AutoReadInventoryBooks.value) else None
    available = fallback if actual is None else actual
    if actual is not None:
        from qfluentwidgets import qconfig
        qconfig.set(cfg.InventoryBooks, actual)
    required = int(summary["remaining_books"])
    if available >= required:
        logger.info(f"进货书库存 {available} 本，足够完成剩余计划（需要 {required} 本）")
        cycle = state["cycle"]
        return two_city_weekly_run(cycle[0], cycle[1], remaining_batches(state))

    logger.warning(f"进货书库存仅 {available} 本，少于剩余计划需要的 {required} 本，重新计算实时替代路线")
    raw_config = dict(state.get("optimizer_config") or {})
    raw_config["books"] = max(0, available)
    raw_config["weekly_fatigue"] = max(1, int(summary["remaining_fatigue"]))
    try:
        replacement = optimize_live_routes(OptimizationConfig(**raw_config))
        state = save_weekly_plan(replacement)
        cycle = state["cycle"]
        logger.info(f"已切换替代路线: {cycle[0]} → {cycle[1]} → {cycle[0]}，计划使用 {replacement['books_used']} 本")
        return two_city_weekly_run(cycle[0], cycle[1], remaining_batches(state))
    except StopExecution:
        raise
    except Exception:
        logger.exception("科伦巴实时替代路线计算失败，降级为原路线不使用进货书")
        safe_batches = [{"runs": summary["remaining_runs"], "books": {city: 0 for city in state["cycle"]}}]
        return two_city_weekly_run(state["cycle"][0], state["cycle"][1], safe_batches)


def stop():
    """
    说明:
        停止运行
    """
    global STOP
    STOP = True
    stop_control()
