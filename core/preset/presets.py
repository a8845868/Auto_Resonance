"""
Author: Night-stars-1 nujj1042633805@gmail.com
Date: 2024-04-05 17:24:47
LastEditTime: 2025-02-11 19:26:36
LastEditors: Night-stars-1 nujj1042633805@gmail.com
"""

import hashlib
import time
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Dict, Optional, Tuple

from loguru import logger

from core.control.control import (
    input_swipe,
    input_tap,
    is_stopped,
    screenshot,
    screenshot_image,
    wait_stopped,
)
from core.exception.exceptions import StopExecution
from core.image.utils import match_template
from core.module.bgr import BGR
from core.image.ocr import predict
from core.preset import blurry_ocr_click, go_home
from core.services.screen_state import is_train_in_transit
from core.services.city_navigation import (
    CityNavigationAdapter as ReadOnlyCityNavigationAdapter,
    CityNavigationState as ReadOnlyCityNavigationState,
)
from core.services.read_only_policy import ActionIntent
from core.services.station_availability import station_unavailable_reason
from core.utils.utils import RESOURCES_PATH, read_json

from .control import click_image
from .station import STATION

FIGHT_TIME = 1000


class CityNavigationState(str, Enum):
    HOME = "HOME"
    HUD = "HUD"
    CITY_ENTRY_AVAILABLE = "CITY_ENTRY_AVAILABLE"
    CITY_MAP = "CITY_MAP"
    CITY_DETAIL = "CITY_DETAIL"
    NPC_DIALOG = "NPC_DIALOG"
    NPC_DIALOGUE = "NPC_DIALOGUE"
    EXCHANGE_MENU = "EXCHANGE_MENU"
    EXCHANGE_BUY = "EXCHANGE_BUY"
    EXCHANGE_SELL = "EXCHANGE_SELL"
    STARTUP_OVERLAY = "STARTUP_OVERLAY"
    UNKNOWN = "UNKNOWN"
    READ_ONLY_DENIED = "READ_ONLY_DENIED"
    CANCELLED = "CANCELLED"
    TIMEOUT = "TIMEOUT"
    STALLED = "STALLED"


@dataclass(frozen=True)
class CityFrameObservation:
    state: CityNavigationState
    page_fingerprint: str
    last_template_score: float
    city_entry_anchor: tuple[int, int, int, int] | None = None
    diagnostics: tuple[str, ...] = ()


@dataclass(frozen=True)
class CityNavigationResult:
    success: bool
    state: CityNavigationState
    attempts: int
    elapsed_seconds: float
    last_template_score: float
    page_fingerprint: str
    actions_attempted: tuple[str, ...]
    actions_executed: tuple[str, ...]
    blocked_reason: str
    diagnostics: tuple[str, ...]

    def __bool__(self) -> bool:
        return self.success


@dataclass(frozen=True)
class OutletNavigationResult:
    success: bool
    stage: str
    reason: str = ""
    city: CityNavigationResult | None = None

    def __bool__(self) -> bool:
        return self.success

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


def click_station(
    name: str,
    cur_station: Optional[str] = None,
    *,
    on_departure_requested: Callable[[], None] | None = None,
):
    """
    点击站点, 该滑动通过站点间相对距离完成

    :param name: 目标站点
    :param cur_station: 当前站点
    """
    logger.info(f"点击站点 => {name}")
    if cur_station == name:
        logger.info("已在目标站点")
        return STATION(True, is_destine=True)
    unavailable_reason = station_unavailable_reason(name)
    if unavailable_reason:
        logger.warning(f"拒绝前往未开放站点: {unavailable_reason}")
        return STATION(False)
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
        # Clicking a station label can first recenter the map and place the red
        # destination pin without opening the action panel. Reacquire the
        # target in the new frame and retry the station click before failing.
        for selection_attempt in range(3):
            time.sleep(0.5)
            logger.info("点击前往目的地按钮")
            if click_image(
                RESOURCES_PATH / "map/go_station.png",
                cropped_pos1=(937, 605),
                cropped_pos2=(1218, 679),
                trynum=1,
                check_err=False,
            ):
                if on_departure_requested is not None:
                    on_departure_requested()
                time.sleep(1.0)
                if _wait_for_departure():
                    return STATION(True)
                logger.error("站台过渡超时，未确认进入自动巡航")
                return STATION(False)
            if selection_attempt == 2:
                break

            retry_image = screenshot()
            retry_image.crop_image((0, 0), (1280, 654))
            retry_result = retry_image.match_template(
                RESOURCES_PATH / "stations" / STATION_NAME2PNG[name], 0.95
            )
            retry_anchor = _station_label_center(retry_image.ocr(), (name,))
            if retry_result:
                retry_pos = retry_result.loc
            elif retry_anchor:
                retry_pos = (retry_anchor[1], retry_anchor[2])
            else:
                logger.warning(
                    f"前往按钮尚未出现，且无法重新定位目标站点: {name}"
                )
                break
            logger.info(
                f"前往按钮尚未出现，重新点击目标站点 "
                f"({selection_attempt + 2}/3): {name} {retry_pos}"
            )
            input_tap(retry_pos)
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


def _ocr_bbox(item: dict) -> tuple[int, int, int, int] | None:
    points = item.get("position") or ()
    if len(points) < 3:
        return None
    xs = [int(point[0]) for point in points]
    ys = [int(point[1]) for point in points]
    return min(xs), min(ys), max(xs), max(ys)


def _city_frame_observation(frame) -> CityFrameObservation:
    items = list(frame.ocr())
    texts = [str(item.get("text", "")).replace(" ", "") for item in items]
    joined = "|".join(texts)
    diagnostics: list[str] = []
    anchors = [
        bounds for item, text in zip(items, texts)
        if "访问城市" in text and (bounds := _ocr_bbox(item)) is not None
    ]
    anchor = anchors[0] if len(anchors) == 1 else None
    if len(anchors) > 1:
        diagnostics.append("visit_city_anchor_not_unique")

    buy_markers = sum(marker in joined for marker in ("预计买入", "买入总价", "全部买入", "载货量"))
    sell_markers = sum(marker in joined for marker in ("预计卖出", "卖出总价", "全部卖出", "载货量"))
    if buy_markers >= 2 and sell_markers >= 2:
        state = CityNavigationState.UNKNOWN
        diagnostics.append("conflicting_exchange_markers")
    elif buy_markers >= 2:
        state = CityNavigationState.EXCHANGE_BUY
    elif sell_markers >= 2:
        state = CityNavigationState.EXCHANGE_SELL
    elif "我要买" in joined and "我要卖" in joined:
        state = CityNavigationState.EXCHANGE_MENU
    elif any(marker in joined for marker in ("你想要什么", "研究报告", "什么都行")):
        state = CityNavigationState.NPC_DIALOG
    elif (
        sum(marker in joined for marker in (
            "当前城市", "城市设施", "城市手册", "城市发展度", "CITY",
        )) >= 2
        and sum(marker in joined for marker in (
            "交易所", "商会", "休息区",
        )) >= 1
    ):
        state = CityNavigationState.CITY_MAP
    elif sum(marker in joined for marker in (
        "当前城市", "城市详情", "城市发展度", "CITY",
    )) >= 2:
        state = CityNavigationState.CITY_DETAIL
    elif anchor is not None and any(marker in joined for marker in ("启程", "作战终端", "整备列车")):
        state = CityNavigationState.HOME
    elif any(marker in joined for marker in ("启程", "作战终端", "整备列车")):
        state = CityNavigationState.HUD
    elif any(marker in joined for marker in ("每日签到奖励", "资讯", "公告")):
        state = CityNavigationState.STARTUP_OVERLAY
    else:
        state = CityNavigationState.UNKNOWN

    template_result = match_template(
        frame.image, RESOURCES_PATH / "fame.png",
        cropped_pos1=(25, 634), cropped_pos2=(99, 707), threshold=0.95,
    )
    score = float(template_result.score)
    if template_result.status and state in {CityNavigationState.HOME, CityNavigationState.HUD}:
        diagnostics.append("template_ocr_conflict")
        state = CityNavigationState.UNKNOWN
    material = "|".join(
        sorted(f"{text}@{_ocr_bbox(item)}" for item, text in zip(items, texts))
    )
    fingerprint = hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]
    return CityFrameObservation(state, fingerprint, score, anchor, tuple(diagnostics))


def go_city(
    *, frame_provider: Callable[[], object] = screenshot,
    frame_analyzer: Callable[[object], CityFrameObservation] = _city_frame_observation,
    tap: Callable[..., object] = input_tap,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    cancellation: Callable[[], bool] | None = None,
    max_attempts: int = 10, timeout: float = 30.0, stall_frames: int = 5,
):
    """
    说明:
        进入城市界面
    """
    if frame_analyzer is _city_frame_observation:
        started = monotonic()
        resolved = ReadOnlyCityNavigationAdapter(
            frame_provider=frame_provider,
            tap=tap,
            sleep=sleep,
            monotonic=monotonic,
            timeout=timeout,
            max_attempts=max_attempts,
            stall_frames=stall_frames,
            cancellation=lambda: is_stopped() or (
                cancellation is not None and cancellation()
            ),
        ).enter_city()
        attempted = (
            ("city_entry_navigation",)
            if any(event.action == "enter_city" for event in resolved.trace)
            else ()
        )
        executed = (
            ("city_entry_navigation",)
            if any(
                event.action == "enter_city"
                and event.guard_result == "ALLOWED"
                and event.reason == "city_entry_action_executed"
                for event in resolved.trace
            )
            else ()
        )
        state_map = {
            ReadOnlyCityNavigationState.CITY_MAP: CityNavigationState.CITY_MAP,
            ReadOnlyCityNavigationState.CITY_DETAIL: CityNavigationState.CITY_DETAIL,
            ReadOnlyCityNavigationState.EXCHANGE_NPC_VISIBLE: CityNavigationState.CITY_DETAIL,
            ReadOnlyCityNavigationState.TIMEOUT: CityNavigationState.TIMEOUT,
            ReadOnlyCityNavigationState.UNKNOWN: CityNavigationState.UNKNOWN,
        }
        if resolved.reason == "NAVIGATION_STALLED":
            state = CityNavigationState.STALLED
        elif resolved.reason == "guard_denied_city_entry":
            state = CityNavigationState.READ_ONLY_DENIED
        else:
            state = state_map.get(resolved.state, CityNavigationState.UNKNOWN)
        last = resolved.trace[-1] if resolved.trace else None
        return CityNavigationResult(
            resolved.status == "PASS",
            state,
            resolved.attempt_count,
            max(0.0, monotonic() - started),
            0.0,
            last.page_fingerprint if last else "",
            attempted,
            executed,
            resolved.reason,
            tuple(event.reason for event in resolved.trace),
        )

    started = monotonic()
    deadline = started + max(0.0, float(timeout))
    attempts = 0
    attempted: list[str] = []
    executed: list[str] = []
    diagnostics: list[str] = []
    last_score = 0.0
    last_fingerprint = ""
    stable_frames = 0
    awaiting_city_map = False

    def finish(state: CityNavigationState, success: bool, reason: str):
        return CityNavigationResult(
            success, state, attempts, max(0.0, monotonic() - started),
            last_score, last_fingerprint, tuple(attempted), tuple(executed),
            reason, tuple(diagnostics),
        )

    while attempts < max(1, int(max_attempts)) and monotonic() < deadline:
        if is_stopped() or (cancellation is not None and cancellation()):
            return finish(CityNavigationState.CANCELLED, False, "cancelled")
        try:
            frame = frame_provider()
        except StopExecution:
            return finish(CityNavigationState.CANCELLED, False, "stop_requested")
        attempts += 1
        evidence = frame_analyzer(frame)
        state = CityNavigationState(str(getattr(evidence.state, "value", evidence.state)))
        last_score = float(evidence.last_template_score)
        fingerprint = str(evidence.page_fingerprint)
        diagnostics.extend(str(item) for item in getattr(evidence, "diagnostics", ()))
        stable_frames = stable_frames + 1 if fingerprint == last_fingerprint else 1
        last_fingerprint = fingerprint

        if state is CityNavigationState.CITY_MAP:
            return finish(state, True, "city_map_verified")
        if state in {
            CityNavigationState.NPC_DIALOG, CityNavigationState.NPC_DIALOGUE,
            CityNavigationState.CITY_DETAIL, CityNavigationState.EXCHANGE_MENU,
            CityNavigationState.EXCHANGE_BUY, CityNavigationState.EXCHANGE_SELL,
            CityNavigationState.STARTUP_OVERLAY, CityNavigationState.UNKNOWN,
        }:
            return finish(state, False, f"unsafe_or_unexpected_state:{state.value}")
        if awaiting_city_map and stable_frames >= max(2, int(stall_frames)):
            return finish(CityNavigationState.STALLED, False, "page_fingerprint_unchanged_after_action")
        if awaiting_city_map:
            # A city-entry tap is a one-shot transition request.  OCR jitter can
            # change the fingerprint while the animation is still on HOME/HUD;
            # it must never authorize a second tap.
            sleep(min(1.0, max(0.0, deadline - monotonic())))
            continue

        anchor = getattr(evidence, "city_entry_anchor", None)
        if state not in {CityNavigationState.HOME, CityNavigationState.HUD} or anchor is None:
            return finish(CityNavigationState.UNKNOWN, False, "visit_city_anchor_not_unique")
        x1, y1, x2, y2 = anchor
        point = (int(round((x1 + x2) / 2)), int(round((y1 + y2) / 2)))
        attempted.append("city_entry_navigation")
        allowed = tap(
            point,
            intent=ActionIntent(
                "city_entry_navigation", "city_entry", "preset:home:city-entry",
            ),
        )
        if allowed is False:
            return finish(CityNavigationState.READ_ONLY_DENIED, False, "guard_denied_city_entry")
        executed.append("city_entry_navigation")
        awaiting_city_map = True
        if is_stopped() or (cancellation is not None and cancellation()):
            return finish(CityNavigationState.CANCELLED, False, "cancelled")
        sleep(min(1.0, max(0.0, deadline - monotonic())))
    return finish(CityNavigationState.TIMEOUT, False, "navigation_deadline_or_attempt_limit")


def go_outlets(
    name: str, *, deadline: float | None = None,
    cancellation: Callable[[], bool] | None = None,
    city_navigator: Callable[..., CityNavigationResult] = go_city,
    ocr_click: Callable[..., object] = blurry_ocr_click,
    swipe: Callable[..., object] = input_swipe,
    monotonic: Callable[[], float] = time.monotonic,
):
    """
    前往指定门店

    :param name: 门店名称
    """
    remaining = 20.0 if deadline is None else max(0.0, deadline - monotonic())
    city = city_navigator(timeout=remaining, cancellation=cancellation)
    if not city.success:
        return OutletNavigationResult(False, "city_navigation", city.blocked_reason, city)
    logger.info(f"前往 => {name}")
    # New stations append their local market name (for example
    # "交易所-武林市集").  Matching "交易所" against the whole label needs a
    # lower length ratio than the legacy 0.7 default.
    swipe_paths = (
        ((457, 340), (457, 440), "scroll-1"),
        ((457, 440), (457, 340), "scroll-2"),
        ((969, 369), (457, 369), "scroll-3"),
        ((457, 369), (969, 369), "scroll-4"),
    )
    for index in range(len(swipe_paths) + 1):
        if is_stopped() or (cancellation is not None and cancellation()):
            return OutletNavigationResult(False, "outlet_search", "cancelled", city)
        if deadline is not None and monotonic() >= deadline:
            return OutletNavigationResult(False, "outlet_search", "overall_deadline_exceeded", city)
        result = ocr_click(
            name,
            cropped_pos1=(160, 40), cropped_pos2=(1000, 500),
            # The read-only permit is bound to the observed label bounding box.
            # An historical +80 px offset left that evidence and was correctly
            # denied by the guard on the real city map.
            excursion_pos=(0, 0), log=False, score=0.3,
            action_key="navigation_anchor", page_id="city_outlets",
            trynum=1,
        )
        if result:
            return OutletNavigationResult(True, "outlet_selected", "", city)
        if index == len(swipe_paths):
            break
        start, end, suffix = swipe_paths[index]
        allowed = swipe(
            start, end, swipe_time=500,
            intent=ActionIntent(
                "outlet_list_scroll", "outlet_list", f"preset:outlet:{suffix}",
            ),
        )
        if allowed is False:
            return OutletNavigationResult(False, "outlet_scroll", "read_only_denied", city)
    return OutletNavigationResult(False, "outlet_search", "outlet_not_found", city)

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
