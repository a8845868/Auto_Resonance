import re
import time
from typing import Literal, Optional

from loguru import logger

from core.control.control import input_tap, screenshot
from core.preset import go_outlets
from core.preset.control import go_home
from app.common.config import cfg


Strength = tuple[int, int]


def read_strength() -> Optional[Strength]:
    """Read current/max fatigue from any screen that displays ``123/816``."""
    candidates = []
    for item in screenshot().ocr():
        match = re.search(r"(\d+)\s*/\s*(\d+)", item["text"])
        if match:
            current, maximum = map(int, match.groups())
            position = item["position"]
            center_x = (position[0][0] + position[2][0]) / 2
            center_y = (position[0][1] + position[2][1]) / 2
            if center_y < 100 and 500 <= maximum <= 2000:
                candidates.append((center_x, current, maximum))
    # Cargo is also rendered as x/y, immediately to the left of fatigue.
    if not candidates:
        return None
    _, current, maximum = max(candidates, key=lambda item: item[0])
    return current, maximum


def check_shop_strength(min_available: int = 60) -> bool:
    strength = read_strength()
    if strength is None:
        logger.warning("未识别到疲劳值，暂不阻止当前操作")
        return True
    current, maximum = strength
    logger.info(f"当前疲劳: {current}/{maximum}，可用余量 {maximum - current}")
    return maximum - current > min_available


def _screen_has(*texts: str) -> bool:
    visible = [item["text"] for item in screenshot().ocr()]
    return any(any(text in item for text in texts) for item in visible)


def _wait_text(*texts: str, timeout: float = 8.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _screen_has(*texts):
            return True
        time.sleep(0.7)
    return False


def exit_negotiation_safely() -> None:
    """Leave negotiation and resolve the reset-warning instead of stranding UI."""
    input_tap((83, 36))
    time.sleep(1.5)
    if _screen_has("退出后议价幅度将重置", "是否继续"):
        input_tap((768, 447))
        time.sleep(2)


def _open_fatigue_panel() -> bool:
    input_tap((970, 30))
    return _wait_text("恢复疲劳值方式", "FATIGUE", timeout=5)


def _silver_prompt_visible() -> bool:
    return _screen_has("是否使用银枝", "银枝气泡水")


def _resolve_silver_prompt() -> bool:
    """Resolve the paid-drink prompt immediately and return whether accepted."""
    if not _silver_prompt_visible():
        return False
    if bool(cfg.UseSilverBranch.value):
        logger.info("已开启“使用银枝恢复疲劳”，确认消耗 1 银枝")
        input_tap((960, 531))
        return True
    logger.info("未开启“使用银枝恢复疲劳”，立即取消")
    input_tap((320, 531))
    time.sleep(1.5)
    return False


def _use_free_rest_area(starting_fatigue: int, target_fatigue: int) -> int:
    """Use free drinks first, then optional silver drinks only as needed."""
    input_tap((1117, 344))  # 前往休息区
    if not _wait_text("喝一杯", "休息区", timeout=8):
        logger.info("当前城市的休息区不可用")
        return starting_fatigue

    # Open the drink selection. The first drink has no repeat-warning dialog.
    input_tap((960, 325))
    time.sleep(1.5)

    current = starting_fatigue
    used = 0
    # Six free drinks plus a bounded number of optional silver drinks. The
    # target prevents consuming paid items after enough fatigue was restored.
    while used < 12 and current >= 50 and current > target_fatigue:
        if _silver_prompt_visible():
            if not _resolve_silver_prompt():
                break
            time.sleep(3)
            input_tap((1215, 35))
            time.sleep(4)
            used += 1
            current = max(0, current - 50)
            continue
        if not _screen_has("本次免费"):
            # The drink list may need one click to expose the paid prompt.
            if _screen_has("银枝气泡水"):
                input_tap((960, 422))
                time.sleep(1.5)
                continue
            break
        input_tap((960, 422))
        time.sleep(1.5)
        if _silver_prompt_visible():
            if not _resolve_silver_prompt():
                break
        elif _screen_has("再喝一杯"):
            input_tap((960, 503))
        time.sleep(3)
        # Skip the drinking animation when it is present.
        input_tap((1215, 35))
        time.sleep(4)
        used += 1
        current = max(0, current - 50)
        logger.info(f"休息区已免费喝酒 {used} 次，预计恢复 {used * 50} 疲劳")
    return current


def _return_to_trade(trade_type: Literal["buy", "sell"]) -> bool:
    go_home()
    if not go_outlets("交易所"):
        return False
    time.sleep(1.5)
    input_tap((927, 321) if trade_type == "buy" else (932, 404))
    time.sleep(2)
    return True


def _use_all_safe_lunchboxes(current_fatigue: int) -> int:
    """Use the cabinet's batch action only when its full recovery cannot waste."""
    input_tap((1117, 607))  # 前往便当柜
    if not _wait_text("便当柜", "BENTO CABINET", timeout=8):
        logger.info("未进入便当柜")
        return current_fatigue

    input_tap((1070, 427))  # 全部使用
    time.sleep(2)
    recovery = None
    for item in screenshot().ocr():
        match = re.search(r"消除\s*(\d+)\s*疲劳值", item["text"])
        if match:
            recovery = int(match.group(1))
            break
    if recovery is None:
        logger.info("没有可批量使用的便当")
        input_tap((320, 503))
        return current_fatigue
    if recovery > current_fatigue:
        logger.info(
            f"全部便当可恢复 {recovery}，当前疲劳 {current_fatigue}，为避免浪费暂不使用"
        )
        input_tap((320, 503))
        return current_fatigue

    input_tap((960, 503))
    time.sleep(6)
    # Dismiss the recovery-result overlay before navigating away.
    input_tap((640, 600))
    time.sleep(2)
    logger.info(f"已一次使用全部安全便当，恢复 {recovery} 疲劳")
    return current_fatigue - recovery


def recover_strength(
    trade_type: Literal["buy", "sell"], min_available: int = 60
) -> bool:
    """Recover fatigue with free rest-area drinks before safe batch lunches."""
    strength = read_strength()
    if strength is None:
        logger.error("无法读取当前疲劳值")
        return False
    current, maximum = strength
    if not _open_fatigue_panel():
        return False

    current = _use_free_rest_area(current, max(0, maximum - min_available))
    if not _return_to_trade(trade_type) or not _open_fatigue_panel():
        return False
    current = _use_all_safe_lunchboxes(current)

    # Recovery pages return to the city/home screen. Re-enter the same trading
    # page so the caller can resume the interrupted bargain/sale operation.
    if not _return_to_trade(trade_type):
        return False
    final = read_strength()
    if final:
        logger.info(f"疲劳恢复完成: {final[0]}/{final[1]}")
        return final[1] - final[0] >= min_available
    return maximum - current >= min_available


def prepare_negotiation(
    trade_type: Literal["buy", "sell"], desired_successes: int = 2
) -> int:
    """Return a safe success target, or zero when recovery is exhausted.

    A negotiation attempt costs 8 fatigue. Reserving 10 attempts (80 fatigue)
    is deliberately conservative and lets the automation pursue two actual
    successes despite failed rolls without becoming trapped in the exit dialog.
    """
    desired_successes = max(0, min(2, desired_successes))
    if desired_successes == 0:
        return 0
    strength = read_strength()
    if strength is None:
        logger.warning("无法读取疲劳，本次放弃议价")
        return 0
    current, maximum = strength
    reserve = 80
    if maximum - current < reserve:
        logger.info(
            f"完成 2 次成功议价保守需要 {reserve} 疲劳，"
            f"当前仅剩 {maximum - current}，尝试恢复"
        )
        if not recover_strength(trade_type, min_available=reserve):
            logger.warning("恢复资源不足，本次放弃议价")
            return 0
    return desired_successes


def can_afford_fatigue(cost: int) -> bool:
    strength = read_strength()
    if strength is None:
        return False
    current, maximum = strength
    available = maximum - current
    logger.info(f"下一段预计需要 {cost} 疲劳，当前可用 {available}")
    return available >= max(0, cost)


# Legacy entrypoint retained for callers outside the trading workflow.
def use_strength():
    return recover_strength("buy")
