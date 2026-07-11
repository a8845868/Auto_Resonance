"""Automatic collection of completed dispatch rewards."""

import time

from loguru import logger

from core.control.control import input_tap, screenshot
from core.preset.control import blurry_ocr_click, go_home


def collect_dispatch_rewards() -> bool:
    """Collect dispatch rewards when the main-screen reminder is visible.

    Returns ``True`` only when a reward was collected.  Missing reminders are a
    normal state and must not interrupt the trading workflow.
    """
    logger.info("检查委派奖励")
    visible_text = [item["text"] for item in screenshot().ocr()]
    if not any("委派奖励可收取" in text for text in visible_text):
        logger.info("当前没有可领取的委派奖励")
        return False

    if not blurry_ocr_click("委派奖励可收取", score=0.6, trynum=2, log=False):
        # Stable fallback for the 1280x720 main-screen reminder.
        input_tap((1185, 350))
    time.sleep(1.5)

    if not blurry_ocr_click(
        "领取奖励",
        cropped_pos1=(720, 560),
        cropped_pos2=(1260, 710),
        score=0.7,
        trynum=3,
        log=False,
    ):
        logger.warning("已进入派遣页面，但没有识别到领取奖励按钮")
        go_home()
        return False

    time.sleep(1.5)
    logger.info("委派奖励领取成功")

    # Re-dispatch the same teams immediately.  This returns to the dispatch
    # page with fresh 20-hour timers and avoids leaving completed teams idle.
    if not blurry_ocr_click(
        "再次委托",
        cropped_pos1=(600, 570),
        cropped_pos2=(1000, 710),
        score=0.7,
        trynum=2,
        log=False,
    ):
        # Stable fallback for the 1280x720 settlement report.
        input_tap((790, 640))
    time.sleep(1.0)
    logger.info("已再次委托原派遣队伍")
    go_home()
    return True
