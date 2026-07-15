"""
Author: Night-stars-1 nujj1042633805@gmail.com
Date: 2024-04-05 17:14:29
LastEditTime: 2025-02-11 19:26:08
LastEditors: Night-stars-1 nujj1042633805@gmail.com
"""

import time
from typing import Any, Dict, Literal

from loguru import logger

from auto.module.strength import can_afford_fatigue, prepare_negotiation, read_strength
from auto.run_business.buy import buy_business
from auto.run_business.sell import (
    has_sellable_cargo,
    is_all_cargo_selected,
    is_sell_page,
    read_raise_percent,
    sell_business,
    sell_existing_cargo,
)
from core.control.control import connect, input_tap, screenshot
from core.control.control import is_stopped, stop as stop_control
from core.exception.exceptions import StopExecution
from core.model import app
from core.model.city_goods import RouteModel, RoutesModel
from core.module.bgr import BGR
from core.preset import click_station, get_station, go_outlets, wait_gbr
from core.preset.control import click, go_home
from core.preset.station import STATION
from core.services.screen_state import is_train_in_transit
from core.services.task_schedule_state import (
    task_result_deferred,
    task_result_succeeded,
)
from core.utils.utils import read_json, RESOURCES_PATH
from core.services.game_recovery import is_game_running, recover_game

_city_sell_data: Any = read_json(RESOURCES_PATH / "goods/CityGoodsSellData.json")
_city_tired_data: Dict[str, int] = read_json(RESOURCES_PATH / "goods/CityTiredData.json")
city_sell_data = {
    city: dict(sorted(goods.items(), key=lambda item: item[1]["price"], reverse=True))
    for city, goods in _city_sell_data.items()
}


def _prepare_max_sell_haggle():
    """Preserve an already completed 20% sell negotiation when resuming."""
    current_raise = read_raise_percent()
    if current_raise is not None and current_raise >= 20.0:
        logger.info("Current sell page is already at the 20% raise cap")
        return 2
    return prepare_negotiation("sell", 2)


def _read_route_city_from_current_screen(routes: RoutesModel):
    """Read one of the planned endpoint cities without leaving the sell page."""
    texts = [item["text"] for item in screenshot().ocr()]
    cities = _route_city_names(routes)
    return next(
        (city for city in cities if any(city in text for text in texts)),
        None,
    )


def _fatigue_deferral(reason: str, required_available: int):
    """Build a scheduler-safe deferral only for a verified fatigue shortage."""
    strength = read_strength()
    if strength is None:
        logger.error("Unable to verify fatigue shortage; keep the task failed")
        return None
    current, maximum = strength
    available = maximum - current
    logger.warning(
        f"Trading deferred: {reason}; available fatigue {available}, "
        f"required {required_available}"
    )
    return {
        "success": True,
        "deferred": True,
        "reason": reason,
        "current": current,
        "maximum": maximum,
        "available": available,
        "required_available": max(0, int(required_available)),
    }


def _clear_residual_cargo(residual_goods: list[str]):
    """Clear route cargo left by an interrupted run before restocking."""
    cargo_selected = is_all_cargo_selected()
    if cargo_selected:
        logger.info("All cargo is already selected with a positive quote; skip cargo-name scan")
    if not (cargo_selected or has_sellable_cargo(residual_goods)):
        return True

    sell_haggle = _prepare_max_sell_haggle()
    if sell_haggle == 0:
        # Preserve the cargo and the maximum-sale policy. Resource exhaustion
        # is an expected scheduling deferral, not a repair-worthy failure.
        return _fatigue_deferral(
            "insufficient_fatigue_for_residual_sale",
            required_available=80,
        ) or False
    if not sell_existing_cargo(sell_haggle, expected_goods=residual_goods):
        logger.error("Failed to clear residual cargo; stop before restocking")
        return False
    return True


def _route_city_names(routes: RoutesModel) -> set[str]:
    return {
        name
        for item in routes.city_data
        for name in (item.buy_city_name, item.sell_city_name)
    }


def _inspect_recovered_station():
    """Recreate the display controller before inspecting a restarted game."""
    if not connect():
        return None
    return get_station()


def _wait_for_verified_arrival(max_false_arrivals: int = 3) -> bool:
    """Wait for arrival and verify the train really stopped afterwards.

    ``STATION.wait`` uses legacy fixed pixels and can report arrival on cabin
    pages with similar colours. Never release the trading workflow until the
    top-level HUD independently confirms that transit markers disappeared.
    """
    for attempt in range(max_false_arrivals):
        # This is called only after a travel HUD was positively identified.
        # Open the route map and let the existing interception/arrival monitor
        # continue doing its job.
        input_tap((78, 38))
        time.sleep(1.0)
        if not STATION(True).wait():
            return False
        if not go_home():
            return False
        if not is_train_in_transit(screenshot().ocr()):
            logger.info("到站状态已二次确认，允许继续站点操作")
            return True
        logger.warning(
            f"行车监控第 {attempt + 1}/{max_false_arrivals} 次返回后列车仍在行驶，"
            "忽略固定像素误判并继续等待"
        )
    logger.error("行车监控连续误判到站，安全停止本轮跑商")
    return False


def _normalize_trade_startup_screen() -> bool:
    """Resolve one startup state before any station-only side operation.

    A restarted GUI may attach while the train is still moving. In that case
    resume the existing travel monitor instead of repeatedly navigating home,
    opening station-only menus, and misreading map artwork as a city name.
    """
    for pass_index in range(2):
        image = screenshot()
        if is_train_in_transit(image.ocr()):
            logger.info("启动状态：检测到列车行驶中，恢复行车监控并等待到站")
            return _wait_for_verified_arrival()
        if pass_index == 0 and not go_home():
            return False
    logger.info("启动状态：已确认处于站点主界面")
    return True


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


def _is_exchange_lobby(image=None) -> bool:
    """Recognize the exchange action menu before attempting city navigation."""
    image = image or screenshot()
    texts = [item["text"] for item in image.ocr()]
    markers = ("我要买", "我要卖", "交易品投资", "私人仓库")
    return sum(any(marker in text for text in texts) for marker in markers) >= 2


def go_business(type: Literal["buy", "sell"] = "buy"):
    if is_train_in_transit(screenshot().ocr()):
        logger.error("安全门禁：列车仍在行驶，拒绝执行前往交易所")
        return False
    if type == "sell" and is_sell_page():
        logger.info("已在交易所卖货页，直接复用当前页面")
        return True
    logger.info("前往交易所")
    if _is_exchange_lobby():
        logger.info("已在交易所入口菜单，跳过城市寻路")
        is_join = True
    else:
        result = go_outlets("交易所")
        is_join = bool(result) and wait_gbr(
            pos=(286, 35),
            min_gbr=BGR(250, 250, 250),
            max_gbr=BGR(255, 255, 255),
            cropped_pos1=(242, 11),
            cropped_pos2=(414, 66),
        )
    if is_join:
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
            expected_cities=_route_city_names(routes),
        )
        if not state:
            return False
        # Reconnect the normal controller after Android recreated the display.
        if not connect():
            return False
    resume_sell_page = is_sell_page()
    if resume_sell_page:
        logger.info("Sell-page recovery state detected; preserve it before all side tasks")
        city_name = _read_route_city_from_current_screen(routes)
    else:
        if not _normalize_trade_startup_screen():
            return False
        city_name = get_station()
    if not city_name:
        logger.error("无法确定当前城市，已安全停止而非抛出异常")
        return False
    expected_cities = _route_city_names(routes)
    if city_name not in expected_cities:
        first_buy_city = routes.city_data[0].buy_city_name
        logger.warning(
            f"安全门禁：识别到非路线城市 {city_name!r}，预期为 {sorted(expected_cities)}；"
            f"先前往计划买货城市 {first_buy_city!r}，到站后再执行清仓预检"
        )
        if is_train_in_transit(screenshot().ocr()):
            logger.error("安全门禁：城市识别后仍检测到行驶状态，拒绝重新规划路线")
            return False
        if not click_station(first_buy_city, cur_station=city_name).wait():
            logger.error(f"无法从路线外城市到达买货城市 {first_buy_city}，停止本次跑商")
            return False
        city_name = first_buy_city
    if is_train_in_transit(screenshot().ocr()):
        logger.error("安全门禁：城市识别后仍检测到行驶状态，拒绝执行清仓预检")
        return False
    if routes.city_data[0].sell_city_name == city_name:
        routes.city_data = [routes.city_data[1], routes.city_data[0]]
    # Interrupted runs can leave cargo from the other city in the warehouse.
    # Inspect the exchange sell page before buying, clear what is sellable in
    # the current city, then restock and depart as usual.
    logger.info(
        f"Preflight warehouse check in {city_name}: "
        "sell residual cargo before restocking"
    )
    if resume_sell_page or is_sell_page():
        logger.info("Already on the exchange sell page; resume the current sale state")
    elif not go_business("sell"):
        return False
    residual_route = next(
        (item for item in routes.city_data if item.sell_city_name == city_name),
        None,
    )
    residual_goods = list(residual_route.goods_data) if residual_route else []
    residual_result = _clear_residual_cargo(residual_goods)
    if task_result_deferred(residual_result):
        return residual_result
    if not residual_result:
        return False
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
            return _fatigue_deferral(
                "insufficient_fatigue_for_route",
                required_available=travel_cost,
            ) or False
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
        # Selling profit is always maximized: pursue the game's two-success cap
        # regardless of the per-city buy-side haggle setting.
        sell_haggle = _prepare_max_sell_haggle()
        if sell_haggle == 0:
            return _fatigue_deferral(
                "insufficient_fatigue_for_endpoint_sale",
                required_available=80,
            ) or False
        if not sell_business(
            sell_haggle,
            expected_goods=list(city.goods_data),
        ):
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
                expected_cities=_route_city_names(routes),
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
        result = run_with_recovery(routes)
        if task_result_deferred(result):
            return result
        if not task_result_succeeded(result) or is_stopped():
            logger.warning(f"端点跑商未完成，已完成 {i}/{count} 轮")
            return False
    return True


def two_city_weekly_run(
    buy_city_name: str,
    sell_city_name: str,
    execution_batches: list[dict],
    max_runs: int | None = None,
):
    """Execute complete round trips, optionally yielding after a safe run boundary.

    A scheduled weekly plan may contain many round trips.  Limiting one worker
    invocation to a small number of *complete* trips lets higher-priority tasks
    that became due meanwhile run before the next trip, without ever stopping a
    train halfway through a leg or leaving a sale unfinished.
    """
    from core.services import record_completed_run

    global STOP
    STOP = False
    buy_haggle_num = app.CityHaggle[buy_city_name]
    sell_haggle_num = app.CityHaggle[sell_city_name]
    total_runs = sum(int(batch["runs"]) for batch in execution_batches)
    run_limit = total_runs if max_runs is None else min(total_runs, max(1, int(max_runs)))
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
            result = run_with_recovery(routes)
            if task_result_deferred(result):
                logger.info(
                    f"周计划资源暂缓，本次已完成 {completed}/{total_runs} 次完整往返"
                )
                return result
            if not task_result_succeeded(result) or is_stopped():
                logger.info(f"周计划停止，本次已完成 {completed}/{total_runs} 次完整往返")
                return False
            completed += 1
            record_completed_run(books)
            if completed >= run_limit and completed < total_runs:
                logger.info(
                    f"本次调度已在完整往返边界让出队列，完成 {completed}/{total_runs} 次；"
                    "剩余计划将在下一次调度继续"
                )
                return True
    logger.info(f"周计划完成，共 {completed} 次完整往返")
    return True


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
        roll_weekly_plan_forward,
        save_weekly_plan,
    )

    state = load_weekly_plan()
    if not state:
        state = roll_weekly_plan_forward()
        if state:
            logger.info(
                f"检测到新一周，已沿用路线 {state['cycle'][0]} → "
                f"{state['cycle'][1]} 并重置本周进度"
            )
    summary = progress_summary(state)
    if not state or not summary or summary["finished"]:
        logger.info("没有待执行的本周跑商计划")
        return bool(state and summary and summary["finished"])
    fallback = int(cfg.InventoryBooks.value)
    sell_resume = is_sell_page()
    if sell_resume:
        logger.info("当前处于卖货中间态，跳过进货书背包扫描以保留议价幅度")
        actual = None
    else:
        actual = read_restock_book_count() if bool(cfg.AutoReadInventoryBooks.value) else None
        if bool(cfg.AutoReadInventoryBooks.value) and actual is None:
            logger.error(
                "已开启自动读取进货书，但本次未能确认真实数量；"
                "停止本次跑商，避免把识别失败误当成 0 本"
            )
            return False
    available = fallback if actual is None else actual
    if actual is not None:
        from qfluentwidgets import qconfig
        qconfig.set(cfg.InventoryBooks, actual)
    required = int(summary["remaining_books"])
    needs_reoptimization = bool(state.get("needs_reoptimization")) and not sell_resume
    if available >= required and not needs_reoptimization:
        logger.info(f"进货书库存 {available} 本，足够完成剩余计划（需要 {required} 本）")
        cycle = state["cycle"]
        return two_city_weekly_run(cycle[0], cycle[1], remaining_batches(state), max_runs=1)

    if needs_reoptimization:
        logger.info(
            f"新周进货书库存已确认：{available} 本；"
            "正在按真实库存重新计算剩余计划"
        )
    else:
        logger.warning(
            f"进货书库存仅 {available} 本，少于剩余计划需要的 {required} 本，"
            "重新计算实时替代路线"
        )
    raw_config = dict(state.get("optimizer_config") or {})
    raw_config["books"] = max(0, available)
    raw_config["weekly_fatigue"] = max(1, int(summary["remaining_fatigue"]))
    try:
        replacement = optimize_live_routes(OptimizationConfig(**raw_config))
        state = save_weekly_plan(replacement)
        cycle = state["cycle"]
        logger.info(f"已切换替代路线: {cycle[0]} → {cycle[1]} → {cycle[0]}，计划使用 {replacement['books_used']} 本")
        return two_city_weekly_run(cycle[0], cycle[1], remaining_batches(state), max_runs=1)
    except StopExecution:
        raise
    except Exception:
        logger.exception("科伦巴实时替代路线计算失败，降级为原路线不使用进货书")
        safe_batches = [{"runs": summary["remaining_runs"], "books": {city: 0 for city in state["cycle"]}}]
        return two_city_weekly_run(
            state["cycle"][0],
            state["cycle"][1],
            safe_batches,
            max_runs=1,
        )


def stop():
    """
    说明:
        停止运行
    """
    global STOP
    STOP = True
    stop_control()
