"""Instance-0 evidence probe protected by a runtime read-only action guard."""

from __future__ import annotations

import argparse
import hashlib
import inspect
import json
import re
import sys
import time
from dataclasses import asdict, is_dataclass
from datetime import datetime, timedelta
from pathlib import Path

import cv2 as cv
from loguru import logger


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from auto import exchange_navigation  # noqa: E402
from auto.module.strength import read_strength  # noqa: E402
from auto.reward_collection import RewardCollector, RewardDriver  # noqa: E402
from core.control.control import (  # noqa: E402
    TrustedControlInputExecutor,
    connect_adb,
    current_display_geometry,
    screenshot,
)
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
    ReadOnlyPermitIssuer,
    installed_read_only_guard,
)
from core.services.station_facilities import rest_area_availability  # noqa: E402


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
    frame = screenshot()
    image_path = output / f"{name}.png"
    cv.imwrite(str(image_path), frame.image)
    payload = {
        "name": name,
        "captured_at": datetime.now().astimezone().isoformat(timespec="milliseconds"),
        "image": image_path.name,
        "sha256": hashlib.sha256(image_path.read_bytes()).hexdigest(),
        "shape": list(frame.image.shape),
        "ocr": _jsonable(frame.ocr()),
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
    if any(marker in joined for marker in ("我要买", "我要卖")):
        return "exchange", ["exchange_menu"]
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


def _trusted_observation() -> PageObservation:
    """Build only safety facts from a fresh frame; never persist raw OCR here."""

    frame = screenshot()
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
    if page_type in {"home", "daily_activity", "manual_tasks", "manual_track", "exchange_buy", "exchange_sell"}:
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
        if text_value == "任务列表":
            anchors.append(ObservedAnchor("manual_tasks_tab", text_value, bounds))
        if text_value == "环游手册":
            anchors.append(ObservedAnchor("manual_track_tab", text_value, bounds))
        if "取消" in text_value:
            anchors.append(ObservedAnchor("cancel", text_value, bounds))
        match = re.search(r"(\d+)\s*/\s*(\d+)", text_value)
        if match and bounds[1] < 100 and 500 <= int(match.group(2)) <= 2000:
            anchors.append(ObservedAnchor("fatigue_value", "fatigue_ratio", bounds))

    screenshot_hash = hashlib.sha256(frame.image.tobytes()).hexdigest()
    observation_id = hashlib.sha256(
        f"{screenshot_hash}|{page_type}|{'|'.join(sorted(markers))}".encode("utf-8")
    ).hexdigest()[:24]
    return PageObservation(
        observation_id=observation_id,
        screenshot_hash=screenshot_hash,
        page_type=page_type,
        markers=tuple(markers),
        anchors=tuple(anchors),
        captured_at=datetime.now().astimezone(),
        display_geometry=geometry,
        static_regions=tuple(static_regions),
    )


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
        observation_id="canary-daily",
        screenshot_hash="c" * 64,
        page_type="daily_activity",
        markers=("daily_activity",),
        anchors=(
            ObservedAnchor("top_left_back", "返回", (20, 10, 130, 85)),
            ObservedAnchor("daily_content", "任务列表", (150, 180, 1180, 650)),
        ),
        captured_at=captured_at,
    )
    detached_issuer = ReadOnlyPermitIssuer(
        PageObserver(lambda: daily), AnchorResolver(), now=lambda: captured_at
    )
    detached_guard = ReadOnlyActionGuard(
        lambda _point: None, permit_issuer=detached_issuer, now=lambda: captured_at
    )
    detached_guard.authorize_coordinate(
        (1000, 620),
        intent=ActionIntent("daily_horizontal_scroll", "daily_content", "canary-kind"),
    )
    kind_entry = asdict(detached_guard.journal[-1])
    kind_entry["canary"] = "scroll_policy_via_coordinate_api"
    results.append(kind_entry)

    captures = iter((daily, daily, PageObservation(
        "canary-unknown", "d" * 64, "unknown", ("unknown",), (), captured_at,
    )))
    post_issuer = ReadOnlyPermitIssuer(
        PageObserver(lambda: next(captures)), AnchorResolver(), now=lambda: captured_at
    )
    post_guard = ReadOnlyActionGuard(
        lambda _point: None, permit_issuer=post_issuer, now=lambda: captured_at
    )
    post_guard.authorize_coordinate(
        (50, 40), intent=ActionIntent("reward_back", "top_left_back", "canary-post")
    )
    post_entry = asdict(post_guard.journal[-1])
    post_entry["canary"] = "postcondition_failure_stops"
    results.append(post_entry)

    def failing_executor(_point):
        raise RuntimeError("canary device write failed")

    failure_issuer = ReadOnlyPermitIssuer(
        PageObserver(lambda: daily), AnchorResolver(), now=lambda: captured_at
    )
    failure_guard = ReadOnlyActionGuard(
        failing_executor, permit_issuer=failure_issuer, now=lambda: captured_at
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
    issuer = _live_guard.permit_issuer
    if issuer is None:
        return results
    try:
        observation = issuer.observer.observe()
        forged = ReadOnlyPermit("CANARY-FORGED-NOT-REGISTRY-ISSUED")
        forged_guard = ReadOnlyActionGuard(lambda _point: None, permit_issuer=issuer)
        forged_guard.authorize_coordinate((1000, 650), permit=forged)
        entry = asdict(forged_guard.journal[-1])
        entry["canary"] = "directly_constructed_permit"
        results.append(entry)

        policy_by_page = {
            "home": ("page_back", "top_left_back", (82, 36)),
            "daily_activity": ("reward_back", "top_left_back", (82, 36)),
            "manual_tasks": ("reward_back", "top_left_back", (82, 36)),
            "manual_track": ("reward_back", "top_left_back", (82, 36)),
            "exchange_buy": ("page_back", "top_left_back", (82, 36)),
            "exchange_sell": ("page_back", "top_left_back", (82, 36)),
            "login": ("enter_game", "enter_game", (640, 560)),
        }
        candidate = policy_by_page.get(observation.page_type)
        if candidate is not None:
            action_key, target, coordinate = candidate
            permit = issuer.issue(
                ActionIntent(action_key, target, "canary-mutation"),
                (coordinate,),
                geometry=observation.display_geometry,
            )
            mutated = ReadOnlyPermit(permit.opaque_token)
            object.__setattr__(mutated, "opaque_token", "CANARY-MUTATED-TOKEN")
            mutation_guard = ReadOnlyActionGuard(lambda _point: None, permit_issuer=issuer)
            mutation_guard.authorize_coordinate(
                coordinate,
                permit=mutated,
                geometry=observation.display_geometry,
            )
            entry = asdict(mutation_guard.journal[-1])
            entry["canary"] = "mutated_permit_fields"
            results.append(entry)
    except Exception as error:
        results.append({
            "canary": "permit_authenticity_setup",
            "action_key": "permit_authenticity_canary",
            "allowed": False,
            "reason": f"canary_setup_blocked:{type(error).__name__}",
        })
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
    if not driver.go_home(attempt_limit=8):
        raise RuntimeError("cannot reach game home safely")
    result["captures"].append(_capture(output, "home-before"))

    buy = exchange_navigation.open_exchange_action(
        exchange_navigation.ExchangeAction.BUY, read_only=True
    )
    result["buy_navigation"] = _jsonable(buy)
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

    result.update(policy_report(guard, policy_canary_results=policy_canaries))
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
    issuer = ReadOnlyPermitIssuer(observer, AnchorResolver())
    guard = ReadOnlyActionGuard(
        TrustedControlInputExecutor(), permit_issuer=issuer
    )
    policy_canaries = run_policy_canaries(guard)
    try:
        with installed_read_only_guard(guard):
            result = _run_probe(
                args, output, guard, policy_canaries=policy_canaries
            )
        result["acceptance_status"] = "PASS"
    except Exception as error:
        result = {
            "mode": "READ_ONLY",
            "adb_port": args.adb_port,
            "acceptance_status": "BLOCKED",
            "blocked_reason": f"{type(error).__name__}: {error}",
            "captures": [],
            **policy_report(guard, policy_canary_results=policy_canaries),
        }
    (output / "read-only-result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({
        "output": str(output),
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
