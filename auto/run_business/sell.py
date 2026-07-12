"""
Author: Night-stars-1 nujj1042633805@gmail.com
Date: 2024-04-05 15:17:19
LastEditTime: 2024-07-08 21:11:34
LastEditors: Night-stars-1 nujj1042633805@gmail.com
"""

import time

from loguru import logger

from core.control.control import input_tap, screenshot
from core.exception.exception_handling import get_excption
from core.module.bgr import BGR
from core.preset import go_home
from core.preset.control import wait_gbr
from auto.module.strength import exit_negotiation_safely


def sell_business(num=0, empty_ok=False):
    """
    说明:
        出售所有商品
    参数:
        :param num: 期望议价的价格
    """
    start_time = time.perf_counter()
    while time.perf_counter() - start_time < 15:
        image = screenshot()
        bgr = image.get_bgr((1156, 100))
        logger.debug(f"是否出售货物颜色检查 {bgr}")
        if not (bgr.b == 0 and bgr.g == 0 and 90 <= bgr.r <= 100):
            logger.debug(f"出售全部货物颜色检查 {bgr}")
            input_tap((1187, 103))
            time.sleep(0.5)
            break
    if is_empty_goods():
        if empty_ok:
            logger.info("No sellable cargo detected; continue with restocking")
            go_home()
            return True
        logger.error("检测到未成功出售物品")
        return False
    else:
        if num > 0 and not click_bargain_button(num):
            logger.error("Maximum sell bargain was not completed; cancel sale")
            return False
        if not click_sell_button():
            logger.error("Sell confirmation did not complete")
            return False
        time.sleep(0.5)
        input_tap((896, 676))
        time.sleep(0.5)
        input_tap((896, 676))
        input_tap((896, 676))
        return True


def sell_existing_cargo(num=0):
    """Sell cargo currently available in the exchange, if any.

    The sell page is the source of truth for the warehouse: selecting all
    exposes every item that can be sold in the current city. An empty
    selection is a valid clean-warehouse result during the preflight check.
    """
    return sell_business(num=num, empty_ok=True)


def has_sellable_cargo():
    """Probe the current sell page without spending fatigue.

    Selecting all lets the exchange determine whether the warehouse contains
    anything sellable in this city. Return home when the selection is empty so
    the caller can continue with restocking.
    """
    start_time = time.perf_counter()
    while time.perf_counter() - start_time < 15:
        image = screenshot()
        bgr = image.get_bgr((1156, 100))
        if not (bgr.b == 0 and bgr.g == 0 and 90 <= bgr.r <= 100):
            input_tap((1187, 103))
            time.sleep(0.5)
            break
    if is_empty_goods():
        logger.info("No sellable cargo detected; continue with restocking")
        go_home()
        return False
    logger.info("Sellable residual cargo detected")
    return True


def is_empty_goods():
    image = screenshot()
    image.crop_image((870, 132), (994, 205))
    bgr = image.get_bgr((898, 169))
    logger.debug(f"货物是否为空检查 {bgr}")
    return BGR(25, 33, 33) == bgr


def click_bargain_button(num=0):
    """
    说明:
        点击议价按钮
    参数:
        :param num: 议价次数
    """
    logger.info(f"议价次数: {num}")
    start = time.perf_counter()
    while time.perf_counter() - start < 15:
        if num <= 0:
            return True
        image = screenshot()
        bgr = image.get_bgr((1176, 461))
        logger.debug(f"抬价界面颜色检查: {bgr}")
        if BGR(0, 170, 240) <= bgr <= BGR(5, 185, 255):
            input_tap((1177, 461))
            time.sleep(1.0)
        elif bgr == [251, 253, 253]:
            logger.info("抬价次数不足")
            return False
        elif bgr == [62, 63, 63]:
            logger.info("疲劳不足")
            exit_negotiation_safely()
            return False
        image = screenshot()
        image.crop_image((516, 224), (787, 439))
        hsv = image.get_hsv((626, 273))
        logger.debug(f"抬价是否成功颜色检查(HSV): {hsv}")
        if 30 <= hsv[0] <= 40:
            logger.info("抬价成功")
            num -= 1
        else:
            logger.info("抬价失败")
        # 等待降价动画消失
        wait_gbr((629, 101), BGR(30, 50, 65), BGR(40, 60, 75))
    return False


def click_sell_button():
    start = time.time()
    while time.time() - start < 10:
        input_tap((1056, 647))
        time.sleep(1)
        image = screenshot()
        bgr = image.get_bgr((1175, 470), offset=5)
        logger.debug(f"出售物品界面颜色检查: {bgr}")
        if bgr == [227, 131, 82]:
            logger.info("检测到包含本地商品")
            input_tap((975, 498))
        if bgr != [0, 183, 253] and bgr != [227, 131, 82] and bgr != [251, 253, 253]:
            return True
    return False
