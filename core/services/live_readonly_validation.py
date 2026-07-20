"""Small, serialisable result model for real-device read-only validation."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum

from core.services.startup_overlay_resolver import StartupResolutionResult


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


def startup_resolution_scenario(
    result: StartupResolutionResult,
) -> dict[str, object]:
    """Map the bounded resolver result into the live-validation matrix."""

    return {
        "scenario": "startup_overlay_resolution",
        "status": result.status,
        "path": list(result.path),
        "reason": result.reason,
        "correlation_id": result.correlation_id,
        "attempt_count": result.attempt_count,
        "screenshot_hash": result.screenshot_hash,
        "page_fingerprint": result.page_fingerprint,
        "irreversible_actions": result.irreversible_actions,
    }


__all__ = [
    "ReadOnlyValidationResult",
    "ReadOnlyValidationStatus",
    "startup_resolution_scenario",
]
