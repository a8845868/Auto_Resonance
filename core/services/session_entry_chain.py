"""Bounded multi-stage resolver for existing-session entry gates."""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Callable

from core.services.read_only_policy import ActionIntent
from core.services.session_entry_resolver import (
    SessionEntryExecution,
    SessionEntryObservation,
    SessionEntryState,
    classify_session_entry_frame,
)


@dataclass(frozen=True)
class EntryChainTraceEvent:
    timestamp: str
    state: str
    screenshot_hash: str
    page_fingerprint: str
    reason: str
    attempt_count: int
    entry_step: int
    correlation_id: str
    action: str = "OBSERVE_ONLY"
    action_allowed: bool = False
    action_executed: bool = False
    guard_result: str = "NOT_REQUESTED"


@dataclass(frozen=True)
class EntryChainResult:
    state: SessionEntryState
    status: str
    reason: str
    path: tuple[str, ...]
    attempt_count: int
    entry_steps: int
    action_attempts: int
    action_allowed: bool
    action_executed: bool
    screenshot_hash: str
    page_fingerprint: str
    trace: tuple[EntryChainTraceEvent, ...]
    correlation_id: str
    irreversible_actions: int = 0
    scenario: str = "session_entry_chain"

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["state"] = self.state.value
        payload["path"] = list(self.path)
        payload["trace"] = [asdict(event) for event in self.trace]
        return payload


def _center(bounds: tuple[int, int, int, int]) -> tuple[int, int]:
    x1, y1, x2, y2 = bounds
    return int(round((x1 + x2) / 2)), int(round((y1 + y2) / 2))


class EntryChainResolver:
    """Follow at most three distinct entry gates and never repeat a gate."""

    def __init__(
        self,
        *,
        frame_provider: Callable[[], object],
        tap: Callable[..., object],
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = lambda: datetime.now().astimezone(),
        timeout: float = 45.0,
        max_attempts: int = 30,
        max_entry_steps: int = 3,
        same_gate_confirmations: int = 2,
        poll_interval: float = 1.0,
        correlation_id: str | None = None,
        initial_observation: SessionEntryObservation | None = None,
    ):
        self.frame_provider = frame_provider
        self.tap = tap
        self.sleep = sleep
        self.monotonic = monotonic
        self.now = now
        self.timeout = max(0.0, float(timeout))
        self.max_attempts = max(1, int(max_attempts))
        self.max_entry_steps = max(1, min(3, int(max_entry_steps)))
        self.same_gate_confirmations = max(1, int(same_gate_confirmations))
        self.poll_interval = max(0.0, float(poll_interval))
        self.correlation_id = correlation_id or self.now().strftime(
            "CHAIN-%Y%m%d-%H%M%S"
        )
        self.initial_observation = initial_observation

    def resolve(self) -> EntryChainResult:
        deadline = self.monotonic() + self.timeout
        attempts = 0
        entry_steps = 0
        action_attempts = 0
        any_allowed = False
        any_executed = False
        path: list[str] = [SessionEntryState.SESSION_READY.value]
        trace: list[EntryChainTraceEvent] = []
        seen_gate_captures: set[tuple[str, str]] = set()
        pending_gate_fingerprint = ""
        pending_same_gate_frames = 0
        home_fingerprint = ""
        home_frames = 0
        initial = self.initial_observation
        last = initial

        def append_path(state: SessionEntryState) -> None:
            if not path or path[-1] != state.value:
                path.append(state.value)

        def record(
            observation: SessionEntryObservation,
            *,
            action: str = "OBSERVE_ONLY",
            execution: SessionEntryExecution | None = None,
        ) -> None:
            trace.append(
                EntryChainTraceEvent(
                    timestamp=self.now().isoformat(timespec="milliseconds"),
                    state=observation.state.value,
                    screenshot_hash=observation.screenshot_hash,
                    page_fingerprint=observation.page_fingerprint,
                    reason=observation.reason,
                    attempt_count=attempts,
                    entry_step=entry_steps,
                    correlation_id=self.correlation_id,
                    action=action,
                    action_allowed=execution.allowed if execution else False,
                    action_executed=execution.executed if execution else False,
                    guard_result=(
                        execution.guard_result if execution else "NOT_REQUESTED"
                    ),
                )
            )

        def finish(
            state: SessionEntryState,
            status: str,
            reason: str,
        ) -> EntryChainResult:
            append_path(state)
            return EntryChainResult(
                state=state,
                status=status,
                reason=reason,
                path=tuple(path),
                attempt_count=attempts,
                entry_steps=entry_steps,
                action_attempts=action_attempts,
                action_allowed=any_allowed,
                action_executed=any_executed,
                screenshot_hash=last.screenshot_hash if last else "",
                page_fingerprint=last.page_fingerprint if last else "",
                trace=tuple(trace),
                correlation_id=self.correlation_id,
            )

        while attempts < self.max_attempts and self.monotonic() < deadline:
            if attempts:
                self.sleep(
                    min(
                        self.poll_interval,
                        max(0.0, deadline - self.monotonic()),
                    )
                )
                if self.monotonic() >= deadline:
                    break
            attempts += 1
            if initial is not None:
                last = initial
                initial = None
            else:
                last = classify_session_entry_frame(self.frame_provider())
            append_path(last.state)
            record(last)

            if last.state is SessionEntryState.BLOCKED:
                return finish(SessionEntryState.BLOCKED, "BLOCKED", last.reason)
            if last.state is SessionEntryState.UNKNOWN:
                return finish(SessionEntryState.UNKNOWN, "UNKNOWN", last.reason)
            if last.state is SessionEntryState.HOME_READY:
                if last.page_fingerprint == home_fingerprint:
                    home_frames += 1
                else:
                    home_fingerprint = last.page_fingerprint
                    home_frames = 1
                if home_frames >= 2:
                    return finish(
                        SessionEntryState.HOME_READY,
                        "PASS",
                        "home_ready_confirmed_after_entry_chain",
                    )
                continue
            home_fingerprint = ""
            home_frames = 0

            if last.state is SessionEntryState.ENTRY_TRANSITION:
                continue
            if last.state is not SessionEntryState.ENTRY_GATE_REQUIRED:
                return finish(
                    SessionEntryState.UNKNOWN,
                    "UNKNOWN",
                    "entry_chain_state_unknown",
                )
            if pending_gate_fingerprint:
                if last.page_fingerprint == pending_gate_fingerprint:
                    pending_same_gate_frames += 1
                    if pending_same_gate_frames < self.same_gate_confirmations:
                        append_path(SessionEntryState.ENTRY_TRANSITION)
                        continue
                pending_gate_fingerprint = ""
                pending_same_gate_frames = 0
            if last.anchor_bbox is None:
                return finish(
                    SessionEntryState.BLOCKED,
                    "BLOCKED",
                    "ENTRY_ANCHOR_MISSING",
                )
            gate_capture = (last.page_fingerprint, last.screenshot_hash)
            if gate_capture in seen_gate_captures:
                return finish(
                    SessionEntryState.ENTRY_FAILED,
                    "FAILED",
                    "ENTRY_GATE_REPEATED",
                )
            if entry_steps >= self.max_entry_steps:
                return finish(
                    SessionEntryState.ENTRY_FAILED,
                    "FAILED",
                    "ENTRY_CHAIN_LIMIT_REACHED",
                )

            action_attempts += 1
            step = entry_steps + 1
            try:
                raw_execution = self.tap(
                    _center(last.anchor_bbox),
                    intent=ActionIntent(
                        "enter_session",
                        "session_entry",
                        f"{self.correlation_id}:ENTER_SESSION:{step}",
                    ),
                )
                execution = (
                    raw_execution
                    if isinstance(raw_execution, SessionEntryExecution)
                    else SessionEntryExecution(
                        allowed=bool(raw_execution),
                        executed=bool(raw_execution),
                        guard_result=(
                            "ALLOWED" if raw_execution else "DENIED"
                        ),
                    )
                )
            except (PermissionError, RuntimeError) as error:
                execution = SessionEntryExecution(
                    allowed=False,
                    executed=False,
                    guard_result=type(error).__name__,
                )
            any_allowed = any_allowed or execution.allowed
            any_executed = any_executed or execution.executed
            append_path(SessionEntryState.ENTRY_CONFIRMING)
            record(last, action="ENTER_SESSION", execution=execution)
            if not execution.executed:
                return finish(
                    SessionEntryState.ENTRY_FAILED,
                    "FAILED",
                    "ENTRY_CLICK_FAILED",
                )
            seen_gate_captures.add(gate_capture)
            entry_steps += 1
            pending_gate_fingerprint = last.page_fingerprint
            pending_same_gate_frames = 0

        return finish(
            SessionEntryState.ENTRY_FAILED,
            "FAILED",
            "ENTRY_CHAIN_TIMEOUT",
        )


__all__ = [
    "EntryChainResolver",
    "EntryChainResult",
    "EntryChainTraceEvent",
]
