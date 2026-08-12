"""Read-only NEMU capture continuity preflight; never sends game input."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import core.control.control as control_module
from core.control.nemu import NEMU
from core.control.nemu_capture import NemuCaptureError


def run(output: Path, *, capture_count: int = 5) -> dict[str, object]:
    result: dict[str, object] = {
        "schema_version": "1.0",
        "preflight": "NEMU_CAPTURE_CONTINUITY",
        "status": "BLOCKED",
        "reason": "not_started",
        "backend": "UNKNOWN",
        "instance_index": None,
        "health_capture": {},
        "captures": [],
        "requested_capture_count": max(1, int(capture_count)),
        "completed_capture_count": 0,
        "real_capture_calls_after_connect": 0,
        "real_ui_actions": 0,
        "real_business_actions": 0,
        "ocr_calls": 0,
        "input_fallback_actions": 0,
    }
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    try:
        if not control_module.connect():
            result["reason"] = "backend_connect_failed"
            return result
        backend = control_module.control
        result["backend"] = type(backend).__name__
        if not isinstance(backend, NEMU):
            result["reason"] = "nemu_backend_required"
            return result
        result["instance_index"] = int(getattr(backend.device, "index", -1))
        if result["instance_index"] != 0:
            result["reason"] = "instance_index_mismatch"
            return result
        result["health_capture"] = {
            "capture_call_index": int(
                getattr(backend, "last_successful_capture_call_index", 0)
            ),
            "session_generation": int(
                getattr(backend, "health_capture_session_generation", 0) or 0
            ),
            "native_return_code": getattr(
                backend, "health_capture_return_code", None
            ),
            "frame_created": (
                getattr(backend, "health_capture_return_code", None) == 0
            ),
        }
        captures: list[dict[str, object]] = []
        result["captures"] = captures
        for _ in range(max(1, int(capture_count))):
            try:
                frame = backend.screenshot(
                    failure_stage="CONTINUITY_PREFLIGHT_CAPTURE"
                )
            except NemuCaptureError as error:
                captures.append({
                    "capture_call_index": error.capture_call_index,
                    "session_generation": error.session_generation,
                    "native_return_code": error.native_return_code,
                    "frame_created": False,
                    "frame_sha256": None,
                    "session_lifecycle_conflict": (
                        error.session_lifecycle_conflict
                    ),
                })
                result["reason"] = "native_capture_failed"
                result["real_capture_calls_after_connect"] = len(captures)
                return result
            captures.append({
                "capture_call_index": int(
                    getattr(backend, "native_capture_call_count", 0)
                ),
                "session_generation": int(
                    getattr(backend, "session_generation", 0)
                ),
                "native_return_code": int(
                    getattr(backend, "last_capture_return_code", 0)
                ),
                "frame_created": True,
                "frame_sha256": hashlib.sha256(frame.tobytes()).hexdigest(),
            })
        result["completed_capture_count"] = len(captures)
        result["real_capture_calls_after_connect"] = len(captures)
        result["status"] = "PASS"
        result["reason"] = "bounded_capture_continuity_confirmed"
        return result
    finally:
        try:
            control_module.kill()
        except Exception:
            pass
        (output / "NEMU_CAPTURE_CONTINUITY_RESULT.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--capture-count", type=int, default=5)
    args = parser.parse_args()
    result = run(args.output, capture_count=args.capture_count)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
