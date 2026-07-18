"""Instance-0 evidence probe protected by a runtime read-only action guard."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from dataclasses import asdict, is_dataclass
from datetime import datetime
from pathlib import Path

import cv2 as cv
from loguru import logger


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from auto import exchange_navigation  # noqa: E402
from auto.module.strength import read_strength  # noqa: E402
from auto.reward_collection import RewardCollector, RewardDriver  # noqa: E402
from core.control.control import connect_adb, screenshot  # noqa: E402
from core.preset import get_station  # noqa: E402
from core.services.read_only_policy import (  # noqa: E402
    AnchorResolver,
    ObservedAnchor,
    PageObservation,
    PageObserver,
    ReadOnlyActionGuard,
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


def _trusted_observation() -> PageObservation:
    """Build only safety facts from a fresh frame; never persist raw OCR here."""

    frame = screenshot()
    items = list(frame.ocr())
    texts = [str(item.get("text", "")).replace(" ", "") for item in items]
    joined = "|".join(texts)
    markers: list[str] = []
    anchors: list[ObservedAnchor] = []

    if any(marker in joined for marker in ("注销", "退出登录", "账号设置")):
        markers.append("account_logout")
    if "每日活跃" in joined or ("完成进度" in joined and "活跃度" in joined):
        page_type = "daily_activity"
        markers.append("daily_activity")
        anchors.append(ObservedAnchor("daily_content", "daily_content", (150, 180, 1180, 650)))
    elif "环游手册" in joined and "任务列表" in joined:
        page_type = "manual_tasks"
        markers.append("manual_tasks")
        anchors.append(ObservedAnchor("manual_content", "manual_content", (150, 100, 1180, 650)))
    elif "环游手册" in joined:
        page_type = "manual_track"
        markers.append("manual_track")
        anchors.append(ObservedAnchor("manual_content", "manual_content", (150, 100, 1180, 650)))
    elif any(marker in joined for marker in ("我要买", "我要卖")):
        page_type = "exchange"
        markers.append("exchange_menu")
    elif any(marker in joined for marker in ("预计买入", "买入总价")):
        page_type = "exchange_buy"
        markers.append("exchange_buy")
    elif any(marker in joined for marker in ("预计卖出", "卖出总价")):
        page_type = "exchange_sell"
        markers.append("exchange_sell")
    elif any(marker in joined for marker in ("访问城市", "启程", "作战终端")):
        page_type = "home"
        markers.append("top_level_hud")
    elif any(marker in joined for marker in ("进入游戏", "启动游戏")):
        page_type = "login"
        markers.append("login")
    elif "取消" in joined:
        page_type = "clarity_dialog"
        markers.append("safe_cancel_dialog")
    else:
        page_type = "unknown"
        markers.append("unknown")

    # Fixed top-left back is usable only on a page that was independently
    # classified above; UNKNOWN is rejected by every policy spec.
    anchors.append(ObservedAnchor("top_left_back", "top_left_back", (20, 10, 130, 85)))
    if page_type == "home":
        anchors.extend((
            ObservedAnchor("daily_shortcut", "daily_shortcut", (998, 32, 1098, 132)),
            ObservedAnchor("manual_shortcut", "manual_shortcut", (1082, 32, 1182, 132)),
        ))
    if page_type == "login":
        anchors.append(ObservedAnchor("enter_game", "enter_game", (560, 500, 720, 620)))
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
    )


def _cached_trusted_observer(window_seconds: float = 0.15):
    """Keep the issuer's one action observation stable through authorization."""

    cache: dict[str, object] = {}

    def observe() -> PageObservation:
        now = time.monotonic()
        value = cache.get("observation")
        if isinstance(value, PageObservation) and now <= float(cache.get("expires", 0.0)):
            return value
        value = _trusted_observation()
        cache["observation"] = value
        cache["expires"] = time.monotonic() + max(0.01, float(window_seconds))
        return value

    return observe


def run_policy_canaries(_live_guard: ReadOnlyActionGuard) -> list[dict]:
    """Exercise a detached policy guard so canaries never pollute live evidence."""

    canary_guard = ReadOnlyActionGuard()
    canary_guard.tap("transaction_buy", (1000, 650), "exchange_buy")
    canary_guard.tap("reward_claim", (1000, 620), "daily_reward")
    canary_guard.tap("fatigue_confirm", (900, 600), "rest_area")
    return [asdict(entry) for entry in canary_guard.journal]


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
    driver.go_home()

    sell = exchange_navigation.open_exchange_action(
        exchange_navigation.ExchangeAction.SELL, read_only=True
    )
    result["sell_navigation"] = _jsonable(sell)
    if sell.success:
        result["captures"].append(_capture(output, "exchange-sell"))
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
    observer = PageObserver(_cached_trusted_observer())
    issuer = ReadOnlyPermitIssuer(observer, AnchorResolver())
    guard = ReadOnlyActionGuard(permit_issuer=issuer)
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
