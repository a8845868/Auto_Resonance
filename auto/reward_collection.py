"""Collect Daily Activity and Travel Manual rewards.

The game UI is normalized to 1280x720 by the control layer.  Navigation uses
OCR for page confirmation and red notification badges only for discovering the
two icon-only home shortcuts.
"""

from __future__ import annotations

import time
import json
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Optional

import cv2 as cv
import numpy as np
from loguru import logger

from core.control.control import connect, input_tap, screenshot
from core.services.screen_state import (
    RESOURCE_DOWNLOAD_CONFIRM_TAP,
    RESOURCE_DOWNLOAD_WAIT_ATTEMPTS,
    clarity_replenish_cancel_position,
    startup_screen_action,
)


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


def _daily_stage_boxes(image) -> list[tuple[int, int]]:
    """Return currently claimable yellow stage boxes from a fresh frame."""
    hsv = cv.cvtColor(image, cv.COLOR_BGR2HSV)
    boxes = []
    for x in (439, 562, 684, 806, 929, 1051):
        patch = hsv[135:195, x - 30:x + 30]
        yellow = cv.inRange(patch, np.array((15, 100, 120)), np.array((40, 255, 255)))
        yellow_count = cv.countNonZero(yellow)
        if yellow_count >= 25:
            boxes.append((x, yellow_count))
    return boxes


def _daily_activity_value(items: list[dict]) -> int | None:
    """Read the current daily activity total from its fixed normalized region."""
    activity = None
    for item in items:
        x, y = _center(item)
        if 150 <= x <= 300 and 160 <= y <= 240:
            digits = "".join(character for character in item["text"] if character.isdigit())
            if digits:
                activity = max(activity or 0, int(digits))
    return activity


def _is_daily_activity_page(items: list[dict]) -> bool:
    texts = [str(item.get("text", "")) for item in items]
    return any(_matches(text, "每日活跃") for text in texts) or (
        any(_matches(text, "完成进度") for text in texts)
        and any(_matches(text, "活跃度") for text in texts)
    )


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
        startup_recovery = False
        resource_download_seen = False
        attempt = 0
        attempt_limit = 45
        while attempt < attempt_limit:
            attempt += 1
            texts = self.texts()
            if any(_matches(i["text"], marker) for i in texts for marker in ("访问城市", "启程", "作战终端")):
                return True
            clarity_cancel = clarity_replenish_cancel_position(texts)
            if clarity_cancel is not None:
                logger.info("检测到澄明度补充提示，取消后继续返回主界面")
                self.tap(clarity_cancel)
                self.sleep(1)
                continue
            action = startup_screen_action(texts)
            if action == "cancel_resource_repair":
                logger.warning("检测到资源完整性修复提示，取消修复")
                self.tap((320, 500))
                startup_recovery = True
                self.sleep(1)
                continue
            if action == "confirm_resource_download":
                logger.info("检测到登录前资源包更新提示，确认下载并等待完成")
                self.tap(RESOURCE_DOWNLOAD_CONFIRM_TAP)
                startup_recovery = True
                if not resource_download_seen:
                    attempt_limit = max(
                        attempt_limit,
                        attempt + RESOURCE_DOWNLOAD_WAIT_ATTEMPTS,
                    )
                    resource_download_seen = True
                self.sleep(2)
                continue
            if action == "enter_game":
                logger.info("检测到游戏登录页，点击安全区域进入游戏")
                self.tap((640, 560))
                startup_recovery = True
                self.sleep(4)
                continue
            if action == "dismiss_startup_overlay":
                logger.info("关闭登录后的启动弹窗")
                self.tap((100, 650))
                startup_recovery = True
                self.sleep(1)
                continue
            if action == "wait_for_game" or startup_recovery:
                self.sleep(2)
                continue
            self.tap((82, 36))
            self.sleep(0.8)
        return False

    def click_exact_text(self, text: str, attempts: int = 3) -> bool:
        """Click an exact OCR label, avoiding prefix matches such as
        ``环游手册等级`` when the requested bottom tab is ``环游手册``.
        """
        expected = text.replace(" ", "")
        for _ in range(attempts):
            for item in self.texts():
                if item["text"].replace(" ", "") == expected:
                    self.tap(_center(item))
                    self.sleep(1)
                    return True
            self.sleep(0.4)
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

    def _page_is_open(self, page_marker: str) -> bool:
        if self.driver.has_text(page_marker):
            return True
        if page_marker == "每日活跃":
            texts = [item["text"] for item in self.driver.texts()]
            return (
                any(_matches(text, "完成进度") for text in texts)
                and any(_matches(text, "活跃度") for text in texts)
            )
        return False

    def _matching_text_count(self, text: str) -> int:
        return sum(
            1 for item in self.driver.texts()
            if _matches(item["text"], text)
        )

    def _open_from_home(self, page_marker: str) -> bool:
        if not self.driver.go_home():
            raise RuntimeError("无法返回主界面，取消领取奖励")
        candidates = self.driver.red_badge_shortcuts()
        learned = self.state.get("shortcut_positions", {}).get(page_marker)
        if learned:
            nearby = [
                pos for pos in candidates
                if abs(pos[0] - learned[0]) <= 35 and abs(pos[1] - learned[1]) <= 35
            ]
            if nearby:
                # The learned coordinate is only a search hint.  Keep every
                # current badge as a fallback because home shortcuts may move
                # after an update or a resolution/layout change.
                candidates = nearby + [pos for pos in candidates if pos not in nearby]
            else:
                # A missing badge normally means "nothing pending", not that
                # the shortcut moved.  Open the learned shortcut once and
                # inspect the page; if it is genuinely stale, the remaining
                # current badges are still tried afterwards.
                logger.info(f"{page_marker}入口当前无角标，使用缓存坐标复核页面")
                learned_pos = tuple(learned)
                candidates = [learned_pos] + [
                    pos for pos in candidates if pos != learned_pos
                ]

        if not candidates:
            logger.info(f"主界面没有发现可用于进入{page_marker}的提醒角标")
            return False

        for pos in candidates:
            self.driver.tap(pos)
            self.driver.sleep(1.5)
            if self._page_is_open(page_marker):
                positions = self.state.setdefault("shortcut_positions", {})
                positions[page_marker] = list(pos)
                _save_state(self.state)
                return True
            self.driver.tap((82, 36))
            self.driver.sleep(0.8)
        logger.info(f"没有找到带提醒角标的{page_marker}入口")
        return False

    def _claim_one_click(self, area: str) -> bool:
        """Claim a one-click batch and require the actionable button to go away."""
        if not self.driver.click_text("一键领取", attempts=2):
            return False
        self.driver.tap((640, 660))
        self.driver.sleep(0.8)
        if self.driver.has_text("一键领取"):
            logger.warning(f"{area}的一键领取按钮点击后仍存在，本次不计为已领取")
            return False
        logger.info(f"{area}的一键领取按钮已消失，确认领取成功")
        return True

    def _daily_completion_confirmed(self, frames: int = 2) -> bool:
        """Require two clean frames before treating the daily reward page as complete."""
        for frame_index in range(frames):
            observation = self.driver.frame()
            items = observation.ocr()
            if not _is_daily_activity_page(items):
                return False
            activity = _daily_activity_value(items)
            if activity is None or activity < 600:
                return False
            if any(_matches(str(item.get("text", "")), "可领取") for item in items):
                return False
            if _daily_stage_boxes(observation.image):
                return False
            if frame_index + 1 < frames:
                self.driver.sleep(0.25)
        return True

    def collect_daily_activity(self) -> int:
        cycle = _daily_cycle()
        cached_complete = self.state.get("daily_activity_completed_cycle") == cycle
        if not self._open_from_home("每日活跃"):
            return 0
        if cached_complete:
            logger.info("本周期存在每日活跃完成缓存，执行两帧轻量复核")
            if self._daily_completion_confirmed():
                logger.info("连续两帧确认无可领取任务和阶段箱，跳过重复领取")
                return 0
            self.state.pop("daily_activity_completed_cycle", None)
            _save_state(self.state)
            logger.warning("每日活跃完成缓存与当前页面不一致，已撤销并重新检查奖励")

        # One task click often claims all completed tasks, but require the
        # actionable-label count to decrease before treating it as success.
        claimed = 0
        for _ in range(8):
            before = self._matching_text_count("可领取")
            if before == 0:
                break
            if not self.driver.click_text("可领取", attempts=1):
                break
            self.driver.tap((640, 660))
            self.driver.sleep(0.8)
            after = self._matching_text_count("可领取")
            if after >= before:
                logger.warning("每日活跃的可领取状态点击后没有减少，本次不计成功")
                break
            claimed += 1

        # Stage boxes have fixed positions in the normalized layout. Claim one
        # fresh box at a time and rescan; never assume one click claimed all.
        yellow_boxes = _daily_stage_boxes(self.driver.frame().image)
        failed_attempts: Counter[int] = Counter()
        for _ in range(6):
            if not yellow_boxes:
                break
            x, before_yellow = yellow_boxes[0]
            self.driver.tap((x, 164))
            self.driver.sleep(0.8)
            self.driver.tap((640, 660))
            self.driver.sleep(0.5)
            updated_boxes = _daily_stage_boxes(self.driver.frame().image)
            updated_by_x = dict(updated_boxes)
            if x not in updated_by_x:
                claimed += 1
                logger.info(
                    f"每日活跃阶段奖励已触发，重新扫描后剩余 {len(updated_boxes)} 个黄色箱"
                )
                yellow_boxes = updated_boxes
            else:
                failed_attempts[x] += 1
                if failed_attempts[x] < 2:
                    logger.warning(f"每日活跃阶段箱 x={x} 首次点击后仍存在，稍后重试")
                    self.driver.sleep(0.5)
                    yellow_boxes = updated_boxes
                    continue
                logger.warning(f"每日活跃阶段箱 x={x} 连续点击无效，改试其他黄色箱")
                yellow_boxes = [box for box in updated_boxes if box[0] != x] + [
                    box for box in updated_boxes if box[0] == x
                ]

        # Reaching 600 only unlocks every stage. Cache completion only after two
        # fresh frames also prove that no task or stage reward remains claimable.
        if self._daily_completion_confirmed():
            self.state["daily_activity_completed_cycle"] = cycle
            _save_state(self.state)
            logger.info("活跃度已达 600 且连续两帧无可领取奖励，记录本周期奖励已完成")
        logger.info(f"每日活跃奖励处理完成，共触发 {claimed} 次领取")
        return claimed

    def collect_travel_manual(self) -> int:
        if not self._open_from_home("环游手册"):
            return 0
        claimed = 0
        if self.driver.click_text("任务列表", attempts=2):
            if self._claim_one_click("环游手册任务列表"):
                claimed += 1
        if not self.driver.click_exact_text("环游手册", attempts=2):
            logger.warning("未能准确点击底部‘环游手册’标签，暂不检查等级奖励")
            return claimed

        # The manual page has its own one-click claim at the bottom right.
        # It becomes marked with a red exclamation after task EXP raises levels.
        if self._claim_one_click("环游手册等级奖励"):
            claimed += 1
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
