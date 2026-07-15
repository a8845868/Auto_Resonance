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

from .control import click_image
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
WORLD_MAP_GESTURE_CENTER = (640.0, 360.0)
WORLD_MAP_MAX_PAN_STEP_X = 400.0
WORLD_MAP_MAX_PAN_STEP_Y = 300.0
WORLD_MAP_MAX_PAN_ATTEMPTS = 18
WORLD_MAP_LABEL_MIN_SCORE = 0.9
WORLD_MAP_PARTIAL_LABEL_MIN_SCORE = 0.97
WORLD_MAP_LABEL_Y_RANGE = (100.0, 620.0)
WORLD_MAP_EDGE_X = (8.0, 1272.0)
WORLD_MAP_RIGHT_SIDE_SUFFIX_MIN_X = 1100.0
WORLD_MAP_DEFAULT_GESTURE_GAIN = 1.3
WORLD_MAP_GAIN_RANGE = (0.7, 2.0)


def _station_label_center(map_ocr, station_names, *, allow_edge_partial=False):
    """Return the most central trustworthy station label in one map frame."""
    candidates = []
    station_names = tuple(station_names)
    for item in map_ocr:
        text = item.get("text", "").strip()
        position = item.get("position")
        if not position or len(position) < 3:
            continue
        score = item.get("score", 1.0)
        exact = text in station_names and score >= WORLD_MAP_LABEL_MIN_SCORE
        matched_station = text if exact else None
        if not exact and allow_edge_partial and score >= WORLD_MAP_PARTIAL_LABEL_MIN_SCORE:
            left = min(point[0] for point in position)
            right = max(point[0] for point in position)
            partial_matches = []
            if len(text) >= 3 and right >= WORLD_MAP_EDGE_X[1]:
                partial_matches.extend(name for name in station_names if name.startswith(text))
            if len(text) >= 3 and left <= WORLD_MAP_EDGE_X[0]:
                partial_matches.extend(name for name in station_names if name.endswith(text))
            # In the incident frame the right-side label was fully on-screen,
            # but OCR dropped its first character ("月游乐城" for
            # "黑月游乐城"). Accept that observed suffix only in the same
            # right-side region; do not broaden both prefix directions across
            # both sides of the map.
            if len(text) >= 3 and left >= WORLD_MAP_RIGHT_SIDE_SUFFIX_MIN_X:
                partial_matches.extend(
                    name
                    for name in station_names
                    if len(name) == len(text) + 1 and name.endswith(text)
                )
            partial_matches = list(dict.fromkeys(partial_matches))
            if len(partial_matches) == 1:
                matched_station = partial_matches[0]
        if matched_station is None:
            continue
        center_x = (position[0][0] + position[2][0]) / 2
        center_y = (position[0][1] + position[2][1]) / 2
        if not WORLD_MAP_LABEL_Y_RANGE[0] <= center_y <= WORLD_MAP_LABEL_Y_RANGE[1]:
            continue
        distance = (
            abs(center_x - WORLD_MAP_GESTURE_CENTER[0])
            + abs(center_y - WORLD_MAP_GESTURE_CENTER[1])
        )
        candidates.append((not exact, distance, matched_station, center_x, center_y))
    if not candidates:
        return None
    _, _, station, center_x, center_y = min(candidates)
    return station, center_x, center_y


def _world_map_pan_vector(
    source_station,
    target_station,
    source_x=WORLD_MAP_GESTURE_CENTER[0],
    source_y=WORLD_MAP_GESTURE_CENTER[1],
    gesture_gain=WORLD_MAP_DEFAULT_GESTURE_GAIN,
):
    """Calculate the calibrated gesture needed from one map landmark."""
    city_differences = STATION_DIFFERENCES.get((source_station, target_station))
    if not city_differences:
        return None
    gain = max(WORLD_MAP_GAIN_RANGE[0], min(float(gesture_gain), WORLD_MAP_GAIN_RANGE[1]))
    return (
        -city_differences[0] / 2.5
        + (WORLD_MAP_GESTURE_CENTER[0] - source_x) / gain,
        -city_differences[1] / 2.5
        + (WORLD_MAP_GESTURE_CENTER[1] - source_y) / gain,
    )


def _world_map_step(move_x, move_y):
    """Clamp a pan vector to a gesture that always starts inside the screen."""
    scale = min(
        1.0,
        WORLD_MAP_MAX_PAN_STEP_X / abs(move_x) if move_x else 1.0,
        WORLD_MAP_MAX_PAN_STEP_Y / abs(move_y) if move_y else 1.0,
    )
    return move_x * scale, move_y * scale


def _updated_gesture_gain(previous_anchor, current_anchor, last_step, current_gain):
    """Update pan gain only when the same reliable landmark survived a swipe."""
    if not previous_anchor or not current_anchor or not last_step:
        return current_gain
    if previous_anchor[0] != current_anchor[0]:
        return current_gain
    observed = (
        current_anchor[1] - previous_anchor[1],
        current_anchor[2] - previous_anchor[2],
    )
    dominant = 0 if abs(last_step[0]) >= abs(last_step[1]) else 1
    commanded = last_step[dominant]
    if abs(commanded) < 20 or observed[dominant] * commanded <= 0:
        return current_gain
    measured = abs(observed[dominant] / commanded)
    measured = max(WORLD_MAP_GAIN_RANGE[0], min(measured, WORLD_MAP_GAIN_RANGE[1]))
    return (current_gain + measured) / 2


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
        source_x, source_y = WORLD_MAP_GESTURE_CENTER
        map_ocr = screenshot().ocr()
        anchor = _station_label_center(map_ocr, (station,))
        if anchor:
            _, source_x, source_y = anchor
            logger.info(f"地图当前站点锚点: {station} ({source_x:.0f}, {source_y:.0f})")
        gesture_gain = WORLD_MAP_DEFAULT_GESTURE_GAIN
        move = _world_map_pan_vector(
            station, name, source_x, source_y, gesture_gain
        )
        if move is None:
            logger.error("没有该站点的坐标信息")
            return STATION(False)
        move_x, move_y = move
        base_move = _world_map_pan_vector(station, name)
        best_anchor_distance = max(abs(base_move[0]), abs(base_move[1]))
        last_anchor = anchor
        last_step = None
        logger.info(f"地图分段拖动: 总位移({move_x:.0f}, {move_y:.0f})")
        result = None
        target_anchor = None
        known_stations = tuple(STATION_POS_DATA)
        for step in range(WORLD_MAP_MAX_PAN_ATTEMPTS):
            probe = screenshot()
            probe.crop_image((0, 0), (1280, 654))
            result = probe.match_template(
                RESOURCES_PATH / "stations" / STATION_NAME2PNG[name], 0.95
            )
            probe_ocr = probe.ocr()
            target_anchor = _station_label_center(probe_ocr, (name,))
            if result or target_anchor:
                logger.info(
                    f"目标站点已在第 {step}/{WORLD_MAP_MAX_PAN_ATTEMPTS} 次拖动前进入视野"
                )
                break

            anchor = _station_label_center(
                probe_ocr, known_stations, allow_edge_partial=True
            )
            if anchor:
                station, source_x, source_y = anchor
                updated_gain = _updated_gesture_gain(
                    last_anchor, anchor, last_step, gesture_gain
                )
                if updated_gain != gesture_gain:
                    gesture_gain = updated_gain
                    logger.info(f"地图手势增益校正为 {gesture_gain:.2f}")

                base_move = _world_map_pan_vector(station, name)
                anchor_distance = max(abs(base_move[0]), abs(base_move[1]))
                if (
                    last_anchor and last_anchor[0] == station
                ) or anchor_distance + 8 < best_anchor_distance:
                    move = _world_map_pan_vector(
                        station, name, source_x, source_y, gesture_gain
                    )
                    if move is not None:
                        move_x, move_y = move
                        best_anchor_distance = min(
                            best_anchor_distance, anchor_distance
                        )
                        logger.info(
                            f"地图重新锚定站点: {station} "
                            f"({source_x:.0f}, {source_y:.0f}), "
                            f"校正位移({move_x:.0f}, {move_y:.0f})"
                        )
                last_anchor = anchor

            if max(abs(move_x), abs(move_y)) < 8:
                break
            step_x, step_y = _world_map_step(move_x, move_y)
            gesture_cx, gesture_cy = WORLD_MAP_GESTURE_CENTER
            start = (gesture_cx - step_x / 2, gesture_cy - step_y / 2)
            end = (gesture_cx + step_x / 2, gesture_cy + step_y / 2)
            input_swipe(start, end, swipe_time=450)
            time.sleep(0.45)
            last_step = (step_x, step_y)
            move_x -= step_x
            move_y -= step_y
        # 向回拖动避免画面长时间移动
        input_swipe(
            WORLD_MAP_GESTURE_CENTER,
            (WORLD_MAP_GESTURE_CENTER[0] - 10, WORLD_MAP_GESTURE_CENTER[1] - 10),
            swipe_time=500,
        )
        # The current world map has a continuously animated background whose
        # frame difference is normally around 7.3M-8.1M.
        wait_stopped(threshold=8500000, timeout=12)  # 等待滑动完成

        if not result and not target_anchor:
            image = screenshot()
            image.crop_image((0, 0), (1280, 654))
            result = image.match_template(
                RESOURCES_PATH / "stations" / STATION_NAME2PNG[name], 0.95
            )
            target_anchor = _station_label_center(image.ocr(), (name,))
        if result:
            # 点击站点
            input_tap(result.loc)
        elif target_anchor:
            input_tap((target_anchor[1], target_anchor[2]))
        else:
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
