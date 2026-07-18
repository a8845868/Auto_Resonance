import re
import time
from dataclasses import dataclass
from typing import Literal, Optional

from loguru import logger

from core.control.control import input_tap as _raw_input_tap, screenshot
from core.services.read_only_policy import ActionIntent
from core.preset import go_outlets
from core.preset.control import go_home
from core.services.station_facilities import (
    remember_rest_area_availability,
    rest_area_availability,
)
from core.services.fatigue_planner import SodaPriceTier, lunch_release_schedule
from app.common.config import cfg


Strength = tuple[int, int]
RestAreaStatus = Literal[
    "used", "not_needed", "unavailable", "exhausted", "failed"
]


@dataclass(frozen=True)
class RestAreaRecovery:
    fatigue: int
    status: RestAreaStatus
    used: int = 0


def input_tap(
    pos: tuple[int, int],
    *,
    action_key: str = "fatigue_confirm",
    page_id: str = "fatigue_flow",
    anchor_key: str = "fatigue_control",
) -> object:
    """Attach explicit fatigue semantics to every module-owned tap."""
    return _raw_input_tap(
        pos,
        intent=ActionIntent(
            (
                "page_back" if action_key == "back"
                else "fatigue_info_open" if action_key == "open_detail"
                else action_key
            ),
            "top_left_back" if action_key == "back" else anchor_key,
            f"fatigue:{page_id}:{anchor_key}",
        ),
    )


def read_strength() -> Optional[Strength]:
    """Read current/max fatigue from any screen that displays ``123/816``."""
    candidates = []
    for item in screenshot().ocr():
        match = re.search(r"(\d+)\s*/\s*(\d+)", item["text"])
        if match:
            current, maximum = map(int, match.groups())
            position = item["position"]
            center_x = (position[0][0] + position[2][0]) / 2
            center_y = (position[0][1] + position[2][1]) / 2
            if center_y < 100 and 500 <= maximum <= 2000:
                candidates.append((center_x, current, maximum))
    # Cargo is also rendered as x/y, immediately to the left of fatigue.
    if not candidates:
        return None
    _, current, maximum = max(candidates, key=lambda item: item[0])
    return current, maximum


def check_shop_strength(min_available: int = 60) -> bool:
    strength = read_strength()
    if strength is None:
        logger.warning("未识别到疲劳值，暂不阻止当前操作")
        return True
    current, maximum = strength
    logger.info(f"当前疲劳: {current}/{maximum}，可用余量 {maximum - current}")
    return maximum - current > min_available


def _screen_has(*texts: str) -> bool:
    visible = [item["text"] for item in screenshot().ocr()]
    return any(any(text in item for text in texts) for item in visible)


def _wait_text(*texts: str, timeout: float = 8.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _screen_has(*texts):
            return True
        time.sleep(0.7)
    return False


def _click_ocr_text(text: str) -> bool:
    for item in screenshot().ocr():
        if text not in str(item.get("text", "")):
            continue
        position = item.get("position")
        if not position:
            continue
        center_x = (position[0][0] + position[2][0]) / 2
        center_y = (position[0][1] + position[2][1]) / 2
        input_tap((center_x, center_y))
        return True
    return False


def _confirm_repeat_drink() -> bool:
    """Accept the active-buff warning and suppress it for the rest of the day."""
    if not _screen_has("还没有到失效时间", "再喝一杯"):
        return False
    if _screen_has("当天不再提醒"):
        input_tap((576, 671))
        time.sleep(0.3)
    input_tap((960, 503))
    return True


def _skip_drink_animation(timeout: float = 8.0) -> bool:
    """Click SKIP as soon as the drinking animation exposes it."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        # The FPS overlay can cover the final letter, while OCR still returns
        # the stable top-right prefix `SKI`.
        if _click_ocr_text("SKI"):
            time.sleep(0.5)
            return True
        time.sleep(0.2)
    # Stable 1280x720 fallback when OCR misses the animated label entirely.
    input_tap((1205, 35))
    time.sleep(0.5)
    return False


def _ensure_drink_selection() -> bool:
    """Open the drink card again when an animation returns to the rest-area root."""
    if _screen_has("银枝气泡水", "本次免费"):
        return True
    if not _screen_has("喝一杯", "休息区"):
        return False
    input_tap((960, 325))
    return _wait_text("银枝气泡水", "本次免费", timeout=6)


def exit_negotiation_safely() -> None:
    """Leave negotiation and resolve the reset-warning instead of stranding UI."""
    input_tap((83, 36), action_key="back", page_id="negotiation", anchor_key="top_left_back")
    time.sleep(1.5)
    if _screen_has("退出后议价幅度将重置", "是否继续"):
        input_tap((768, 447), action_key="navigation_anchor", page_id="negotiation_exit", anchor_key="confirm_exit")
        time.sleep(2)


def _open_fatigue_panel() -> bool:
    input_tap((970, 30), action_key="open_detail", page_id="hud", anchor_key="fatigue_value")
    return _wait_text("恢复疲劳值方式", "FATIGUE", timeout=5)


def _silver_prompt_visible() -> bool:
    # The free drink card is also named "银枝气泡水".  Only the separate
    # confirmation dialog means that consuming a silver branch is required.
    return _screen_has("是否使用银枝")


def _resolve_silver_prompt() -> bool:
    """Resolve the paid-drink prompt immediately and return whether accepted."""
    if not _silver_prompt_visible():
        return False
    if bool(cfg.UseSilverBranch.value):
        logger.info("已开启“使用银枝恢复疲劳”，确认消耗 1 银枝")
        input_tap((960, 531))
        return True
    logger.info("未开启“使用银枝恢复疲劳”，立即取消")
    input_tap((320, 531))
    time.sleep(1.5)
    return False


def _fatigue_after_drink(current: int, target: int) -> int:
    """Prefer a fresh HUD reading, falling back to the known 50-point recovery."""
    estimated = max(target, current - 50)
    try:
        observed = read_strength()
    except (KeyError, TypeError, ValueError):
        observed = None
    if observed and 0 <= observed[0] < current:
        return max(target, observed[0])
    return estimated


def _use_free_rest_area(
    starting_fatigue: int,
    target_fatigue: int = 0,
    station_name: str | None = None,
) -> RestAreaRecovery:
    """Consume free or 500-iron drinks until fatigue is fully recovered.

    Iron currency is intentionally treated as effectively free for this
    workflow.  Only the separate silver-branch confirmation remains guarded by
    the corresponding setting.
    """
    target_fatigue = max(0, target_fatigue)
    availability = rest_area_availability(station_name)
    if availability is False:
        logger.info(f"站点 {station_name or '未知'} 不设休息区，跳过气泡水入口")
        return RestAreaRecovery(starting_fatigue, "unavailable")

    # The fatigue panel exposes this state before any navigation. It is a
    # facility verdict, not a button that should be clicked until timeout.
    if _screen_has("不在范围内"):
        remember_rest_area_availability(station_name, False)
        logger.info(
            f"站点 {station_name or '未知'} 的疲劳面板已确认“不在范围内”，"
            "本次及本进程后续均跳过休息区"
        )
        return RestAreaRecovery(starting_fatigue, "unavailable")

    if starting_fatigue - target_fatigue < 50:
        logger.info(
            f"当前疲劳 {starting_fatigue}，不足气泡水单次恢复量 50；"
            "为避免浪费，跳过气泡水"
        )
        return RestAreaRecovery(starting_fatigue, "not_needed")

    input_tap((1117, 344))  # 前往休息区
    if not _wait_text("喝一杯", "休息区", timeout=8):
        logger.warning("点击休息区后未确认到达；按画面异常处理，不继续执行便当")
        return RestAreaRecovery(starting_fatigue, "failed")

    if not _ensure_drink_selection():
        logger.warning("已进入休息区但未显示气泡水选项；按画面异常处理")
        return RestAreaRecovery(starting_fatigue, "failed")
    remember_rest_area_availability(station_name, True)

    current = starting_fatigue
    used = 0
    iron_or_free_used = 0
    paid_used = 0
    # Max fatigue is currently below 1,000.  Derive the required number of
    # 50-point drinks and retain a defensive cap against a bad OCR value.
    max_uses = min(24, max(0, (current - target_fatigue) // 50))
    while used < max_uses and current - target_fatigue >= 50:
        if _silver_prompt_visible():
            if not _resolve_silver_prompt():
                break
            _skip_drink_animation()
            _wait_text("喝一杯", "休息区", timeout=6)
            used += 1
            paid_used += 1
            current = _fatigue_after_drink(current, target_fatigue)
            continue
        if not _ensure_drink_selection():
            logger.info("未能重新打开气泡水列表，停止连续恢复")
            break
        is_free = _screen_has("本次免费")
        has_iron_drink = _screen_has("银枝气泡水")
        if not is_free and not has_iron_drink:
            break
        # Both the daily-free card and the 500-iron card are always allowed.
        # A rare-currency cost is handled only if the separate silver prompt
        # appears after this click.
        input_tap((960, 422))
        time.sleep(1.0)
        paid = False
        if _silver_prompt_visible():
            if not _resolve_silver_prompt():
                break
            paid = True
        else:
            _confirm_repeat_drink()
        _skip_drink_animation()
        _wait_text("喝一杯", "休息区", timeout=6)
        used += 1
        if paid:
            paid_used += 1
        else:
            iron_or_free_used += 1
        current = _fatigue_after_drink(current, target_fatigue)
        logger.info(
            f"休息区已饮用气泡水 {used} 次（免费/铁盟币 {iron_or_free_used}，"
            f"银枝 {paid_used}），"
            f"当前疲劳预计 {current}"
        )
    return RestAreaRecovery(
        current,
        "used" if used else "exhausted",
        used,
    )


def _return_to_trade(trade_type: Literal["buy", "sell"]) -> bool:
    if not go_home():
        logger.error("未能返回主界面，拒绝继续进入交易所")
        return False
    if not go_outlets("交易所"):
        return False
    time.sleep(1.5)
    input_tap((927, 321) if trade_type == "buy" else (932, 404))
    time.sleep(2)
    return True


def _lunchbox_inventory(image) -> int | None:
    """Read the cabinet's remaining-count badge without guessing on OCR failure."""
    candidates = []
    for item in image.ocr():
        text = str(item.get("text", "")).strip()
        position = item.get("position")
        if not position or not re.fullmatch(r"\d+", text):
            continue
        center_x = (position[0][0] + position[2][0]) / 2
        center_y = (position[0][1] + position[2][1]) / 2
        if 1100 <= center_x <= 1215 and 400 <= center_y <= 535:
            candidates.append(int(text))
    return candidates[-1] if candidates else None


def _lunchbox_total_recovery(items) -> int | None:
    """Read the authoritative total from the use-all confirmation dialog."""

    for item in items:
        match = re.search(
            r"(?:使用全部便当[^\d]*)?(?:消除|恢复)\s*(\d+)\s*疲劳值",
            str(item.get("text", "")),
        )
        if match:
            return int(match.group(1))
    return None


def _lunchbox_recovery_values(items) -> tuple[int, ...]:
    """Read per-bento recovery values from cabinet cards, preserving order."""

    values = []
    for item in items:
        match = re.search(
            r"(?:消除|恢复)\s*(\d+)\s*疲劳(?:值)?",
            str(item.get("text", "")),
        )
        if match:
            values.append(int(match.group(1)))
    return tuple(values)


def _observed_soda_price_tier(items, use_index: int) -> SodaPriceTier | None:
    texts = [str(item.get("text", "")) for item in items]
    joined = " ".join(texts)
    if "本次免费" in joined or "免费" in joined:
        return SodaPriceTier(use_index, "FREE", 0, True)
    iron = re.search(r"(\d+)\s*(?:铁盟币|铁币)", joined)
    if iron:
        return SodaPriceTier(use_index, "IRON", int(iron.group(1)), True)
    silver = re.search(r"(\d+)\s*银枝", joined)
    if silver or "是否使用银枝" in joined:
        cost = int(silver.group(1)) if silver else 1
        return SodaPriceTier(use_index, "SILVER", cost, bool(cfg.UseSilverBranch.value))
    return None


def observe_recovery_resources(station_name: str | None = None) -> dict[str, object]:
    """Open recovery pages read-only and return only values observed in UI."""

    observation: dict[str, object] = {
        "lunches_remaining": None,
        "lunch_recovery_values": (),
        "lunch_total_recovery": None,
        "soda_price_tiers": (),
        "rest_area_available": rest_area_availability(station_name),
        "source_confidence": "UNKNOWN",
    }
    if not _open_fatigue_panel():
        return observation

    if not _screen_has("不在范围内"):
        input_tap((1117, 344))
        if _wait_text("喝一杯", "休息区", timeout=8) and _ensure_drink_selection():
            tier = _observed_soda_price_tier(screenshot().ocr(), 1)
            if tier is not None:
                observation["soda_price_tiers"] = (tier,)
            observation["rest_area_available"] = True
            remember_rest_area_availability(station_name, True)
        go_home()
        _open_fatigue_panel()
    else:
        observation["rest_area_available"] = False
        remember_rest_area_availability(station_name, False)

    input_tap((1117, 607))
    if _wait_text("便当柜", "BENTO CABINET", timeout=8):
        cabinet = screenshot()
        observation["lunches_remaining"] = _lunchbox_inventory(cabinet)
        observation["lunch_recovery_values"] = _lunchbox_recovery_values(
            cabinet.ocr()
        )
        input_tap((1070, 427))
        time.sleep(1.0)
        total = _lunchbox_total_recovery(screenshot().ocr())
        observation["lunch_total_recovery"] = total
        # Observation is read-only; always cancel the irreversible batch use.
        input_tap((320, 503))
        observation["source_confidence"] = (
            "HIGH"
            if observation["lunches_remaining"] is not None
            and (total is not None or observation["lunches_remaining"] == 0)
            else "PARTIAL"
        )
    go_home()
    return observation


def execute_planned_recovery_action(
    kind: str,
    *,
    station_name: str | None,
) -> dict[str, object]:
    """Execute exactly one planned recovery action and verify its effect."""

    before = read_strength()
    if before is None or not _open_fatigue_panel():
        return {"success": False, "reason": "fatigue_not_observed"}
    current, _maximum = before
    if kind == "DRINK_SODA":
        result = _use_free_rest_area(
            current,
            max(0, current - 50),
            station_name,
        )
        go_home()
        after = read_strength()
        success = result.used == 1 and after is not None and after[0] < current
        return {
            "success": success,
            "kind": kind,
            "bubble_water_uses": result.used if success else 0,
            "before": current,
            "after": after[0] if after else current,
        }
    if kind == "USE_ALL_BENTOS":
        usage: dict[str, object] = {}
        after_value = _use_all_safe_lunchboxes(current, usage)
        go_home()
        return {
            "success": after_value < current,
            "kind": kind,
            "lunch_batches": 1 if after_value < current else 0,
            "lunch_fatigue_restored": max(0, current - after_value),
            "lunches_remaining": usage.get("lunches_remaining"),
            "before": current,
            "after": after_value,
        }
    go_home()
    return {"success": False, "reason": f"unsupported_action:{kind}"}


def _use_all_safe_lunchboxes(
    current_fatigue: int,
    usage: dict[str, object] | None = None,
) -> int:
    """Use the cabinet's batch action only when its full recovery cannot waste."""
    input_tap((1117, 607))  # 前往便当柜
    if not _wait_text("便当柜", "BENTO CABINET", timeout=8):
        logger.info("未进入便当柜")
        return current_fatigue

    cabinet_image = screenshot()
    if usage is not None:
        usage["lunch_schedule"] = lunch_release_schedule()
        remaining = _lunchbox_inventory(cabinet_image)
        if remaining is not None:
            usage["lunches_remaining"] = remaining

    input_tap((1070, 427))  # 全部使用
    time.sleep(2)
    recovery = _lunchbox_total_recovery(screenshot().ocr())
    if recovery is None:
        logger.info("没有可批量使用的便当")
        input_tap((320, 503))
        return current_fatigue
    if recovery > current_fatigue:
        logger.info(
            f"全部便当可恢复 {recovery}，当前疲劳 {current_fatigue}，为避免浪费暂不使用"
        )
        input_tap((320, 503))
        return current_fatigue

    input_tap((960, 503))
    time.sleep(6)
    # Dismiss the recovery-result overlay before navigating away.
    input_tap((640, 600))
    time.sleep(2)
    if usage is not None:
        usage["lunches_remaining"] = 0
    logger.info(f"已一次使用全部安全便当，恢复 {recovery} 疲劳")
    return current_fatigue - recovery


def recover_strength(
    trade_type: Literal["buy", "sell"],
    min_available: int = 60,
    station_name: str | None = None,
    usage: dict[str, object] | None = None,
) -> bool:
    """Recover fatigue with free rest-area drinks before safe batch lunches."""
    strength = read_strength()
    if strength is None:
        logger.error("无法读取当前疲劳值")
        return False
    current, maximum = strength

    # Preserve the required resource order. If a non-wasteful drink is still
    # available in principle but this station has no rest area, do not consume
    # lunches first and do not report the daily plan as completed.
    no_rest_area = rest_area_availability(station_name) is False
    available = maximum - current
    urgent_shortfall = available < max(0, min_available)
    if no_rest_area and current >= 50 and not urgent_shortfall:
        logger.warning(
            f"站点 {station_name or '未知'} 不设休息区，当前疲劳 {current} 可无浪费使用气泡水；"
            "疲劳规划暂缓，便当保持不动"
        )
        return False
    if no_rest_area and current >= 50 and urgent_shortfall:
        logger.warning(
            f"站点 {station_name or '未知'} 不设休息区，但当前可用疲劳 {available} "
            f"低于最低安全余量 {min_available}；允许先使用不会浪费的便当"
        )

    if not _open_fatigue_panel():
        return False

    # Free and 500-iron drinks are cheap recovery: continue to zero instead of
    # stopping as soon as the current bargain reserve is satisfied.
    rest_area = _use_free_rest_area(current, 0, station_name)
    current = rest_area.fatigue
    if usage is not None and rest_area.used:
        usage["bubble_water_uses"] = (
            usage.get("bubble_water_uses", 0) + rest_area.used
        )
    if rest_area.status == "failed":
        go_home()
        return False
    if (
        rest_area.status == "unavailable"
        and current >= 50
        and not urgent_shortfall
    ):
        logger.warning(
            "当前疲劳仍可无浪费使用气泡水；保留疲劳任务到有休息区的核心城市，"
            "不提前使用便当"
        )
        go_home()
        return False

    # When the station has no rest area and drinking is not needed, the
    # fatigue panel is still open; inspect the lunchbox directly and navigate
    # back only once.
    if rest_area.status == "unavailable":
        if current > 0:
            before_lunch = current
            current = _use_all_safe_lunchboxes(current, usage)
            if usage is not None and current < before_lunch:
                usage["lunch_batches"] = usage.get("lunch_batches", 0) + 1
                usage["lunch_fatigue_restored"] = (
                    usage.get("lunch_fatigue_restored", 0)
                    + before_lunch
                    - current
                )
        if not _return_to_trade(trade_type):
            return False
        final = read_strength()
        if final:
            logger.info(f"疲劳恢复完成: {final[0]}/{final[1]}")
            return final[1] - final[0] >= min_available
        return maximum - current >= min_available

    if not _return_to_trade(trade_type):
        return False
    observed = read_strength()
    if observed:
        current, maximum = observed

    if current > 0 or usage is not None:
        if not _open_fatigue_panel():
            return False
        before_lunch = current
        current = _use_all_safe_lunchboxes(current, usage)
        if usage is not None and current < before_lunch:
            usage["lunch_batches"] = usage.get("lunch_batches", 0) + 1
            usage["lunch_fatigue_restored"] = (
                usage.get("lunch_fatigue_restored", 0) + before_lunch - current
            )

        # Recovery pages return to the city/home screen. Re-enter the same
        # trading page so the caller can resume the interrupted operation.
        if not _return_to_trade(trade_type):
            return False
    else:
        logger.info("免费/铁盟币气泡水已将疲劳恢复至满状态，跳过便当柜")
    final = read_strength()
    if final:
        logger.info(f"疲劳恢复完成: {final[0]}/{final[1]}")
        return final[1] - final[0] >= min_available
    return maximum - current >= min_available


def prepare_negotiation(
    trade_type: Literal["buy", "sell"], desired_successes: int = 2
) -> int:
    """Return a safe success target, or zero when recovery is exhausted.

    A negotiation attempt costs 8 fatigue. Reserving 10 attempts (80 fatigue)
    is deliberately conservative and lets the automation pursue two actual
    successes despite failed rolls without becoming trapped in the exit dialog.
    """
    desired_successes = max(0, min(2, desired_successes))
    if desired_successes == 0:
        return 0
    strength = read_strength()
    if strength is None:
        logger.warning("无法读取疲劳，本次放弃议价")
        return 0
    current, maximum = strength
    reserve = 80
    if maximum - current < reserve:
        logger.warning(
            f"完成 2 次成功议价保守需要 {reserve} 疲劳，"
            f"当前仅剩 {maximum - current}；疲劳恢复已由独立疲劳规划负责"
        )
        return 0
    return desired_successes


def can_afford_fatigue(cost: int) -> bool:
    strength = read_strength()
    if strength is None:
        return False
    current, maximum = strength
    available = maximum - current
    logger.info(f"下一段预计需要 {cost} 疲劳，当前可用 {available}")
    return available >= max(0, cost)


# Legacy entrypoint retained for callers outside the trading workflow.
def use_strength():
    return recover_strength("buy")
