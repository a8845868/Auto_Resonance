"""
Author: Night-stars-1 nujj1042633805@gmail.com
Date: 2024-04-05 17:24:47
LastEditTime: 2025-02-11 19:26:36
LastEditors: Night-stars-1 nujj1042633805@gmail.com
"""

import time
from typing import Dict, Optional, Tuple

from loguru import logger

from core.control.control import (
    input_swipe,
    input_tap,
    screenshot,
    screenshot_image,
    wait_stopped,
)
from core.module.bgr import BGR
from core.image.ocr import predict
from core.preset import blurry_ocr_click, go_home
from core.services.screen_state import is_train_in_transit
from core.utils.utils import RESOURCES_PATH, read_json

from .control import click_image, ocr_click
from .station import STATION

FIGHT_TIME = 1000

STATION_NAME2PNG: Dict[str, str] = read_json(RESOURCES_PATH / "stations/name2id.json")

# 站点坐标，左上角为(0, 0)
# STATION_POS_DATA = {
#     "澄明数据中心": (1049, 345),
#     "7号自由港": (665, 577),
#     "阿妮塔战备工厂": (832, 664),
#     "阿妮塔发射中心": (164, 420 + 577),
#     "阿妮塔能源研究所": (614, 454 + 577),
#     "修格里城": (285 + 1049, 121 + 345),
#     "铁盟哨站": (501 + 1049, 122 + 345),
#     "荒原站": (753 + 1049, 121 + 345),
#     "曼德矿场": (602 + 1049, 322 + 345),
#     "淘金乐园": (701 + 1049, 604 + 345),
#     "海角城": (164 + 293, 420 + 577 + 569),
# }
STATION_POS_DATA: Dict[str, Tuple[int, int]] = read_json(
    RESOURCES_PATH / "goods/CityPosData.json"
)


def calculate_station_differences(station_map_data: dict):
    differences = {}
    for site1, coords1 in station_map_data.items():
        for site2, coords2 in station_map_data.items():
            if site1 != site2:
                x_diff = coords2[0] - coords1[0]
                y_diff = coords1[1] - coords2[1]
                differences[(site1, site2)] = (x_diff, y_diff)
    return differences


# 计算站点之间的差值
STATION_DIFFERENCES = calculate_station_differences(STATION_POS_DATA)

# CityPosData and the /2.5 gesture calibration were recorded at the default
# (middle) world-map zoom. The game remembers the last zoom level, so a route
# started at minimum zoom can overshoot by several screens.
WORLD_MAP_OPEN_POS = (1201, 666)
WORLD_MAP_BACK_POS = (84, 40)
WORLD_MAP_DEFAULT_ZOOM_POS = (1151, 465)


def _wait_for_departure(timeout: float = 12.0) -> bool:
    """Wait through the station-platform transition until driving is real."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        image = screenshot()
        if is_train_in_transit(image.ocr()):
            logger.info("站台过渡完成，已确认进入自动巡航")
            return True
        # Some stations still show a short-lived explicit Join button, while
        # newer ones transition automatically. Handle both without logging a
        # false template error during the animation.
        click_image(
            RESOURCES_PATH / "map/join_station.png",
            cropped_pos1=(719, 405),
            cropped_pos2=(927, 485),
            trynum=1,
            check_err=False,
        )
        time.sleep(0.6)
    return False


def _open_world_map_at_default_zoom():
    input_tap(WORLD_MAP_OPEN_POS)
    time.sleep(0.6)
    wait_stopped(threshold=8500000, timeout=8)

    input_tap(WORLD_MAP_DEFAULT_ZOOM_POS)
    time.sleep(0.8)

    # Zooming keeps the previous viewport centre. Reopening makes the game
    # focus the current station again and restores a stable coordinate origin.
    input_tap(WORLD_MAP_BACK_POS)
    time.sleep(0.8)
    input_tap(WORLD_MAP_OPEN_POS)
    time.sleep(0.8)
    wait_stopped(threshold=8500000, timeout=8)


def click_station(name: str, cur_station: Optional[str] = None):
    """
    点击站点, 该滑动通过站点间相对距离完成

    :param name: 目标站点
    :param cur_station: 当前站点
    """
    logger.info(f"点击站点 => {name}")
    if screenshot().match_template(RESOURCES_PATH / "main_map.png", 0.95) == False:
        logger.info("未检测到主地图界面，返回主地图")
        go_home()
    logger.info("检测到主地图界面，识别站点")
    if not cur_station:
        station = get_station(is_go_home=False)
    else:
        station = cur_station
    if name == station:
        logger.info("已在目标站点")
        return STATION(True, is_destine=True)
    else:
        go_home()

    if name not in STATION_NAME2PNG:
        raise ValueError(f"未找到站点 {name} 的图片")
    city_differences = STATION_DIFFERENCES.get((station, name))
    if city_differences:
        _open_world_map_at_default_zoom()

        # The current game version does not place the current station at the
        # exact screen center.  Locate its visible label and pan relative to
        # that real anchor; the old hard-coded (640, 360) can miss a distant
        # target by hundreds of pixels.
        source_x = 640.0
        source_y = 360.0
        map_ocr = screenshot().ocr()
        for item in map_ocr:
            if station in item["text"]:
                position = item["position"]
                source_x = (position[0][0] + position[2][0]) / 2
                source_y = (position[0][1] + position[2][1]) / 2
                logger.info(f"地图当前站点锚点: {station} ({source_x:.0f}, {source_y:.0f})")
                break
        # 如果有路线则进行寻找
        x1 = source_x + city_differences[0] / 2.5
        y1 = source_y + city_differences[1] / 2.5

        # Long routes can be several screen widths apart. Android ignores a
        # swipe when its start point is outside the screen, so split the map
        # movement into valid in-screen gestures.
        move_x = source_x - x1
        move_y = source_y - y1
        max_step = 760.0
        steps = max(1, int(max(abs(move_x), abs(move_y)) / max_step) + 1)
        step_x = move_x / steps
        step_y = move_y / steps
        gesture_cx, gesture_cy = 640.0, 360.0
        logger.info(
            f"地图分段拖动: 总位移({move_x:.0f}, {move_y:.0f}), {steps} 段"
        )
        result = None
        target_visible = False
        for step in range(steps):
            start = (gesture_cx - step_x / 2, gesture_cy - step_y / 2)
            end = (gesture_cx + step_x / 2, gesture_cy + step_y / 2)
            input_swipe(start, end, swipe_time=450)
            time.sleep(0.45)

            # ADB/NEMU gestures have momentum. Stop as soon as the destination
            # enters view instead of blindly executing all calculated swipes.
            probe = screenshot()
            probe.crop_image((0, 0), (1280, 654))
            result = probe.match_template(
                RESOURCES_PATH / "stations" / STATION_NAME2PNG[name], 0.95
            )
            if result or any(name in item["text"] for item in probe.ocr()):
                target_visible = True
                logger.info(f"目标站点已在第 {step + 1}/{steps} 段拖动后进入视野")
                break
        # 向回拖动避免画面长时间移动
        input_swipe(
            (source_x, source_y), (source_x - 10, source_y - 10), swipe_time=500
        )
        # The current world map has a continuously animated background whose
        # frame difference is normally around 7.3M-8.1M.
        wait_stopped(threshold=8500000, timeout=12)  # 等待滑动完成

        if not result and not target_visible:
            image = screenshot()
            image.crop_image((0, 0), (1280, 654))
            result = image.match_template(
                RESOURCES_PATH / "stations" / STATION_NAME2PNG[name], 0.95
            )
        if result:
            # 点击站点
            input_tap(result.loc)
        else:
            logger.info(f"未找到站点 {name}，尝试OCR识别")
            if not ocr_click(name):
                logger.error(f"未找到站点: {name}")
                return STATION(False)
        time.sleep(0.5)
        # 点击前往目的地按钮
        logger.info("点击前往目的地按钮")
        if click_image(
            RESOURCES_PATH / "map/go_station.png",
            cropped_pos1=(937, 605),
            cropped_pos2=(1218, 679),
            trynum=5,
        ):
            time.sleep(1.0)
            if _wait_for_departure():
                return STATION(True)
            logger.error("站台过渡超时，未确认进入自动巡航")
            return STATION(False)
        else:
            logger.error(f"未找到前往目的地按钮: {name}")
    else:
        logger.error("没有该站点的坐标信息")
    return STATION(False)


def get_station(is_go_home: bool = True):
    """
    获取当前站点

    :param is_go_home: 是否返回主界面
    """
    result = []
    for attempt in range(3):
        # A fatigue failure can leave the negotiation reset-warning above the
        # shop. Resolve it before trying to navigate to the city information.
        visible = [item["text"] for item in screenshot().ocr()]
        if any("退出后议价幅度将重置" in text for text in visible):
            input_tap((768, 447))
            time.sleep(2)
        go_home()
        input_tap((1170, 493))
        time.sleep(1.0)
        result = predict(
            screenshot_image(), cropped_pos1=(166, 520), cropped_pos2=(470, 600)
        )
        if result:
            break
        logger.warning(f"第 {attempt + 1}/3 次未识别当前城市，重新回主界面")
    if not result:
        logger.error("连续 3 次未识别当前城市，安全暂停本轮")
        return None
    logger.info(f"当前站点: {result[0]['text']}")
    if is_go_home:
        # 返回主界面，回溯进入城市地图操作
        go_home()
    return result[0]["text"]


def go_city():
    """
    说明:
        进入城市界面
    """
    while (
        screenshot()
        .crop_image(cropped_pos1=(25, 634), cropped_pos2=(99, 707))
        .match_template(RESOURCES_PATH / "fame.png", 0.95)
        == False
    ):
        input_tap((1270, 494))
        time.sleep(2.0)


def go_outlets(name: str):
    """
    前往指定门店

    :param name: 门店名称
    """
    go_city()
    logger.info(f"前往 => {name}")
    # New stations append their local market name (for example
    # "交易所-武林市集").  Matching "交易所" against the whole label needs a
    # lower length ratio than the legacy 0.7 default.
    if result := blurry_ocr_click(name, excursion_pos=(0, 80), log=False, score=0.3):
        return result
    input_swipe((457, 340), (457, 369), swipe_time=500)
    if result := blurry_ocr_click(name, excursion_pos=(0, 80), log=False, score=0.3):
        return result
    input_swipe((400, 340), (457, 340), swipe_time=500)
    if result := blurry_ocr_click(name, excursion_pos=(0, 80), log=False, score=0.3):
        return result
    input_swipe((969, 369), (457, 340), swipe_time=500)
    if result := blurry_ocr_click(name, excursion_pos=(0, 80), log=False, score=0.3):
        return result
    input_swipe((641, 246), (637, 615), swipe_time=500)
    if result := blurry_ocr_click(name, excursion_pos=(0, 80), score=0.3):
        return result

def go_shop():
    """
    前往交易所
    """
    click_image(RESOURCES_PATH / "shop" / "1.png", trynum=1, check_err=False)

def wait_fight_end():
    """
    说明:
        等待战斗结束
    """
    logger.info("等待战斗结束")
    start = time.perf_counter()
    while time.perf_counter() - start < FIGHT_TIME:
        image = screenshot()
        bgrs = image.get_bgrs([(1114, 630), (1204, 624), (167, 29)])
        logger.debug(f"等待战斗结束颜色检查: {bgrs}")
        if (
            BGR(198, 200, 200) <= bgrs[0] <= BGR(202, 204, 204) 
            and BGR(183, 185, 185) <= bgrs[1] <= BGR(187, 189, 189)
        ):
            logger.info("检测到执照等级提升")
            input_tap((1151, 626))
            continue
        elif image.crop_image((1070, 600), (1251, 670)).match_template(
            RESOURCES_PATH / "fight/end_fight.png", 0.995
        ):
            logger.info("战斗结束")
            time.sleep(1.0)
            input_tap((1151, 626))
            return True
        # elif (
        #     BGRGroup([245, 245, 245], [255, 255, 255]) == bgrs[0]
        #     and BGRGroup([9, 9, 9], [10, 10, 10]) == bgrs[3]
        # ):
        #     logger.info("战斗失败")
        #     time.sleep(1.0)
        #     input_tap((1151, 626))
        #     return True
        elif bgrs[2] == [124, 126, 125]:
            logger.info("开启自动战斗")
            input_tap((233, 44))
        time.sleep(3)
    logger.error("战斗超时")
    return False
