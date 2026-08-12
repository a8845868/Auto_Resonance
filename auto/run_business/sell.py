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
from core.services.read_only_policy import ActionIntent
from core.services.session_evidence import capture_session_evidence
from core.exception.exceptions import StopExecution
from core.module.bgr import BGR
from core.preset import go_home
from auto.module.strength import exit_negotiation_safely


SELL_BARGAIN_TIMEOUT = 45
RAISE_RESULT_TIMEOUT = 3.0
FRAME_RETRY_INTERVAL = 0.2


def _capture_sell_evidence(
    state_transition_name: str,
    *,
    ledger_context: dict | None,
    leg_id: str,
    current_page_classification: str,
) -> None:
    """Record opt-in sell-flow evidence without changing sale decisions."""
    try:
        capture_session_evidence(
            state_transition_name,
            ledger_context=ledger_context,
            leg_id=leg_id,
            current_page_classification=current_page_classification,
        )
    except Exception as error:
        logger.warning(
            "Unable to capture sell-flow evidence for "
            f"{state_transition_name}: {type(error).__name__}"
        )


def _sell_tap(pos: tuple[int, int], action_key: str = "transaction_sell") -> object:
    return input_tap(
        pos,
        intent=ActionIntent(
            action_key, "transaction_control", f"business:sell:{action_key}",
        ),
    )

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


def sell_business(
    num=0,
    empty_ok=False,
    expected_goods=None,
    *,
    detailed=False,
    ledger_context: dict | None = None,
    leg_id: str = "",
):
    """
    说明:
        出售所有商品
    参数:
        :param num: 期望议价的价格
    """
    # Validate the planned route while cargo is still visible. After "sell
    # all", the game moves every card to the right selection panel and the
    # left-side cargo OCR can no longer be used for route matching.
    _capture_sell_evidence(
        "SELL_FLOW_START",
        ledger_context=ledger_context,
        leg_id=leg_id,
        current_page_classification=(
            f"SELL_PAGE_READY|expected_goods={len(expected_goods or ())}|"
            f"haggle_target={num}"
        ),
    )
    selection_preserved = is_all_cargo_selected()
    _capture_sell_evidence(
        "SELL_CARGO_VALIDATE",
        ledger_context=ledger_context,
        leg_id=leg_id,
        current_page_classification=(
            "CARGO_SELECTION_PRESERVED"
            if selection_preserved
            else "CARGO_SELECTION_NOT_PRESERVED"
        ),
    )
    if (
        expected_goods
        and not selection_preserved
        and not cargo_contains_expected_goods(expected_goods)
    ):
        _capture_sell_evidence(
            "SELL_FLOW_FAILED",
            ledger_context=ledger_context,
            leg_id=leg_id,
            current_page_classification="ROUTE_CARGO_MISMATCH",
        )
        logger.error("Cargo does not match the planned endpoint route; cancel sale")
        return False
    if selection_preserved:
        logger.info("Existing all-selected cargo state verified from quote and button state")

    # Bargaining resets the game's sell selection, so it must happen before
    # the single "sell all" click. A resumed 20% sell page is already at the
    # cap and must not be exited or bargained again.
    current_raise = read_raise_percent()
    _capture_sell_evidence(
        "SELL_RAISE_OBSERVE",
        ledger_context=ledger_context,
        leg_id=leg_id,
        current_page_classification=f"SELL_RAISE_PERCENT|value={current_raise}",
    )
    if current_raise is not None:
        logger.info(f"Current sell raise: {current_raise:.1f}%")
    bargain_complete = current_raise is not None and current_raise >= 20.0
    if bargain_complete:
        logger.info("Sell raise already reached 20%; reuse the current sell state")
    elif num > 0:
        _capture_sell_evidence(
            "SELL_BARGAIN_BEFORE",
            ledger_context=ledger_context,
            leg_id=leg_id,
            current_page_classification=f"SELL_BARGAIN_PENDING|target={num}",
        )
        if not click_bargain_button(num):
            _capture_sell_evidence(
                "SELL_BARGAIN_AFTER",
                ledger_context=ledger_context,
                leg_id=leg_id,
                current_page_classification="SELL_BARGAIN_NOT_CONFIRMED",
            )
            logger.error("Maximum sell bargain was not completed; cancel sale")
            return False
        _capture_sell_evidence(
            "SELL_BARGAIN_AFTER",
            ledger_context=ledger_context,
            leg_id=leg_id,
            current_page_classification="SELL_BARGAIN_COMPLETED",
        )

    _capture_sell_evidence(
        "SELL_SELECT_ALL_BEFORE",
        ledger_context=ledger_context,
        leg_id=leg_id,
        current_page_classification="SELL_SELECTION_PENDING",
    )
    selected = is_all_cargo_selected() or select_all_sellable_cargo()
    _capture_sell_evidence(
        "SELL_SELECT_ALL_AFTER",
        ledger_context=ledger_context,
        leg_id=leg_id,
        current_page_classification=(
            "SELL_SELECTION_CONFIRMED" if selected else "SELL_SELECTION_NOT_CONFIRMED"
        ),
    )
    if not selected:
        # The left warehouse list and the right selected list are different
        # states. A blank right panel means "selection did not apply", not
        # "the warehouse is empty". Never restock while known cargo remains.
        logger.error("Cargo is still visible but Sell All did not create a selection; cancel sale")
        return False

    quote = read_selected_sell_quote()
    if not quote:
        _capture_sell_evidence(
            "SELL_QUOTE_OBSERVE",
            ledger_context=ledger_context,
            leg_id=leg_id,
            current_page_classification="SELL_QUOTE_UNREADABLE",
        )
        logger.error("Unable to read selected sale profit and total; cancel sale")
        return False
    profit, total = quote
    _capture_sell_evidence(
        "SELL_QUOTE_OBSERVE",
        ledger_context=ledger_context,
        leg_id=leg_id,
        current_page_classification=f"SELL_QUOTE_READ|profit={profit}|total={total}",
    )
    logger.info(f"Selected endpoint sale verified: profit={profit}, total={total}")
    if profit <= 0 or total <= 0:
        logger.error("Selected cargo is not a profitable endpoint sale; cancel sale")
        return False
    _capture_sell_evidence(
        "SELL_CONFIRM_BEFORE",
        ledger_context=ledger_context,
        leg_id=leg_id,
        current_page_classification="SALE_CONFIRMATION_PENDING",
    )
    if not click_sell_button():
        _capture_sell_evidence(
            "SELL_CONFIRM_AFTER",
            ledger_context=ledger_context,
            leg_id=leg_id,
            current_page_classification="SALE_NOT_CONFIRMED",
        )
        logger.error("Sell confirmation did not complete")
        return False
    _capture_sell_evidence(
        "SELL_CONFIRM_AFTER",
        ledger_context=ledger_context,
        leg_id=leg_id,
        current_page_classification="SALE_CONFIRMED",
    )
    time.sleep(0.5)
    _sell_tap((896, 676))
    _capture_sell_evidence(
        "SELL_FLOW_COMPLETE",
        ledger_context=ledger_context,
        leg_id=leg_id,
        current_page_classification=f"SELL_COMPLETED|profit={profit}|total={total}",
    )
    time.sleep(0.5)
    _sell_tap((896, 676))
    _sell_tap((896, 676))
    if detailed:
        return {
            "success": True,
            "confirmed_profit": int(profit),
            "confirmed_sale_total": int(total),
        }
    return True


def sell_existing_cargo(num=0, expected_goods=None):
    """Sell cargo currently available in the exchange, if any.

    The sell page is the source of truth for the warehouse: selecting all
    exposes every item that can be sold in the current city. An empty
    selection is a valid clean-warehouse result during the preflight check.
    """
    return sell_business(num=num, empty_ok=True, expected_goods=expected_goods)


def _read_roi_number(pos1, pos2, timeout=5.0):
    """Read a numeric ROI across multiple frames to tolerate NEMU blanks."""
    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        image = screenshot()
        image.crop_image(pos1, pos2)
        values = []
        for item in image.ocr():
            for raw in re.findall(r"-?\d[\d,]*(?:\.\d+)?", item["text"]):
                try:
                    values.append(float(raw.replace(",", "")))
                except ValueError:
                    continue
        if values:
            return max(values, key=abs)
        logger.warning("Numeric trade ROI was blank; retrying NEMU IPC capture")
        time.sleep(FRAME_RETRY_INTERVAL)
    return None


def read_raise_percent():
    """Read the current sell raise percentage from the right trade panel."""
    return _read_roi_number((965, 425), (1110, 485))


def is_sell_page():
    """Recognize an already-open exchange sell page for safe task resume."""
    try:
        image = screenshot()
        texts = [item["text"] for item in image.ocr()]
    except StopExecution:
        raise
    except Exception as exc:
        logger.debug(f"Unable to inspect sell-page state: {exc}")
        return False
    if any("退出后议价幅度将重置" in text for text in texts):
        logger.info("Negotiation-exit warning detected; cancel it and preserve the sell state")
        # Left button is Cancel. Never confirm here because confirmation resets
        # the completed raise and leaves the exchange.
        _sell_tap((319, 512), "navigation_anchor")
        time.sleep(1.5)
        return True
    markers = ("我要卖", "抬价幅度", "卖出总价")
    if sum(any(marker in text for text in texts) for marker in markers) >= 2:
        return True

    # OCR can miss the sell labels when all cargo cards have moved to the
    # right selection panel. Use stable 1280x720 visual anchors for that exact
    # resumable state: selected sell tab, white Sell action, orange bargain
    # action and red "cancel all" selection button.
    sell_tab = image.get_bgr((315, 675))
    sell_action = image.get_bgr((1056, 647))
    bargain_action = image.get_bgr((1176, 461))
    cancel_all = image.get_bgr((1156, 100))
    selected_sell_tab = min(tuple(sell_tab)) >= 200
    visible_sell_action = min(tuple(sell_action)) >= 200
    visible_bargain_action = (
        bargain_action.b <= 10
        and 120 <= bargain_action.g <= 200
        and bargain_action.r >= 220
    )
    all_cargo_selected = (
        cancel_all.b <= 10 and cancel_all.g <= 10 and 80 <= cancel_all.r <= 130
    )
    if (
        selected_sell_tab
        and visible_sell_action
        and visible_bargain_action
        and all_cargo_selected
    ):
        logger.info("Selected-cargo sell page detected from visual anchors")
        return True
    return False


def read_selected_sell_quote():
    """Return selected (profit, after-tax total), or None when OCR is unsafe."""
    profit = _read_roi_number((1080, 525), (1245, 570))
    total = _read_roi_number((1080, 570), (1245, 620))
    if profit is None or total is None:
        return None
    return profit, total


def is_all_cargo_selected():
    """Recognize the red Cancel All state with a positive selected quote."""
    image = screenshot()
    cancel_all = image.get_bgr((1156, 100))
    selected = cancel_all.b <= 10 and cancel_all.g <= 10 and 80 <= cancel_all.r <= 130
    if not selected:
        return False
    quote = read_selected_sell_quote()
    return bool(quote and quote[0] > 0 and quote[1] > 0)


def _select_all_button_active(image=None) -> bool:
    """Return whether Sell All has changed into the red Cancel All state."""
    image = image or screenshot()
    cancel_all = image.get_bgr((1156, 100))
    return (
        cancel_all.b <= 10
        and cancel_all.g <= 10
        and 80 <= cancel_all.r <= 130
    )


def select_all_sellable_cargo(attempts=5) -> bool:
    """Apply Sell All and verify selection without ever toggling it off.

    The old flow tapped once, then sampled one blank pixel in the right panel.
    When that tap was dropped during dialogue animation it incorrectly treated
    the blank selection as an empty warehouse. Here the red button state and a
    positive quote are both required before the sale may continue.
    """
    for attempt in range(attempts):
        image = screenshot()
        if _select_all_button_active(image):
            quote = read_selected_sell_quote()
            if quote and quote[0] > 0 and quote[1] > 0:
                logger.info(
                    f"Sell All selection verified on attempt {attempt + 1}: "
                    f"profit={quote[0]}, total={quote[1]}"
                )
                return True
            logger.warning("Cancel All is active but selected quote is not ready; wait without deselecting")
            time.sleep(0.6)
            continue

        logger.info(f"Apply Sell All selection ({attempt + 1}/{attempts})")
        _sell_tap((1187, 103))
        time.sleep(0.8)
    return False


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
            _sell_tap((1177, 461))
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
    _sell_tap((1177, 461))
    deadline = time.time() + timeout
    while time.time() < deadline:
        texts = [item["text"] for item in screenshot().ocr()]
        if any("重新议价" in text for text in texts):
            _sell_tap((960, 512))
            time.sleep(2)
            logger.info("已使用议价书重置议价次数，继续抬价")
            return True
        time.sleep(0.5)
    logger.error("未识别到使用议价书的重新议价确认框")
    return False


def click_sell_button(timeout=25):
    """Complete a sale and verify the settlement report.

    Market volatility can insert an extra confirmation after the Sell click.
    A colour change is not proof of settlement; only the settlement report (or
    an emptied selected quote) is accepted as success.
    """
    deadline = time.time() + timeout
    should_click_sell = True
    while time.time() < deadline:
        if should_click_sell:
            _sell_tap((1056, 647))
            should_click_sell = False
            time.sleep(1)

        image = screenshot()
        texts = [item["text"] for item in image.ocr()]
        if any(
            marker in text
            for text in texts
            for marker in ("SETTLEMENTREPORT", "结算报告")
        ):
            logger.info("Settlement report detected; sale completed")
            return True

        if any("点击空白处退出" in text for text in texts):
            # This phrase is shared by reward, announcement and item-detail
            # overlays. Without the settlement title it is not sale evidence.
            logger.warning(
                "Generic dismiss overlay detected without settlement identity; "
                "do not infer sale completion"
            )

        if any("行情" in text and "波动" in text for text in texts):
            logger.warning("Market volatility prompt detected; confirm and revalidate sale")
            _sell_tap((960, 512))
            time.sleep(1.5)
            # The confirmation may settle immediately or return to the sell
            # page with a refreshed quote. The next loop identifies either.
            should_click_sell = False
            continue

        if any("本地商品" in text for text in texts):
            logger.info("检测到包含本地商品，确认出售非本地货物")
            _sell_tap((975, 498))
            time.sleep(1.5)
            continue

        bgr = image.get_bgr((1175, 470), offset=5)
        logger.debug(f"出售物品界面颜色检查: {bgr}")
        if bgr == [227, 131, 82]:
            _sell_tap((975, 498))
            time.sleep(1.5)
            continue

        if is_sell_page():
            quote = read_selected_sell_quote(timeout=2.0)
            if quote and quote[0] > 0 and quote[1] > 0:
                logger.info(
                    f"Sale still pending after prompt: profit={quote[0]}, total={quote[1]}"
                )
                should_click_sell = True
            elif quote and quote[0] == 0 and quote[1] == 0:
                logger.info("Selected quote cleared; sale completed")
                return True
        time.sleep(FRAME_RETRY_INTERVAL)
    logger.error("Sale did not reach a verified settlement state")
    return False
