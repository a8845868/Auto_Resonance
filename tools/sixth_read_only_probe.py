"""Instance-0 evidence probe protected by a runtime read-only action guard."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
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
    ReadOnlyActionGuard,
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


def _page_context() -> str:
    return " ".join(str(item.get("text", "")) for item in screenshot().ocr())


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
    guard = ReadOnlyActionGuard(context_provider=_page_context)
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
