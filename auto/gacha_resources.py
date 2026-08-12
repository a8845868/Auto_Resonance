from __future__ import annotations

import re
import time

import cv2 as cv
from loguru import logger

from core.control.control import connect, input_tap, screenshot
from core.preset import go_home
from core.services.runtime_errors import BlockedBySafetyError
from core.utils.utils import RESOURCES_PATH


def _find_recruit_entry(image) -> tuple[tuple[int, int] | None, float]:
    template = cv.imread(str(RESOURCES_PATH / "gacha" / "recruit_entry.png"))
    if template is None:
        return None, 0.0
    height, width = image.shape[:2]
    x1 = int(width * 0.55)
    roi = image[0 : int(height * 0.20), x1:width]
    best_score = 0.0
    best_location = None
    for scale_percent in range(45, 101, 5):
        scale = scale_percent / 100
        resized = cv.resize(template, None, fx=scale, fy=scale, interpolation=cv.INTER_AREA)
        th, tw = resized.shape[:2]
        if th >= roi.shape[0] or tw >= roi.shape[1]:
            continue
        result = cv.matchTemplate(roi, resized, cv.TM_CCOEFF_NORMED)
        _, score, _, location = cv.minMaxLoc(result)
        if score > best_score:
            best_score = score
            best_location = (x1 + location[0] + tw // 2, location[1] + th // 2)
    return (best_location if best_score >= 0.68 else None), best_score


def parse_recruit_resource_counts(items: list[dict], screen_width: int = 1280) -> dict[str, int]:
    """Read the two numeric fields in the recruitment page's top-right bar."""
    ticket_values = []
    stone_values = []
    for item in items:
        position = item.get("position") or []
        if len(position) < 3:
            continue
        x = (position[0][0] + position[2][0]) / 2
        y = (position[0][1] + position[2][1]) / 2
        match = re.search(r"[\d,]+", str(item.get("text", "")).replace("，", ","))
        if not match or y > 85:
            continue
        value = int(match.group(0).replace(",", ""))
        relative_x = x / max(1, screen_width)
        if 0.77 <= relative_x < 0.86:
            ticket_values.append(value)
        elif 0.88 <= relative_x <= 0.995:
            stone_values.append(value)
    result = {}
    if ticket_values:
        result["tickets"] = max(ticket_values)
    if stone_values:
        result["stones"] = max(stone_values)
    return result


def scan_gacha_resources() -> dict[str, int]:
    """Open recruitment from the home toolbar and read protocol/stone balances."""
    if not connect():
        raise BlockedBySafetyError("ADB 连接失败，请先在“ADB信息”中确认模拟器连接")
    try:
        if not go_home():
            raise BlockedBySafetyError("无法返回游戏主界面")
        home = screenshot()
        location, score = _find_recruit_entry(home.image)
        if not location:
            raise BlockedBySafetyError(
                f"未找到主界面的招募入口图标（最高匹配度 {score:.3f}）"
            )
        logger.info(f"识别到招募入口：{location}（匹配度 {score:.3f}）")
        input_tap(location)
        time.sleep(2.0)
        recruit = screenshot()
        values = parse_recruit_resource_counts(recruit.ocr(), recruit.image.shape[1])
        if "tickets" not in values or "stones" not in values:
            missing = "、".join(name for key, name in (("tickets", "拉普拉斯协议"), ("stones", "桦石")) if key not in values)
            raise BlockedBySafetyError(f"已进入招募页面，但未识别到{missing}数量")
        logger.info(f"抽卡资源读取完成：协议 {values['tickets']}，桦石 {values['stones']}")
        return values
    finally:
        try:
            go_home()
        except Exception:
            logger.warning("读取抽卡资源后返回主界面失败")
