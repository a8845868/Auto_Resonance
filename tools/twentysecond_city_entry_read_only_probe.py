"""Instance-0 HOME to CITY_DETAIL probe through ReadOnlyActionGuard."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

import cv2 as cv
from loguru import logger


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.control.control import (  # noqa: E402
    _create_production_read_only_safety_session,
    connect_adb,
    screenshot,
)
from core.services.city_entry_resolver import (  # noqa: E402
    CityEntryExecution,
    CityEntryResolver,
    CityEntryState,
    observe_city_entry_frame,
)
from core.services.read_only_policy import (  # noqa: E402
    AnchorResolver,
    PageObserver,
    installed_read_only_guard,
)
from tools.sixth_read_only_probe import _trusted_observation  # noqa: E402


def _save_frame(frame: object, output: Path, name: str) -> None:
    image = getattr(frame, "image", None)
    if image is not None:
        cv.imwrite(str(output / f"{name}.png"), image)


def _sanitized_guard_journal(guard) -> list[dict[str, object]]:
    return [
        {
            "correlation_id": entry.correlation_id,
            "action_key": entry.action_key,
            "stage": entry.stage,
            "allowed": entry.allowed,
            "reason": entry.reason,
            "side_effect_occurred": entry.side_effect_occurred,
            "screenshot_hash": entry.screenshot_hash,
            "page_type": entry.page_type,
        }
        for entry in guard.journal
    ]


def run(output: Path, *, adb_port: int = 16384) -> dict[str, object]:
    output.mkdir(parents=True, exist_ok=True)
    if not connect_adb(adb_port):
        raise RuntimeError("instance_0_adb_unavailable")
    logger.disable("core.image.ocr")
    first_frame = screenshot()
    _save_frame(first_frame, output, "city-entry-before")
    before = observe_city_entry_frame(first_frame)
    correlation_id = datetime.now().astimezone().strftime(
        "CITYENTRY-%Y%m%d-%H%M%S"
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
        return CityEntryExecution(
            # The guard can execute a pre-authorized input and then return
            # false when its short postcondition window expires. Preserve the
            # postcondition reason, but do not relabel an executed input as a
            # pre-action denial.
            allowed=bool(allowed or (entry and entry.side_effect_occurred)),
            executed=bool(entry and entry.side_effect_occurred),
            guard_result=entry.reason if entry is not None else "NO_JOURNAL_ENTRY",
        )

    with installed_read_only_guard(guard):
        result = CityEntryResolver(
            frame_provider=screenshot,
            tap=guarded_entry,
            timeout=30.0,
            max_attempts=10,
            stall_frames=5,
            poll_interval=0.5,
            correlation_id=correlation_id,
            initial_observation=before,
        ).enter_city()

    final_capture_error = ""
    try:
        final_frame = screenshot()
        _save_frame(final_frame, output, "city-entry-after")
        after = observe_city_entry_frame(final_frame)
    except Exception as error:  # noqa: BLE001 - preserve journals on disconnect
        after = None
        final_capture_error = f"CAPTURE_FAILED:{type(error).__name__}"
    irreversible_actions = sum(
        1
        for entry in guard.journal
        if entry.action_key in guard.BLOCKED_ACTIONS
        and entry.side_effect_occurred
    )
    payload = result.to_dict()
    payload.update(
        {
            "scenario": "city_entry_live_validation",
            "instance": "0",
            "before": before.state.value,
            "after": after.state.value if after is not None else result.state.value,
            "screenshot_hash_before": before.screenshot_hash,
            "screenshot_hash_after": (
                after.screenshot_hash if after is not None else result.screenshot_hash
            ),
            "final_evidence": list(after.evidence) if after is not None else [],
            "final_capture_error": final_capture_error,
            "guard_journal": _sanitized_guard_journal(guard),
            "irreversible_actions": irreversible_actions,
        }
    )
    if result.status == "PASS" and (
        after is None or after.state is not CityEntryState.CITY_DETAIL
    ):
        payload["status"] = "FAILED"
        payload["reason"] = final_capture_error or "final_capture_not_city_detail"
    if irreversible_actions:
        payload["status"] = "FAILED"
        payload["reason"] = "irreversible_action_detected"
    return payload


def _blocked_result(reason: str) -> dict[str, object]:
    return {
        "scenario": "city_entry_live_validation",
        "instance": "0",
        "before": CityEntryState.UNKNOWN.value,
        "after": CityEntryState.UNKNOWN.value,
        "status": "BLOCKED",
        "reason": reason,
        "action_count": 0,
        "trace": [],
        "irreversible_actions": 0,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--adb-port", type=int, default=16384)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    output = Path(args.output).resolve()
    try:
        result = run(output, adb_port=args.adb_port)
    except Exception as error:  # noqa: BLE001 - sanitized live failure
        result = _blocked_result(f"runtime_error:{type(error).__name__}")

    output.mkdir(parents=True, exist_ok=True)
    trace_payload = {
        "correlation_id": result.get("correlation_id", ""),
        "instance": "0",
        "events": result.get("trace", []),
    }
    (output / "LIVE_CITY_ENTRY_TRACE.json").write_text(
        json.dumps(trace_payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (output / "LIVE_CITY_ENTRY_RESULT.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "output": output.name,
                "before": result["before"],
                "after": result["after"],
                "status": result["status"],
                "reason": result["reason"],
                "action_count": result["action_count"],
                "irreversible_actions": result["irreversible_actions"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
