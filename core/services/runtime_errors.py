"""Explicit runtime failure taxonomy for unattended automation tasks."""

from __future__ import annotations


class AutomationRuntimeError(RuntimeError):
    """Base class carrying a stable category without hiding the original error."""

    def __init__(self, message: str, *, original: BaseException | None = None):
        super().__init__(str(message))
        self.original_type = (
            type(original).__name__ if original is not None else type(self).__name__
        )
        self.original_message = str(original) if original is not None else str(message)
        if original is not None:
            self.__cause__ = original


class RecoverableAutomationError(AutomationRuntimeError):
    """A transient observation or transport failure eligible for bounded retry."""


class BlockedBySafetyError(AutomationRuntimeError):
    """A known environment or page precondition failed and the task must stop.

    Queue semantics (frozen by AUTO_RESONANCE_BLOCKED_SAFETY_OUTCOME_TAXONOMY_V1):
    the task terminates as ``TaskOutcome.BLOCKED_SAFETY`` — never queue-fatal,
    never self-healing eligible, never retried inside the queue run.  Only the
    ordinary scheduler-level next-run policy applies.
    """


class FatalAutomationError(AutomationRuntimeError):
    """A programming/schema failure that must stop the remaining task queue."""


class AdbTimeoutError(RecoverableAutomationError):
    pass


class OcrUnknownError(RecoverableAutomationError):
    pass


class ScreenshotTimeoutError(RecoverableAutomationError):
    pass


class TransientConnectionError(RecoverableAutomationError):
    pass


class SchemaCorruptionError(FatalAutomationError):
    pass


_FATAL_BUILTINS = (
    NameError,
    ImportError,
    AttributeError,
    AssertionError,
    TypeError,
)


def _wrapped(
    error_type: type[AutomationRuntimeError], error: BaseException
) -> AutomationRuntimeError:
    return error_type(
        f"{type(error).__name__}: {error}",
        original=error,
    )


def classify_runtime_error(error: BaseException) -> AutomationRuntimeError:
    """Classify one caught task exception; unknown exceptions fail closed."""

    if isinstance(error, AutomationRuntimeError):
        return error
    if isinstance(error, TimeoutError):
        return _wrapped(ScreenshotTimeoutError, error)
    if isinstance(error, ConnectionError):
        return _wrapped(TransientConnectionError, error)
    if isinstance(error, _FATAL_BUILTINS):
        return _wrapped(FatalAutomationError, error)
    return _wrapped(FatalAutomationError, error)


__all__ = [
    "AutomationRuntimeError",
    "RecoverableAutomationError",
    "BlockedBySafetyError",
    "FatalAutomationError",
    "AdbTimeoutError",
    "OcrUnknownError",
    "ScreenshotTimeoutError",
    "TransientConnectionError",
    "SchemaCorruptionError",
    "classify_runtime_error",
]
