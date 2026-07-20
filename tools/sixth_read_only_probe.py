"""Instance-0 evidence probe protected by a runtime read-only action guard."""

from __future__ import annotations

import argparse
import hashlib
import inspect
import json
import re
import sys
from dataclasses import asdict, is_dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path

import cv2 as cv
from loguru import logger


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from auto import exchange_navigation  # noqa: E402
from auto.module.strength import (  # noqa: E402
    _open_fatigue_panel,
    observe_fatigue_frame,
    read_strength,
)
from auto.resident_activity import ResidentActivityAutomation  # noqa: E402
from auto.reward_collection import RewardCollector, RewardDriver  # noqa: E402
from core.control.control import (  # noqa: E402
    connect_adb,
    capture_envelope,
    _create_production_read_only_safety_session,
    current_display_geometry,
    screenshot,
)
from core.image.image import Image  # noqa: E402
from core.preset import get_station  # noqa: E402
from core.services.read_only_policy import (  # noqa: E402
    AnchorResolver,
    ActionIntent,
    CalibratedStaticRegion,
    ObservedAnchor,
    PageObservation,
    PageObserver,
    ReadOnlyActionGuard,
    ReadOnlyPermit,
    TrustedFrameEvidence,
    create_test_read_only_session,
    installed_read_only_guard,
)
from core.services.station_facilities import rest_area_availability  # noqa: E402
from tools.audit_export import (  # noqa: E402
    build_live_validation_result,
    render_live_validation_addendum,
)


def _jsonable(value):
    if is_dataclass(value):
        return _jsonable(asdict(value))
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if hasattr(value, "item"):
        return value.item()
    return value


def _capture(output: Path, name: str) -> dict:
    envelope = capture_envelope()
    frame = Image(envelope.frame)
    image_path = output / f"{name}.png"
    cv.imwrite(str(image_path), frame.image)
    items = list(frame.ocr())
    page_type, markers = _classify_page(
        [str(item.get("text", "")).replace(" ", "") for item in items]
    )
    payload = {
        "name": name,
        "captured_at": datetime.now().astimezone().isoformat(timespec="milliseconds"),
        "image": image_path.name,
        "sha256": hashlib.sha256(image_path.read_bytes()).hexdigest(),
        "shape": list(frame.image.shape),
        "page_type": page_type,
        "markers": markers,
    }
    (output / f"{name}.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return payload


def _bbox(item: dict) -> tuple[int, int, int, int] | None:
    points = item.get("position") or ()
    if len(points) < 3:
        return None
    xs = [int(point[0]) for point in points]
    ys = [int(point[1]) for point in points]
    return min(xs), min(ys), max(xs), max(ys)


def _classify_page(texts: list[str]) -> tuple[str, list[str]]:
    """Classify specific transactional pages before their generic menu shell."""

    joined = "|".join(texts)
    if any(marker in joined for marker in ("每日签到奖励", "签到奖励", "签到")):
        return "checkin_overlay", ["checkin_overlay"]
    if any(marker in joined for marker in ("公告", "资讯")):
        return "announcement_overlay", ["announcement_overlay"]
    if "触碰空白区域退出" in joined:
        return "unknown_overlay", ["unknown_overlay"]
    buy_features = {
        marker for marker in ("预计买入", "买入总价", "载货量", "买入")
        if marker in joined
    }
    sell_features = {
        marker for marker in ("预计卖出", "卖出总价", "载货量", "卖出")
        if marker in joined
    }
    buy_strong = len(buy_features) >= 3 and bool(
        {"预计买入", "买入总价"} & buy_features
    )
    sell_strong = len(sell_features) >= 3 and bool(
        {"预计卖出", "卖出总价"} & sell_features
    )
    if buy_strong and sell_strong:
        return "unknown", ["conflicting_exchange_markers", "unknown"]
    if buy_strong:
        return "exchange_buy", ["exchange_buy"]
    if sell_strong:
        return "exchange_sell", ["exchange_sell"]
    if "每日活跃" in joined or ("完成进度" in joined and "活跃度" in joined):
        return "daily_activity", ["daily_activity"]
    if "环游手册" in joined and "任务列表" in joined:
        return "manual_tasks", ["manual_tasks"]
    if "环游手册" in joined:
        return "manual_track", ["manual_track"]
    if any(marker in joined for marker in ("恢复疲劳值方式", "疲劳值恢复", "FATIGUE")):
        return "fatigue_info", ["fatigue_info"]
    if any(marker in joined for marker in ("我要买", "我要卖")):
        return "exchange", ["exchange_menu"]
    if any(marker in joined for marker in ("你想要什么", "研究报告", "什么都行")):
        return "npc_dialogue", ["npc_dialogue"]
    city_context = sum(
        marker in joined
        for marker in ("当前城市", "城市设施", "城市手册", "城市发展度", "CITY")
    )
    city_facilities = sum(
        marker in joined for marker in ("交易所", "商会", "休息区")
    )
    if city_context >= 2 and city_facilities >= 1:
        return "city_map", ["city_map"]
    if any(marker in joined for marker in ("访问城市", "启程", "作战终端")):
        return "home", ["top_level_hud"]
    if any(marker in joined for marker in ("进入游戏", "启动游戏")):
        return "login", ["login"]
    if "取消" in joined:
        return "clarity_dialog", ["safe_cancel_dialog"]
    return "unknown", ["unknown"]


def _static_region(
    anchor_id: str,
    bbox: tuple[int, int, int, int],
    page_type: str,
    action_key: str,
    postcondition: str,
    geometry_revision: str,
) -> CalibratedStaticRegion:
    return CalibratedStaticRegion(
        anchor_id=anchor_id,
        bbox=bbox,
        page_classifier=page_type,
        allowed_action=action_key,
        postcondition=postcondition,
        geometry_revision=geometry_revision,
    )


def _trusted_observation() -> TrustedFrameEvidence:
    """Build only safety facts from a fresh frame; never persist raw OCR here."""

    envelope = capture_envelope()
    frame = Image(envelope.frame)
    items = list(frame.ocr())
    texts = [str(item.get("text", "")).replace(" ", "") for item in items]
    joined = "|".join(texts)
    page_type, markers = _classify_page(texts)
    anchors: list[ObservedAnchor] = []
    geometry = current_display_geometry()
    static_regions: list[CalibratedStaticRegion] = []

    if any(marker in joined for marker in ("注销", "退出登录", "账号设置")):
        markers.append("account_logout")
    if page_type in {"daily_activity", "manual_tasks", "manual_track"}:
        static_regions.append(_static_region(
            "top_left_back", (20, 10, 130, 85), page_type,
            "reward_back", "page_identity_must_change_or_remain_safe",
            geometry.geometry_revision,
        ))
    if page_type in {
        "home", "daily_activity", "manual_tasks", "manual_track",
        "city_map", "npc_dialogue", "exchange", "exchange_buy", "exchange_sell",
    }:
        static_regions.append(_static_region(
            "top_left_back", (20, 10, 130, 85), page_type,
            "page_back", "page_identity_must_change_or_remain_safe",
            geometry.geometry_revision,
        ))
    if page_type == "daily_activity":
        static_regions.append(_static_region(
            "daily_content", (150, 180, 1180, 650), page_type,
            "daily_horizontal_scroll", "daily_anchor_remains_valid",
            geometry.geometry_revision,
        ))
    if page_type in {"manual_tasks", "manual_track"}:
        static_regions.append(_static_region(
            "manual_content", (150, 100, 1180, 650), page_type,
            "manual_horizontal_scroll", "manual_anchor_remains_valid",
            geometry.geometry_revision,
        ))
    if page_type == "home":
        static_regions.extend((
            _static_region("daily_shortcut", (998, 32, 1098, 132), page_type, "daily_page_open", "page_identity_must_change_or_remain_safe", geometry.geometry_revision),
            _static_region("manual_shortcut", (1082, 32, 1182, 132), page_type, "manual_page_open", "page_identity_must_change_or_remain_safe", geometry.geometry_revision),
        ))
    if page_type == "city_map":
        static_regions.append(_static_region(
            "outlet_list", (180, 120, 1080, 650), page_type,
            "outlet_list_scroll", "city_map_content_changed",
            geometry.geometry_revision,
        ))
    if page_type == "login":
        static_regions.append(_static_region(
            "enter_game", (560, 500, 720, 620), page_type,
            "enter_game", "top_level_hud_or_safe_startup_transition",
            geometry.geometry_revision,
        ))
    for item in items:
        text_value = str(item.get("text", "")).replace(" ", "")
        bounds = _bbox(item)
        if bounds is None:
            continue
        if "我要买" in text_value:
            anchors.append(ObservedAnchor("buy_navigation", text_value, bounds))
        if "我要卖" in text_value:
            anchors.append(ObservedAnchor("sell_navigation", text_value, bounds))
        if "访问城市" in text_value:
            anchors.append(ObservedAnchor("city_entry", text_value, bounds))
        if page_type == "city_map" and "交易所" in text_value:
            anchors.append(ObservedAnchor("交易所", text_value, bounds))
        if text_value == "任务列表":
            anchors.append(ObservedAnchor("manual_tasks_tab", text_value, bounds))
        if text_value == "环游手册":
            anchors.append(ObservedAnchor("manual_track_tab", text_value, bounds))
        if "取消" in text_value:
            anchors.append(ObservedAnchor("cancel", text_value, bounds))
        match = re.search(r"(\d+)\s*/\s*(\d+)", text_value)
        if match and bounds[1] < 100 and 500 <= int(match.group(2)) <= 2000:
            anchors.append(ObservedAnchor("fatigue_value", "fatigue_ratio", bounds))

    screenshot_hash = envelope.raw_frame_hash
    observation_id = hashlib.sha256(
        f"{screenshot_hash}|{page_type}|{'|'.join(sorted(markers))}".encode("utf-8")
    ).hexdigest()[:24]
    source_sequence = envelope.backend_monotonic_sequence
    captured_at = envelope.captured_at
    source_capture_id = envelope.backend_capture_id
    content_marker_hash = hashlib.sha256(
        "|".join(
            sorted(
                f"{str(item.get('text', '')).strip()}@{_bbox(item)}"
                for item in items if item.get("position")
            )
        ).encode("utf-8")
    ).hexdigest()
    observation = PageObservation(
        observation_id=observation_id,
        screenshot_hash=screenshot_hash,
        page_type=page_type,
        markers=tuple(markers),
        anchors=tuple(anchors),
        captured_at=captured_at,
        display_geometry=geometry,
        static_regions=tuple(static_regions),
        capture_sequence=source_sequence,
        source_capture_id=source_capture_id,
        source_monotonic_sequence=source_sequence,
        backend_generation=envelope.backend_generation,
        instance_id=envelope.instance_id,
        adb_serial=envelope.adb_serial,
        content_marker_hash=content_marker_hash,
    )
    return TrustedFrameEvidence(
        source_capture_id=source_capture_id,
        source_monotonic_sequence=source_sequence,
        raw_frame_hash=screenshot_hash,
        captured_at=captured_at,
        backend_generation=envelope.backend_generation,
        instance_id=envelope.instance_id,
        adb_serial=envelope.adb_serial,
        logical_resolution=(1280, 720),
        observation=observation,
    )


def _return_home_safely(driver: RewardDriver, *, attempt_limit: int = 8) -> bool:
    """Return through classified safe pages and independently verify HOME."""

    safe_back_pages = {
        "daily_activity", "manual_tasks", "manual_track", "city_map",
        "npc_dialogue", "exchange", "exchange_buy", "exchange_sell",
        "inventory", "fatigue_info",
    }
    for attempt in range(max(1, int(attempt_limit))):
        page_type = _trusted_observation().observation.page_type
        if page_type in {"home", "hud"}:
            return True
        if page_type not in safe_back_pages:
            return False
        try:
            driver.tap(
                (82, 36), action_key="page_back",
                page_id=page_type, anchor_key="top_left_back",
            )
        except PermissionError:
            return False
        if attempt + 1 < int(attempt_limit):
            driver.sleep(0.8)
    return False


def run_policy_canaries(_live_guard: ReadOnlyActionGuard) -> list[dict]:
    """Exercise a detached policy guard so canaries never pollute live evidence."""

    canary_guard = ReadOnlyActionGuard()
    canary_guard.tap("transaction_buy", (1000, 650), "exchange_buy")
    canary_guard.tap("reward_claim", (1000, 620), "daily_reward")
    canary_guard.tap("fatigue_confirm", (900, 600), "rest_area")
    results = [asdict(entry) for entry in canary_guard.journal]
    results.append({
        "canary": "caller_executor_callback_absent",
        "action_key": "guard_public_api",
        "allowed": False,
        "reason": "api_absent" if all(
            "_execute" not in inspect.signature(method).parameters
            for method in (
                ReadOnlyActionGuard.authorize_coordinate,
                ReadOnlyActionGuard.authorize_swipe,
            )
        ) else "api_present",
    })

    captured_at = datetime.now().astimezone()
    daily = PageObservation(
        observation_id="canary-daily", screenshot_hash="c" * 64,
        page_type="daily_activity", markers=("daily_activity",),
        anchors=(
            ObservedAnchor("top_left_back", "返回", (20, 10, 130, 85)),
            ObservedAnchor("daily_content", "任务列表", (150, 180, 1180, 650)),
        ), captured_at=captured_at,
    )
    home = PageObservation(
        "canary-home", "h" * 64, "home", ("home",), (), captured_at,
    )
    unknown = PageObservation(
        "canary-unknown", "d" * 64, "unknown", ("unknown",), (), captured_at,
    )

    def source(pages):
        state = {"index": 0}
        def capture():
            state["index"] += 1
            sequence = state["index"]
            page = pages[min(sequence - 1, len(pages) - 1)]
            return replace(
                page,
                captured_at=captured_at + timedelta(microseconds=sequence),
                capture_sequence=sequence,
                source_capture_id=f"canary-{id(state)}-{sequence}",
                source_monotonic_sequence=sequence,
                backend_generation=1,
                instance_id="test-instance-0",
                adb_serial="test-adb-0",
            )
        return capture

    detached_guard, detached_issuer = create_test_read_only_session(
        PageObserver(source([daily])), lambda _point: None,
        now=lambda: captured_at,
    )
    detached_guard.authorize_coordinate(
        (1000, 620),
        intent=ActionIntent("daily_horizontal_scroll", "daily_content", "canary-kind"),
    )
    kind_entry = asdict(detached_guard.journal[-1])
    kind_entry["canary"] = "scroll_policy_via_coordinate_api"
    results.append(kind_entry)

    post_guard, post_issuer = create_test_read_only_session(
        PageObserver(source([daily, daily, unknown])), lambda _point: None,
        now=lambda: captured_at,
    )
    post_guard.authorize_coordinate(
        (50, 40), intent=ActionIntent("reward_back", "top_left_back", "canary-post")
    )
    post_entry = asdict(post_guard.journal[-1])
    post_entry["canary"] = "postcondition_failure_stops"
    results.append(post_entry)

    def failing_executor(_point):
        raise RuntimeError("canary device write failed")

    failure_guard, failure_issuer = create_test_read_only_session(
        PageObserver(source([daily, daily, home])), failing_executor,
        now=lambda: captured_at,
    )
    try:
        failure_guard.authorize_coordinate(
            (50, 40), intent=ActionIntent("reward_back", "top_left_back", "canary-executor")
        )
    except RuntimeError:
        pass
    failure_entry = asdict(failure_guard.journal[-1])
    failure_entry["canary"] = "executor_failure_not_success"
    results.append(failure_entry)
    authenticity_guard, authenticity_issuer = create_test_read_only_session(
        PageObserver(source([daily, daily, home, daily])), lambda _point: None,
        now=lambda: captured_at,
    )
    forged = ReadOnlyPermit("CANARY-FORGED-NOT-REGISTRY-ISSUED")
    authenticity_guard.authorize_coordinate((50, 40), permit=forged)
    entry = asdict(authenticity_guard.journal[-1])
    entry["canary"] = "directly_constructed_permit"
    results.append(entry)

    permit = authenticity_issuer.issue(
        ActionIntent("reward_back", "top_left_back", "canary-mutation"), ((50, 40),)
    )
    mutated = ReadOnlyPermit(permit.opaque_token)
    object.__setattr__(mutated, "opaque_token", "CANARY-MUTATED-TOKEN")
    authenticity_guard.authorize_coordinate((50, 40), permit=mutated)
    entry = asdict(authenticity_guard.journal[-1])
    entry["canary"] = "mutated_permit_fields"
    results.append(entry)
    return results


def policy_report(
    guard: ReadOnlyActionGuard,
    *,
    policy_canary_results: list[dict] | None = None,
) -> dict:
    live = [asdict(entry) for entry in guard.journal]
    actual = [entry for entry in live if not entry["allowed"]]
    return {
        "mode": "READ_ONLY",
        "policy_canary_results": list(policy_canary_results or []),
        "policy_canaries": list(policy_canary_results or []),
        "live_action_journal": live,
        "actual_blocked_production_actions": actual,
        # Compatibility aliases remain truthful: only live entries appear.
        "journal": live,
        "blocked_actions": [entry["action_key"] for entry in actual],
    }


def _run_probe(
    args,
    output: Path,
    guard: ReadOnlyActionGuard,
    *,
    policy_canaries: list[dict],
) -> dict:
    driver = RewardDriver()
    collector = RewardCollector(driver)
    result = {
        "mode": "READ_ONLY",
        "adb_port": args.adb_port,
        "captures": [],
    }
    initial_capture = _capture(output, "initial-state")
    result["captures"].append(initial_capture)
    result["initial_page"] = initial_capture.get("page_type", "unknown")
    home_navigation_ok = True
    if (
        isinstance(guard, ReadOnlyActionGuard)
        and result["initial_page"] not in {"home", "hud"}
    ):
        home_navigation_ok = bool(driver.go_home(attempt_limit=45))
    if not home_navigation_ok or not _return_home_safely(driver, attempt_limit=8):
        blocked_capture = _capture(output, "home-blocked")
        result["captures"].append(blocked_capture)
        blocked_page = blocked_capture.get("page_type", "unknown")
        blocked_reason = {
            "checkin_overlay": "checkin_overlay_observed_no_click",
            "announcement_overlay": "announcement_overlay_observed_no_click",
            "unknown_overlay": "unknown_overlay_observed_no_click",
        }.get(blocked_page, "cannot_reach_game_home_safely")
        result.update({
            "acceptance_status": "BLOCKED",
            "blocked_stage": "home_navigation",
            "blocked_reason": blocked_reason,
            "blocked_page": blocked_page,
        })
        result.update(policy_report(guard, policy_canary_results=policy_canaries))
        result["prohibited_actions_invoked"] = [
            entry["action_key"] for entry in result["actual_blocked_production_actions"]
        ]
        return result
    result["captures"].append(_capture(output, "home-before"))

    result["resident_activity"] = {"success": False, "scope": "not_run"}
    result["fatigue_observation"] = None
    # Unit tests use a small journal stub; device-only observations must remain
    # excluded from deterministic test execution.
    if isinstance(guard, ReadOnlyActionGuard):
        resident = ResidentActivityAutomation()
        result["resident_activity"] = {
            "success": bool(resident.driver.go_home()),
            "scope": "go_home_readonly_chain",
        }
        if _return_home_safely(driver, attempt_limit=8) and _open_fatigue_panel():
            result["fatigue_observation"] = _jsonable(observe_fatigue_frame())
            result["captures"].append(_capture(output, "fatigue-observation"))
            result["fatigue_home_returned"] = _return_home_safely(driver, attempt_limit=8)

    buy = exchange_navigation.open_exchange_action(
        exchange_navigation.ExchangeAction.BUY, read_only=True
    )
    result["buy_navigation"] = _jsonable(buy)
    if not buy.success:
        result.update({
            "acceptance_status": "BLOCKED",
            "blocked_stage": "buy_navigation",
            "blocked_reason": buy.reason or "buy_navigation_failed",
        })
        result.update(policy_report(guard, policy_canary_results=policy_canaries))
        result["prohibited_actions_invoked"] = [
            entry["action_key"] for entry in result["actual_blocked_production_actions"]
        ]
        return result
    if buy.success:
        result["buy_strength"] = _jsonable(read_strength())
        result["captures"].append(_capture(output, "exchange-buy"))
        driver.tap(
            (82, 36), action_key="page_back",
            page_id="exchange_buy", anchor_key="top_left_back",
        )
    else:
        driver.go_home()

    sell = exchange_navigation.open_exchange_action(
        exchange_navigation.ExchangeAction.SELL, read_only=True
    )
    result["sell_navigation"] = _jsonable(sell)
    if not sell.success:
        result.update({
            "acceptance_status": "BLOCKED",
            "blocked_stage": "sell_navigation",
            "blocked_reason": sell.reason or "sell_navigation_failed",
        })
        result.update(policy_report(guard, policy_canary_results=policy_canaries))
        result["prohibited_actions_invoked"] = [
            entry["action_key"] for entry in result["actual_blocked_production_actions"]
        ]
        return result
    if sell.success:
        result["captures"].append(_capture(output, "exchange-sell"))
        driver.tap(
            (82, 36), action_key="page_back",
            page_id="exchange_sell", anchor_key="top_left_back",
        )
    else:
        driver.go_home()

    if not args.navigation_only:
        station = get_station()
        result["station"] = station
        result["rest_area_available"] = rest_area_availability(station) if station else None
        result["daily_activity"] = _jsonable(collector.observe_daily_activity())
        result["captures"].append(_capture(output, "daily-observation"))
        driver.go_home()
        result["manual"] = _jsonable(collector.observe_travel_manual())
        result["captures"].append(_capture(output, "manual-observation"))
        driver.go_home()

    result["home_returned"] = _return_home_safely(driver, attempt_limit=8)
    if not result["home_returned"]:
        result.update({
            "acceptance_status": "BLOCKED",
            "blocked_stage": "safe_return_home",
            "blocked_reason": "cannot_reach_home_safely",
        })
        result.update(policy_report(guard, policy_canary_results=policy_canaries))
        result["prohibited_actions_invoked"] = [
            entry["action_key"] for entry in result["actual_blocked_production_actions"]
        ]
        return result

    result.update(policy_report(guard, policy_canary_results=policy_canaries))
    result["acceptance_status"] = "PASS"
    result["prohibited_actions_invoked"] = [
        entry["action_key"] for entry in result["actual_blocked_production_actions"]
    ]
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--adb-port", type=int, default=16384)
    parser.add_argument("--output", required=True)
    parser.add_argument("--navigation-only", action="store_true")
    args = parser.parse_args()
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    if not connect_adb(args.adb_port):
        raise RuntimeError(f"cannot connect instance-0 ADB port {args.adb_port}")

    # Raw OCR is private evidence and must not be echoed to terminal logs.
    logger.disable("core.image.ocr")
    observer = PageObserver(_trusted_observation)
    guard = _create_production_read_only_safety_session(
        observer, resolver=AnchorResolver()
    )
    policy_canaries = run_policy_canaries(guard)
    try:
        with installed_read_only_guard(guard):
            result = _run_probe(
                args, output, guard, policy_canaries=policy_canaries
            )
        result.setdefault("acceptance_status", "PASS")
    except Exception as error:
        result = {
            "mode": "READ_ONLY",
            "adb_port": args.adb_port,
            "acceptance_status": "BLOCKED",
            "blocked_reason": f"{type(error).__name__}: {error}",
            "captures": [],
            **policy_report(guard, policy_canary_results=policy_canaries),
        }
    if result.get("acceptance_status") == "PASS":
        try:
            live_result = build_live_validation_result(
                result.get("journal", ()),
                instance_id="instance-0",
                session_id=f"guard:{id(guard)}:{datetime.now().astimezone().isoformat()}",
            )
            (output / "LIVE-VALIDATION-RESULT.json").write_text(
                json.dumps(live_result, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            (output / "LIVE-VALIDATION-ADDENDUM.md").write_text(
                render_live_validation_addendum(live_result), encoding="utf-8"
            )
            result["machine_readable_live_result"] = "LIVE-VALIDATION-RESULT.json"
            result["generated_live_addendum"] = "LIVE-VALIDATION-ADDENDUM.md"
        except Exception as error:
            result["acceptance_status"] = "BLOCKED"
            result["blocked_reason"] = (
                f"shareable_live_evidence_failed:{type(error).__name__}:{error}"
            )
    live_entries = [asdict(entry) for entry in guard.journal]
    irreversible_actions = sum(
        1 for entry in live_entries
        if entry.get("action_key") in guard.BLOCKED_ACTIONS
        and entry.get("side_effect_occurred")
    )
    overall = str(result.get("acceptance_status", "UNKNOWN")).upper()
    exact_live_result = {
        "correlation_id": datetime.now().astimezone().strftime("READONLY-%Y%m%d-%H%M%S"),
        "instance": "0",
        "backend_generation": (
            live_entries[-1].get("backend_generation") if live_entries else None
        ),
        "status": overall if overall in {"PASS", "BLOCKED", "UNKNOWN", "FAILED"} else "UNKNOWN",
        "reason": result.get("blocked_reason", "validation_completed"),
        "scenarios": {
            "HOME": (
                "PASS" if any(
                    capture.get("page_type") in {"home", "hud"}
                    for capture in result.get("captures", ())
                ) else "BLOCKED"
            ),
            "Resident Activity": (
                "PASS" if result.get("resident_activity", {}).get("success") else "BLOCKED"
            ),
            "BUY": "PASS" if result.get("buy_navigation", {}).get("success") else "BLOCKED",
            "SELL": "PASS" if result.get("sell_navigation", {}).get("success") else "BLOCKED",
            "fatigue": (
                str(result.get("fatigue_observation", {}).get("status", "BLOCKED"))
                if result.get("fatigue_observation") else "BLOCKED"
            ),
            "daily": (
                "PASS" if result.get("daily_activity", {}).get("confidence") == "HIGH"
                else "UNKNOWN" if result.get("daily_activity") else "BLOCKED"
            ),
            "manual": "PASS" if result.get("manual") else "BLOCKED",
        },
        "irreversible_actions": irreversible_actions,
    }
    (output / "LIVE_VALIDATION_RESULT.json").write_text(
        json.dumps(exact_live_result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (output / "read-only-result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({
        "output": output.name,
        "acceptance_status": result.get("acceptance_status", "UNKNOWN"),
        "buy_success": bool(result.get("buy_navigation", {}).get("success")),
        "sell_success": bool(result.get("sell_navigation", {}).get("success")),
        "daily_known": bool(result.get("daily_activity")),
        "manual_known": bool(result.get("manual")),
        "blocked_actions": result["blocked_actions"],
        "journal_entries": len(result["journal"]),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
