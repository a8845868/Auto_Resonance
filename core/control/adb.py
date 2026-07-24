import time
from typing import Optional

import cv2 as cv
from loguru import logger
import numpy as np
from core.control.base_control import IADB
from adb_shell.adb_device import AdbDeviceTcp
from adb_shell.exceptions import TcpTimeoutException

from core.services.repair_safety import ensure_automation_allowed

PNG_KEY = b"\x89PNG"

class ADB(IADB):
    def __init__(self) -> None:
        super().__init__()
        self.adb_host = "127.0.0.1"
        self.device = AdbDeviceTcp(self.adb_host)

    def connect(self, adb_port: Optional[int] = None) -> bool:
        ensure_automation_allowed("建立底层 ADB 连接")
        from core.control.control import get_runtime_device

        runtime_device = get_runtime_device()
        self.adb_host = str(runtime_device.adb_host or "127.0.0.1")
        name = "自定义ADB端口"
        if adb_port is None:
            device = runtime_device
            adb_port = device.port
            name = device.name
        if adb_port is None:
            logger.info(f"未知ADB端口信息 {name}，请检测ADB端口是否设置正确")
            return False
        logger.info(f"ADB端口：{name}-{adb_port}")
        self.device = AdbDeviceTcp(self.adb_host, port=adb_port)
        status = False
        try:
            status = self.device.connect()
            if not status:
                logger.error("ADB连接失败")
            else:
                image = self.screenshot()
                height, width = image.shape[:2]
                status = self.check_resolution_ratio(width, height)
                return status
        except ConnectionRefusedError:
            status = False
            logger.error("ADB端口错误或者未打开模拟器，无法连接")
        except TcpTimeoutException as error:
            status = False
            logger.error(f"ADB连接超时: {error}")
        finally:
            if not status:
                try:
                    self.device.close()
                except Exception as error:
                    logger.debug(f"ADB失败连接关闭异常: {error}")
        return status

    def input_swipe(self, x1: int, y1: int, x2: int, y2: int, millisecond: int = 100) -> None:
        ensure_automation_allowed("通过 ADB 滑动游戏界面")
        shell = [
            "input",
            "swipe",
            f"{x1} {y1} {x2} {y2}",
            f"{millisecond}",
        ]
        self.device.shell(" ".join(shell))
        time.sleep(millisecond / 1000)

    def input_tap(self, x: int, y: int):
        ensure_automation_allowed("通过 ADB 点击游戏界面")
        shell = [
            "input",
            "tap",
            str(x),
            str(y),
        ]
        self.device.shell(" ".join(shell))

    def screenshot(self) -> cv.typing.MatLike:
        ensure_automation_allowed("通过 ADB 读取游戏画面")
        screenshot_data = self.device.shell("screencap -p", decode=False)
        if isinstance(screenshot_data, str):
            raise Exception(f"无法获取屏幕截图: {screenshot_data}")
        if screenshot_data[:4] != PNG_KEY:
            index = screenshot_data.find(PNG_KEY) # pyright: ignore[reportAttributeAccessIssue]
            if index == -1:
                raise Exception("无法获取屏幕截图: 截图数据不包含PNG头")
            screenshot_data = screenshot_data[index:]
            logger.debug(f"screenshot: {screenshot_data[:10]}...")

        image_array = np.frombuffer(screenshot_data, np.uint8)

        screenshot = cv.imdecode(image_array, cv.IMREAD_COLOR)

        return screenshot

    def kill(self):
        """
        说明:
            关闭ADB
        """
        ensure_automation_allowed("关闭底层 ADB 连接")
        self.device.close()
