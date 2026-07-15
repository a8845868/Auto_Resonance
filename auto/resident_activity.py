"""Automation for the Comprehensive Preparation permanent activity."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Callable, Optional

from loguru import logger

from core.control.control import connect, input_swipe, input_tap, screenshot
from core.services.screen_state import (
    RESOURCE_DOWNLOAD_CONFIRM_TAP,
    RESOURCE_DOWNLOAD_WAIT_ATTEMPTS,
    clarity_replenish_cancel_position,
    startup_screen_action,
)


SIEGE_TASKS = (
    "特殊订单",
    "利刃行动",
    "挑灯看剑",
    "武器材质分析",
    "骑士小说",
    "我思我在",
    "所知所闻",
    "大的！",
    "总体围剿",
)

# User-facing reward labels.  The game calls these stages by abstract names,
# while players usually decide what to sweep by the material they need.
FULL_REALM_REWARDS = {
    "学会装备箱": "特供·救世",
    "黑月装备箱": "特供·雅致",
    "帝国装备箱": "特供·魔力",
}

SIEGE_REWARDS = {
    "特殊订单": ("纯金零件 / 火控单元", "gold_part.png"),
    "利刃行动": ("桦树核仁 / 劫掠锯轮", "birch_core.png"),
    "挑灯看剑": ("照夜双刃 / 噪音激酶", "night_blades.png"),
    "武器材质分析": ("骨龙头骨 / 骨龙脊骨", "bone_dragon_skull.png"),
    "骑士小说": ("暮光坚壳 / 昏聩头壳", "dim_shell.png"),
    "我思我在": ("笃学灯芯 / 远祖的根系", "learning_wick.png"),
    "所知所闻": ("对策系统载体 / 笃学灯芯", "countermeasure_core.png"),
    "大的！": ("尘鸣坚骨 / 裂首骨龙材料", "hard_bone.png"),
    "总体围剿": ("深眠木 / 游星之眼", "deep_sleep_wood.png"),
}

# The emulator image is normalized to 1280 x 720. Only advance the sweep
# state machine when the expected labels are present in their real screen
# regions; matching the same word in a background layer is not sufficient.
SWEEP_BUTTON_CENTER = (872, 501)
START_SWEEP_BUTTON_CENTER = (772, 526)
DETAIL_SWEEP_ROI = (780, 455, 970, 540)
DETAIL_MARKER_ROI = (55, 645, 170, 705)
TEAM_TITLE_ROI = (540, 130, 735, 200)
TEAM_START_ROI = (635, 485, 930, 565)
REWARD_TITLE_ROI = (500, 70, 820, 270)


def _center(item: dict) -> tuple[int, int]:
    position = item["position"]
    return (
        int((position[0][0] + position[2][0]) / 2),
        int((position[0][1] + position[2][1]) / 2),
    )


def _normalize_text(text: str) -> str:
    return (
        text.replace('"', "")
        .replace("“", "")
        .replace("”", "")
        .replace("！", "!")
        .strip()
    )


def _matches(actual: str, expected: str, *, exact: bool = False) -> bool:
    normalized = _normalize_text(actual)
    expected = _normalize_text(expected)
    if exact:
        return normalized == expected
    return normalized == expected or expected in normalized


@dataclass
class ScreenDriver:
    """Thin device adapter kept separate so the workflow is unit-testable."""

    sleep: Callable[[float], None] = time.sleep

    def texts(self) -> list[dict]:
        return screenshot().ocr()

    def tap(self, pos: tuple[int, int]) -> None:
        input_tap(pos)

    def swipe_left(self) -> None:
        input_swipe((1100, 450), (500, 450), swipe_time=650)
        self.sleep(0.8)

    def swipe_right(self) -> None:
        input_swipe((500, 450), (1100, 450), swipe_time=650)
        self.sleep(0.8)

    def click_text(
        self,
        text: str,
        *,
        offset: tuple[int, int] = (0, 0),
        attempts: int = 5,
        exact: bool = False,
    ) -> bool:
        for _ in range(attempts):
            for item in self.texts():
                if _matches(item["text"], text, exact=exact):
                    x, y = _center(item)
                    self.tap((x + offset[0], y + offset[1]))
                    self.sleep(1)
                    return True
            self.sleep(0.5)
        return False

    def has_text(self, text: str, *, exact: bool = False) -> bool:
        return any(
            _matches(item["text"], text, exact=exact) for item in self.texts()
        )

    def go_home(self) -> bool:
        """Return to the station home without depending on a versioned screenshot."""
        startup_recovery = False
        resource_download_seen = False
        attempt = 0
        attempt_limit = 45
        while attempt < attempt_limit:
            attempt += 1
            texts = self.texts()
            if any(
                _matches(item["text"], marker)
                for item in texts
                for marker in ("作战终端", "访问城市", "启程")
            ):
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
            self.sleep(1)
        return False

    def dismiss_result(self) -> None:
        # Reward dialogs accept a tap on the lower blank area. If there is no
        # dialog this is harmless and avoids depending on animated button text.
        self.tap((640, 660))
        self.sleep(1)


class ResidentActivityAutomation:
    def __init__(self, driver: Optional[ScreenDriver] = None):
        self.driver = driver or ScreenDriver()

    def open_action_summary(self) -> bool:
        if not self.driver.go_home():
            logger.error("无法返回主界面")
            return False
        # The home shortcut is visually stable and avoids OCR accidentally
        # matching the same words in a quest description.
        self.driver.tap((1180, 415))
        self.driver.sleep(2)
        # 常规活动 is an expand/collapse header. Do not click it when the target
        # card is already visible, otherwise it would hide 全域整备.
        if not self.driver.has_text("全域整备"):
            if not self.driver.click_text("常规活动", attempts=2):
                self.driver.tap((180, 150))
                self.driver.sleep(0.8)
        # Fixed card position in the required 1280x720 layout.
        self.driver.tap((110, 285))
        self.driver.sleep(2)
        # First entry can show a dialogue overlay; dismiss it once if needed.
        if not self.driver.has_text("行动汇总"):
            self.driver.tap((800, 100))
            self.driver.sleep(0.8)
        self.driver.tap((1060, 380))
        self.driver.sleep(2)
        if not self.driver.has_text("利刃围剿"):
            logger.error("进入行动汇总失败")
            return False
        return True

    def _reward_attempts(self, fallback: int = 3) -> int:
        """Read an ``n/3`` reward counter; use the safe activity cap on OCR miss."""
        for item in self.driver.texts():
            match = re.search(r"([0-3])\s*/\s*3", item["text"])
            if match:
                return int(match.group(1))
        return fallback

    @staticmethod
    def text_in_roi(
        items: list[dict],
        text: str,
        roi: tuple[int, int, int, int],
        *,
        exact: bool = True,
    ) -> bool:
        """Return true only when OCR text appears inside the expected area."""
        x1, y1, x2, y2 = roi
        for item in items:
            x, y = _center(item)
            if x1 <= x <= x2 and y1 <= y <= y2:
                if _matches(item["text"], text, exact=exact):
                    return True
        return False

    def wait_for_screen(
        self,
        markers: tuple[tuple[str, tuple[int, int, int, int]], ...],
        *,
        attempts: int,
        delay: float = 0.5,
    ) -> bool:
        """Confirm a screen using all region-locked OCR markers."""
        for _ in range(attempts):
            items = self.driver.texts()
            if all(self.text_in_roi(items, text, roi) for text, roi in markers):
                return True
            self.driver.sleep(delay)
        return False

    def sweep_current_activity(self, max_attempts: int) -> int:
        completed = 0
        for _ in range(max_attempts):
            # Strict state machine: detail -> team selection -> reward result.
            # A sweep is counted only after the reward screen is observed.
            if not self.wait_for_screen(
                (("扫荡", DETAIL_SWEEP_ROI), ("难度选择", DETAIL_MARKER_ROI)),
                attempts=4,
            ):
                logger.info("未确认处于关卡详情页，停止扫荡")
                break

            self.driver.tap(SWEEP_BUTTON_CENTER)
            self.driver.sleep(0.8)
            if not self.wait_for_screen(
                (("选择队伍", TEAM_TITLE_ROI), ("开始扫荡", TEAM_START_ROI)),
                attempts=8,
            ):
                logger.warning("点击“扫荡”后未进入队伍选择页，本次不计入完成")
                break

            self.driver.tap(START_SWEEP_BUTTON_CENTER)
            self.driver.sleep(0.8)
            if not self.wait_for_screen(
                (("获得物品", REWARD_TITLE_ROI),), attempts=14
            ):
                logger.warning("点击“开始扫荡”后未出现“获得物品”，本次不计入完成")
                break

            self.driver.dismiss_result()
            completed += 1
        return completed

    def run_limited_activity(self, name: str, stage: Optional[str] = None) -> int:
        if not self.driver.click_text(name):
            logger.warning(f"未找到活动：{name}")
            return 0
        attempts = self._reward_attempts()
        if attempts == 0:
            logger.info(f"{name}次数已用完")
            return 0
        if stage and not self.driver.click_text(stage, attempts=3):
            logger.warning(f"{name}未找到目标奖励关卡：{stage}")
            return 0
        # The selected limited activity opens its stage list. Enter the visible
        # challenge and then use the sweep button on the detail screen.
        if not self.driver.click_text("进入挑战", attempts=3):
            logger.warning(f"{name}没有可进入的挑战")
            return 0
        completed = self.sweep_current_activity(attempts)
        logger.info(f"{name}完成 {completed}/{attempts} 次")
        return completed

    def select_siege_task(self, task: str) -> bool:
        if task not in SIEGE_TASKS:
            raise ValueError(f"未知利刃围剿任务：{task}")

        # Reset the horizontal carousel to its left edge, then scan each page.
        for _ in range(4):
            self.driver.swipe_right()
        for _ in range(7):
            for item in self.driver.texts():
                if _matches(item["text"], task):
                    x, y = _center(item)
                    # The challenge button is directly below the task title.
                    self.driver.tap((x, min(y + 190, 620)))
                    self.driver.sleep(1)
                    return True
            self.driver.swipe_left()
        logger.error(f"未找到利刃围剿任务：{task}")
        return False

    def run_siege(self, task: str, safety_limit: int = 100) -> int:
        if not self.driver.click_text("利刃围剿"):
            return 0
        if not self.select_siege_task(task):
            return 0

        completed = self.sweep_current_activity(safety_limit)
        if completed < safety_limit:
            logger.info("澄清度不足、扫荡不可用或扫荡确认失败，停止利刃围剿")
        logger.info(f"{task}完成 {completed} 次")
        return completed

    def run(self, task: str, full_realm_reward: str = "学会装备箱") -> dict[str, int]:
        if not connect():
            raise RuntimeError("ADB连接失败")
        if not self.open_action_summary():
            raise RuntimeError("无法打开活动总览，未执行扫荡与全域整备")

        results = {"私贩追缴": self.run_limited_activity("私贩追缴")}
        # Limited activity detail screens have a back button. Re-open the summary
        # instead of relying on a particular post-reward screen.
        if not self.open_action_summary():
            raise RuntimeError("无法重新打开活动总览，未完成全境特供")
        stage = FULL_REALM_REWARDS.get(full_realm_reward)
        results["全境特供"] = self.run_limited_activity("全境特供", stage=stage)
        if not self.open_action_summary():
            raise RuntimeError(f"无法重新打开活动总览，未完成{task}")
        results[task] = self.run_siege(task)
        return results

    def run_once(self, task: str) -> dict[str, int]:
        """Run exactly one selected siege sweep for end-to-end verification."""
        if not connect():
            raise RuntimeError("ADB连接失败")
        if not self.open_action_summary():
            return {task: 0}
        if not self.driver.click_text("利刃围剿"):
            return {task: 0}
        if not self.select_siege_task(task):
            return {task: 0}
        completed = self.sweep_current_activity(1)
        logger.info(f"单次扫荡验证完成：{task} {completed}/1 次")
        return {task: completed}


def run_resident_activity(
    task: str, full_realm_reward: str = "学会装备箱"
) -> dict[str, int]:
    """GUI entry point."""
    return ResidentActivityAutomation().run(task, full_realm_reward)


def run_resident_activity_once(task: str) -> dict[str, int]:
    """GUI entry point for one non-repeating verification sweep."""
    return ResidentActivityAutomation().run_once(task)
