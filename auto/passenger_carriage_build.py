"""Sequential six-hour passenger-carriage construction monitor."""

import time
import re
from dataclasses import dataclass
from datetime import datetime

from loguru import logger

from core.control.control import connect, input_tap, is_stopped, screenshot
from core.preset.control import blurry_ocr_click, go_home
from core.services.game_recovery import is_game_running, start_game
from core.services.passenger_build_planner import (
    build_monitor_summary,
    load_build_monitor_plan,
    record_carriage_completed,
    record_carriage_started,
    resync_active_build,
)


@dataclass(frozen=True)
class BuildScreenState:
    building: bool
    remaining_seconds: int | None = None
    existing: bool = False


def parse_build_remaining(texts: list[str]) -> int | None:
    """Parse the workshop's `施工剩余时长：HH:MM:SS` OCR result."""
    combined = " ".join(str(text) for text in texts)
    match = re.search(r"(\d{1,2})\s*[:：]\s*(\d{2})\s*[:：]\s*(\d{2})", combined)
    if not match:
        return None
    hours, minutes, seconds = map(int, match.groups())
    if minutes > 59 or seconds > 59:
        return None
    return hours * 3600 + minutes * 60 + seconds


def inspect_build_screen() -> BuildScreenState:
    texts = [item["text"] for item in screenshot().ocr()]
    remaining = parse_build_remaining(texts)
    building = remaining is not None or any(
        "施工剩余时长" in text or "立刻完成" in text for text in texts
    )
    return BuildScreenState(building=building, remaining_seconds=remaining)


def _wait_for_game(timeout: float = 180.0) -> bool:
    if not is_game_running():
        start_game()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and not is_stopped():
        if connect():
            try:
                if screenshot().ocr():
                    return True
            except Exception:
                pass
        time.sleep(5)
    return False


def start_next_passenger_carriage() -> BuildScreenState | None:
    """Navigate from an arbitrary game page and start one passenger carriage."""
    if not _wait_for_game():
        logger.error("游戏启动超时，未开始下一节客厢")
        return None
    go_home()
    if not blurry_ocr_click("整备列车", score=0.6, trynum=3, log=False):
        input_tap((68, 72))
    time.sleep(2)
    if not blurry_ocr_click("编组", score=0.6, trynum=3, log=False):
        input_tap((646, 37))
    time.sleep(2)
    current = inspect_build_screen()
    if current.building:
        logger.info(f"识别到客厢正在施工，游戏剩余 {current.remaining_seconds} 秒")
        return BuildScreenState(True, current.remaining_seconds, True)
    # A completed build returns the workshop to the idle state; opening it also
    # makes the finished carriage available before the next build is selected.
    if not blurry_ocr_click("建造车厢", score=0.6, trynum=3, log=False):
        input_tap((851, 137))
    time.sleep(2)
    if not blurry_ocr_click("客厢", score=0.55, trynum=3, log=False):
        input_tap((338, 487))
    time.sleep(1)
    if not blurry_ocr_click("开始施工", score=0.6, trynum=3, log=False):
        logger.error("未识别到“开始施工”，可能材料不足、车库已满或界面状态异常")
        return None
    time.sleep(1.5)
    state = inspect_build_screen()
    if not state.building:
        logger.error("点击后仍停留在施工确认页，未记录开工时间")
        return None
    logger.info(f"下一节客厢已开始施工，游戏剩余 {state.remaining_seconds} 秒")
    return BuildScreenState(True, state.remaining_seconds, False)


def run_build_monitor() -> bool:
    """Perform one check; the GUI scheduler wakes this task again when due."""
    state = load_build_monitor_plan()
    summary = build_monitor_summary(state)
    if not state:
        logger.warning(summary["message"])
        return False
    if not summary["active"]:
        logger.info(summary["message"])
        return True
    if state.get("active_due_at") and not summary["due_now"]:
        logger.info(summary["message"])
        return True
    if state.get("active_due_at"):
        final_carriage = int(state["completed_carriages"]) + 1 >= int(state["target_carriages"])
        screen_state = None
        if final_carriage:
            if not _wait_for_game():
                return False
        else:
            screen_state = start_next_passenger_carriage()
            if screen_state and screen_state.existing:
                if screen_state.remaining_seconds is not None:
                    resync_active_build(
                        state, remaining_seconds=screen_state.remaining_seconds
                    )
                else:
                    logger.warning("识别到施工中状态，但本次未读出倒计时，稍后重试")
                return True
            if not screen_state:
                return False
        state = record_carriage_completed(state)
        if int(state["completed_carriages"]) >= int(state["target_carriages"]):
            logger.info("客厢连续建造计划已全部完成")
            return True
        record_carriage_started(
            state, remaining_seconds=screen_state.remaining_seconds
        )
        return True
    screen_state = start_next_passenger_carriage()
    if not screen_state:
        return False
    record_carriage_started(
        state, remaining_seconds=screen_state.remaining_seconds
    )
    return True


def stop():
    from core.control.control import stop as stop_control
    stop_control()
