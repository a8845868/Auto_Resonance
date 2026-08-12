"""Typed NEMU capture failures and bounded session-recovery receipts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


NEMU_CAPTURE_ERROR_SCHEMA_VERSION = "1.0"


@dataclass(frozen=True)
class CaptureSessionRecoveryResult:
    success: bool
    reason: str
    previous_session_generation: int | None = None
    current_session_generation: int | None = None
    lifecycle_conflict: str = "NOT_PROVEN"
    capture_failure: "NemuCaptureError | None" = None


class NemuCaptureError(RuntimeError):
    """Preserve one rejected native capture call without guessing its meaning."""

    schema_version = NEMU_CAPTURE_ERROR_SCHEMA_VERSION

    def __init__(
        self,
        *,
        native_return_code: int,
        backend: str = "NEMU",
        instance_index: int | None = None,
        instance_handle: int | None = None,
        display_id: int | None = None,
        session_generation: int = 0,
        connect_epoch: str = "",
        capture_call_index: int = 0,
        last_successful_capture_call_index: int = 0,
        capture_width: int = 0,
        capture_height: int = 0,
        thread_id: int = 0,
        failure_stage: str = "RUNTIME_CAPTURE",
        reason_codes: tuple[str, ...] = (
            "native_capture_rejected",
            "native_return_code_unmapped",
        ),
        session_lifecycle_conflict: str = "NOT_PROVEN",
        health_capture_return_code: int | None = None,
        health_capture_session_generation: int | None = None,
        business_capture_return_code: int | None = None,
        business_capture_session_generation: int | None = None,
        disconnect_call_count: int = 0,
        kill_call_count: int = 0,
        last_disconnect_timestamp: str = "",
        last_kill_timestamp: str = "",
    ) -> None:
        self.native_return_code = int(native_return_code)
        self.backend = str(backend)
        self.instance_index = (
            int(instance_index) if instance_index is not None else None
        )
        self.instance_handle = (
            int(instance_handle) if instance_handle is not None else None
        )
        self.display_id = int(display_id) if display_id is not None else None
        self.session_generation = int(session_generation)
        self.connect_epoch = str(connect_epoch)
        self.capture_call_index = int(capture_call_index)
        self.last_successful_capture_call_index = int(
            last_successful_capture_call_index
        )
        self.capture_width = int(capture_width)
        self.capture_height = int(capture_height)
        self.thread_id = int(thread_id)
        self.failure_stage = str(failure_stage)
        self.reason_codes = tuple(dict.fromkeys(map(str, reason_codes)))
        self.session_lifecycle_conflict = str(session_lifecycle_conflict)
        self.health_capture_return_code = health_capture_return_code
        self.health_capture_session_generation = health_capture_session_generation
        self.business_capture_return_code = business_capture_return_code
        self.business_capture_session_generation = business_capture_session_generation
        self.disconnect_call_count = int(disconnect_call_count)
        self.kill_call_count = int(kill_call_count)
        self.last_disconnect_timestamp = str(last_disconnect_timestamp)
        self.last_kill_timestamp = str(last_kill_timestamp)
        self.capture_native_status = "FAILED"
        self.frame_created = False
        self.frame_sha256 = None
        super().__init__(f"nemu_capture_failed:{self.native_return_code}")

    def with_failure_stage(self, failure_stage: str) -> "NemuCaptureError":
        document = self.to_dict()
        document.pop("schema_version", None)
        document.pop("capture_native_status", None)
        document.pop("frame_created", None)
        document.pop("frame_sha256", None)
        document["failure_stage"] = str(failure_stage)
        document["reason_codes"] = tuple(document["reason_codes"])
        return type(self)(**document)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "native_return_code": self.native_return_code,
            "backend": self.backend,
            "instance_index": self.instance_index,
            "instance_handle": self.instance_handle,
            "display_id": self.display_id,
            "session_generation": self.session_generation,
            "connect_epoch": self.connect_epoch,
            "capture_call_index": self.capture_call_index,
            "last_successful_capture_call_index": (
                self.last_successful_capture_call_index
            ),
            "capture_width": self.capture_width,
            "capture_height": self.capture_height,
            "thread_id": self.thread_id,
            "failure_stage": self.failure_stage,
            "reason_codes": list(self.reason_codes),
            "session_lifecycle_conflict": self.session_lifecycle_conflict,
            "health_capture_return_code": self.health_capture_return_code,
            "health_capture_session_generation": (
                self.health_capture_session_generation
            ),
            "business_capture_return_code": self.business_capture_return_code,
            "business_capture_session_generation": (
                self.business_capture_session_generation
            ),
            "disconnect_call_count": self.disconnect_call_count,
            "kill_call_count": self.kill_call_count,
            "last_disconnect_timestamp": self.last_disconnect_timestamp,
            "last_kill_timestamp": self.last_kill_timestamp,
            "capture_native_status": self.capture_native_status,
            "frame_created": self.frame_created,
            "frame_sha256": self.frame_sha256,
        }


__all__ = [
    "CaptureSessionRecoveryResult",
    "NEMU_CAPTURE_ERROR_SCHEMA_VERSION",
    "NemuCaptureError",
]
