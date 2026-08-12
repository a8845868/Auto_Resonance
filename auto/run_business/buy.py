"""
Author: Night-stars-1 nujj1042633805@gmail.com
Date: 2024-04-04 17:54:58
LastEditTime: 2025-02-11 19:29:24
LastEditors: Night-stars-1 nujj1042633805@gmail.com
"""

import re
import time
from typing import Callable, List, Tuple

import cv2 as cv
import numpy as np
from loguru import logger

from core.control.control import input_swipe, input_tap, screenshot, screenshot_image
from core.exception.exception_handling import get_excption
from core.exception.exceptions import StopExecution
from core.image.image import Image
from core.module.bgr import BGR
from core.module.hsv import HSV
from core.preset import click, find_text, go_home
from core.services.read_only_policy import ActionIntent
from core.services.session_evidence import capture_session_evidence
from auto.module.strength import exit_negotiation_safely


BUY_BARGAIN_TIMEOUT = 45
BUY_RESULT_TIMEOUT = 3.0
BUY_RESULT_POLL_INTERVAL = 0.2
BUY_CONFIRM_TIMEOUT = 10.0
BUY_CONFIRM_STABLE_FRAMES = 2
CARGO_CAPACITY_ROI = (1080, 350, 1270, 430)


def _capture_buy_evidence(
    state_transition_name: str,
    *,
    ledger_context: dict | None,
    leg_id: str,
    current_page_classification: str,
) -> None:
    """Record opt-in buy-flow evidence without affecting trade decisions."""
    try:
        capture_session_evidence(
            state_transition_name,
            ledger_context=ledger_context,
            leg_id=leg_id,
            current_page_classification=current_page_classification,
        )
    except Exception as error:
        logger.warning(
            "Unable to capture buy-flow evidence for "
            f"{state_transition_name}: {type(error).__name__}"
        )


def _buy_tap(pos: tuple[int, int]) -> object:
    return input_tap(
        pos,
        intent=ActionIntent(
            "transaction_buy", "transaction_control", "business:buy:transaction",
        ),
    )


def _cargo_capacity_value(items) -> tuple[int, int] | None:
    """Read the buy-page cargo counter without inferring from unrelated text."""
    x1, y1, x2, y2 = CARGO_CAPACITY_ROI
    for item in items:
        position = item.get("position")
        if not position or len(position) < 3:
            continue
        x = (position[0][0] + position[2][0]) / 2
        y = (position[0][1] + position[2][1]) / 2
        if not (x1 <= x <= x2 and y1 <= y <= y2):
            continue
        match = re.search(r"(\d+)\s*/\s*(\d+)\+?", item.get("text", ""))
        if match and int(match.group(2)) > 0:
            return int(match.group(1)), int(match.group(2))
    return None


def _cargo_capacity_full(items) -> bool:
    """Confirm the buy-page cargo counter reports current >= capacity."""
    value = _cargo_capacity_value(items)
    return value is not None and value[0] >= value[1]


def _purchase_completion_observed(
    before: tuple[int, int],
    *,
    timeout: float = BUY_CONFIRM_TIMEOUT,
    stable_frames: int = BUY_CONFIRM_STABLE_FRAMES,
) -> bool:
    """Require affirmative purchase evidence after the confirmation dispatch.

    A stable cargo increase and the complete reward overlay marker pair are
    independent confirmation channels.  Overlay dismissal is best-effort UI
    cleanup and cannot invalidate an already observed purchase fact.
    """
    deadline = time.perf_counter() + max(0.0, float(timeout))
    stable = 0
    expected_capacity = before[1]
    while time.perf_counter() < deadline:
        items = screenshot().ocr()
        texts = {
            re.sub(r"\s+", "", str(item.get("text", "")))
            for item in items
            if item.get("text")
        }
        if {"获得物品", "触碰空白区域退出"}.issubset(texts):
            try:
                dismissed = _buy_tap((896, 676))
            except StopExecution:
                raise
            except Exception as error:
                logger.warning(
                    "购买结果已确认，但奖励覆盖层关闭异常；保留购买事实: "
                    f"{type(error).__name__}"
                )
            else:
                if dismissed is False:
                    logger.warning(
                        "购买结果已确认，但奖励覆盖层关闭点击被拒绝；保留购买事实"
                    )
            return True

        observed = _cargo_capacity_value(items)
        if (
            observed is not None
            and observed[1] == expected_capacity
            and before[0] < observed[0] <= observed[1]
        ):
            stable += 1
            if stable >= max(1, int(stable_frames)):
                return True
        else:
            stable = 0
        time.sleep(BUY_RESULT_POLL_INTERVAL)
    return False


def _confirm_cargo_full(attempts: int = 3, stable_frames: int = 2) -> bool:
    stable = 0
    for _ in range(attempts):
        if _cargo_capacity_full(screenshot().ocr()):
            stable += 1
            if stable >= stable_frames:
                return True
        else:
            stable = 0
        time.sleep(BUY_RESULT_POLL_INTERVAL)
    return False


def _wait_for_discount_result(timeout=BUY_RESULT_TIMEOUT):
    """Poll the transient discount colour instead of one delayed frame."""
    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        hsv = screenshot().crop_image((516, 224), (787, 439)).get_hsv((629, 271))
        logger.debug(f"降价是否成功颜色检查(HSV): {hsv}")
        if 95 <= hsv.h <= 105:
            return True
        time.sleep(BUY_RESULT_POLL_INTERVAL)
    return False


def buy_business(
    primary_goods: List[str],
    secondary_goods: List[str],
    num: int = 0,
    max_book: int = 0,
    *,
    detailed: bool = False,
    confirmed_books: int = 0,
    on_book_confirmed: Callable[[int], None] | None = None,
    on_purchase_confirmed: Callable[[], None] | None = None,
    ledger_context: dict | None = None,
    leg_id: str = "",
):
    """
    购买商品

    :param primary_goods: 主要商品列表
    :param secondary_goods: 次要商品列表
    :param num: 议价的次数
    :param max_book: 最大使用进货书量
    """

    _capture_buy_evidence(
        "BUY_FLOW_START",
        ledger_context=ledger_context,
        leg_id=leg_id,
        current_page_classification=(
            f"BUY_PAGE_READY|primary={len(primary_goods)}|"
            f"secondary={len(secondary_goods)}|max_book={max_book}"
        ),
    )

    def process_goods(book, good):
        nonlocal cargo_full
        if (boatload := get_boatload()) == 0:
            cargo_full = _confirm_cargo_full()
            _capture_buy_evidence(
                "BUY_CARGO_CAPACITY_OBSERVE",
                ledger_context=ledger_context,
                leg_id=leg_id,
                current_page_classification=(
                    "CARGO_FULL_CONFIRMED" if cargo_full else "CARGO_ZERO_UNVERIFIED"
                ),
            )
            if cargo_full:
                logger.info("载货量已连续确认满载，跳过购买")
            else:
                logger.warning("载货条显示无剩余空间，但未确认满载数值，停止购买")
            return True
        _capture_buy_evidence(
            "BUY_GOOD_BEFORE",
            ledger_context=ledger_context,
            leg_id=leg_id,
            current_page_classification=f"GOOD_SELECTION_PENDING|good={good}|book={book}",
        )
        result, book = buy_good(
            good,
            book,
            max_book,
            on_book_confirmed=on_book_confirmed,
        )
        _capture_buy_evidence(
            "BUY_GOOD_AFTER",
            ledger_context=ledger_context,
            leg_id=leg_id,
            current_page_classification=(
                f"GOOD_SELECTION_RESULT|good={good}|result={result}|book={book}"
            ),
        )
        if result is None:
            logger.info(f"进货书已用完")
        elif not result:
            logger.info(f"商品{good}购买失败")
        logger.info(f"剩余载货量: {boatload}%")
        return book

    book = max(0, int(confirmed_books))
    done = False
    cargo_full = False
    for i in range(max_book + 1):
        if done:
            break
        for good in primary_goods:
            if (book := process_goods(book, good)) is True:
                done = True
                break
    for good in secondary_goods:
        if (book := process_goods(book, good)) is True:
            break
    if not is_empty_goods():
        _capture_buy_evidence(
            "BUY_BARGAIN_BEFORE",
            ledger_context=ledger_context,
            leg_id=leg_id,
            current_page_classification=f"BUY_CART_NONEMPTY|attempts={num}",
        )
        if not click_bargain_button(num):
            _capture_buy_evidence(
                "BUY_BARGAIN_AFTER",
                ledger_context=ledger_context,
                leg_id=leg_id,
                current_page_classification="BARGAIN_NOT_CONFIRMED",
            )
            logger.error("购买议价未完成")
            return False
        _capture_buy_evidence(
            "BUY_BARGAIN_AFTER",
            ledger_context=ledger_context,
            leg_id=leg_id,
            current_page_classification="BARGAIN_COMPLETED",
        )
        _capture_buy_evidence(
            "BUY_CONFIRM_BEFORE",
            ledger_context=ledger_context,
            leg_id=leg_id,
            current_page_classification="PURCHASE_CONFIRMATION_PENDING",
        )
        if not click_buy_button():
            _capture_buy_evidence(
                "BUY_CONFIRM_AFTER",
                ledger_context=ledger_context,
                leg_id=leg_id,
                current_page_classification="PURCHASE_NOT_CONFIRMED",
            )
            logger.error("点击购买后未确认成交")
            return False
        _capture_buy_evidence(
            "BUY_CONFIRM_AFTER",
            ledger_context=ledger_context,
            leg_id=leg_id,
            current_page_classification="PURCHASE_CONFIRMED",
        )
        if on_purchase_confirmed is not None:
            on_purchase_confirmed()
        _capture_buy_evidence(
            "BUY_FLOW_COMPLETE",
            ledger_context=ledger_context,
            leg_id=leg_id,
            current_page_classification=f"BUY_COMPLETED|confirmed_books={book}",
        )
        return (
            {
                "success": True,
                "confirmed_books": book,
                "purchase_confirmed": True,
                "cargo_already_full": False,
            }
            if detailed
            else True
        )
    elif cargo_full:
        _capture_buy_evidence(
            "BUY_FLOW_COMPLETE",
            ledger_context=ledger_context,
            leg_id=leg_id,
            current_page_classification=f"CARGO_ALREADY_FULL|confirmed_books={book}",
        )
        return (
            {
                "success": True,
                "confirmed_books": book,
                "purchase_confirmed": False,
                "cargo_already_full": True,
            }
            if detailed
            else True
        )
    else:
        _capture_buy_evidence(
            "BUY_FLOW_FAILED",
            ledger_context=ledger_context,
            leg_id=leg_id,
            current_page_classification="BUY_CART_EMPTY",
        )
        logger.error("未购买物品")
        go_home()
        return False


def is_empty_goods():
    image = screenshot()
    image.crop_image((870, 132), (994, 205))
    bgr = image.get_bgr((898, 169))
    logger.debug(f"货物是否为空检查 {bgr}")
    return BGR(27, 26, 26) == bgr


def buy_good(
    good: str,
    book: int,
    max_book: int,
    again: bool = False,
    *,
    on_book_confirmed: Callable[[int], None] | None = None,
):
    logger.info(f"正在购买: {good}")
    pos, image = find_text(
        good,
        cropped_pos1=(622, 136),
        cropped_pos2=(854, 685),
        log=False,
    )  # 点击商品
    if not pos:
        pos, image = find_good(good)  # 点击失败查找并点击商品
    if pos and image is not None:
        bgr = image.get_bgr((641, pos[1]))
        logger.debug(f"是否进货检测: {bgr}")
        if 13 <= bgr.r <= 16:
            if book < max_book:
                if not use_book(pos, book):
                    return False, book
                confirmed = book + 1
                if on_book_confirmed is not None:
                    # This callback is the transaction boundary: the book has
                    # already left inventory even if the later purchase fails.
                    on_book_confirmed(confirmed)
                refreshed, _ = buy_good(
                    good,
                    confirmed,
                    max_book,
                    again=True,
                    on_book_confirmed=on_book_confirmed,
                )
                return bool(refreshed) and not again, confirmed
            else:
                return None, book
        else:
            logger.info(f"点击商品: {good}")
            click(pos)
            return True, book
    else:
        return False, book


def use_book(pos: Tuple[int, int], book: int, timeout: float = 10.0) -> bool:
    """
    说明:
        使用进货书
    """
    logger.info(f"使用进货书:{book+1}")
    click((pos[0] - 215, pos[1]))
    time.sleep(1.0)
    click((959, 541))
    deadline = time.monotonic() + max(1.0, float(timeout))
    while time.monotonic() < deadline:
        hsv = screenshot().get_hsv(pos)
        if hsv[-1] >= 60:
            return True
        logger.debug(f"进货书是否所有成功颜色检查: {hsv}")
        time.sleep(0.5)
    logger.error("进货书确认后商品状态未在时限内刷新，停止后续买入")
    return False


def find_good(good, timeout=10):
    """
    说明:
        查找并点击商品
    """
    start = time.time()
    while (spend_time := time.time() - start) < timeout:
        if spend_time < timeout / 2:
            input_swipe((678, 558), (693, 314), swipe_time=500)
        else:
            input_swipe((693, 314), (678, 558), swipe_time=500)
        # 等待拖到动画结束
        time.sleep(1)
        result, image = find_text(
            good,
            cropped_pos1=(622, 136),
            cropped_pos2=(854, 685),
            log=False,
        )
        if result:
            return result, image
    return None, None


def get_boatload():
    """
    说明:
        获取载货量百分比
    """
    image = screenshot_image()
    lower_color_bound = np.array([35, 35, 35])
    upper_color_bound = np.array([36, 36, 36])

    y = 418
    x_start = 872
    x_end = 1240

    # 获取指定行的指定区间
    row_segment = image[y : y + 1, x_start:x_end]
    # 寻找指定颜色
    mask = cv.inRange(row_segment, lower_color_bound, upper_color_bound)

    boatload = np.sum(mask == 255) / (x_end - x_start)
    return int(boatload * 100)


def click_bargain_button_of_bargain(target_bargain=0):
    """
    说明:
        点击议价按钮
    参数:
        :param target_bargain: 目标议价百分比
    """
    start = time.perf_counter()
    while time.perf_counter() - start < 15:
        image = screenshot()
        image.crop_image((988, 450), (1042, 475))
        reslut = image.ocr()
        bargain = reslut[0]["text"][:-1] if len(reslut) > 0 else None
        logger.info(f"降价幅度: {bargain}%")
        if bargain and target_bargain <= bargain:
            return True
        if get_excption() == "议价次数不足":
            return False
        bgr = screenshot().get_bgr((1176, 461))
        logger.debug(f"降价界面颜色检查: {bgr}")
        if BGR(5, 135, 245) == bgr:
            _buy_tap((1177, 461))
            time.sleep(0.5)
        elif bgr == [251, 253, 253]:
            logger.info("议价次数不足")
            return True
        elif bgr == [62, 63, 63]:
            logger.info("疲劳不足")
            exit_negotiation_safely()
            return False
    return False


def click_bargain_button(num=0):
    """
    说明:
        点击议价按钮
    参数:
        :param num: 议价次数
    """
    logger.info(f"议价次数: {num}")
    start = time.perf_counter()
    while time.perf_counter() - start < BUY_BARGAIN_TIMEOUT:
        if num <= 0:
            return True
        bgr = screenshot().get_bgr((1176, 461))
        logger.debug(f"降价界面颜色检查: {bgr}")
        if BGR(0, 123, 240) <= bgr <= BGR(2, 133, 255):
            _buy_tap((1177, 461))
            if _wait_for_discount_result():
                logger.info("降价成功")
                num -= 1
            else:
                logger.info("降价失败")
            time.sleep(0.5)
            continue
        elif bgr == [251, 253, 253]:
            logger.info("降价次数不足")
            return True
        elif bgr == [62, 63, 63]:
            logger.info("疲劳不足")
            exit_negotiation_safely()
            return False
        time.sleep(BUY_RESULT_POLL_INTERVAL)
    return False


def click_buy_button():
    """
    说明:
        点击一次购买按钮，并以购买前后载货计数增加作为成交证明。
    """
    before = _cargo_capacity_value(screenshot().ocr())
    if before is None or before[0] >= before[1]:
        logger.warning("购买前未取得可信的载货计数，拒绝派发购买确认")
        return False
    dispatch_result = _buy_tap((1056, 647))
    if dispatch_result is False:
        logger.warning("购买确认点击被安全策略拒绝")
        return False
    if _purchase_completion_observed(before):
        return True
    logger.warning("购买确认已派发，但未观察到稳定的载货计数增加")
    return False
