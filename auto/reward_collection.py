"""Collect Daily Activity and Travel Manual rewards.

The game UI is normalized to 1280x720 by the control layer.  Navigation uses
OCR for page confirmation and red notification badges only for discovering the
two icon-only home shortcuts.
"""

from __future__ import annotations

import time
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Optional

import cv2 as cv
import numpy as np
from loguru import logger

from core.control.control import connect, input_tap, screenshot


STATE_PATH = Path("config") / "reward_state.json"


def _load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def _daily_cycle(now: Optional[datetime] = None) -> str:
    """Return the game-day key; daily tasks refresh at local time 05:00."""
    current = now or datetime.now()
    if current.hour < 5:
        current -= timedelta(days=1)
    return current.date().isoformat()


def _center(item: dict) -> tuple[int, int]:
    points = item["position"]
    return int((points[0][0] + points[2][0]) / 2), int((points[0][1] + points[2][1]) / 2)


def _matches(actual: str, expected: str) -> bool:
    return expected.replace(" ", "") in actual.replace(" ", "")


@dataclass
class RewardDriver:
    sleep: Callable[[float], None] = time.sleep

    def frame(self):
        return screenshot()

    def texts(self) -> list[dict]:
        return self.frame().ocr()

    def tap(self, pos: tuple[int, int]) -> None:
        input_tap(pos)

    def click_text(self, text: str, attempts: int = 3) -> bool:
        for _ in range(attempts):
            for item in self.texts():
                if _matches(item["text"], text):
                    self.tap(_center(item))
                    self.sleep(1)
                    return True
            self.sleep(0.4)
        return False

    def has_text(self, text: str) -> bool:
        return any(_matches(item["text"], text) for item in self.texts())

    def go_home(self) -> bool:
        for _ in range(12):
            texts = self.texts()
            if any(_matches(i["text"], marker) for i in texts for marker in ("访问城市", "启程", "作战终端")):
                return True
            self.tap((82, 36))
            self.sleep(0.8)
        return False

    def red_badge_shortcuts(self) -> list[tuple[int, int]]:
        """Return icon centers inferred from small red exclamation badges."""
        image = self.frame().image
        hsv = cv.cvtColor(image, cv.COLOR_BGR2HSV)
        mask1 = cv.inRange(hsv, np.array((0, 150, 120)), np.array((10, 255, 255)))
        mask2 = cv.inRange(hsv, np.array((170, 150, 120)), np.array((179, 255, 255)))
        mask = cv.morphologyEx(mask1 | mask2, cv.MORPH_CLOSE, np.ones((3, 3), np.uint8))
        count, _, stats, centers = cv.connectedComponentsWithStats(mask)
        result = []
        for index in range(1, count):
            x, y, width, height, area = stats[index]
            if 6 <= width <= 35 and 6 <= height <= 35 and 35 <= area <= 700:
                badge_x, badge_y = centers[index]
                result.append((int(badge_x - 25), int(badge_y + 20)))
        return result


class RewardCollector:
    def __init__(self, driver: Optional[RewardDriver] = None):
        self.driver = driver or RewardDriver()
        self.state = _load_state()

    def _open_from_home(self, page_marker: str) -> bool:
        if not self.driver.go_home():
            logger.error("无法返回主界面，取消领取奖励")
            return False
        candidates = self.driver.red_badge_shortcuts()
        learned = self.state.get("shortcut_positions", {}).get(page_marker)
        if learned:
            nearby = [
                pos for pos in candidates
                if abs(pos[0] - learned[0]) <= 35 and abs(pos[1] - learned[1]) <= 35
            ]
            if not nearby:
                logger.info(f"{page_marker}入口没有红点，当前无奖励可领取")
                return False
            candidates = nearby

        for pos in candidates:
            self.driver.tap(pos)
            self.driver.sleep(1.5)
            if self.driver.has_text(page_marker):
                positions = self.state.setdefault("shortcut_positions", {})
                positions[page_marker] = list(pos)
                _save_state(self.state)
                return True
            self.driver.tap((82, 36))
            self.driver.sleep(0.8)
        logger.info(f"没有找到带提醒角标的{page_marker}入口")
        return False

    def collect_daily_activity(self) -> int:
        cycle = _daily_cycle()
        if self.state.get("daily_activity_completed_cycle") == cycle:
            logger.info("本周期每日活跃奖励已全部领取，跳过检查")
            return 0
        if not self._open_from_home("每日活跃"):
            return 0
        self.driver.click_text("每日活跃", attempts=1)

        # One task click currently claims all completed tasks, but keep a small
        # loop for game versions that require individual clicks.
        claimed = 0
        for _ in range(8):
            if not self.driver.click_text("可领取", attempts=1):
                break
            claimed += 1

        # Stage boxes have fixed positions in the normalized layout.  Only tap
        # boxes whose center area is yellow; blue/unavailable boxes are skipped.
        frame = self.driver.frame().image
        hsv = cv.cvtColor(frame, cv.COLOR_BGR2HSV)
        for x in (439, 562, 684, 806, 929, 1051):
            patch = hsv[135:195, x - 30:x + 30]
            yellow = cv.inRange(patch, np.array((15, 100, 120)), np.array((40, 255, 255)))
            if cv.countNonZero(yellow) >= 25:
                self.driver.tap((x, 164))
                self.driver.sleep(0.8)
                self.driver.tap((640, 660))
                self.driver.sleep(0.5)
                claimed += 1

        # Only cache a completed day after the UI itself confirms 600 activity.
        # If it is below 600, later tasks can still make more rewards available.
        activity = None
        for item in self.driver.texts():
            x, y = _center(item)
            if 150 <= x <= 300 and 160 <= y <= 240:
                digits = "".join(character for character in item["text"] if character.isdigit())
                if digits:
                    activity = max(activity or 0, int(digits))
        if activity is not None and activity >= 600:
            self.state["daily_activity_completed_cycle"] = cycle
            _save_state(self.state)
            logger.info("检测到每日活跃度已达 600，记录本周期奖励已完成")
        logger.info(f"每日活跃奖励处理完成，共触发 {claimed} 次领取")
        return claimed

    def collect_travel_manual(self) -> int:
        if not self._open_from_home("环游手册"):
            return 0
        claimed = 0
        if self.driver.click_text("任务列表", attempts=2):
            if self.driver.click_text("一键领取", attempts=2):
                claimed += 1
                self.driver.tap((640, 660))
                self.driver.sleep(0.8)
        if not self.driver.click_text("环游手册", attempts=2):
            return claimed

        # The manual page has its own one-click claim at the bottom right.
        # It becomes marked with a red exclamation after task EXP raises levels.
        if self.driver.click_text("一键领取", attempts=2):
            claimed += 1
            self.driver.tap((640, 660))
            self.driver.sleep(0.8)
        logger.info(f"环游手册奖励处理完成，共触发 {claimed} 次领取")
        return claimed

    def run(self, daily_activity: bool = True, travel_manual: bool = True) -> dict[str, int]:
        if not connect():
            raise RuntimeError("ADB连接失败")
        result = {"每日活跃": 0, "环游手册": 0}
        if daily_activity:
            result["每日活跃"] = self.collect_daily_activity()
        if travel_manual:
            result["环游手册"] = self.collect_travel_manual()
        return result


def collect_rewards(daily_activity: bool = True, travel_manual: bool = True) -> dict[str, int]:
    return RewardCollector().run(daily_activity, travel_manual)
