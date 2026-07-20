"""Read-only login/session state classification and bounded waiting."""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from enum import Enum
from typing import Callable


class LoginState(str, Enum):
    UNKNOWN = "UNKNOWN"
    LOGIN_REQUIRED = "LOGIN_REQUIRED"
    LOGIN_LOADING = "LOGIN_LOADING"
    SESSION_VALIDATING = "SESSION_VALIDATING"
    SERVER_CONNECTING = "SERVER_CONNECTING"
    SESSION_READY = "SESSION_READY"
    LOGIN_FAILED = "LOGIN_FAILED"
    SERVER_ERROR = "SERVER_ERROR"
    TIMEOUT = "TIMEOUT"
    HOME_READY = "HOME_READY"


@dataclass(frozen=True)
class LoginObservation:
    state: LoginState
    confidence: str
    screenshot_hash: str
    page_fingerprint: str
    reason: str
    evidence: tuple[str, ...]


@dataclass(frozen=True)
class LoginTraceEvent:
    timestamp: str
    state: str
    confidence: str
    screenshot_hash: str
    page_fingerprint: str
    reason: str
    attempt_count: int
    correlation_id: str
    action: str = "OBSERVE_ONLY"
    action_taken: bool = False


@dataclass(frozen=True)
class LoginResolutionResult:
    state: LoginState
    state_before: LoginState
    state_after: LoginState
    status: str
    reason: str
    path: tuple[LoginState, ...]
    attempt_count: int
    screenshot_hash: str
    page_fingerprint: str
    trace: tuple[LoginTraceEvent, ...]
    correlation_id: str
    irreversible_actions: int = 0
    scenario: str = "login_state_resolution"

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["state"] = self.state.value
        payload["state_before"] = self.state_before.value
        payload["state_after"] = self.state_after.value
        payload["path"] = [state.value for state in self.path]
        payload["trace"] = [asdict(event) for event in self.trace]
        return payload


def _frame_hash(frame: object) -> str:
    raw_hash = str(getattr(frame, "raw_frame_hash", ""))
    if raw_hash:
        return raw_hash
    image = getattr(frame, "image", None)
    if image is None:
        return ""
    return hashlib.sha256(image.tobytes()).hexdigest()


def _contains(texts: list[str], markers: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(
        marker
        for marker in markers
        if any(marker in text for text in texts)
    )


def classify_login_frame(frame: object) -> LoginObservation:
    """Classify a login/session frame without generating an action."""

    items = list(frame.ocr())
    texts = [str(item.get("text", "")).replace(" ", "") for item in items]
    screenshot_hash = _frame_hash(frame)

    def observation(
        state: LoginState,
        reason: str,
        evidence: tuple[str, ...],
    ) -> LoginObservation:
        fingerprint = hashlib.sha256(
            json.dumps(
                {"state": state.value, "reason": reason},
                ensure_ascii=False,
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()[:24]
        return LoginObservation(
            state=state,
            confidence="UNKNOWN" if state is LoginState.UNKNOWN else "HIGH",
            screenshot_hash=screenshot_hash,
            page_fingerprint=fingerprint,
            reason=reason,
            evidence=evidence,
        )

    home_markers = _contains(
        texts, ("访问城市", "作战终端", "启程", "整备列车")
    )
    if "访问城市" in home_markers and len(home_markers) >= 3:
        return observation(
            LoginState.HOME_READY,
            "home_anchors_confirmed",
            home_markers,
        )

    server_errors = _contains(
        texts,
        (
            "服务器异常",
            "服务器错误",
            "网络错误",
            "连接服务器失败",
            "无法连接服务器",
            "服务器维护",
        ),
    )
    if server_errors:
        return observation(
            LoginState.SERVER_ERROR,
            "server_error_observed",
            server_errors,
        )

    login_failures = _contains(
        texts,
        ("登录失败", "认证失败", "会话失效", "登录已过期", "登录状态失效"),
    )
    if login_failures:
        return observation(
            LoginState.LOGIN_FAILED,
            "login_failure_observed",
            login_failures,
        )

    manual_auth = _contains(
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
        ),
    )
    if manual_auth:
        return observation(
            LoginState.LOGIN_REQUIRED,
            "BLOCKED_MANUAL_AUTH_REQUIRED",
            manual_auth,
        )

    session_ready = _contains(
        texts,
        (
            "点击屏幕进入游戏",
            "点击任意位置进入游戏",
            "下载已经完成",
            "下载已完成",
        ),
    )
    if session_ready:
        return observation(
            LoginState.SESSION_READY,
            "existing_session_waiting_for_user_gesture",
            session_ready,
        )

    session_validating = _contains(
        texts,
        ("正在验证登录状态", "验证登录状态", "正在校验会话", "正在登录", "登录中"),
    )
    if session_validating:
        return observation(
            LoginState.SESSION_VALIDATING,
            "existing_session_validation_in_progress",
            session_validating,
        )

    server_connecting = _contains(
        texts,
        ("正在连接服务器", "连接服务器", "正在进入服务器", "正在获取服务器"),
    )
    if server_connecting:
        return observation(
            LoginState.SERVER_CONNECTING,
            "server_connection_in_progress",
            server_connecting,
        )

    login_loading = _contains(
        texts,
        ("登录加载中", "正在加载登录", "加载登录信息", "正在加载"),
    )
    if login_loading:
        return observation(
            LoginState.LOGIN_LOADING,
            "login_ui_loading",
            login_loading,
        )

    return observation(LoginState.UNKNOWN, "login_state_unknown", ())


class LoginStateResolver:
    """Wait for an existing session without clicking or collecting credentials."""

    _WAITABLE = frozenset(
        {
            LoginState.LOGIN_LOADING,
            LoginState.SESSION_VALIDATING,
            LoginState.SERVER_CONNECTING,
            LoginState.SESSION_READY,
            LoginState.HOME_READY,
        }
    )

    def __init__(
        self,
        *,
        frame_provider: Callable[[], object],
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = lambda: datetime.now().astimezone(),
        timeout: float = 60.0,
        max_attempts: int = 60,
        poll_interval: float = 1.0,
        correlation_id: str | None = None,
        initial_observation: LoginObservation | None = None,
    ):
        self.frame_provider = frame_provider
        self.sleep = sleep
        self.monotonic = monotonic
        self.now = now
        self.timeout = max(0.0, float(timeout))
        self.max_attempts = max(1, int(max_attempts))
        self.poll_interval = max(0.0, float(poll_interval))
        self.correlation_id = correlation_id or self.now().strftime(
            "LOGIN-%Y%m%d-%H%M%S"
        )
        self.initial_observation = initial_observation

    def resolve(self) -> LoginResolutionResult:
        deadline = self.monotonic() + self.timeout
        attempts = 0
        first_state: LoginState | None = None
        last: LoginObservation | None = None
        path: list[LoginState] = []
        trace: list[LoginTraceEvent] = []
        home_fingerprint = ""
        home_frames = 0
        initial_observation = self.initial_observation

        def record(observation: LoginObservation) -> None:
            nonlocal first_state
            if first_state is None:
                first_state = observation.state
            if not path or path[-1] is not observation.state:
                path.append(observation.state)
            trace.append(
                LoginTraceEvent(
                    timestamp=self.now().isoformat(timespec="milliseconds"),
                    state=observation.state.value,
                    confidence=observation.confidence,
                    screenshot_hash=observation.screenshot_hash,
                    page_fingerprint=observation.page_fingerprint,
                    reason=observation.reason,
                    attempt_count=attempts,
                    correlation_id=self.correlation_id,
                )
            )

        def finish(
            state: LoginState,
            status: str,
            reason: str,
        ) -> LoginResolutionResult:
            if not path or path[-1] is not state:
                path.append(state)
            return LoginResolutionResult(
                state=state,
                state_before=first_state or LoginState.UNKNOWN,
                state_after=state,
                status=status,
                reason=reason,
                path=tuple(path),
                attempt_count=attempts,
                screenshot_hash=last.screenshot_hash if last else "",
                page_fingerprint=last.page_fingerprint if last else "",
                trace=tuple(trace),
                correlation_id=self.correlation_id,
            )

        while attempts < self.max_attempts and self.monotonic() < deadline:
            attempts += 1
            if initial_observation is not None:
                last = initial_observation
                initial_observation = None
            else:
                last = classify_login_frame(self.frame_provider())
            record(last)

            if last.state is LoginState.LOGIN_REQUIRED:
                return finish(
                    LoginState.LOGIN_REQUIRED,
                    "BLOCKED",
                    "BLOCKED_MANUAL_AUTH_REQUIRED",
                )
            if last.state in {LoginState.LOGIN_FAILED, LoginState.SERVER_ERROR}:
                return finish(last.state, "BLOCKED", last.reason)
            if last.state is LoginState.UNKNOWN:
                if first_state is LoginState.UNKNOWN:
                    return finish(
                        LoginState.UNKNOWN,
                        "UNKNOWN",
                        "login_state_unknown",
                    )
                self.sleep(
                    min(
                        self.poll_interval,
                        max(0.0, deadline - self.monotonic()),
                    )
                )
                continue

            if last.state is LoginState.HOME_READY:
                if last.page_fingerprint == home_fingerprint:
                    home_frames += 1
                else:
                    home_fingerprint = last.page_fingerprint
                    home_frames = 1
                if home_frames >= 2:
                    return finish(
                        LoginState.HOME_READY,
                        "PASS",
                        "home_ready_confirmed",
                    )
            else:
                home_fingerprint = ""
                home_frames = 0

            if last.state not in self._WAITABLE:
                return finish(last.state, "UNKNOWN", last.reason)
            self.sleep(
                min(self.poll_interval, max(0.0, deadline - self.monotonic()))
            )

        return finish(
            LoginState.TIMEOUT,
            "BLOCKED",
            "login_deadline_or_attempt_limit",
        )


__all__ = [
    "LoginObservation",
    "LoginResolutionResult",
    "LoginState",
    "LoginStateResolver",
    "LoginTraceEvent",
    "classify_login_frame",
]
