"""
Author: Night-stars-1 nujj1042633805@gmail.com
Date: 2024-04-05 17:14:29
LastEditTime: 2025-02-11 19:26:08
LastEditors: Night-stars-1 nujj1042633805@gmail.com
"""

import time
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Dict, Literal

from loguru import logger

from auto import exchange_navigation

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
from core.services.read_only_policy import ActionIntent
from core.services.server_calendar import SERVER_CLOCK
from core.services.task_schedule_state import (
    task_result_deferred,
    task_result_succeeded,
)
from core.utils.utils import read_json, RESOURCES_PATH
from core.services.game_recovery import is_game_running, recover_game
from core.services.station_availability import unavailable_stations

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
    next_run = SERVER_CLOCK.server_now() + timedelta(minutes=6)
    return {
        "success": True,
        "deferred": True,
        "reason": reason,
        "current": current,
        "maximum": maximum,
        "available": available,
        "required_available": max(0, int(required_available)),
        "next_run_at": next_run.isoformat(timespec="seconds"),
    }


def _route_availability_deferral(*cities: str):
    unavailable = unavailable_stations(cities)
    if not unavailable:
        return None
    logger.warning(
        f"跑商路线包含当前未开放站点: {', '.join(unavailable)}；"
        "停止地图操作并等待路线重新规划"
    )
    return {
        "success": True,
        "deferred": True,
        "reason": "route_station_unavailable",
        "stations": unavailable,
        "next_run_at": (
            SERVER_CLOCK.server_now() + timedelta(minutes=30)
        ).isoformat(timespec="seconds"),
    }


def _departure_wait_deferral(cycle_id: str) -> dict[str, object]:
    """Use bounded short polling only while a confirmed train is in flight."""

    return {
        "success": True,
        "deferred": True,
        "progress_made": False,
        "reason": "departure_confirmed_waiting_for_arrival",
        "cycle_id": cycle_id,
        "next_run_at": (
            SERVER_CLOCK.server_now() + timedelta(seconds=45)
        ).isoformat(timespec="seconds"),
    }


def _stale_price_deferral(reason: str = "stale_price_reoptimization_failed") -> dict[str, object]:
    return {
        "success": True,
        "deferred": True,
        "progress_made": False,
        "reason": reason,
        "next_run_at": (
            SERVER_CLOCK.server_now() + timedelta(minutes=15)
        ).isoformat(timespec="seconds"),
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
        input_tap(
            (78, 38),
            intent=ActionIntent(
                "back", "transit_hud", "top_left_route", (78, 38),
                correlation_id="business:transit:route-monitor",
            ),
        )
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
    action = (
        exchange_navigation.ExchangeAction.BUY
        if type == "buy"
        else exchange_navigation.ExchangeAction.SELL
    )
    return exchange_navigation.open_exchange_action(action, read_only=False).success


def _record_ledger_event(
    context: dict | None,
    event_type: str,
    *,
    origin: str,
    destination: str,
    leg_id: str,
    operation_sequence: int = 0,
    purchase_book_delta: int = 0,
    fatigue_delta: int = 0,
    profit_delta: int = 0,
    observed_at=None,
) -> bool:
    if context is None:
        return
    from core.services.trade_ledger import (
        LEDGER_PATH,
        TradeEvent,
        TradeEventType,
        append_trade_event,
        stable_trade_event_id,
    )
    from core.services.server_calendar import SERVER_CLOCK

    event_kind = TradeEventType(event_type)
    when = observed_at or SERVER_CLOCK.server_now()
    return append_trade_event(
        context.get("ledger_path", LEDGER_PATH),
        TradeEvent(
            event_id=stable_trade_event_id(
                context["server_week_id"],
                context["route_id"],
                context["cycle_id"],
                leg_id,
                event_kind,
                operation_sequence,
            ),
            server_week_id=context["server_week_id"],
            route_id=context["route_id"],
            cycle_id=context["cycle_id"],
            leg_id=leg_id,
            event_type=event_kind,
            origin=origin,
            destination=destination,
            observed_at=when,
            confirmed_by="GAME_OBSERVED",
            operation_sequence=max(0, int(operation_sequence)),
            purchase_book_delta=max(0, int(purchase_book_delta)),
            fatigue_delta=max(0, int(fatigue_delta)),
            profit_delta=int(profit_delta),
        ),
    )


def resume_action_for_leg(context: dict | None, leg_id: str) -> str:
    """Return the first safe action after replaying one leg's confirmed facts."""

    if context is None:
        return "START"
    from core.services.trade_ledger import LEDGER_PATH, load_trade_cycle_state

    state = load_trade_cycle_state(
        context.get("ledger_path", LEDGER_PATH), context["cycle_id"]
    )
    phases = {
        str(item.get("event_type", ""))
        for item in state.events
        if str(item.get("leg_id", "")) == leg_id
    }
    if "LEG_COMPLETED" in phases:
        return "COMPLETE"
    if "SALE_CONFIRMED" in phases:
        return "FINALIZE_LEG"
    if "ARRIVAL_CONFIRMED" in phases:
        return "SELL"
    if "DEPARTURE_CONFIRMED" in phases:
        return "WAIT_ARRIVAL"
    if "DEPARTURE_REQUESTED" in phases:
        return "DEPART"
    if "PURCHASE_CONFIRMED" in phases:
        return "DEPART"
    if phases & {"BOOK_USE_CONFIRMED", "PURCHASE_BOOK_CONFIRMED", "LEG_STARTED", "LEG_PLANNED"}:
        return "PURCHASE"
    return "START"


@dataclass(frozen=True)
class ActiveLegRecovery:
    cycle_id: str
    leg_id: str
    origin: str
    destination: str
    phase: str
    current_city: str
    next_action: str
    server_week_id: str


def active_leg_recovery(
    context: dict | None,
    current_city: str,
) -> ActiveLegRecovery | None:
    """Resolve the ledger-owned active leg before any city-driven reordering."""

    if context is None:
        return None
    from core.services.trade_ledger import LEDGER_PATH, load_trade_cycle_state

    state = load_trade_cycle_state(
        context.get("ledger_path", LEDGER_PATH), context["cycle_id"]
    )
    if not state.events or state.phase == "CYCLE_COMPLETED" or not state.current_leg_id:
        return None
    leg_events = [
        item
        for item in state.events
        if str(item.get("leg_id", "")) == state.current_leg_id
    ]
    if not leg_events:
        return None
    latest = leg_events[-1]
    origin = str(latest.get("origin", ""))
    destination = str(latest.get("destination", ""))
    phase = state.current_leg_phase
    action = resume_action_for_leg(context, state.current_leg_id)
    if phase in {"DEPARTURE_REQUESTED", "DEPARTURE_CONFIRMED"} and current_city == destination:
        action = "ARRIVE_AND_SELL"
    elif phase == "DEPARTURE_REQUESTED" and current_city == origin:
        action = "RETRY_DEPARTURE"
    elif phase == "DEPARTURE_CONFIRMED":
        action = "WAIT_ARRIVAL"
    return ActiveLegRecovery(
        cycle_id=state.cycle_id,
        leg_id=state.current_leg_id,
        origin=origin,
        destination=destination,
        phase=phase,
        current_city=current_city,
        next_action=action,
        server_week_id=state.server_week_id,
    )


def should_issue_departure(
    resume_action: str,
    current_city: str,
    destination: str,
) -> bool:
    """Prevent a replay from issuing an already confirmed departure."""

    return resume_action != "WAIT_ARRIVAL" and current_city != destination


def _begin_departure(
    ledger_context: dict | None,
    *,
    origin: str,
    destination: str,
    leg_id: str,
):
    """Record click intent separately from stable observed travel state."""

    def departure_requested():
        _record_ledger_event(
            ledger_context,
            "DEPARTURE_REQUESTED",
            origin=origin,
            destination=destination,
            leg_id=leg_id,
        )

    travel = click_station(
        destination,
        cur_station=origin,
        on_departure_requested=departure_requested,
    )
    if travel:
        # click_station becomes truthy only after a stable driving frame has
        # been observed; a click alone is only DEPARTURE_REQUESTED.
        _record_ledger_event(
            ledger_context,
            "DEPARTURE_CONFIRMED",
            origin=origin,
            destination=destination,
            leg_id=leg_id,
        )
    return travel


def _cycle_books_used(context: dict | None) -> int:
    if context is None:
        return 0
    from core.services.trade_ledger import LEDGER_PATH, load_trade_cycle_state

    return load_trade_cycle_state(
        context.get("ledger_path", LEDGER_PATH), context["cycle_id"]
    ).purchase_books_used


def _leg_books_used(context: dict | None, leg_id: str) -> int:
    if context is None:
        return 0
    from core.services.trade_ledger import BOOK_EVENT_TYPES, LEDGER_PATH, load_trade_cycle_state

    state = load_trade_cycle_state(
        context.get("ledger_path", LEDGER_PATH), context["cycle_id"]
    )
    return sum(
        max(0, int(item.get("purchase_book_delta", 0)))
        for item in state.events
        if item.get("leg_id") == leg_id and item.get("event_type") in BOOK_EVENT_TYPES
    )


def _revalidate_purchase_guard(
    context: dict | None,
    city: RouteModel,
    *,
    confirmed_books: int,
) -> bool | dict[str, object]:
    """Run the latest price/resource guard immediately before irreversible buy."""

    if context is None:
        return True
    validator = context.get("purchase_validator")
    if not callable(validator):
        return (
            _stale_price_deferral("purchase_guard_missing")
            if context.get("require_purchase_guard") is True
            else True
        )
    try:
        result = validator(
            city,
            confirmed_books=max(0, int(confirmed_books)),
            observed_at=SERVER_CLOCK.server_now(),
        )
    except StopExecution:
        raise
    except Exception as error:
        logger.exception("不可逆购买前复核异常，阻止点击购买")
        return _stale_price_deferral(f"purchase_guard_error:{type(error).__name__}")
    if result is True:
        return True
    if isinstance(result, dict):
        return result
    return _stale_price_deferral("purchase_guard_rejected")


def run(
    routes: RoutesModel,
    recovery_attempts: int = 2,
    ledger_context: dict | None = None,
):
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
    recovery = active_leg_recovery(ledger_context, city_name)
    if recovery is not None:
        logger.info(
            "Active leg recovery: cycle={cycle} leg={leg} phase={phase} "
            "city={city} next={action}".format(
                cycle=recovery.cycle_id,
                leg=recovery.leg_id,
                phase=recovery.phase,
                city=recovery.current_city,
                action=recovery.next_action,
            )
        )
        if city_name not in {recovery.origin, recovery.destination}:
            logger.error(
                f"账本 active leg {recovery.leg_id} 尚未收敛，但当前城市 {city_name!r} "
                f"不在其两端 {recovery.origin!r}/{recovery.destination!r}；拒绝通用导航或清仓"
            )
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
    in_flight_cycle = False
    if ledger_context is not None:
        from core.services.trade_ledger import LEDGER_PATH, load_trade_cycle_state

        cycle_state = load_trade_cycle_state(
            ledger_context.get("ledger_path", LEDGER_PATH),
            ledger_context["cycle_id"],
        )
        in_flight_cycle = bool(cycle_state.events) and cycle_state.phase != "CYCLE_COMPLETED"
    # Interrupted runs can leave cargo from the other city in the warehouse.
    # Inspect the exchange sell page before buying, clear what is sellable in
    # the current city, then restock and depart as usual.
    if in_flight_cycle:
        logger.info("检测到有事务事件的未完成周期，跳过清仓预检并按阶段恢复")
    else:
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
    route_items = list(routes.city_data)
    if recovery is not None:
        active_route = next(
            (
                item
                for item in route_items
                if item.buy_city_name == recovery.origin
                and item.sell_city_name == recovery.destination
            ),
            None,
        )
        if active_route is None:
            logger.error(
                f"账本 active leg {recovery.leg_id} 不在当前路线中，拒绝重排或买入"
            )
            return False
        route_items = [active_route] + [
            item for item in route_items if item is not active_route
        ]
    elif route_items and route_items[0].sell_city_name == city_name:
        route_items = list(reversed(route_items))
    if (
        recovery is None
        and ledger_context
        and int(ledger_context.get("completed_legs", 0)) == 1
    ):
        origin = ledger_context.get("origin")
        resume = [
            item
            for item in route_items
            if item.buy_city_name == city_name and item.sell_city_name == origin
        ]
        if resume:
            route_items = resume
            logger.info(
                f"从账本恢复部分往返 {ledger_context['cycle_id']}："
                f"仅继续 {city_name} → {origin}"
            )
    _record_ledger_event(
        ledger_context,
        "CYCLE_STARTED",
        origin=ledger_context.get("origin", city_name) if ledger_context else city_name,
        destination=ledger_context.get("origin", city_name) if ledger_context else city_name,
        leg_id="",
    )
    for city in route_items:
        logger.info(f"{city.buy_city_name}->{city.sell_city_name}")
        leg_id = f"{city.buy_city_name}|{city.sell_city_name}"
        resume_action = resume_action_for_leg(ledger_context, leg_id)
        if resume_action == "COMPLETE":
            logger.info(f"账本已确认腿 {leg_id} 完成，跳过重放")
            city_name = city.sell_city_name
            continue
        if resume_action == "FINALIZE_LEG":
            _record_ledger_event(
                ledger_context,
                "LEG_COMPLETED",
                origin=city.buy_city_name,
                destination=city.sell_city_name,
                leg_id=leg_id,
            )
            from core.services.fatigue_triggers import notify_fatigue_event

            notify_fatigue_event("leg_completed", city.sell_city_name)
            from core.services.fatigue_triggers import fatigue_checkpoint_deferral

            checkpoint = fatigue_checkpoint_deferral(city.sell_city_name)
            if checkpoint is not None:
                checkpoint["checkpoint_boundary"] = "POST_SALE"
                return checkpoint
            city_name = city.sell_city_name
            continue
        if resume_action == "START":
            _record_ledger_event(
                ledger_context,
                "LEG_PLANNED",
                origin=city.buy_city_name,
                destination=city.sell_city_name,
                leg_id=leg_id,
            )
            _record_ledger_event(
                ledger_context,
                "LEG_STARTED",
                origin=city.buy_city_name,
                destination=city.sell_city_name,
                leg_id=leg_id,
            )
        travel_cost = int(
            _city_tired_data.get(f"{city.buy_city_name}-{city.sell_city_name}", 0)
        )
        if resume_action in {"START", "PURCHASE"}:
            if city_name != city.buy_city_name:
                if not click_station(city.buy_city_name, cur_station=city_name).wait():
                    logger.error(f"无法到达买货城市 {city.buy_city_name}，停止本次跑商")
                    return False
                city_name = city.buy_city_name
            if not go_business("buy"):
                return False
            buy_haggle = prepare_negotiation("buy", min(city.haggle_num, 2))
            if buy_haggle == 0 and not can_afford_fatigue(travel_cost):
                logger.warning(
                    "恢复资源已用完，剩余疲劳不足以到达下一城市，本轮不进货并暂停"
                )
                return _fatigue_deferral(
                    "insufficient_fatigue_for_route",
                    required_available=travel_cost,
                ) or False
            confirmed_before = _leg_books_used(ledger_context, leg_id)
            guard_result = _revalidate_purchase_guard(
                ledger_context, city, confirmed_books=confirmed_before
            )
            if guard_result is not True:
                return guard_result

            def book_committed(sequence: int):
                _record_ledger_event(
                    ledger_context,
                    "BOOK_USE_CONFIRMED",
                    origin=city.buy_city_name,
                    destination=city.sell_city_name,
                    leg_id=leg_id,
                    operation_sequence=sequence,
                    purchase_book_delta=1,
                )

            def purchase_committed():
                _record_ledger_event(
                    ledger_context,
                    "PURCHASE_CONFIRMED",
                    origin=city.buy_city_name,
                    destination=city.sell_city_name,
                    leg_id=leg_id,
                )

            goods_data = list(city.goods_data.keys())
            buy_result = buy_business(
                goods_data[:1],
                goods_data[1:],
                buy_haggle,
                max_book=city.book,
                detailed=ledger_context is not None,
                confirmed_books=confirmed_before,
                on_book_confirmed=book_committed,
                on_purchase_confirmed=purchase_committed,
            )
            if not buy_result:
                return False
            # Full-cargo verification is also a confirmed purchase outcome and
            # does not invoke the click callback; the stable event deduplicates.
            _record_ledger_event(
                ledger_context,
                "PURCHASE_CONFIRMED",
                origin=city.buy_city_name,
                destination=city.sell_city_name,
                leg_id=leg_id,
            )
        if resume_action in {"START", "PURCHASE", "DEPART", "WAIT_ARRIVAL"}:
            before_travel = None
            after_travel = None
            if should_issue_departure(
                resume_action, city_name, city.sell_city_name
            ):
                before_travel = read_strength()
                travel = _begin_departure(
                    ledger_context,
                    origin=city.buy_city_name,
                    destination=city.sell_city_name,
                    leg_id=leg_id,
                )
                if not travel.wait():
                    logger.error(f"无法到达卖货城市 {city.sell_city_name}，停止本次跑商")
                    return False
                after_travel = read_strength()
            elif city_name != city.sell_city_name:
                logger.info(
                    f"账本已确认 {leg_id} 发车，当前尚未确认到站；等待下一次安全复核"
                )
                return _departure_wait_deferral(ledger_context["cycle_id"])
            actual_fatigue = (
                max(0, int(after_travel[0]) - int(before_travel[0]))
                if before_travel and after_travel
                else 0
            )
            _record_ledger_event(
                ledger_context,
                "ARRIVAL_CONFIRMED",
                origin=city.buy_city_name,
                destination=city.sell_city_name,
                leg_id=leg_id,
                fatigue_delta=actual_fatigue,
            )
            from core.services.fatigue_triggers import notify_fatigue_event

            notify_fatigue_event("arrival", city.sell_city_name)
            if after_travel is not None:
                notify_fatigue_event(
                    "fatigue_threshold",
                    fatigue_used=int(after_travel[0]),
                )
        if not (is_sell_page() or go_business("sell")):
            return False
        # Selling profit is always maximized: pursue the game's two-success cap
        # regardless of the per-city buy-side haggle setting.
        sell_haggle = _prepare_max_sell_haggle()
        if sell_haggle == 0:
            return _fatigue_deferral(
                "insufficient_fatigue_for_endpoint_sale",
                required_available=80,
            ) or False
        sell_result = sell_business(
            sell_haggle,
            expected_goods=list(city.goods_data),
            detailed=ledger_context is not None,
        )
        if not sell_result:
            logger.error("卖货未完成，不将本轮记为完成")
            return False
        confirmed_profit = (
            int(sell_result.get("confirmed_profit", 0))
            if isinstance(sell_result, dict)
            else 0
        )
        _record_ledger_event(
            ledger_context,
            "SALE_CONFIRMED",
            origin=city.buy_city_name,
            destination=city.sell_city_name,
            leg_id=leg_id,
            profit_delta=confirmed_profit,
        )
        from core.services.fatigue_triggers import notify_fatigue_event

        notify_fatigue_event("sale_confirmed", city.sell_city_name)
        _record_ledger_event(
            ledger_context,
            "LEG_COMPLETED",
            origin=city.buy_city_name,
            destination=city.sell_city_name,
            leg_id=leg_id,
        )
        notify_fatigue_event("leg_completed", city.sell_city_name)
        from core.services.fatigue_triggers import fatigue_checkpoint_deferral

        checkpoint = fatigue_checkpoint_deferral(city.sell_city_name)
        if checkpoint is not None:
            checkpoint["checkpoint_boundary"] = "POST_SALE"
            return checkpoint
        # 流程跑完，更改站点名称为当前出售商品的站点
        city_name = city.sell_city_name
    logger.info("运行完成")
    if ledger_context is not None:
        return {
            "success": True,
            "cycle_id": ledger_context["cycle_id"],
            "confirmed_books": _cycle_books_used(ledger_context),
        }
    return True


def run_with_recovery(
    routes: RoutesModel,
    recovery_attempts: int = 2,
    ledger_context: dict | None = None,
):
    """Run one round, restarting a crashed client and re-checking its station."""
    for attempt in range(recovery_attempts + 1):
        try:
            return run(
                routes,
                recovery_attempts=recovery_attempts - attempt,
                ledger_context=ledger_context,
            )
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


def execute_weekly_cycle(
    routes: RoutesModel,
    ledger_context: dict,
    *,
    runner=None,
    now=None,
):
    """Execute or deterministically finalize one persisted weekly cycle."""

    from core.services.trade_ledger import (
        LEDGER_PATH,
        finalize_trade_cycle,
        load_trade_cycle_state,
    )

    path = ledger_context.get("ledger_path", LEDGER_PATH)
    cycle_id = ledger_context["cycle_id"]
    state = load_trade_cycle_state(path, cycle_id)
    if state.phase == "CYCLE_COMPLETED":
        return {
            "success": True,
            "cycle_id": cycle_id,
            "confirmed_books": state.purchase_books_used,
            "finalized_without_route_rerun": True,
        }
    if state.ready_to_finalize:
        finalize_trade_cycle(path, cycle_id, observed_at=now)
        finalized = load_trade_cycle_state(path, cycle_id)
        return {
            "success": True,
            "cycle_id": cycle_id,
            "confirmed_books": finalized.purchase_books_used,
            "finalized_without_route_rerun": True,
        }
    execute = runner or run_with_recovery
    result = execute(routes, ledger_context=ledger_context)
    if task_result_succeeded(result):
        finalize_trade_cycle(path, cycle_id, observed_at=now)
    return result


def two_city_run(buy_city_name: str, sell_city_name: str):
    global STOP
    STOP = False
    unavailable = _route_availability_deferral(buy_city_name, sell_city_name)
    if unavailable:
        return unavailable
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


MAX_WEEKLY_RUNS_PER_INVOCATION = 1


def two_city_weekly_run(
    buy_city_name: str,
    sell_city_name: str,
    execution_batches: list[dict],
    max_runs: int | None = None,
    available_books: int | None = None,
):
    """Execute complete round trips, optionally yielding after a safe run boundary.

    A scheduled weekly plan may contain many round trips.  Limiting one worker
    invocation to a small number of *complete* trips lets higher-priority tasks
    that became due meanwhile run before the next trip, without ever stopping a
    train halfway through a leg or leaving a sale unfinished.
    """
    if max_runs is not None and int(max_runs) != MAX_WEEKLY_RUNS_PER_INVOCATION:
        raise ValueError("production weekly API permits one complete round trip per invocation")

    from app.common.config import cfg
    from core.services import load_weekly_plan, record_completed_run
    from core.services.server_calendar import SERVER_CLOCK
    from core.services.trade_ledger import (
        LEDGER_PATH,
        find_recoverable_cycle,
        load_trade_cycle_state,
    )
    from core.services.trade_planning import validate_executable_trade_budget

    global STOP
    STOP = False
    unavailable = _route_availability_deferral(buy_city_name, sell_city_name)
    if unavailable:
        return unavailable
    buy_haggle_num = app.CityHaggle[buy_city_name]
    sell_haggle_num = app.CityHaggle[sell_city_name]
    total_runs = sum(int(batch["runs"]) for batch in execution_batches)
    run_limit = min(total_runs, MAX_WEEKLY_RUNS_PER_INVOCATION)
    logger.info(f"准备运行周计划，共 {total_runs} 次完整往返，{len(execution_batches)} 个阶段")
    completed = 0
    initial_state = load_weekly_plan() or {}
    expected_price_revision = str(
        initial_state.get("price_revision", initial_state.get("price_time", ""))
    )
    confirmed_book_budget = (
        max(0, int(available_books))
        if available_books is not None
        else max(0, int(cfg.InventoryBooks.value))
    )
    for batch_index, batch in enumerate(execution_batches, start=1):
        books = batch.get("books", {})
        batch_runs = int(batch.get("runs", 0))
        logger.info(f"周计划阶段 {batch_index}/{len(execution_batches)}: {batch_runs} 次完整往返，进货书 {books}")
        for _ in range(batch_runs):
            route_id = f"{buy_city_name}|{sell_city_name}"
            recoverable = find_recoverable_cycle(LEDGER_PATH, route_id)
            if recoverable is not None:
                cycle_id = recoverable.cycle_id
                completed_legs = len(recoverable.completed_leg_ids)
                server_week_id = recoverable.server_week_id
            else:
                cycle_id = uuid.uuid4().hex
                completed_legs = 0
                server_week_id = SERVER_CLOCK.server_week_id()
            ledger_context = {
                "cycle_id": cycle_id,
                "route_id": route_id,
                "origin": buy_city_name,
                "server_week_id": server_week_id,
                "completed_legs": completed_legs,
                "require_purchase_guard": True,
            }

            def purchase_validator(city, *, confirmed_books=0, observed_at=None):
                current_state = load_weekly_plan()
                if not current_state:
                    return _stale_price_deferral("weekly_plan_missing_before_purchase")
                current_revision = str(
                    current_state.get(
                        "price_revision", current_state.get("price_time", "")
                    )
                )
                if not expected_price_revision or current_revision != expected_price_revision:
                    return _stale_price_deferral("price_revision_changed_before_purchase")
                unavailable_now = unavailable_stations(
                    [city.buy_city_name, city.sell_city_name],
                    at=observed_at or SERVER_CLOCK.server_now(),
                )
                if unavailable_now:
                    return _route_availability_deferral(
                        city.buy_city_name, city.sell_city_name
                    )
                strength = read_strength()
                if not strength:
                    return _fatigue_deferral("fatigue_unknown_before_purchase") or False
                available_fatigue = max(0, int(strength[1]) - int(strength[0]))
                cycle = load_trade_cycle_state(LEDGER_PATH, cycle_id)
                validated_state = dict(current_state)
                validated_state["current_partial_cycle"] = {
                    "confirmed_legs": len(cycle.completed_leg_ids)
                }
                validated_state["cycle_fatigue"] = max(
                    1,
                    int(
                        _city_tired_data.get(
                            f"{city.buy_city_name}-{city.sell_city_name}", 0
                        )
                    ),
                )
                try:
                    validate_executable_trade_budget(
                        validated_state,
                        now=observed_at or SERVER_CLOCK.server_now(),
                        fatigue_budget=available_fatigue,
                        purchase_books=max(
                            0,
                            confirmed_book_budget
                            - int(cycle.purchase_books_used),
                        ),
                    )
                except Exception as error:
                    return _stale_price_deferral(
                        f"purchase_revalidation_failed:{type(error).__name__}"
                    )
                return True

            ledger_context["purchase_validator"] = purchase_validator
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
            result = execute_weekly_cycle(routes, ledger_context)
            if task_result_deferred(result):
                logger.info(
                    f"周计划资源暂缓，本次已完成 {completed}/{total_runs} 次完整往返"
                )
                return result
            if not task_result_succeeded(result) or is_stopped():
                logger.info(f"周计划停止，本次已完成 {completed}/{total_runs} 次完整往返")
                return False
            completed += 1
            record_completed_run(
                books,
                cycle_id=cycle_id,
                confirmed_books=(
                    int(result.get("confirmed_books", 0))
                    if isinstance(result, dict)
                    else 0
                ),
                server_week_id=server_week_id,
            )
            if completed >= run_limit and completed < total_runs:
                logger.info(
                    f"本次调度已在完整往返边界让出队列，完成 {completed}/{total_runs} 次；"
                    "剩余计划将在下一次调度继续"
                )
                return {
                    "success": True,
                    "deferred": True,
                    "progress_made": True,
                    "reason": "route_completed_yield",
                    "next_run_at": (
                        SERVER_CLOCK.server_now() + timedelta(seconds=5)
                    ).isoformat(timespec="seconds"),
                }
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
    from core.services.trade_planning import (
        StalePriceSnapshot,
        validate_executable_trade_budget,
    )
    from core.services.daily_capabilities import CurrentResourceEvidence
    from core.services.weekly_plan_state import (
        save_current_city_evidence,
        save_current_resource_evidence,
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
    unavailable_cycle = unavailable_stations(state["cycle"])
    fallback = int(cfg.InventoryBooks.value)
    sell_resume = is_sell_page()
    if unavailable_cycle and sell_resume:
        logger.warning(
            "当前处于卖货中间态且原计划包含未开放站点，"
            "为保留现有货物与议价状态，本次暂停并等待人工复核"
        )
        return _route_availability_deferral(*state["cycle"])
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
    observed_available_fatigue = None
    if not sell_resume:
        if not go_business("buy"):
            logger.error("未能进入已验证的买入页，实际疲劳资源保持 UNKNOWN")
            return False
        strength = read_strength()
        station = get_station()
        if strength is None or not station:
            logger.error("未能同时确认当前疲劳与站点，禁止把计划需求当作实际预算")
            return False
        observed_at = SERVER_CLOCK.server_now()
        observed_available_fatigue = max(0, int(strength[1]) - int(strength[0]))
        evidence = CurrentResourceEvidence(
            fatigue_used=int(strength[0]),
            fatigue_cap=int(strength[1]),
            available_fatigue=observed_available_fatigue,
            recoverable_fatigue_today=0,
            purchase_books_available=max(0, int(available)),
            source="game_observed" if actual is not None else "user_calibrated",
            observed_at=observed_at,
            valid_until=observed_at + timedelta(minutes=10),
            server_day_id=SERVER_CLOCK.server_day_id(observed_at),
            revision=f"trade-preflight:{uuid.uuid4().hex}",
        )
        save_current_resource_evidence(evidence)
        save_current_city_evidence(
            station,
            source="game_observed",
            observed_at=observed_at,
            valid_until=observed_at + timedelta(minutes=10),
            revision=evidence.revision,
        )
    price_invalid = False
    execution_invalid = False
    try:
        executable = validate_executable_trade_budget(
            state,
            now=SERVER_CLOCK.server_now(),
            fatigue_budget=(
                max(0, int(observed_available_fatigue))
                if observed_available_fatigue is not None
                else 0
            ),
            purchase_books=max(0, int(available)),
        )
        logger.info(
            "执行前实时计划已通过价格时效与有限预算校验："
            f"预计净利润 {executable.expected_total_net_profit}，"
            f"疲劳 {executable.expected_total_fatigue}"
        )
    except StalePriceSnapshot as error:
        logger.warning(f"现有周计划价格不可执行，强制重新读取并优化: {error}")
        price_invalid = True
    except ValueError as error:
        logger.warning(f"现有周计划不满足实时利润或资源预算，强制重新优化: {error}")
        execution_invalid = True
    needs_reoptimization = (
        bool(state.get("needs_reoptimization"))
        or bool(unavailable_cycle)
        or price_invalid
        or execution_invalid
    ) and not sell_resume
    if available >= required and not needs_reoptimization:
        logger.info(f"进货书库存 {available} 本，足够完成剩余计划（需要 {required} 本）")
        cycle = state["cycle"]
        return two_city_weekly_run(
            cycle[0],
            cycle[1],
            remaining_batches(state),
            max_runs=1,
            available_books=available,
        )

    if needs_reoptimization:
        if unavailable_cycle:
            logger.warning(
                f"原周计划包含未开放站点 {unavailable_cycle}；"
                f"按当前 {available} 本进货书强制计算替代路线"
            )
        else:
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
    raw_config["weekly_fatigue"] = max(1, int(observed_available_fatigue or 0))
    try:
        replacement = optimize_live_routes(OptimizationConfig(**raw_config))
        preview = {
            **replacement,
            "expected_profit": int(
                replacement.get(
                    "expected_profit",
                    replacement.get("combined_profit", replacement.get("profit", 0)),
                )
            ),
            "books_total": int(replacement.get("books_used", 0)),
            "total_runs": int(replacement.get("repeats", 1)),
            "completed_runs": 0,
            "runs": [
                dict(batch.get("books", {}))
                for batch in replacement.get("execution_batches", ())
                for _ in range(int(batch.get("runs", 0)))
            ],
        }
        validate_executable_trade_budget(
            preview,
            now=SERVER_CLOCK.server_now(),
            fatigue_budget=max(0, int(observed_available_fatigue or 0)),
            purchase_books=max(0, int(available)),
        )
        state = save_weekly_plan(replacement)
        validate_executable_trade_budget(
            state,
            now=SERVER_CLOCK.server_now(),
            fatigue_budget=max(0, int(observed_available_fatigue or 0)),
            purchase_books=max(0, int(available)),
        )
        cycle = state["cycle"]
        logger.info(f"已切换替代路线: {cycle[0]} → {cycle[1]} → {cycle[0]}，计划使用 {replacement['books_used']} 本")
        return two_city_weekly_run(
            cycle[0],
            cycle[1],
            remaining_batches(state),
            max_runs=1,
            available_books=available,
        )
    except StopExecution:
        raise
    except StalePriceSnapshot:
        logger.exception("重新计算后的价格快照仍不可执行，阻止真实跑商")
        return _stale_price_deferral("stale_price_replacement_not_fresh")
    except Exception:
        if unavailable_cycle:
            logger.exception(
                "未能为含关闭站点的周计划生成替代路线；"
                "禁止回退原路线，留待下次复核"
            )
            return _route_availability_deferral(*state["cycle"])
        if price_invalid:
            logger.exception(
                "陈旧价格重优化失败；禁止使用旧路线、零进货书或离线价格回退"
            )
            return _stale_price_deferral()
        logger.exception("实时替代路线计算失败；保留原计划但禁止本轮真实交易")
        return _stale_price_deferral("trade_reoptimization_failed")


def stop():
    """
    说明:
        停止运行
    """
    global STOP
    STOP = True
    from core.services.fatigue_triggers import cancel_deferred_fatigue_actions

    cancel_deferred_fatigue_actions()
    stop_control()
