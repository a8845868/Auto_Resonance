"""
Author: Night-stars-1 nujj1042633805@gmail.com
Date: 2024-03-20 22:24:35
LastEditTime: 2025-02-11 16:56:45
LastEditors: Night-stars-1 nujj1042633805@gmail.com
"""

import random
import time
from typing import Optional, Tuple

import cv2 as cv
import numpy as np
from loguru import logger

from core.control.adb import ADB
from core.control.adb_port import EmulatorInfo, EmulatorType
from core.control.base_control import IADB
from core.control.nemu import NEMU
from core.exception.exceptions import StopExecution
from core.image.image import Image
from core.model import app
from core.services.repair_safety import ensure_automation_allowed

EXCURSIONX = [-10, 10]
EXCURSIONY = [-10, 10]
STOP = False
MAX_SWIPE_SEGMENTS = 100

control: IADB = ADB()
_runtime_device: EmulatorInfo | None = None
_runtime_auto_start_emulator: bool | None = None


def set_runtime_device(device: EmulatorInfo | None) -> None:
    """Freeze the target used by an active queue independently of GUI config."""

    global _runtime_device
    _runtime_device = (
        EmulatorInfo.from_dict(device.to_dict()) if device is not None else None
    )


def clear_runtime_device() -> None:
    global _runtime_auto_start_emulator
    set_runtime_device(None)
    _runtime_auto_start_emulator = None


def has_runtime_device() -> bool:
    return _runtime_device is not None


def set_runtime_auto_start_emulator(enabled: bool) -> None:
    global _runtime_auto_start_emulator
    _runtime_auto_start_emulator = bool(enabled)


def get_runtime_auto_start_emulator() -> bool:
    if _runtime_auto_start_emulator is None:
        return True
    return _runtime_auto_start_emulator


def get_runtime_device() -> EmulatorInfo:
    device = _runtime_device or app.Global.device
    # Tests and legacy callers may provide a lightweight device-like object.
    # Preserve it instead of requiring the newer serialisation API.
    if not hasattr(device, "to_dict"):
        return device
    return EmulatorInfo.from_dict(device.to_dict())


def _close_backend(candidate: IADB, name: str) -> None:
    try:
        candidate.kill()
    except Exception:
        logger.exception(f"{name}连接资源清理失败")


def _activate_backend(candidate: IADB) -> None:
    global control
    previous = control
    control = candidate
    if previous is not candidate:
        _close_backend(previous, "旧控制后端")


def connect(adb_port: Optional[int] = None):
    """
    连接ADB

    :param order: ADB端口
    """
    ensure_automation_allowed("连接 ADB/NEMU")
    global control
    device = get_runtime_device()
    if device.is_mumu:
        nemu_candidate = None
        try:
            nemu_candidate = NEMU(device)
            status = nemu_candidate.connect(adb_port)
        except Exception:
            logger.exception("MUMUIPC连接异常，尝试使用ADB连接")
            status = False
        if status:
            _activate_backend(nemu_candidate)
            return True
        if nemu_candidate is not None:
            _close_backend(nemu_candidate, "NEMUIPC")
        logger.warning("MUMUIPC连接失败，尝试使用ADB连接")

    adb_candidate = ADB()
    try:
        status = adb_candidate.connect(adb_port if adb_port is not None else device.port)
    except Exception:
        _close_backend(adb_candidate, "ADB")
        raise
    if status:
        _activate_backend(adb_candidate)
    else:
        _close_backend(adb_candidate, "ADB")
    return status


def connect_adb(adb_port: Optional[int] = None):
    """Force the TCP ADB transport for workflows that require shell evidence."""
    ensure_automation_allowed("连接 ADB")
    global control
    device = get_runtime_device()
    control = ADB()
    return control.connect(adb_port if adb_port is not None else device.port)


def stop():
    global STOP
    STOP = True


def reset_stop():
    """Clear a previous global stop request before a new queue starts."""
    global STOP
    STOP = False


def is_stopped() -> bool:
    return STOP


def kill():
    """
    关闭连接
    """
    control.kill()


def input_swipe(pos1=(919, 617), pos2=(919, 908), swipe_time: int = 100):
    """
    滑动屏幕(可超出屏幕)

    :param pos1: 坐标1
    :param pos2: 坐标2
    :param time: 操作时间(毫秒)
    """
    ensure_automation_allowed("滑动游戏界面")
    if STOP:
        raise StopExecution()
    # 添加随机值
    pos_x1 = control.ratio * pos1[0] + random.randint(*EXCURSIONX)
    pos_y1 = control.ratio * pos1[1] + random.randint(*EXCURSIONY)
    pos_x2 = control.ratio * pos2[0] + random.randint(*EXCURSIONX)
    pos_y2 = control.ratio * pos2[1] + random.randint(*EXCURSIONY)

    logger.debug(f"滑动 ({pos_x1}, {pos_y1}) -> ({pos_x2}, {pos_y2})")
    remaining_x = pos_x2 - pos_x1
    remaining_y = pos_y2 - pos_y1
    safe_x1, safe_y1, safe_x2, safe_y2 = control.safe_area
    preferred_start = (
        max(safe_x1, min(pos_x1, safe_x2)),
        max(safe_y1, min(pos_y1, safe_y2)),
    )

    for segment in range(MAX_SWIPE_SEGMENTS):
        if abs(remaining_x) <= 10 and abs(remaining_y) <= 10:
            return
        if STOP:
            raise StopExecution()
        if segment >= 1:
            time.sleep(0.5)
            if STOP:
                raise StopExecution()

        if segment == 0:
            start_x, start_y = preferred_start
        else:
            start_x = (
                safe_x1
                if remaining_x > 0
                else safe_x2
                if remaining_x < 0
                else preferred_start[0]
            )
            start_y = (
                safe_y1
                if remaining_y > 0
                else safe_y2
                if remaining_y < 0
                else preferred_start[1]
            )

        def progress_ratio(x, y):
            ratios = [1.0]
            if remaining_x > 0:
                ratios.append((safe_x2 - x) / remaining_x)
            elif remaining_x < 0:
                ratios.append((x - safe_x1) / -remaining_x)
            if remaining_y > 0:
                ratios.append((safe_y2 - y) / remaining_y)
            elif remaining_y < 0:
                ratios.append((y - safe_y1) / -remaining_y)
            return max(0.0, min(ratios))

        ratio = progress_ratio(start_x, start_y)
        limit_pos_x1 = int(round(start_x))
        limit_pos_y1 = int(round(start_y))
        limit_pos_x2 = int(round(start_x + remaining_x * ratio))
        limit_pos_y2 = int(round(start_y + remaining_y * ratio))
        actual_x = limit_pos_x2 - limit_pos_x1
        actual_y = limit_pos_y2 - limit_pos_y1

        if actual_x == 0 and actual_y == 0 and segment == 0:
            start_x = (
                safe_x1
                if remaining_x > 0
                else safe_x2
                if remaining_x < 0
                else preferred_start[0]
            )
            start_y = (
                safe_y1
                if remaining_y > 0
                else safe_y2
                if remaining_y < 0
                else preferred_start[1]
            )
            ratio = progress_ratio(start_x, start_y)
            limit_pos_x1 = int(round(start_x))
            limit_pos_y1 = int(round(start_y))
            limit_pos_x2 = int(round(start_x + remaining_x * ratio))
            limit_pos_y2 = int(round(start_y + remaining_y * ratio))
            actual_x = limit_pos_x2 - limit_pos_x1
            actual_y = limit_pos_y2 - limit_pos_y1

        if actual_x == 0 and actual_y == 0:
            raise RuntimeError(
                f"滑动无法推进：剩余位移 ({remaining_x}, {remaining_y})，"
                f"安全区域 {control.safe_area}"
            )

        logger.debug(
            f"多次滑动 ({limit_pos_x1}, {limit_pos_y1}) -> ({limit_pos_x2}, {limit_pos_y2})"
        )

        control.input_swipe(
            limit_pos_x1, limit_pos_y1, limit_pos_x2, limit_pos_y2, swipe_time
        )

        previous_distance = abs(remaining_x) + abs(remaining_y)
        remaining_x -= actual_x
        remaining_y -= actual_y
        if abs(remaining_x) + abs(remaining_y) >= previous_distance:
            raise RuntimeError(
                f"滑动无法推进：分段位移 ({actual_x}, {actual_y}) 未减少剩余距离"
            )
        if abs(remaining_x) <= 10 and abs(remaining_y) <= 10:
            return

    raise RuntimeError(
        f"滑动超过最大分段次数 {MAX_SWIPE_SEGMENTS}，"
        f"仍剩余位移 ({remaining_x}, {remaining_y})"
    )


def input_tap(pos: Tuple[int, int] = (880, 362), random_offset: bool = True):
    """
    点击坐标

    :param pos: 坐标
    """
    ensure_automation_allowed("点击游戏界面")
    if STOP:
        raise StopExecution()
    offset_x = random.randint(*EXCURSIONX) if random_offset else 0
    offset_y = random.randint(*EXCURSIONY) if random_offset else 0
    control.input_tap(
        int(control.ratio * pos[0] + offset_x),
        int(control.ratio * pos[1] + offset_y),
    )


def screenshot() -> Image:
    """
    截图
    """
    if STOP:
        raise StopExecution()

    screenshot = screenshot_image()

    return Image(screenshot)


def screenshot_image() -> cv.typing.MatLike:
    """
    截图并返回图片对象
    """
    ensure_automation_allowed("读取游戏画面")
    if STOP:
        raise StopExecution()

    screenshot = control.screenshot()
    screenshot = cv.resize(screenshot, control.dsize, interpolation=cv.INTER_AREA)
    return screenshot

def wait_stopped(threshold=7100000, timeout=15.0):
    """
    等待画面静止
    参数:
        :param threshold: 参数阈值
    """
    logger.info("等待图像静止")
    start = time.perf_counter()
    while time.perf_counter() - start < timeout:
        gray1 = cv.cvtColor(screenshot_image(), cv.COLOR_BGR2GRAY)
        # 等待画面变动，并再次截图
        time.sleep(0.5)
        gray2 = cv.cvtColor(screenshot_image(), cv.COLOR_BGR2GRAY)

        # 计算两帧之间的绝对差异
        diff = cv.absdiff(gray1, gray2)

        # 计算差异值
        diff_sum: int = np.sum(diff)  # type: ignore
        logger.debug(f"画面差异 {diff_sum}")

        if diff_sum < threshold:
            return True
        time.sleep(1)
    logger.warning(f"等待图像静止超时（{timeout}秒），继续后续识别")
    return False
