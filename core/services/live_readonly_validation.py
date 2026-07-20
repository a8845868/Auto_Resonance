"""Small, serialisable result model for real-device read-only validation."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum


class ReadOnlyValidationStatus(str, Enum):
    PASS = "PASS"
    BLOCKED = "BLOCKED"
    UNKNOWN = "UNKNOWN"
    FAILED = "FAILED"


@dataclass(frozen=True)
class ReadOnlyValidationResult:
    scenario: str
    page_before: str
    action_attempted: str
    action_allowed: bool
    action_executed: bool
    page_after: str
    postcondition: str
    screenshot_hash_before: str
    screenshot_hash_after: str
    status: ReadOnlyValidationStatus
    reason: str

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["status"] = ReadOnlyValidationStatus(self.status).value
        return payload


__all__ = ["ReadOnlyValidationResult", "ReadOnlyValidationStatus"]
