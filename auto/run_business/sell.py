"""
Author: Night-stars-1 nujj1042633805@gmail.com
Date: 2024-04-05 15:17:19
LastEditTime: 2024-07-08 21:11:34
LastEditors: Night-stars-1 nujj1042633805@gmail.com
"""

import re
import time

from loguru import logger

from app.common.config import cfg
from core.control.control import input_swipe, input_tap, screenshot
from core.exception.exceptions import StopExecution
from core.module.bgr import BGR
from core.preset import go_home
from auto.module.strength import exit_negotiation_safely


SELL_BARGAIN_TIMEOUT = 45
RAISE_RESULT_TIMEOUT = 3.0
FRAME_RETRY_INTERVAL = 0.2

# Spread across the trade panel so a partially black NEMU IPC frame is not
# mistaken for a real bargain result.
TRADE_FRAME_ANCHORS = (
    (500, 100),
    (629, 101),
    (900, 300),
    (1176, 461),
    (1056, 647),
)


def _is_complete_trade_frame(image):
    """Return whether enough of the trade UI is present in this frame."""
    visible_anchors = 0
    for pos in TRADE_FRAME_ANCHORS:
        color = image.get_bgr(pos)
        if max(tuple(color)) > 12:
            visible_anchors += 1
    return visible_anchors >= 4


def _wait_for_raise_result(timeout=RAISE_RESULT_TIMEOUT):
    """Poll the transient result colour instead of trusting one screenshot."""
    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        image = screenshot()
        if not _is_complete_trade_frame(image):
            logger.warning("Incomplete NEMU IPC trade frame; retry bargain result capture")
            time.sleep(FRAME_RETRY_INTERVAL)
            continue

        image.crop_image((516, 224), (787, 439))
        hsv = image.get_hsv((626, 273))
        logger.debug(f"Raise result colour check (HSV): {hsv}")
        if 30 <= hsv[0] <= 40:
            return True
        time.sleep(FRAME_RETRY_INTERVAL)
    return False


def sell_business(num=0, empty_ok=False, expected_goods=None):
    """
    说明:
        出售所有商品
    参数:
        :param num: 期望议价的价格
    """
    # Bargaining resets the game's sell selection, so it must happen before
    # the single "sell all" click. A resumed 20% sell page is already at the
    # cap and must not be exited or bargained again.
    current_raise = read_raise_percent()
    if current_raise is not None:
        logger.info(f"Current sell raise: {current_raise:.1f}%")
    bargain_complete = current_raise is not None and current_raise >= 20.0
    if bargain_complete:
        logger.info("Sell raise already reached 20%; reuse the current sell state")
    elif num > 0 and not click_bargain_button(num):
        logger.error("Maximum sell bargain was not completed; cancel sale")
        return False

    start_time = time.perf_counter()
    while time.perf_counter() - start_time < 15:
        image = screenshot()
        bgr = image.get_bgr((1156, 100))
        logger.debug(f"是否出售货物颜色检查 {bgr}")
        if not (bgr.b == 0 and bgr.g == 0 and 90 <= bgr.r <= 100):
            logger.debug(f"出售全部货物颜色检查 {bgr}")
            input_tap((1187, 103))
            time.sleep(0.5)
            break
    if is_empty_goods():
        if empty_ok:
            logger.info("No sellable cargo detected; continue with restocking")
            go_home()
            return True
        logger.error("检测到未成功出售物品")
        return False
    else:
        quote = read_selected_sell_quote()
        if not quote:
            logger.error("Unable to read selected sale profit and total; cancel sale")
            return False
        profit, total = quote
        logger.info(f"Selected endpoint sale verified: profit={profit}, total={total}")
        if profit <= 0 or total <= 0:
            logger.error("Selected cargo is not a profitable endpoint sale; cancel sale")
            return False
        if expected_goods and not cargo_contains_expected_goods(expected_goods):
            logger.error("Selected cargo does not match the planned endpoint route; cancel sale")
            return False
        if not click_sell_button():
            logger.error("Sell confirmation did not complete")
            return False
        time.sleep(0.5)
        input_tap((896, 676))
        time.sleep(0.5)
        input_tap((896, 676))
        input_tap((896, 676))
        return True


def sell_existing_cargo(num=0, expected_goods=None):
    """Sell cargo currently available in the exchange, if any.

    The sell page is the source of truth for the warehouse: selecting all
    exposes every item that can be sold in the current city. An empty
    selection is a valid clean-warehouse result during the preflight check.
    """
    return sell_business(num=num, empty_ok=True, expected_goods=expected_goods)


def _read_roi_number(pos1, pos2):
    image = screenshot()
    image.crop_image(pos1, pos2)
    values = []
    for item in image.ocr():
        for raw in re.findall(r"-?\d[\d,]*(?:\.\d+)?", item["text"]):
            try:
                values.append(float(raw.replace(",", "")))
            except ValueError:
                continue
    return max(values, key=abs) if values else None


def read_raise_percent():
    """Read the current sell raise percentage from the right trade panel."""
    return _read_roi_number((965, 425), (1110, 485))


def is_sell_page():
    """Recognize an already-open exchange sell page for safe task resume."""
    try:
        texts = [item["text"] for item in screenshot().ocr()]
    except StopExecution:
        raise
    except Exception as exc:
        logger.debug(f"Unable to inspect sell-page state: {exc}")
        return False
    markers = ("我要卖", "抬价幅度", "卖出总价")
    return sum(any(marker in text for text in texts) for marker in markers) >= 2


def read_selected_sell_quote():
    """Return selected (profit, after-tax total), or None when OCR is unsafe."""
    profit = _read_roi_number((1080, 525), (1245, 570))
    total = _read_roi_number((1080, 570), (1245, 620))
    if profit is None or total is None:
        return None
    return profit, total


def cargo_contains_expected_goods(expected_goods):
    texts = [item["text"] for item in screenshot().ocr()]
    return any(
        name and any(name in text for text in texts)
        for name in expected_goods
    )


def has_sellable_cargo(expected_goods, max_pages=5):
    """Read the cargo list without changing the sell selection.

    Residual cargo must belong to the route whose destination is the current
    city. OCR that route's goods directly from the left warehouse list so the
    actual "sell all" action happens only once, after bargaining.
    """
    expected = tuple(name for name in expected_goods if name)
    if not expected:
        logger.warning("No residual-route goods available for cargo inspection")
        go_home()
        return False

    for page in range(max_pages):
        texts = [item["text"] for item in screenshot().ocr()]
        matched = next(
            (name for name in expected if any(name in text for text in texts)),
            None,
        )
        if matched:
            logger.info(f"Sellable residual cargo detected: {matched}")
            return True
        if page + 1 < max_pages:
            input_swipe((700, 590), (700, 270), swipe_time=500)
            time.sleep(0.8)

    logger.info("No sellable residual cargo detected; continue with restocking")
    go_home()
    return False


def is_empty_goods():
    image = screenshot()
    image.crop_image((870, 132), (994, 205))
    bgr = image.get_bgr((898, 169))
    logger.debug(f"货物是否为空检查 {bgr}")
    return BGR(25, 33, 33) == bgr


def click_bargain_button(num=0):
    """
    说明:
        点击议价按钮
    参数:
        :param num: 议价次数
    """
    logger.info(f"议价次数: {num}")
    start = time.perf_counter()
    book_resets = 0
    while time.perf_counter() - start < SELL_BARGAIN_TIMEOUT:
        if num <= 0:
            return True
        image = screenshot()
        if not _is_complete_trade_frame(image):
            logger.warning("Incomplete NEMU IPC trade frame; wait before bargain input")
            time.sleep(FRAME_RETRY_INTERVAL)
            continue
        bgr = image.get_bgr((1176, 461))
        logger.debug(f"抬价界面颜色检查: {bgr}")
        if BGR(0, 170, 240) <= bgr <= BGR(5, 185, 255):
            input_tap((1177, 461))
            if _wait_for_raise_result():
                logger.info("抬价成功")
                num -= 1
            else:
                logger.info("抬价失败")
            # Give the animation a moment to release input. The next loop also
            # verifies a complete frame and an enabled bargain button.
            time.sleep(0.5)
            continue
        elif bgr == [251, 253, 253]:
            logger.info("抬价次数不足")
            if not bool(cfg.UseNegotiationBook.value):
                logger.info("未开启使用议价书，停止本次出售")
                return False
            if book_resets >= 10:
                logger.error("议价书重置已达安全上限，停止本次出售")
                return False
            if not reset_negotiation_with_book():
                return False
            book_resets += 1
            # A reset re-enables the bargain button. Keep the remaining
            # success target and continue until the two-success cap is met.
            start = time.perf_counter()
            continue
        elif bgr == [62, 63, 63]:
            logger.info("疲劳不足")
            exit_negotiation_safely()
            return False
        time.sleep(FRAME_RETRY_INTERVAL)
    return False


def reset_negotiation_with_book(timeout=6):
    """Use one negotiation book from the exhausted-attempt prompt."""
    logger.info("议价次数已耗尽，尝试使用议价书重新议价")
    input_tap((1177, 461))
    deadline = time.time() + timeout
    while time.time() < deadline:
        texts = [item["text"] for item in screenshot().ocr()]
        if any("重新议价" in text for text in texts):
            input_tap((960, 512))
            time.sleep(2)
            logger.info("已使用议价书重置议价次数，继续抬价")
            return True
        time.sleep(0.5)
    logger.error("未识别到使用议价书的重新议价确认框")
    return False


def click_sell_button():
    start = time.time()
    while time.time() - start < 10:
        input_tap((1056, 647))
        time.sleep(1)
        image = screenshot()
        bgr = image.get_bgr((1175, 470), offset=5)
        logger.debug(f"出售物品界面颜色检查: {bgr}")
        if bgr == [227, 131, 82]:
            logger.info("检测到包含本地商品")
            input_tap((975, 498))
        if bgr != [0, 183, 253] and bgr != [227, 131, 82] and bgr != [251, 253, 253]:
            return True
    return False
