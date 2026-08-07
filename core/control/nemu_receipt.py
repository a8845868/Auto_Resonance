"""Fail-closed native input receipts for the NEMU transport."""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import Enum


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


class NativeCallStatus(str, Enum):
    NOT_CALLED = "NOT_CALLED"
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    UNKNOWN = "UNKNOWN"


class DeliveryStatus(str, Enum):
    NOT_DISPATCHED = "NOT_DISPATCHED"
    REJECTED_BEFORE_DELIVERY = "REJECTED_BEFORE_DELIVERY"
    NATIVE_ACCEPTED = "NATIVE_ACCEPTED"
    UNKNOWN_AFTER_PARTIAL_DISPATCH = "UNKNOWN_AFTER_PARTIAL_DISPATCH"
    UNKNOWN_AFTER_EXCEPTION = "UNKNOWN_AFTER_EXCEPTION"


class ReleaseStatus(str, Enum):
    NOT_REQUIRED = "NOT_REQUIRED"
    CONFIRMED = "CONFIRMED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class NemuTouchReceipt:
    schema_version: str
    attempt_id: str
    dispatch_id: str
    instance_id: str
    display_id: int
    session_generation: int
    capture_width: int
    capture_height: int
    display_width: int
    display_height: int
    rotation: int
    capture_point: tuple[int, int]
    mapped_nemu_point: tuple[int, int]
    touch_down_called: bool
    touch_down_return_code: int | None
    touch_down_status: str
    touch_up_called: bool
    touch_up_return_code: int | None
    touch_up_status: str
    python_call_returned: bool
    delivery_status: str
    release_status: str
    started_at: str
    finished_at: str
    reason_codes: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(slots=True)
class NemuTouchReceiptBuilder:
    instance_id: str
    display_id: int
    session_generation: int
    capture_size: tuple[int, int]
    display_size: tuple[int, int]
    rotation: int
    capture_point: tuple[int, int]
    mapped_nemu_point: tuple[int, int]
    attempt_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    dispatch_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    started_at: str = field(default_factory=_now)
    down_called: bool = False
    down_code: int | None = None
    down_status: NativeCallStatus = NativeCallStatus.NOT_CALLED
    up_called: bool = False
    up_code: int | None = None
    up_status: NativeCallStatus = NativeCallStatus.NOT_CALLED
    delivery: DeliveryStatus = DeliveryStatus.NOT_DISPATCHED
    release: ReleaseStatus = ReleaseStatus.NOT_REQUIRED
    reasons: list[str] = field(default_factory=list)

    def finish(self, *, python_call_returned: bool) -> NemuTouchReceipt:
        return NemuTouchReceipt(
            schema_version="1.0",
            attempt_id=self.attempt_id,
            dispatch_id=self.dispatch_id,
            instance_id=self.instance_id,
            display_id=self.display_id,
            session_generation=self.session_generation,
            capture_width=self.capture_size[0],
            capture_height=self.capture_size[1],
            display_width=self.display_size[0],
            display_height=self.display_size[1],
            rotation=self.rotation,
            capture_point=self.capture_point,
            mapped_nemu_point=self.mapped_nemu_point,
            touch_down_called=self.down_called,
            touch_down_return_code=self.down_code,
            touch_down_status=self.down_status.value,
            touch_up_called=self.up_called,
            touch_up_return_code=self.up_code,
            touch_up_status=self.up_status.value,
            python_call_returned=python_call_returned,
            delivery_status=self.delivery.value,
            release_status=self.release.value,
            started_at=self.started_at,
            finished_at=_now(),
            reason_codes=tuple(self.reasons),
        )


class NemuInputDispatchError(RuntimeError):
    def __init__(self, receipt: NemuTouchReceipt):
        super().__init__(receipt.delivery_status)
        self.receipt = receipt


def map_capture_to_nemu(
    point: tuple[int, int],
    *,
    capture_size: tuple[int, int],
    display_size: tuple[int, int],
    rotation: int = 0,
) -> tuple[int, int]:
    """Map capture pixels to NEMU input pixels with bounds and round-trip checks."""

    cw, ch = capture_size
    dw, dh = display_size
    x, y = point
    if rotation not in (0, 90, 180, 270):
        raise ValueError("nemu_rotation_unsupported")
    if min(cw, ch, dw, dh) <= 0 or not (0 <= x < cw and 0 <= y < ch):
        raise ValueError("nemu_coordinate_out_of_bounds")
    logical_x = round(x * (dw - 1) / max(1, cw - 1))
    logical_y = round(y * (dh - 1) / max(1, ch - 1))
    if rotation == 0:
        mapped = (logical_x, logical_y)
    elif rotation == 90:
        mapped = (dh - 1 - logical_y, logical_x)
    elif rotation == 180:
        mapped = (dw - 1 - logical_x, dh - 1 - logical_y)
    else:
        mapped = (logical_y, dw - 1 - logical_x)
    max_x, max_y = ((dw, dh) if rotation in (0, 180) else (dh, dw))
    if not (0 <= mapped[0] < max_x and 0 <= mapped[1] < max_y):
        raise ValueError("nemu_coordinate_transform_invalid")
    return mapped
