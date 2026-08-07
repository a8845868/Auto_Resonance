"""Bounded recovery policy for observation failures before any UI input."""

from __future__ import annotations

from dataclasses import dataclass

from core.control.nemu_capture import NemuCaptureError


@dataclass(frozen=True)
class CaptureRecoveryPolicy:
    max_pre_dispatch_session_recovery: int = 1

    def allows(
        self,
        failure: NemuCaptureError,
        *,
        dispatch_count: int,
        recovery_count: int,
    ) -> bool:
        return (
            dispatch_count == 0
            and recovery_count < max(
                0, int(self.max_pre_dispatch_session_recovery)
            )
            and failure.session_lifecycle_conflict != "YES"
        )


DEFAULT_CAPTURE_RECOVERY_POLICY = CaptureRecoveryPolicy()


__all__ = ["CaptureRecoveryPolicy", "DEFAULT_CAPTURE_RECOVERY_POLICY"]
