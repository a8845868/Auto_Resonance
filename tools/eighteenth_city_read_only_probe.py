"""MuMu instance-0 city/exchange probe guarded against irreversible input."""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from loguru import logger


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.control.control import (  # noqa: E402
    _create_production_read_only_safety_session,
    connect_adb,
    screenshot,
)
from core.services.city_navigation import (  # noqa: E402
    CityNavigationAdapter,
    CityNavigationState,
    ExchangeEntryAdapter,
    observe_city_frame,
)
from core.services.read_only_policy import (  # noqa: E402
    ActionIntent,
    AnchorResolver,
    PageObserver,
    installed_read_only_guard,
)
from tools.sixth_read_only_probe import _trusted_observation  # noqa: E402


def _scenario(
    scenario: str,
    before: str,
    action: str,
    after: str,
    status: str,
    reason: str,
) -> dict[str, object]:
    return {
        "scenario": scenario,
        "before": before,
        "action": action,
        "after": after,
        "status": status,
        "reason": reason,
        "irreversible_actions": 0,
    }


def _trace(result) -> list[dict[str, object]]:
    return [asdict(event) for event in result.trace]


def _wait_for_state(
    target: CityNavigationState,
    *,
    deadline: float,
    max_attempts: int = 10,
) -> tuple[bool, object | None]:
    last = None
    for _ in range(max(1, int(max_attempts))):
        if time.monotonic() >= deadline:
            break
        time.sleep(min(0.5, max(0.0, deadline - time.monotonic())))
        if time.monotonic() >= deadline:
            break
        last = observe_city_frame(screenshot())
        if last.state is target:
            return True, last
        if last.state is CityNavigationState.UNKNOWN and last.text_count == 0:
            continue
        if last.state is CityNavigationState.NPC_DIALOG and target is CityNavigationState.EXCHANGE_MENU:
            continue
        return False, last
    return False, last


def _back_to_exchange_menu(guard, correlation_id: str) -> tuple[bool, dict[str, object]]:
    before = observe_city_frame(screenshot())
    allowed = guard.authorize_coordinate(
        (82, 36),
        intent=ActionIntent(
            "page_back", "top_left_back", f"{correlation_id}:back-to-exchange-menu",
        ),
    )
    if not allowed:
        return False, {
            "correlation_id": correlation_id,
            "page_before": before.state.value,
            "action": "page_back",
            "guard_result": "DENIED",
            "page_after": "UNKNOWN",
            "screenshot_hash_before": before.screenshot_hash,
            "screenshot_hash_after": "",
            "postcondition": "EXCHANGE_MENU",
            "status": "BLOCKED",
            "reason": "guard_denied_page_back",
        }
    ok, after = _wait_for_state(
        CityNavigationState.EXCHANGE_MENU,
        deadline=time.monotonic() + 10.0,
    )
    return ok, {
        "correlation_id": correlation_id,
        "page_before": before.state.value,
        "action": "page_back",
        "guard_result": "ALLOWED",
        "page_after": after.state.value if after is not None else "TIMEOUT",
        "screenshot_hash_before": before.screenshot_hash,
        "screenshot_hash_after": after.screenshot_hash if after is not None else "",
        "postcondition": "EXCHANGE_MENU",
        "status": "PASS" if ok else "BLOCKED",
        "reason": "exchange_menu_verified" if ok else "exchange_menu_not_reached",
    }


def _result_skeleton(correlation_id: str, page_before: str) -> dict[str, object]:
    return {
        "correlation_id": correlation_id,
        "instance": "0",
        "page_before": page_before,
        "status": "BLOCKED",
        "reason": "home_ready_unavailable",
        "scenarios": [
            _scenario("HOME_READY", page_before, "OBSERVE", page_before, "BLOCKED", "home_ready_unavailable"),
            _scenario("HOME_TO_CITY", "HOME_READY", "enter_city", "UNKNOWN", "BLOCKED", "not_run"),
            _scenario("CITY_TO_EXCHANGE_MENU", "CITY_DETAIL", "enter_exchange", "UNKNOWN", "BLOCKED", "not_run"),
            _scenario("EXCHANGE_MENU_TO_BUY", "EXCHANGE_MENU", "open_exchange_buy", "UNKNOWN", "BLOCKED", "not_run"),
            _scenario("EXCHANGE_MENU_TO_SELL", "EXCHANGE_MENU", "open_exchange_sell", "UNKNOWN", "BLOCKED", "not_run"),
        ],
        "trace": [],
        "irreversible_actions": 0,
    }


def run(output: Path, *, adb_port: int = 16384) -> dict[str, object]:
    output.mkdir(parents=True, exist_ok=True)
    if not connect_adb(adb_port):
        raise RuntimeError(f"cannot connect instance-0 ADB port {adb_port}")
    logger.disable("core.image.ocr")
    initial = _trusted_observation().as_observation()
    correlation_id = datetime.now().astimezone().strftime("CITYNAV-%Y%m%d-%H%M%S")
    result = _result_skeleton(correlation_id, initial.page_type.upper())
    if initial.page_type not in {"home", "hud"}:
        return result

    result["scenarios"][0] = _scenario(
        "HOME_READY", initial.page_type.upper(), "OBSERVE", "HOME_READY",
        "PASS", "trusted_home_observation",
    )
    observer = PageObserver(_trusted_observation)
    guard = _create_production_read_only_safety_session(
        observer, resolver=AnchorResolver()
    )
    trace: list[dict[str, object]] = []
    with installed_read_only_guard(guard):
        city = CityNavigationAdapter(
            frame_provider=screenshot,
            tap=lambda point, *, intent: guard.authorize_coordinate(point, intent=intent),
            timeout=30.0,
            max_attempts=10,
            stall_frames=5,
            correlation_id=correlation_id,
        ).enter_city()
        trace.extend(_trace(city))
        result["scenarios"][1] = _scenario(
            "HOME_TO_CITY", "HOME_READY", "enter_city", city.state.value,
            city.status, city.reason,
        )
        if city.status != "PASS":
            result["reason"] = city.reason
        else:
            exchange = ExchangeEntryAdapter(
                frame_provider=screenshot,
                tap=lambda point, *, intent: guard.authorize_coordinate(point, intent=intent),
                timeout=30.0,
                max_attempts=10,
                stall_frames=5,
                correlation_id=correlation_id,
            )
            menu = exchange.open_menu()
            trace.extend(_trace(menu))
            result["scenarios"][2] = _scenario(
                "CITY_TO_EXCHANGE_MENU", city.state.value, "enter_exchange",
                menu.state.value, menu.status, menu.reason,
            )
            if menu.status != "PASS":
                result["reason"] = menu.reason
            else:
                buy = exchange.open_action("BUY")
                trace.extend(_trace(buy))
                result["scenarios"][3] = _scenario(
                    "EXCHANGE_MENU_TO_BUY", "EXCHANGE_MENU", "open_exchange_buy",
                    buy.state.value, buy.status, buy.reason,
                )
                if buy.status != "PASS":
                    result["reason"] = buy.reason
                else:
                    back_ok, back_trace = _back_to_exchange_menu(guard, correlation_id)
                    trace.append(back_trace)
                    if back_ok:
                        sell = exchange.open_action("SELL")
                        trace.extend(_trace(sell))
                        result["scenarios"][4] = _scenario(
                            "EXCHANGE_MENU_TO_SELL", "EXCHANGE_MENU", "open_exchange_sell",
                            sell.state.value, sell.status, sell.reason,
                        )
                        result["reason"] = sell.reason
                        if sell.status == "PASS":
                            result["status"] = "PASS"
                            result["reason"] = "BUY_PAGE_READONLY_PASS;SELL_PAGE_READONLY_PASS"
                    else:
                        result["scenarios"][4] = _scenario(
                            "EXCHANGE_MENU_TO_SELL", "EXCHANGE_BUY", "page_back_then_sell",
                            "UNKNOWN", "BLOCKED", "exchange_menu_not_restored",
                        )
                        result["reason"] = "exchange_menu_not_restored"

    irreversible = sum(
        1 for entry in guard.journal
        if entry.action_key in guard.BLOCKED_ACTIONS and entry.side_effect_occurred
    )
    result["irreversible_actions"] = irreversible
    result["trace"] = trace
    for scenario in result["scenarios"]:
        scenario["irreversible_actions"] = irreversible
    if irreversible:
        result["status"] = "FAILED"
        result["reason"] = "irreversible_action_detected"
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--adb-port", type=int, default=16384)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    output = Path(args.output).resolve()
    try:
        result = run(output, adb_port=args.adb_port)
    except Exception as error:
        result = _result_skeleton(
            datetime.now().astimezone().strftime("CITYNAV-%Y%m%d-%H%M%S"),
            "UNKNOWN",
        )
        result["status"] = "BLOCKED"
        result["reason"] = f"runtime_error:{type(error).__name__}"
    payload = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    (output / "LIVE_CITY_NAV_TRACE.json").write_text(payload, encoding="utf-8")
    (output / "LIVE_CITY_NAV_RESULT.json").write_text(payload, encoding="utf-8")
    print(json.dumps({
        "output": output.name,
        "status": result["status"],
        "reason": result["reason"],
        "irreversible_actions": result["irreversible_actions"],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
