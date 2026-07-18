"""Collect Daily Activity and Travel Manual rewards.

The game UI is normalized to 1280x720 by the control layer.  Navigation uses
OCR for page confirmation and red notification badges only for discovering the
two icon-only home shortcuts.
"""

from __future__ import annotations

import time
import json
import re
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
from core.services.daily_rewards import (
    DailyProgressSnapshot,
    RewardStrategy,
    decide_reward_run,
)
from core.services.server_calendar import SERVER_CLOCK


STATE_PATH = Path("config") / "reward_state.json"
READ_ONLY_SHORTCUTS = {
    "每日活跃": (1048, 82),
    "环游手册": (1132, 82),
}


class RewardTransientError(RuntimeError):
    """Explicit retryable screenshot, OCR, or device-transport failure."""


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
    current = now or datetime.now(SERVER_CLOCK.timezone)
    if current.tzinfo is None or current.utcoffset() is None:
        current = current.replace(
            tzinfo=datetime.now().astimezone().tzinfo
        ).astimezone(SERVER_CLOCK.timezone)
    else:
        current = current.astimezone(SERVER_CLOCK.timezone)
    return SERVER_CLOCK.server_day_id(current)


def _center(item: dict) -> tuple[int, int]:
    points = item["position"]
    return int((points[0][0] + points[2][0]) / 2), int((points[0][1] + points[2][1]) / 2)


def _manual_daily_progress_observation(
    items: list[dict],
) -> tuple[int, int, int, int] | None:
    """Read one uniquely anchored aggregate ratio and preserve its center."""

    task_markers = (
        "姣忔棩浠诲姟", "浠婃棩浠诲姟", "浠诲姟鍒楄〃",
        "每日任务", "今日任务", "任务列表",
    )
    aggregate_markers = (
        "鎬昏繘搴?", "瀹屾垚杩涘害", "总进度", "完成进度", "AGGREGATE",
    )
    task_anchors = [
        _center(item)
        for item in items
        if item.get("position")
        and any(marker in str(item.get("text", "")) for marker in task_markers)
    ]
    aggregate_anchors = [
        _center(item)
        for item in items
        if item.get("position")
        and any(marker in str(item.get("text", "")) for marker in aggregate_markers)
    ]
    if not task_anchors or not aggregate_anchors:
        return None
    candidates: set[tuple[int, int, int, int]] = set()
    for item in items:
        if not item.get("position"):
            continue
        match = re.search(r"(\d+)\s*/\s*(\d+)", str(item.get("text", "")))
        if not match:
            continue
        x, y = _center(item)
        if not (100 <= x <= 620 and 90 <= y <= 360):
            continue
        completed, total = map(int, match.groups())
        if not (0 < total <= 50 and 0 <= completed <= total):
            continue
        for anchor_x, anchor_y in aggregate_anchors:
            # The aggregate value is below its label in the same summary card.
            # Ratios above/beside the label are per-task counters.
            if abs(x - anchor_x) <= 100 and 35 <= y - anchor_y <= 100:
                candidates.add((completed, total, x, y))
    if len(candidates) != 1:
        return None
    return next(iter(candidates))


def _manual_daily_progress(items: list[dict]) -> tuple[int, int] | None:
    observation = _manual_daily_progress_observation(items)
    return observation[:2] if observation is not None else None


def _manual_level_frame_observation(frame) -> tuple | None:
    """Return page anchor, numeric level and claim evidence from fixed ROIs."""

    items = frame.ocr()
    page_markers = ("鐜父鎵嬪唽", "绛夌骇濂栧姳", "环游手册", "等级奖励")
    if not any(
        any(marker in str(item.get("text", "")) for marker in page_markers)
        for item in items
    ):
        return None
    levels: set[tuple[int, int, int]] = set()
    claim_positions: list[tuple[int, int]] = []
    claim_markers = ("鍙鍙?", "涓€閿鍙?", "可领取", "一键领取")
    for item in items:
        if not item.get("position"):
            continue
        x, y = _center(item)
        text = str(item.get("text", ""))
        if 120 <= x <= 560 and 80 <= y <= 260:
            match = re.search(
                r"(?:LV\.?|等级|绛夌骇)\s*[:：]?\s*(\d{1,3})",
                text,
                re.IGNORECASE,
            )
            if match:
                level = int(match.group(1))
                if 1 <= level <= 100:
                    levels.add((level, x, y))
        if 780 <= x <= 1220 and 430 <= y <= 680 and any(
            marker in text for marker in claim_markers
        ):
            claim_positions.append((x, y))
    if len(levels) != 1:
        return None
    image = getattr(frame, "image", None)
    red_dot_center = None
    if isinstance(image, np.ndarray) and image.size:
        hsv = cv.cvtColor(image, cv.COLOR_BGR2HSV)
        roi = hsv[60:680, 760:1240]
        red = cv.bitwise_or(
            cv.inRange(roi, np.array((0, 120, 120)), np.array((10, 255, 255))),
            cv.inRange(roi, np.array((170, 120, 120)), np.array((179, 255, 255))),
        )
        pixels = cv.findNonZero(red)
        if pixels is not None and len(pixels) >= 12:
            mean = pixels.reshape(-1, 2).mean(axis=0)
            red_dot_center = (int(round(mean[0])) + 760, int(round(mean[1])) + 60)
    level, level_x, level_y = next(iter(levels))
    return (
        level,
        len(claim_positions),
        int(red_dot_center is not None),
        level_x,
        level_y,
        tuple(sorted(claim_positions)),
        red_dot_center,
    )


def _centers_stable(first, second, tolerance: int = 8) -> bool:
    if first is None or second is None:
        return first is second
    if isinstance(first, tuple) and first and isinstance(first[0], tuple):
        return len(first) == len(second) and all(
            _centers_stable(left, right, tolerance) for left, right in zip(first, second)
        )
    return abs(first[0] - second[0]) <= tolerance and abs(first[1] - second[1]) <= tolerance


def _manual_level_observations_stable(first: tuple, other: tuple) -> bool:
    return bool(
        first[:3] == other[:3]
        and _centers_stable(first[3:5], other[3:5])
        and _centers_stable(first[5], other[5])
        and _centers_stable(first[6], other[6])
    )


def _observe_manual_level_rewards(driver, stable_frames: int = 2) -> int | None:
    observations = []
    count = max(2, int(stable_frames))
    for index in range(count):
        observation = _manual_level_frame_observation(driver.frame())
        if observation is None:
            return None
        observations.append(observation)
        if index + 1 < count:
            driver.sleep(0.25)
    if any(
        not _manual_level_observations_stable(observations[0], item)
        for item in observations[1:]
    ):
        return None
    _, claimable, red_dot, *_ = observations[0]
    return max(claimable, red_dot)


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
            text = str(item.get("text", ""))
            progress = re.search(r"(\d+)\s*/\s*(\d+)", text)
            if progress:
                value = int(progress.group(1))
            else:
                numbers = re.findall(r"\d+", text)
                value = int(numbers[0]) if len(numbers) == 1 else None
            if value is not None:
                activity = max(activity or 0, value)
    return activity


def _daily_activity_progress(items: list[dict]) -> tuple[int, int] | None:
    """Read current and maximum from the normalized daily-activity region."""

    if not _is_daily_activity_page(items):
        return None
    for item in items:
        position = item.get("position")
        if not position:
            continue
        x, y = _center(item)
        if not (130 <= x <= 340 and 145 <= y <= 260):
            continue
        match = re.search(r"(\d+)\s*/\s*(\d+)", str(item.get("text", "")))
        if not match:
            continue
        current, maximum = map(int, match.groups())
        if 0 < maximum <= 5000 and 0 <= current <= maximum:
            return current, maximum
    return None


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
            stable = READ_ONLY_SHORTCUTS.get(page_marker)
            if stable:
                logger.info(f"{page_marker}当前无提醒角标，使用稳定入口坐标只读复核")
                candidates = [stable]
            else:
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
            progress = _daily_activity_progress(items)
            if progress is None or progress[0] < progress[1]:
                return False
            if any(_matches(str(item.get("text", "")), "可领取") for item in items):
                return False
            if _daily_stage_boxes(observation.image):
                return False
            if frame_index + 1 < frames:
                self.driver.sleep(0.25)
        return True

    def _claim_daily_stage_batch(
        self,
        yellow_boxes: list[tuple[int, int]],
        attempts: int = 2,
        confirmation_frames: int = 3,
    ) -> bool:
        """Claim the game-side stage batch without chasing stale yellow boxes."""
        if not yellow_boxes:
            return False

        # The game claims every unlocked stage reward when any yellow gift is
        # tapped. Prefer the rightmost (highest unlocked) gift and keep the
        # retry on that same target instead of producing clicks across boxes.
        target_x = yellow_boxes[-1][0]
        for attempt_index in range(attempts):
            self.driver.tap((target_x, 164))

            # Let the reward presentation appear before dismissing it. The old
            # 0.8-second blind tap was often early, after which stale page pixels
            # were mistaken for a failed claim and several other boxes got hit.
            self.driver.sleep(1.2)
            self.driver.tap((640, 660))

            saw_daily_page = False
            for frame_index in range(confirmation_frames):
                self.driver.sleep(0.7)
                observation = self.driver.frame()
                items = observation.ocr()
                if not _is_daily_activity_page(items):
                    # A reward presentation may temporarily cover the page.
                    # Dismiss it, but never interpret another page as success.
                    self.driver.tap((640, 660))
                    continue
                saw_daily_page = True
                if not _daily_stage_boxes(observation.image):
                    logger.info("每日活跃阶段奖励已批量领取，并在每日活跃页确认黄色箱消失")
                    return True
                if frame_index + 1 < confirmation_frames:
                    self.driver.sleep(0.4)

            if attempt_index + 1 < attempts:
                reason = "黄色箱仍存在" if saw_daily_page else "领奖后尚未回到每日活跃页"
                logger.info(
                    f"每日活跃阶段箱 x={target_x} {reason}，等待稳定后重试同一箱"
                )

        logger.warning(
            f"每日活跃阶段箱 x={target_x} 两次受控点击后仍未在每日活跃页确认领取，"
            "停止额外点击并留待下次复核"
        )
        return False

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

        # One yellow gift claims all currently unlocked stage rewards. Wait for
        # the UI to stabilize and confirm the result on this page instead of
        # iterating over candidates from stale screenshots.
        yellow_boxes = _daily_stage_boxes(self.driver.frame().image)
        if self._claim_daily_stage_batch(yellow_boxes):
            claimed += 1

        # Reaching 600 only unlocks every stage. Cache completion only after two
        # fresh frames also prove that no task or stage reward remains claimable.
        if self._daily_completion_confirmed():
            self.state["daily_activity_completed_cycle"] = cycle
            _save_state(self.state)
            logger.info("活跃度已达 600 且连续两帧无可领取奖励，记录本周期奖励已完成")
        logger.info(f"每日活跃奖励处理完成，共触发 {claimed} 次领取")
        return claimed

    def observe_daily_activity(self, stable_frames: int = 2) -> dict[str, int] | None:
        """Read a stable, known daily activity value and reward-tier state."""

        if not self._open_from_home("每日活跃"):
            return None
        observations = []
        for frame_index in range(max(2, stable_frames)):
            frame = self.driver.frame()
            items = frame.ocr()
            progress = _daily_activity_progress(items)
            if progress is None:
                return None
            current, maximum = progress
            stage_claimable = len(_daily_stage_boxes(frame.image))
            button_claimable = int(
                any(_matches(str(item.get("text", "")), "可领取") for item in items)
            )
            claimable = max(stage_claimable, button_claimable)
            thresholds = [maximum * index // 6 for index in range(1, 7)]
            locked = sum(1 for threshold in thresholds if current < threshold)
            observations.append((current, maximum, claimable, claimable + locked))
            if frame_index + 1 < max(2, stable_frames):
                self.driver.sleep(0.25)
        if any(item != observations[0] for item in observations[1:]):
            logger.warning("每日活跃 OCR 多帧不稳定，本次保持 UNKNOWN")
            return None
        current, maximum, claimable, unclaimed = observations[0]
        return {
            "current": current,
            "maximum": maximum,
            "claimable_tiers": claimable,
            "unclaimed_tiers": unclaimed,
            "claimed_tiers": max(0, 6 - unclaimed),
        }

    def collect_travel_manual(self) -> int:
        if not self._open_from_home("环游手册"):
            return 0
        claimed = 0
        if self.driver.click_text("任务列表", attempts=2):
            if self._claim_one_click("环游手册任务列表"):
                claimed += 1
            progress = _manual_daily_progress(self.driver.texts())
            if progress is not None:
                completed, total = progress
                self.state["travel_manual_progress"] = {
                    "cycle": _daily_cycle(),
                    "completed": completed,
                    "total": total,
                }
                _save_state(self.state)
        if not self.driver.click_exact_text("环游手册", attempts=2):
            logger.warning("未能准确点击底部‘环游手册’标签，暂不检查等级奖励")
            return claimed

        # The manual page has its own one-click claim at the bottom right.
        # It becomes marked with a red exclamation after task EXP raises levels.
        if self._claim_one_click("环游手册等级奖励"):
            claimed += 1
        logger.info(f"环游手册奖励处理完成，共触发 {claimed} 次领取")
        return claimed

    def observe_travel_manual(self, stable_frames: int = 2) -> dict[str, int] | None:
        """Observe task completion and reward availability as separate facts."""

        if not self._open_from_home("环游手册"):
            return None
        if not self.driver.click_text("任务列表", attempts=2):
            return None
        frames = []
        for frame_index in range(max(2, stable_frames)):
            items = self.driver.texts()
            observation = _manual_daily_progress_observation(items)
            if observation is None:
                return None
            claimable = sum(
                1
                for item in items
                if any(
                    marker in str(item.get("text", ""))
                    for marker in ("可领取", "一键领取")
                )
            )
            frames.append((*observation, claimable))
            if frame_index + 1 < max(2, stable_frames):
                self.driver.sleep(0.25)
        if any(
            item[:2] != frames[0][:2]
            or abs(item[2] - frames[0][2]) > 8
            or abs(item[3] - frames[0][3]) > 8
            or item[4] != frames[0][4]
            for item in frames[1:]
        ):
            logger.warning("手册每日任务 OCR 多帧不稳定，本次保持 UNKNOWN")
            return None
        completed, total, _ratio_x, _ratio_y, task_rewards = frames[0]
        if not self.driver.click_exact_text("环游手册", attempts=2):
            return None
        level_rewards = _observe_manual_level_rewards(
            self.driver, stable_frames=stable_frames
        )
        if level_rewards is None:
            logger.warning("手册等级奖励页多帧证据不稳定，本次保持 UNKNOWN")
            return None
        return {
            "completed": completed,
            "total": total,
            "claimable_rewards": task_rewards + level_rewards,
            "unclaimed_rewards": task_rewards + level_rewards,
        }

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


def collect_scheduled_rewards(
    daily_activity: bool = True,
    travel_manual: bool = True,
    *,
    strategy: str = RewardStrategy.MAXIMIZE_PROGRESS.value,
    running_dependencies: bool = False,
) -> dict:
    """Collect currently unlocked rewards and return a conservative schedule result."""
    now = SERVER_CLOCK.server_now()
    collector = RewardCollector()
    try:
        rewards = collector.run(daily_activity, travel_manual)
    except Exception as error:
        programmer_errors = (NameError, AttributeError, TypeError, AssertionError)
        retryable_runtime = isinstance(error, RuntimeError) and any(
            marker in str(error).lower()
            for marker in ("adb", "ocr", "screenshot", "timeout", "连接", "截图", "识别")
        )
        if isinstance(error, programmer_errors) or not (
            isinstance(error, (RewardTransientError, OSError, TimeoutError, ConnectionError))
            or retryable_runtime
        ):
            raise
        attempt = int(collector.state.get("transient_attempt", 0)) + 1
        collector.state["transient_attempt"] = min(attempt, 99)
        _save_state(collector.state)
        try:
            selected_strategy = RewardStrategy(strategy)
        except ValueError:
            selected_strategy = RewardStrategy.MAXIMIZE_PROGRESS
        logger.exception(
            f"奖励生产观察发生暂时异常，使用有界退避: {type(error).__name__}: {error}"
        )
        decision = decide_reward_run(
            None,
            now=now,
            strategy=selected_strategy,
            transient_error=True,
            attempt=attempt,
            blocked_reasons=(f"{type(error).__name__}: {error}",),
        )
        return {
            **decision.to_dict(),
            "task_rewards": {},
            "rewards_claimed": 0,
            "progress_made": False,
            "completion_predicate": False,
        }
    collector.state["transient_attempt"] = 0
    _save_state(collector.state)
    cycle = SERVER_CLOCK.server_day_id(now)
    daily_observation = (
        {"current": 0, "maximum": 0, "claimable_tiers": 0, "unclaimed_tiers": 0}
        if not daily_activity
        else collector.observe_daily_activity()
    )
    manual_observation = (
        {"completed": 0, "total": 0, "claimable_rewards": 0, "unclaimed_rewards": 0}
        if not travel_manual
        else collector.observe_travel_manual()
    )
    snapshot = DailyProgressSnapshot(
        server_day_id=cycle,
        daily_activity_current=(daily_observation or {}).get("current"),
        daily_activity_max=(daily_observation or {}).get("maximum"),
        daily_activity_source="OCR" if daily_observation is not None else "UNKNOWN",
        daily_activity_confidence="HIGH" if daily_observation is not None else "UNKNOWN",
        daily_activity_claimable_tiers=(daily_observation or {}).get("claimable_tiers"),
        daily_activity_unclaimed_tiers=(daily_observation or {}).get("unclaimed_tiers"),
        handbook_daily_tasks_total=(manual_observation or {}).get("total"),
        handbook_daily_tasks_completed=(manual_observation or {}).get("completed"),
        handbook_rewards_claimable=(manual_observation or {}).get("claimable_rewards"),
        handbook_rewards_unclaimed=(manual_observation or {}).get("unclaimed_rewards"),
        observed_at=now,
        handbook_confidence="HIGH" if manual_observation is not None else "UNKNOWN",
        daily_reward_confidence="HIGH" if daily_observation is not None else "UNKNOWN",
        handbook_reward_confidence=(
            "HIGH" if manual_observation is not None else "UNKNOWN"
        ),
    )
    try:
        selected_strategy = RewardStrategy(strategy)
    except ValueError:
        selected_strategy = RewardStrategy.MAXIMIZE_PROGRESS
    decision = decide_reward_run(
        snapshot,
        now=now,
        strategy=selected_strategy,
        running_dependencies=running_dependencies,
        daily_activity_enabled=daily_activity,
        travel_manual_enabled=travel_manual,
    )
    payload = decision.to_dict()
    payload.update(
        task_rewards=rewards,
        rewards_claimed=sum(rewards.values()),
        progress_made=any(rewards.values()),
        completion_predicate=decision.all_tracked_objectives_complete,
    )
    return payload
