"""Side-effect-free diagnostic entry point for planner acceptance checks."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from datetime import datetime
from enum import Enum

from core.services.daily_rewards import DailyProgressSnapshot, decide_reward_run
from core.services.fatigue_planner import FatigueSnapshot, plan_fatigue_recovery
from core.services.server_calendar import SERVER_CLOCK
from core.services.trade_ledger import load_trade_week_state


def _json_value(value):
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat(timespec="seconds")
    if isinstance(value, (tuple, set, frozenset, list)):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    return value


def simulated_report() -> dict:
    now = SERVER_CLOCK.server_now()
    reward = decide_reward_run(
        DailyProgressSnapshot(
            SERVER_CLOCK.server_day_id(now),
            300,
            600,
            "SIMULATED",
            "HIGH",
            0,
            0,
            5,
            2,
            0,
            0,
            now,
        ),
        now=now,
    )
    fatigue = plan_fatigue_recovery(
        FatigueSnapshot(
            SERVER_CLOCK.server_day_id(now),
            now,
            44,
            816,
            "SIMULATED_CITY",
            "SIMULATED_STATION",
            frozenset({"REST_AREA"}),
            0,
            6,
            50,
            ("FREE", "IRON"),
            3,
            72,
            None,
            None,
            "HIGH",
        )
    )
    trade = load_trade_week_state(now=now)
    return {
        "mode": "SIMULATED_DRY_RUN",
        "side_effects": "none",
        "reward": reward.to_dict(),
        "fatigue": _json_value(asdict(fatigue)),
        "trade": _json_value(asdict(trade)),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Print planner decisions without game input")
    parser.parse_args()
    print(json.dumps(simulated_report(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
