"""Automation for the Comprehensive Preparation permanent activity."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Callable, Optional

from loguru import logger

from core.control.control import connect, input_swipe, input_tap, screenshot
from core.control.adb_port import EmulatorInfo, get_adb_port


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


def _find_resonance_port(devices: list[EmulatorInfo]) -> Optional[int]:
    """Return the live MuMu instance explicitly named for Resonance."""
    for device in devices:
        if device.port and "雷索纳斯" in device.name:
            return int(device.port)
    return None


def connect_resonance() -> bool:
    """Connect to Resonance instead of accepting any reachable emulator."""
    try:
        port = _find_resonance_port(get_adb_port())
    except Exception:
        logger.exception("自动识别雷索纳斯模拟器失败，回退到已配置端口")
        port = None
    if port:
        logger.info(f"自动选择雷索纳斯模拟器 ADB 端口：{port}")
        return bool(connect(port))
    logger.warning("未发现名为“雷索纳斯”的 MuMu 实例，回退到已配置端口")
    return bool(connect())


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
    return normalized == expected if exact else normalized == expected or expected in normalized


@dataclass
class ScreenDriver:
    """Thin device adapter kept separate so the workflow is unit-testable."""

    sleep: Callable[[float], None] = time.sleep

    def texts(self) -> list[dict]:
        return screenshot().ocr()

    def tap(self, pos: tuple[int, int], *, precise: bool = False) -> None:
        input_tap(pos, random_offset=not precise)

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
        precise: bool = False,
    ) -> bool:
        for _ in range(attempts):
            for item in self.texts():
                if _matches(item["text"], text, exact=exact):
                    x, y = _center(item)
                    self.tap((x + offset[0], y + offset[1]), precise=precise)
                    self.sleep(1)
                    return True
            self.sleep(0.5)
        return False

    def has_text(self, text: str) -> bool:
        return any(_matches(item["text"], text) for item in self.texts())

    def go_home(self) -> bool:
        """Return to the station home without depending on a versioned screenshot."""
        for _ in range(12):
            texts = self.texts()
            if any(
                _matches(item["text"], marker)
                for item in texts
                for marker in ("作战终端", "访问城市", "启程")
            ):
                return True
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

    def click_action_button(
        self,
        text: str,
        *,
        fallback: tuple[int, int],
        screen_marker: Optional[str] = None,
        attempts: int = 3,
    ) -> bool:
        """Click an action label precisely, with a measured fallback point."""
        # ADB evidence shows the label center is the reliable hit target.  The
        # visual center of buttons that also contain a cost row can miss.
        if self.driver.click_text(
            text, attempts=attempts, exact=True, precise=True
        ):
            return True
        if screen_marker and self.driver.has_text(screen_marker):
            logger.warning(f"未识别到“{text}”文字，使用已验证坐标兜底")
            self.driver.tap(fallback, precise=True)
            self.driver.sleep(1)
            return True
        return False

    def sweep_current_activity(self, max_attempts: int) -> int:
        completed = 0
        for _ in range(max_attempts):
            # Use exact matching so the confirmation button "开始扫荡" cannot
            # be mistaken for the initial "扫荡" button on a stale dialog.
            if not self.click_action_button(
                "扫荡",
                fallback=(871, 490),
                screen_marker="难度选择",
                attempts=2,
            ):
                break
            if not self.click_action_button(
                "开始扫荡",
                fallback=(771, 526),
                screen_marker="选择队伍",
                attempts=3,
            ):
                logger.warning("已打开扫荡队伍选择，但未找到“开始扫荡”，本次不计入完成")
                break
            self.driver.sleep(1.5)
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
        if not self.driver.click_text(
            "进入挑战", attempts=3, exact=True, precise=True
        ):
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
            items = self.driver.texts()
            if any(
                _matches(item["text"], "挑战次数已用完") for item in items
            ):
                logger.info("利刃围剿今日挑战次数已用完")
                return False
            for item in items:
                if _matches(item["text"], task):
                    x, y = _center(item)
                    challenge_items = [
                        candidate
                        for candidate in items
                        if _matches(candidate["text"], "进入挑战", exact=True)
                    ]
                    if challenge_items:
                        challenge = min(
                            challenge_items,
                            key=lambda candidate: abs(_center(candidate)[0] - x),
                        )
                        target = _center(challenge)
                    else:
                        target = (x, min(y + 190, 606))
                    for _ in range(2):
                        self.driver.tap(target, precise=True)
                        self.driver.sleep(1.2)
                        if self.driver.has_text("扫荡"):
                            return True
                    logger.warning(f"已点击{task}的进入挑战，但未进入任务详情")
                    return False
            self.driver.swipe_left()
        logger.error(f"未找到利刃围剿任务：{task}")
        return False

    def run_siege(self, task: str, safety_limit: int = 100) -> int:
        if not self.select_siege_task(task):
            return 0

        completed = self.sweep_current_activity(safety_limit)
        if completed < safety_limit:
            logger.info("澄清度不足、扫荡不可用或队伍确认失败，停止利刃围剿")
        logger.info(f"{task}完成 {completed} 次")
        return completed

    def run(self, task: str, full_realm_reward: str = "学会装备箱") -> dict[str, int]:
        if not connect_resonance():
            raise RuntimeError("ADB连接失败")
        if not self.open_action_summary():
            return {"私贩追缴": 0, "全境特供": 0, task: 0}

        results = {"私贩追缴": self.run_limited_activity("私贩追缴")}
        # Limited activity detail screens have a back button. Re-open the summary
        # instead of relying on a particular post-reward screen.
        if not self.open_action_summary():
            results.update({"全境特供": 0, task: 0})
            return results
        stage = FULL_REALM_REWARDS.get(full_realm_reward)
        results["全境特供"] = self.run_limited_activity("全境特供", stage=stage)
        if not self.open_action_summary():
            results[task] = 0
            return results
        results[task] = self.run_siege(task)
        return results

    def run_once(self, task: str) -> dict[str, int]:
        """Run exactly one selected siege sweep for end-to-end verification."""
        if not connect_resonance():
            raise RuntimeError("ADB连接失败")
        if not self.open_action_summary():
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
