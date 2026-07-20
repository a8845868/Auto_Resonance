"""Resolve a non-authentication existing-session entry gate exactly once."""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from enum import Enum
from typing import Callable

from core.services.read_only_policy import ActionIntent


class SessionEntryState(str, Enum):
    UNKNOWN = "UNKNOWN"
    ENTRY_GATE_REQUIRED = "ENTRY_GATE_REQUIRED"
    ENTRY_CONFIRMING = "ENTRY_CONFIRMING"
    HOME_READY = "HOME_READY"
    BLOCKED = "BLOCKED"
    ENTRY_FAILED = "ENTRY_FAILED"


@dataclass(frozen=True)
class SessionEntryObservation:
    state: SessionEntryState
    confidence: str
    screenshot_hash: str
    page_fingerprint: str
    reason: str
    anchor_bbox: tuple[int, int, int, int] | None
    evidence: tuple[str, ...]


@dataclass(frozen=True)
class SessionEntryExecution:
    allowed: bool
    executed: bool
    guard_result: str


@dataclass(frozen=True)
class SessionEntryTraceEvent:
    timestamp: str
    state: str
    screenshot_hash: str
    page_fingerprint: str
    confidence: str
    reason: str
    attempt_count: int
    correlation_id: str
    action: str = "OBSERVE_ONLY"
    action_allowed: bool = False
    action_executed: bool = False
    guard_result: str = "NOT_REQUESTED"


@dataclass(frozen=True)
class SessionEntryResult:
    state: SessionEntryState
    status: str
    reason: str
    path: tuple[str, ...]
    attempt_count: int
    screenshot_hash: str
    page_fingerprint: str
    action_attempted: bool
    action_allowed: bool
    action_executed: bool
    trace: tuple[SessionEntryTraceEvent, ...]
    correlation_id: str
    irreversible_actions: int = 0
    scenario: str = "session_resume_entry"

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["state"] = self.state.value
        payload["path"] = list(self.path)
        payload["trace"] = [asdict(event) for event in self.trace]
        return payload


def _bbox(item: dict) -> tuple[int, int, int, int] | None:
    points = item.get("position") or ()
    if len(points) < 3:
        return None
    xs = [int(point[0]) for point in points]
    ys = [int(point[1]) for point in points]
    return min(xs), min(ys), max(xs), max(ys)


def _center(bounds: tuple[int, int, int, int]) -> tuple[int, int]:
    x1, y1, x2, y2 = bounds
    return int(round((x1 + x2) / 2)), int(round((y1 + y2) / 2))


def _frame_hash(frame: object) -> str:
    raw_hash = str(getattr(frame, "raw_frame_hash", ""))
    if raw_hash:
        return raw_hash
    image = getattr(frame, "image", None)
    return hashlib.sha256(image.tobytes()).hexdigest() if image is not None else ""


def _matches(texts: list[str], markers: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(
        marker for marker in markers if any(marker in text for text in texts)
    )


def classify_session_entry_frame(frame: object) -> SessionEntryObservation:
    """Classify the session gate and reject every auth/irreversible ambiguity."""

    items = list(frame.ocr())
    texts = [str(item.get("text", "")).replace(" ", "") for item in items]
    screenshot_hash = _frame_hash(frame)

    def observation(
        state: SessionEntryState,
        reason: str,
        evidence: tuple[str, ...],
        anchor_bbox: tuple[int, int, int, int] | None = None,
    ) -> SessionEntryObservation:
        fingerprint = hashlib.sha256(
            json.dumps(
                {"state": state.value, "reason": reason},
                ensure_ascii=False,
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()[:24]
        return SessionEntryObservation(
            state=state,
            confidence="UNKNOWN" if state is SessionEntryState.UNKNOWN else "HIGH",
            screenshot_hash=screenshot_hash,
            page_fingerprint=fingerprint,
            reason=reason,
            anchor_bbox=anchor_bbox,
            evidence=evidence,
        )

    forbidden = _matches(
        texts,
        (
            "输入账号",
            "请输入账号",
            "账号登录",
            "输入密码",
            "请输入密码",
            "验证码",
            "获取验证码",
            "第三方授权",
            "授权确认",
            "账号绑定",
            "实名认证",
            "确认购买",
            "购买",
            "确认支付",
            "支付",
            "充值",
            "领取奖励",
            "领取",
        ),
    )
    if forbidden:
        auth_markers = {
            "输入账号",
            "请输入账号",
            "账号登录",
            "输入密码",
            "请输入密码",
            "验证码",
            "获取验证码",
            "第三方授权",
            "授权确认",
            "账号绑定",
            "实名认证",
        }
        reason = (
            "AUTHENTICATION_INPUT_PRESENT"
            if any(marker in auth_markers for marker in forbidden)
            else "IRREVERSIBLE_CONTROL_PRESENT"
        )
        return observation(SessionEntryState.BLOCKED, reason, forbidden)

    home = _matches(texts, ("访问城市", "作战终端", "启程", "整备列车"))
    if "访问城市" in home and len(home) >= 3:
        return observation(
            SessionEntryState.HOME_READY,
            "home_anchors_confirmed",
            home,
        )

    entry_markers = (
        "点击任意位置进入游戏",
        "点击屏幕进入游戏",
        "继续游戏",
        "开始游戏",
    )
    candidates: list[tuple[str, tuple[int, int, int, int]]] = []
    for item, text in zip(items, texts):
        if any(marker in text for marker in entry_markers):
            bounds = _bbox(item)
            if bounds is not None:
                candidates.append((text, bounds))
    if len(candidates) == 1:
        return observation(
            SessionEntryState.ENTRY_GATE_REQUIRED,
            "existing_session_entry_anchor_confirmed",
            (candidates[0][0],),
            candidates[0][1],
        )

    confirming = _matches(
        texts,
        ("正在连接服务器", "正在进入服务器", "正在加载", "加载中", "正在验证登录状态"),
    )
    if confirming:
        return observation(
            SessionEntryState.ENTRY_CONFIRMING,
            "session_entry_transition_in_progress",
            confirming,
        )
    return observation(SessionEntryState.UNKNOWN, "session_entry_unknown", ())


class SessionEntryResolver:
    """Execute one guarded entry gesture, then require two HOME frames."""

    def __init__(
        self,
        *,
        frame_provider: Callable[[], object],
        tap: Callable[..., object],
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = lambda: datetime.now().astimezone(),
        timeout: float = 30.0,
        max_attempts: int = 20,
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
        self.poll_interval = max(0.0, float(poll_interval))
        self.correlation_id = correlation_id or self.now().strftime(
            "SESSION-%Y%m%d-%H%M%S"
        )
        self.initial_observation = initial_observation

    def resolve(self) -> SessionEntryResult:
        deadline = self.monotonic() + self.timeout
        attempts = 1
        trace: list[SessionEntryTraceEvent] = []
        path: list[str] = []
        initial = self.initial_observation or classify_session_entry_frame(
            self.frame_provider()
        )
        last = initial
        action_attempted = False
        action_allowed = False
        action_executed = False

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
                SessionEntryTraceEvent(
                    timestamp=self.now().isoformat(timespec="milliseconds"),
                    state=observation.state.value,
                    screenshot_hash=observation.screenshot_hash,
                    page_fingerprint=observation.page_fingerprint,
                    confidence=observation.confidence,
                    reason=observation.reason,
                    attempt_count=attempts,
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
        ) -> SessionEntryResult:
            append_path(state)
            return SessionEntryResult(
                state=state,
                status=status,
                reason=reason,
                path=tuple(path),
                attempt_count=attempts,
                screenshot_hash=last.screenshot_hash,
                page_fingerprint=last.page_fingerprint,
                action_attempted=action_attempted,
                action_allowed=action_allowed,
                action_executed=action_executed,
                trace=tuple(trace),
                correlation_id=self.correlation_id,
            )

        append_path(initial.state)
        record(initial)
        if initial.state is SessionEntryState.BLOCKED:
            return finish(SessionEntryState.BLOCKED, "BLOCKED", initial.reason)
        if initial.state is SessionEntryState.UNKNOWN:
            return finish(SessionEntryState.UNKNOWN, "UNKNOWN", initial.reason)
        if (
            initial.state is not SessionEntryState.ENTRY_GATE_REQUIRED
            or initial.anchor_bbox is None
        ):
            return finish(
                SessionEntryState.UNKNOWN,
                "UNKNOWN",
                "entry_gate_not_confirmed",
            )

        action_attempted = True
        point = _center(initial.anchor_bbox)
        try:
            raw_execution = self.tap(
                point,
                intent=ActionIntent(
                    "enter_session",
                    "session_entry",
                    f"{self.correlation_id}:ENTER_SESSION",
                ),
            )
            execution = (
                raw_execution
                if isinstance(raw_execution, SessionEntryExecution)
                else SessionEntryExecution(
                    allowed=bool(raw_execution),
                    executed=bool(raw_execution),
                    guard_result="ALLOWED" if raw_execution else "DENIED",
                )
            )
        except (PermissionError, RuntimeError) as error:
            execution = SessionEntryExecution(
                allowed=False,
                executed=False,
                guard_result=type(error).__name__,
            )
        action_allowed = execution.allowed
        action_executed = execution.executed
        append_path(SessionEntryState.ENTRY_CONFIRMING)
        record(initial, action="ENTER_SESSION", execution=execution)
        if not execution.executed:
            return finish(
                SessionEntryState.BLOCKED,
                "BLOCKED",
                "SESSION_ENTRY_GUARD_DENIED",
            )

        home_fingerprint = ""
        home_frames = 0
        while attempts < self.max_attempts and self.monotonic() < deadline:
            self.sleep(
                min(self.poll_interval, max(0.0, deadline - self.monotonic()))
            )
            if self.monotonic() >= deadline:
                break
            attempts += 1
            last = classify_session_entry_frame(self.frame_provider())
            record(last)

            if last.state is SessionEntryState.BLOCKED:
                return finish(SessionEntryState.BLOCKED, "BLOCKED", last.reason)
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
                        "home_ready_confirmed_after_session_entry",
                    )
            else:
                home_fingerprint = ""
                home_frames = 0

        return finish(
            SessionEntryState.ENTRY_FAILED,
            "FAILED",
            "ENTRY_FAILED",
        )


__all__ = [
    "SessionEntryExecution",
    "SessionEntryObservation",
    "SessionEntryResolver",
    "SessionEntryResult",
    "SessionEntryState",
    "SessionEntryTraceEvent",
    "classify_session_entry_frame",
]
