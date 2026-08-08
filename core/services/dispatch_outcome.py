"""Structured results for guarded input dispatches.

Literal ``False`` remains reserved for pre-dispatch denial.  Once a guarded
executor has been called, callers receive a truthy :class:`DispatchOutcome`
even when the guard's own postcondition could not be verified.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from core.control.nemu_receipt import DeliveryStatus


class DispatchStatus(str, Enum):
    DENIED = "DENIED"
    DISPATCHED_VERIFIED = "DISPATCHED_VERIFIED"
    DISPATCHED_UNVERIFIED = "DISPATCHED_UNVERIFIED"


@dataclass(frozen=True)
class DispatchOutcome:
    status: DispatchStatus
    reason: str = ""
    delivery_status: str = ""
    release_status: str = ""
    receipt: object | None = None

    def __bool__(self) -> bool:
        return self.status is not DispatchStatus.DENIED


def outcome_from_receipt(
    status: DispatchStatus,
    *,
    reason: str = "",
    receipt: object | None = None,
) -> DispatchOutcome:
    """Build an outcome without assuming that every backend returns a receipt."""

    return DispatchOutcome(
        status=status,
        reason=str(reason),
        delivery_status=str(getattr(receipt, "delivery_status", "") or ""),
        release_status=str(getattr(receipt, "release_status", "") or ""),
        receipt=receipt,
    )


def receipt_from_dispatch_error(error: BaseException) -> object | None:
    """Return a backend receipt carried by an exception, when present."""

    return getattr(error, "receipt", None)


def physical_input_count_from_dispatch_error(error: BaseException) -> int:
    """Conservatively account for physical input reported by a NEMU receipt."""

    receipt = receipt_from_dispatch_error(error)
    if receipt is None:
        return 0
    try:
        delivery = DeliveryStatus(str(getattr(receipt, "delivery_status", "")))
    except ValueError:
        return 0
    if delivery is DeliveryStatus.UNKNOWN_AFTER_PARTIAL_DISPATCH:
        return 1
    if delivery is DeliveryStatus.UNKNOWN_AFTER_EXCEPTION:
        return int(bool(getattr(receipt, "touch_down_called", False)))
    return 0


__all__ = [
    "DispatchOutcome",
    "DispatchStatus",
    "outcome_from_receipt",
    "physical_input_count_from_dispatch_error",
    "receipt_from_dispatch_error",
]
