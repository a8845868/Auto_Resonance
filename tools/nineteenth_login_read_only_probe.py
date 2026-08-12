"""Observe instance-0 login/session recovery without sending any input."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

from loguru import logger


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.control.control import connect_adb, screenshot  # noqa: E402
from core.services.login_state_resolver import (  # noqa: E402
    LoginState,
    LoginStateResolver,
    classify_login_frame,
)


def run(
    output: Path,
    *,
    adb_port: int = 16384,
    timeout: float = 60.0,
) -> dict[str, object]:
    output.mkdir(parents=True, exist_ok=True)
    if not connect_adb(adb_port):
        raise RuntimeError("instance_0_adb_unavailable")

    logger.disable("core.image.ocr")
    before = classify_login_frame(screenshot())
    correlation_id = datetime.now().astimezone().strftime(
        "LOGIN-%Y%m%d-%H%M%S"
    )
    resolution = LoginStateResolver(
        frame_provider=screenshot,
        timeout=timeout,
        max_attempts=max(1, int(timeout)),
        poll_interval=1.0,
        correlation_id=correlation_id,
        initial_observation=before,
    ).resolve()
    payload = resolution.to_dict()
    payload.update(
        {
            "scenario": "login_state_resolution",
            "instance": "0",
            "state_before": before.state.value,
            "state_after": resolution.state.value,
            "irreversible_actions": 0,
        }
    )
    return payload


def _blocked_result(reason: str) -> dict[str, object]:
    return {
        "scenario": "login_state_resolution",
        "instance": "0",
        "state_before": LoginState.UNKNOWN.value,
        "state_after": LoginState.UNKNOWN.value,
        "status": "BLOCKED",
        "reason": reason,
        "path": [LoginState.UNKNOWN.value],
        "trace": [],
        "irreversible_actions": 0,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--adb-port", type=int, default=16384)
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    output = Path(args.output).resolve()
    try:
        result = run(
            output,
            adb_port=args.adb_port,
            timeout=max(0.0, args.timeout),
        )
    except Exception as error:  # noqa: BLE001 - sanitize live runtime failure
        result = _blocked_result(f"runtime_error:{type(error).__name__}")

    output.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    (output / "LOGIN_VALIDATION_RESULT.json").write_text(
        payload,
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "output": output.name,
                "state_before": result["state_before"],
                "state_after": result["state_after"],
                "status": result["status"],
                "reason": result["reason"],
                "irreversible_actions": result["irreversible_actions"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
