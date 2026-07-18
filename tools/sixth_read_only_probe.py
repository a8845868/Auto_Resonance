"""Instance-0 audit probe. It never invokes a transaction or reward claim."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import asdict, is_dataclass
from datetime import datetime
from pathlib import Path

import cv2 as cv


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from auto import exchange_navigation  # noqa: E402
from auto.module.strength import read_strength  # noqa: E402
from auto.reward_collection import RewardCollector  # noqa: E402
from core.control.control import connect_adb, screenshot  # noqa: E402
from core.preset import get_station  # noqa: E402
from core.preset.control import go_home  # noqa: E402
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

    result = {
        "mode": "READ_ONLY",
        "adb_port": args.adb_port,
        "prohibited_actions_invoked": [],
        "captures": [],
    }
    if not go_home():
        raise RuntimeError("cannot reach game home safely")
    result["captures"].append(_capture(output, "home-before"))

    buy = exchange_navigation.open_exchange_action(
        exchange_navigation.ExchangeAction.BUY, read_only=True
    )
    result["buy_navigation"] = _jsonable(buy)
    if buy.success:
        result["buy_strength"] = _jsonable(read_strength())
        result["captures"].append(_capture(output, "exchange-buy"))
    go_home()

    sell = exchange_navigation.open_exchange_action(
        exchange_navigation.ExchangeAction.SELL, read_only=True
    )
    result["sell_navigation"] = _jsonable(sell)
    if sell.success:
        result["captures"].append(_capture(output, "exchange-sell"))
    go_home()

    if args.navigation_only:
        (output / "read-only-result.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(json.dumps({
            "output": str(output),
            "buy_success": buy.success,
            "sell_success": sell.success,
            "prohibited_actions_invoked": [],
        }, ensure_ascii=False, indent=2))
        return 0

    station = get_station()
    result["station"] = station
    result["rest_area_available"] = rest_area_availability(station) if station else None

    collector = RewardCollector()
    result["daily_activity"] = _jsonable(collector.observe_daily_activity())
    result["captures"].append(_capture(output, "daily-observation"))
    go_home()
    result["manual"] = _jsonable(collector.observe_travel_manual())
    result["captures"].append(_capture(output, "manual-observation"))
    go_home()

    (output / "read-only-result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({
        "output": str(output),
        "buy_success": buy.success,
        "sell_success": sell.success,
        "daily_known": bool(result["daily_activity"]),
        "manual_known": bool(result["manual"]),
        "prohibited_actions_invoked": [],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
