"""Sequential six-hour passenger-carriage construction monitor."""

import time
import re
from dataclasses import dataclass
from datetime import datetime

from loguru import logger

from core.control.control import connect, input_tap, is_stopped, screenshot
from core.preset.control import blurry_ocr_click, go_home
from core.services.game_recovery import is_game_running, start_game
from core.services.runtime_errors import BlockedBySafetyError
from core.services.passenger_build_planner import (
    build_monitor_summary,
    load_build_monitor_plan,
    record_carriage_completed,
    record_carriage_started,
    resync_active_build,
)


BUILD_CARRIAGE_TEXT = "建造车厢"
CLAIM_COMPLETED_TEXT = "建造完成"
START_CONSTRUCTION_TEXT = "开始施工"
SCREEN_TRANSITION_TIMEOUT = 20.0
PASSENGER_DIALOG_TIMEOUT = 20.0
BUILD_START_TIMEOUT = 20.0


@dataclass(frozen=True)
class BuildScreenState:
    building: bool
    remaining_seconds: int | None = None
    existing: bool = False


@dataclass(frozen=True)
class PassengerBuildInventory:
    installed_passenger_carriages: int
    garage_standard_carriages: int
    total_passenger_carriages: int
    built_extra_passenger_carriages: int
    installed_seat_groups: int


def parse_build_remaining(texts: list[str]) -> int | None:
    """Parse the workshop's `施工剩余时长：HH:MM:SS` OCR result."""
    combined = " ".join(str(text) for text in texts)
    match = re.search(r"(\d{1,2})\s*[:：]\s*(\d{2})\s*[:：]\s*(\d{2})", combined)
    if not match:
        return None
    hours, minutes, seconds = map(int, match.groups())
    if minutes > 59 or seconds > 59:
        return None
    return hours * 3600 + minutes * 60 + seconds


def inspect_build_screen() -> BuildScreenState:
    texts = [item["text"] for item in screenshot().ocr()]
    remaining = parse_build_remaining(texts)
    building = remaining is not None or any(
        "施工剩余时长" in text or "立刻完成" in text for text in texts
    )
    return BuildScreenState(building=building, remaining_seconds=remaining)


def _read_screen_texts() -> list[str]:
    try:
        return [str(item.get("text", "")) for item in screenshot().ocr()]
    except Exception:
        return []


def _item_center(item: dict) -> tuple[float, float]:
    points = item.get("position") or []
    if not points:
        return 0.0, 0.0
    return (
        sum(float(point[0]) for point in points) / len(points),
        sum(float(point[1]) for point in points) / len(points),
    )


def _compact_text(value) -> str:
    return str(value or "").replace(" ", "").strip()


def _garage_standard_carriage_count(items: list[dict], height: int = 720) -> int:
    """Count completed standard passenger carriages in the garage row only."""
    return sum(
        1
        for item in items
        if _compact_text(item.get("text")) == "标准客厢"
        and _item_center(item)[1] >= height * 0.72
    )


def _passenger_capacity(items: list[dict]) -> int | None:
    """Read the denominator in the train overview's `载客总量 A/B` row."""
    labels = [item for item in items if "载客总量" in _compact_text(item.get("text"))]
    if not labels:
        return None
    for label in labels:
        lx, ly = _item_center(label)
        candidates = []
        for item in items:
            raw = _compact_text(item.get("text"))
            match = re.fullmatch(r"(\d+)\s*/\s*(\d+)", raw)
            if not match:
                continue
            x, y = _item_center(item)
            if x > lx and abs(y - ly) <= 45:
                candidates.append((abs(y - ly) + abs(x - lx) * 0.05, int(match.group(2))))
        if candidates:
            return min(candidates)[1]
    return None


def parse_passenger_build_inventory(
    workshop_items: list[dict], overview_items: list[dict], *, height: int = 720
) -> PassengerBuildInventory | None:
    """Combine train capacity and garage OCR into the real build progress.

    The initial basic passenger carriage is part of the target total.  Every
    passenger carriage supplies 64 seats, i.e. sixteen four-seat groups.
    Standard carriages may be installed on the train or parked in the garage,
    so train overview capacity and garage cards are counted separately.
    """
    if not _is_workshop_screen([str(item.get("text", "")) for item in workshop_items]):
        return None
    capacity = _passenger_capacity(overview_items)
    if capacity is None or capacity < 64 or capacity % 64:
        return None
    installed = capacity // 64
    garage = _garage_standard_carriage_count(workshop_items, height)
    total = installed + garage
    if not 1 <= installed <= 8 or not 1 <= total <= 8:
        return None
    return PassengerBuildInventory(
        installed_passenger_carriages=installed,
        garage_standard_carriages=garage,
        total_passenger_carriages=total,
        built_extra_passenger_carriages=total - 1,
        installed_seat_groups=total * 16,
    )


def _wait_for_overview(timeout: float = 5.0) -> list[dict] | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and not is_stopped():
        try:
            items = screenshot().ocr()
        except Exception:
            items = []
        if _passenger_capacity(items) is not None:
            return items
        time.sleep(0.4)
    return None


def read_passenger_build_inventory_on_workshop() -> PassengerBuildInventory | None:
    """Read and cross-check completed passenger carriages on the build screen."""
    frames = []
    for frame_index in range(3):
        image = screenshot()
        items = image.ocr()
        if not _is_workshop_screen([str(item.get("text", "")) for item in items]):
            return None
        frames.append((items, image.image.shape[0]))
        if frame_index < 2:
            time.sleep(0.25)
    garage_counts = [_garage_standard_carriage_count(items, height) for items, height in frames]
    if len(set(garage_counts)) != 1:
        logger.warning(f"车库标准客厢多帧 OCR 不一致: {garage_counts}")
        return None

    if not blurry_ocr_click("列车总览", score=0.6, trynum=2, log=False):
        logger.warning("未能打开列车总览，无法核对已装车客厢容量")
        return None
    overview_frames = []
    for _ in range(2):
        items = _wait_for_overview()
        if not items:
            break
        overview_frames.append(items)
        time.sleep(0.25)
    # Close the overview popover before the caller continues workshop actions.
    blurry_ocr_click("列车总览", score=0.6, trynum=1, log=False)
    capacities = [_passenger_capacity(items) for items in overview_frames]
    if len(capacities) < 2 or len(set(capacities)) != 1:
        logger.warning(f"列车载客容量多帧 OCR 未通过核对: {capacities}")
        return None
    result = parse_passenger_build_inventory(
        frames[-1][0], overview_frames[-1], height=frames[-1][1]
    )
    if result:
        logger.info(
            "客厢实况 OCR："
            f"列车内 {result.installed_passenger_carriages} 节，"
            f"车库标准客厢 {result.garage_standard_carriages} 节，"
            f"合计 {result.total_passenger_carriages} 节；"
            f"已建额外客厢 {result.built_extra_passenger_carriages} 节，"
            f"四座椅组 {result.installed_seat_groups} 组"
        )
    return result


def persist_passenger_build_inventory(result: PassengerBuildInventory) -> None:
    from qfluentwidgets import qconfig
    from app.common.config import cfg

    qconfig.set(cfg.PassengerBuiltExtraCarriages, result.built_extra_passenger_carriages)
    qconfig.set(cfg.PassengerInstalledSeatGroups, result.installed_seat_groups)


def scan_passenger_build_inventory() -> dict:
    """Navigate safely, OCR actual build counts, persist them, and return a mapping."""
    if not _wait_for_game():
        raise BlockedBySafetyError("游戏启动超时，无法同步客厢数量")
    try:
        if not go_home():
            raise BlockedBySafetyError("无法返回主界面")
        if not _click_until_ready("整备列车", _is_train_management_screen, score=0.6):
            raise BlockedBySafetyError("未能进入整备列车页面")
        if not _click_until_ready("编组", _is_workshop_screen, score=0.6):
            raise BlockedBySafetyError("未能进入编组工坊页面")
        result = read_passenger_build_inventory_on_workshop()
        if not result:
            raise BlockedBySafetyError("客厢/座椅数量未通过多帧 OCR 核对")
        persist_passenger_build_inventory(result)
        return result.__dict__.copy()
    finally:
        try:
            go_home()
        except Exception:
            logger.warning("客厢实况同步后返回主界面失败")


def _joined_text(texts: list[str]) -> str:
    return " ".join(str(text) for text in texts)


def _is_train_management_screen(texts: list[str]) -> bool:
    combined = _joined_text(texts)
    return "编组" in combined and ("维护" in combined or "改装" in combined)


def _is_workshop_screen(texts: list[str]) -> bool:
    combined = _joined_text(texts)
    has_workshop_status = any(
        marker in combined
        for marker in (
            "工坊空置中",
            "施工剩余时长",
            "立刻完成",
            "施工已完成",
            CLAIM_COMPLETED_TEXT,
        )
    )
    return has_workshop_status and "编组" in combined


def _is_idle_workshop_screen(texts: list[str]) -> bool:
    combined = _joined_text(texts)
    if "编组" not in combined:
        return False
    has_active_status = any(
        marker in combined
        for marker in (
            "施工剩余时长",
            "立刻完成",
            "立即完成",
            "施工已完成",
            CLAIM_COMPLETED_TEXT,
        )
    )
    if has_active_status:
        return False
    return "工坊空置中" in combined or BUILD_CARRIAGE_TEXT in combined


def _is_completed_build_screen(texts: list[str]) -> bool:
    combined = _joined_text(texts)
    return "编组" in combined and (
        "施工已完成" in combined or CLAIM_COMPLETED_TEXT in combined
    )


def _is_carriage_build_dialog(texts: list[str]) -> bool:
    combined = _joined_text(texts)
    return START_CONSTRUCTION_TEXT in combined and (
        "建造所需时长" in combined or "消耗材料" in combined
    )


def _wait_for_screen(predicate, timeout: float) -> list[str] | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and not is_stopped():
        texts = _read_screen_texts()
        if predicate(texts):
            return texts
        time.sleep(0.5)
    return None


def _click_until_ready(
    text: str,
    ready,
    *,
    timeout: float = SCREEN_TRANSITION_TIMEOUT,
    **click_kwargs,
) -> bool:
    """Retry a navigation click until the destination screen is actually ready."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and not is_stopped():
        if ready(_read_screen_texts()):
            return True
        blurry_ocr_click(text, trynum=1, log=False, **click_kwargs)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        if _wait_for_screen(ready, min(3.0, remaining)):
            return True
    return False


def _is_passenger_build_dialog(texts: list[str]) -> bool:
    """Distinguish the passenger confirmation dialog from other carriage types."""
    combined = _joined_text(texts)
    has_start = START_CONSTRUCTION_TEXT in combined
    has_passenger_stats = "载客量" in combined or "64座客车" in combined
    return has_start and has_passenger_stats


def _wait_for_passenger_build_dialog(
    timeout: float = PASSENGER_DIALOG_TIMEOUT,
) -> bool:
    return bool(_wait_for_screen(_is_passenger_build_dialog, timeout))


def _wait_for_build_started(timeout: float = BUILD_START_TIMEOUT) -> BuildScreenState:
    deadline = time.monotonic() + timeout
    last_state = BuildScreenState(False)
    while time.monotonic() < deadline and not is_stopped():
        last_state = inspect_build_screen()
        if last_state.building:
            return last_state
        time.sleep(0.5)
    return last_state


def _click_claim_completed() -> None:
    if not blurry_ocr_click(
        CLAIM_COMPLETED_TEXT,
        score=0.55,
        trynum=3,
        log=False,
    ):
        # Stable green claim button; used only after the completed-state guard.
        input_tap((855, 130))


def _claim_completed_carriage(
    timeout: float = SCREEN_TRANSITION_TIMEOUT,
    retry_interval: float = 2.0,
) -> bool:
    """Claim a finished carriage and wait until the workshop is idle."""
    if not _is_completed_build_screen(_read_screen_texts()):
        return True
    _click_claim_completed()
    deadline = time.monotonic() + timeout
    next_claim_attempt = time.monotonic() + max(0.0, retry_interval)
    dismissed_result = False
    while time.monotonic() < deadline and not is_stopped():
        texts = _read_screen_texts()
        if _is_idle_workshop_screen(texts):
            logger.info("已领取建造完成的客厢，工坊恢复空置")
            return True
        combined = _joined_text(texts)
        if "建造成功" in combined and not dismissed_result:
            input_tap((1100, 650))
            dismissed_result = True
        elif (
            _is_completed_build_screen(texts)
            and time.monotonic() >= next_claim_attempt
        ):
            logger.warning("完成状态仍存在，重试领取建造完成的客厢")
            _click_claim_completed()
            next_claim_attempt = time.monotonic() + max(0.0, retry_interval)
        time.sleep(0.8)
    return False


def _wait_for_game(timeout: float = 180.0) -> bool:
    if not is_game_running():
        start_game()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and not is_stopped():
        if connect():
            try:
                if screenshot().ocr():
                    return True
            except Exception:
                pass
        time.sleep(5)
    return False


def start_next_passenger_carriage(
    *, allow_new_construction: bool = True
) -> BuildScreenState | None:
    """Inspect the workshop, claim completion, and optionally start one carriage."""
    if not _wait_for_game():
        logger.error("游戏启动超时，未开始下一节客厢")
        return None
    go_home()
    if not _click_until_ready(
        "整备列车", _is_train_management_screen, score=0.6
    ):
        logger.error("未能进入整备列车页面")
        return None
    if not _click_until_ready("编组", _is_workshop_screen, score=0.6):
        logger.error("未能进入编组工坊页面")
        return None
    inventory = read_passenger_build_inventory_on_workshop()
    if inventory:
        persist_passenger_build_inventory(inventory)
    else:
        logger.warning("本次未更新客厢建设数量：实况 OCR 未通过多帧核对")
    current = inspect_build_screen()
    if current.building:
        logger.info(f"识别到客厢正在施工，游戏剩余 {current.remaining_seconds} 秒")
        return BuildScreenState(True, current.remaining_seconds, True)
    if _is_completed_build_screen(_read_screen_texts()):
        if not _claim_completed_carriage():
            logger.error("已在编组页识别到建造完成，但未能领取完成客厢")
            return None
        if not allow_new_construction:
            logger.info("最终一节客厢已领取，按计划停止，不再开始下一节")
            return BuildScreenState(False, None, False)
    if not allow_new_construction:
        logger.error("未识别到最终一节客厢的完成状态，不根据缓存直接确认完成")
        return None
    if not _click_until_ready(
        BUILD_CARRIAGE_TEXT,
        _is_carriage_build_dialog,
        score=0.6,
    ):
        logger.error("工坊空置，但未能打开建造车厢页面")
        return None
    if not blurry_ocr_click(
        "客厢",
        score=0.55,
        trynum=3,
        log=False,
    ):
        # The small label on the third card is often omitted by full-screen OCR.
        # This coordinate is safe only after `_is_carriage_build_dialog` proves
        # that the modal is open; the following state check prevents a wrong type.
        input_tap((338, 487))
    if not _wait_for_passenger_build_dialog():
        logger.error("已选择客厢，但建造确认页未就绪或仍显示其他车厢类型")
        return None
    if not blurry_ocr_click(
        START_CONSTRUCTION_TEXT,
        score=0.6,
        trynum=6,
        log=False,
    ):
        logger.error("未识别到客厢建造确认页的“开始施工”按钮")
        return None
    state = _wait_for_build_started()
    if not state.building:
        logger.error("点击“开始施工”后仍未识别到施工中状态，未记录开工时间")
        return None
    logger.info(f"下一节客厢已开始施工，游戏剩余 {state.remaining_seconds} 秒")
    return BuildScreenState(True, state.remaining_seconds, False)


def run_build_monitor(*, force_verify: bool = False) -> bool:
    """Perform one check; explicit manual runs bypass the cached due time."""
    state = load_build_monitor_plan()
    summary = build_monitor_summary(state)
    if not state:
        logger.warning(summary["message"])
        return False
    if not summary["active"]:
        logger.info(summary["message"])
        return True
    if state.get("active_due_at") and not summary["due_now"] and not force_verify:
        logger.info(summary["message"])
        return True
    if force_verify and state.get("active_due_at") and not summary["due_now"]:
        logger.info("人工立即执行客厢监控，跳过预计完成时间并实时复核建造状态")
    if state.get("active_due_at"):
        final_carriage = int(state["completed_carriages"]) + 1 >= int(state["target_carriages"])
        if final_carriage:
            screen_state = start_next_passenger_carriage(allow_new_construction=False)
        else:
            screen_state = start_next_passenger_carriage()
        if screen_state and screen_state.existing:
            if screen_state.remaining_seconds is not None:
                resync_active_build(
                    state, remaining_seconds=screen_state.remaining_seconds
                )
            else:
                logger.warning("识别到施工中状态，但本次未读出倒计时，稍后重试")
            return True
        if not screen_state:
            return False
        state = record_carriage_completed(state)
        if int(state["completed_carriages"]) >= int(state["target_carriages"]):
            logger.info("客厢连续建造计划已全部完成")
            return True
        record_carriage_started(
            state, remaining_seconds=screen_state.remaining_seconds
        )
        return True
    screen_state = start_next_passenger_carriage()
    if not screen_state:
        return False
    record_carriage_started(
        state, remaining_seconds=screen_state.remaining_seconds
    )
    return True


def stop():
    from core.control.control import stop as stop_control
    stop_control()
