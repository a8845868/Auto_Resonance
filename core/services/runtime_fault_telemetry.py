"""Privacy-minimized persistent telemetry for known runtime faults."""

from __future__ import annotations

import json
import os
import threading
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path


DEFAULT_RUNTIME_FAULT_PATH = Path("logs/runtime_faults.jsonl")
_FAULT_LOCK = threading.RLock()


@dataclass(frozen=True)
class RuntimeFaultEvent:
    task_id: str
    attempt_id: str
    backend: str
    session_generation: int
    native_return_code: int
    failure_stage: str
    recovery_attempted: bool
    recovery_result: str
    capture_call_index: int = 0
    session_lifecycle_conflict: str = "NOT_PROVEN"
    self_healing_eligible: bool = False
    runtime_diagnostic_event: bool = True
    schema_version: str = "1.0"
    recorded_at: str = ""

    def to_dict(self) -> dict:
        document = asdict(self)
        if not document["recorded_at"]:
            document["recorded_at"] = datetime.now().astimezone().isoformat(
                timespec="seconds"
            )
        return document


def record_runtime_fault(
    event: RuntimeFaultEvent,
    *,
    path: Path = DEFAULT_RUNTIME_FAULT_PATH,
) -> bool:
    """Append only allowlisted fields; never include frames, OCR, or identity."""

    document = event.to_dict()
    path = Path(path)
    temporary_line = json.dumps(
        document,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    with _FAULT_LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as stream:
            stream.write(temporary_line + "\n")
            stream.flush()
            os.fsync(stream.fileno())
    return True


__all__ = [
    "DEFAULT_RUNTIME_FAULT_PATH",
    "RuntimeFaultEvent",
    "record_runtime_fault",
]
