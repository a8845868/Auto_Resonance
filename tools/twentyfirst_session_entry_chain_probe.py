"""Follow up to three existing-session gates through ReadOnlyActionGuard."""

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

from core.control.control import (  # noqa: E402
    _create_production_read_only_safety_session,
    connect_adb,
    screenshot,
)
from core.services.read_only_policy import (  # noqa: E402
    AnchorResolver,
    PageObserver,
    installed_read_only_guard,
)
from core.services.session_entry_chain import EntryChainResolver  # noqa: E402
from core.services.session_entry_resolver import (  # noqa: E402
    SessionEntryExecution,
    SessionEntryState,
    classify_session_entry_frame,
)
from tools.sixth_read_only_probe import _trusted_observation  # noqa: E402


def run(
    output: Path,
    *,
    adb_port: int = 16384,
    timeout: float = 45.0,
    max_entry_steps: int = 3,
) -> dict[str, object]:
    output.mkdir(parents=True, exist_ok=True)
    if not connect_adb(adb_port):
        raise RuntimeError("instance_0_adb_unavailable")
    logger.disable("core.image.ocr")
    before = classify_session_entry_frame(screenshot())
    correlation_id = datetime.now().astimezone().strftime(
        "CHAIN-%Y%m%d-%H%M%S"
    )
    observer = PageObserver(_trusted_observation)
    guard = _create_production_read_only_safety_session(
        observer,
        resolver=AnchorResolver(),
    )

    def guarded_entry(point, *, intent):
        journal_size = len(guard.journal)
        allowed = guard.authorize_coordinate(point, intent=intent)
        entry = guard.journal[-1] if len(guard.journal) > journal_size else None
        return SessionEntryExecution(
            allowed=bool(allowed),
            executed=bool(entry and entry.side_effect_occurred),
            guard_result=entry.reason if entry is not None else "NO_JOURNAL_ENTRY",
        )

    with installed_read_only_guard(guard):
        result = EntryChainResolver(
            frame_provider=screenshot,
            tap=guarded_entry,
            timeout=timeout,
            max_attempts=max(5, int(timeout)),
            max_entry_steps=max_entry_steps,
            poll_interval=1.0,
            correlation_id=correlation_id,
            initial_observation=before,
        ).resolve()

    irreversible_actions = sum(
        1
        for entry in guard.journal
        if entry.action_key in guard.BLOCKED_ACTIONS
        and entry.side_effect_occurred
    )
    payload = result.to_dict()
    payload.update(
        {
            "scenario": "session_entry_chain",
            "instance": "0",
            "state_before": before.state.value,
            "state_after": result.state.value,
            "irreversible_actions": irreversible_actions,
        }
    )
    if irreversible_actions:
        payload["status"] = "FAILED"
        payload["reason"] = "irreversible_action_detected"
    return payload


def _blocked_result(reason: str) -> dict[str, object]:
    return {
        "scenario": "session_entry_chain",
        "instance": "0",
        "state_before": SessionEntryState.UNKNOWN.value,
        "state_after": SessionEntryState.UNKNOWN.value,
        "status": "BLOCKED",
        "reason": reason,
        "path": [SessionEntryState.UNKNOWN.value],
        "trace": [],
        "entry_steps": 0,
        "action_attempts": 0,
        "irreversible_actions": 0,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--adb-port", type=int, default=16384)
    parser.add_argument("--timeout", type=float, default=45.0)
    parser.add_argument("--max-entry-steps", type=int, default=3)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    output = Path(args.output).resolve()
    try:
        result = run(
            output,
            adb_port=args.adb_port,
            timeout=max(0.0, args.timeout),
            max_entry_steps=max(1, min(3, args.max_entry_steps)),
        )
    except Exception as error:  # noqa: BLE001 - sanitize live runtime failure
        result = _blocked_result(f"runtime_error:{type(error).__name__}")

    output.mkdir(parents=True, exist_ok=True)
    (output / "SESSION_ENTRY_CHAIN_RESULT.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
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
                "entry_steps": result["entry_steps"],
                "irreversible_actions": result["irreversible_actions"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
