"""Bounded, evidence-driven resolver for startup overlays."""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from enum import Enum
from typing import Callable

from core.services.read_only_policy import ActionIntent
from core.services.login_state_resolver import (
    LoginObservation,
    LoginState,
    classify_login_frame,
)
from core.services.session_entry_resolver import (
    SessionEntryState,
    classify_session_entry_frame,
)
from core.services.session_entry_chain import EntryChainResolver


class OverlayType(str, Enum):
    UNKNOWN = "UNKNOWN"
    SAFE_DISMISSABLE = "SAFE_DISMISSABLE"
    READ_ONLY_INFORMATION = "READ_ONLY_INFORMATION"
    REWARD_RELATED = "REWARD_RELATED"
    CONSUMABLE_CONFIRMATION = "CONSUMABLE_CONFIRMATION"
    PURCHASE_CONFIRMATION = "PURCHASE_CONFIRMATION"
    ACCOUNT_SECURITY = "ACCOUNT_SECURITY"
    HOME_READY = "HOME_READY"


class StartupState(str, Enum):
    STARTING = "STARTING"
    LOGIN_STATE = "LOGIN_STATE"
    SESSION_READY = "SESSION_READY"
    ENTRY_GATE_REQUIRED = "ENTRY_GATE_REQUIRED"
    ENTRY_CONFIRMING = "ENTRY_CONFIRMING"
    ENTRY_FAILED = "ENTRY_FAILED"
    OVERLAY_RESOLUTION = "OVERLAY_RESOLUTION"
    WAITING_UI = "WAITING_UI"
    SAFE_OVERLAY = "SAFE_OVERLAY"
    DISMISS_PENDING = "DISMISS_PENDING"
    HOME_READY = "HOME_READY"
    BLOCKED = "BLOCKED"
    TIMEOUT = "TIMEOUT"
    FAILED = "FAILED"


@dataclass(frozen=True)
class OverlayAction:
    type: str
    evidence_id: str
    irreversible: bool
    point: tuple[int, int]


@dataclass(frozen=True)
class OverlayObservation:
    page_type: str
    overlay_type: OverlayType
    confidence: str
    screenshot_hash: str
    page_fingerprint: str
    reason: str
    action: OverlayAction | None = None


@dataclass(frozen=True)
class StartupTraceEvent:
    timestamp: str
    page_type: str
    overlay_type: str
    confidence: str
    allowed_action: str
    action_taken: bool
    screenshot_hash: str
    page_fingerprint: str
    state: str
    reason: str
    attempt_count: int
    correlation_id: str
    guard_result: str = "NOT_REQUESTED"
    postcondition: str = "NOT_CHECKED"


@dataclass(frozen=True)
class StartupResolutionResult:
    state: StartupState
    status: str
    path: tuple[str, ...]
    attempt_count: int
    reason: str
    screenshot_hash: str
    page_fingerprint: str
    trace: tuple[StartupTraceEvent, ...]
    correlation_id: str
    irreversible_actions: int = 0

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


def _hash_frame(frame) -> str:
    raw_hash = str(getattr(frame, "raw_frame_hash", ""))
    if raw_hash:
        return raw_hash
    image = getattr(frame, "image", None)
    return hashlib.sha256(image.tobytes()).hexdigest() if image is not None else ""


def _claim_control(text: str) -> bool:
    normalized = text.replace(" ", "")
    if "已领取" in normalized:
        return False
    return normalized in {"领取", "签到领取"} or any(
        marker in normalized
        for marker in ("点击领取", "可领取", "领取奖励", "立即领取")
    )


def classify_startup_frame(frame) -> OverlayObservation:
    items = list(frame.ocr())
    texts = [str(item.get("text", "")).replace(" ", "") for item in items]
    joined = "|".join(texts)
    screenshot_hash = _hash_frame(frame)
    close_candidates = []
    for item, text in zip(items, texts):
        if any(marker in text for marker in ("关闭", "返回", "触碰空白区域退出")):
            bounds = _bbox(item)
            if bounds is not None:
                close_candidates.append((text, bounds))
    close_anchor = close_candidates[0] if len(close_candidates) == 1 else None

    def observation(
        page_type: str,
        overlay_type: OverlayType,
        reason: str,
        *,
        action_type: str = "",
    ) -> OverlayObservation:
        fingerprint = hashlib.sha256(
            json.dumps(
                {
                    "page_type": page_type,
                    "overlay_type": overlay_type.value,
                    "reason": reason,
                },
                ensure_ascii=False,
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()[:24]
        evidence_id = hashlib.sha256(
            f"{overlay_type.value}|{fingerprint}|{reason}".encode("utf-8")
        ).hexdigest()[:24]
        action = (
            OverlayAction(action_type, evidence_id, False, _center(close_anchor[1]))
            if action_type and close_anchor is not None
            else None
        )
        return OverlayObservation(
            page_type,
            overlay_type,
            "HIGH" if overlay_type is not OverlayType.UNKNOWN else "UNKNOWN",
            screenshot_hash,
            fingerprint,
            reason,
            action,
        )

    if any(marker in joined for marker in ("确认购买", "确认支付", "充值", "支付订单")):
        return observation("PURCHASE_CONFIRMATION", OverlayType.PURCHASE_CONFIRMATION, "purchase_confirmation_forbidden")
    if any(marker in joined for marker in (
        "使用道具", "使用便当", "使用气泡水", "是否使用银枝", "确认消耗", "消耗货币",
    )):
        return observation("CONSUMABLE_CONFIRMATION", OverlayType.CONSUMABLE_CONFIRMATION, "consumable_confirmation_forbidden")
    if any(marker in joined for marker in ("账号安全", "实名认证", "登录验证", "验证码", "修改密码")):
        return observation("ACCOUNT_SECURITY", OverlayType.ACCOUNT_SECURITY, "account_security_requires_manual")

    reward_related = any(marker in joined for marker in (
        "每日签到奖励", "签到奖励", "每日奖励", "活跃奖励", "手册奖励", "今日奖励",
    ))
    if reward_related:
        if any(_claim_control(text) for text in texts):
            return observation("CHECKIN_OVERLAY", OverlayType.REWARD_RELATED, "CHECKIN_REWARD_REQUIRES_MANUAL")
        if close_anchor is not None:
            return observation(
                "CHECKIN_OVERLAY", OverlayType.REWARD_RELATED,
                "checkin_information_has_explicit_exit",
                action_type="DISMISS_CHECKIN_INFORMATION",
            )
        return observation("CHECKIN_OVERLAY", OverlayType.REWARD_RELATED, "CHECKIN_REWARD_REQUIRES_MANUAL")

    home_markers = sum(marker in joined for marker in ("访问城市", "作战终端", "启程", "整备列车"))
    if "访问城市" in joined and home_markers >= 3 and not any(
        marker in joined for marker in ("加载中", "确认", "取消", "领取", "购买")
    ):
        return observation("HOME", OverlayType.HOME_READY, "home_anchors_confirmed")

    safe_information = any(marker in joined for marker in ("公告", "新闻", "资讯", "活动介绍", "登录公告"))
    if safe_information and close_anchor is not None:
        return observation(
            "STARTUP_OVERLAY", OverlayType.SAFE_DISMISSABLE,
            "safe_information_with_explicit_close",
            action_type="DISMISS_ANNOUNCEMENT",
        )
    if safe_information or any(marker in joined for marker in ("任务说明", "活动详情", "规则说明")):
        return observation("READ_ONLY_INFORMATION", OverlayType.READ_ONLY_INFORMATION, "information_observe_only")
    if any(marker in joined for marker in ("加载中", "正在加载")):
        return observation("GAME_LOADING", OverlayType.UNKNOWN, "game_loading")
    if any(marker in joined for marker in ("进入游戏", "点击屏幕进入游戏")):
        return observation("LOGIN", OverlayType.UNKNOWN, "login_waiting_for_existing_flow")
    return observation("UNKNOWN", OverlayType.UNKNOWN, "overlay_unknown")


class StartupResolver:
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
    ):
        self.frame_provider = frame_provider
        self.tap = tap
        self.sleep = sleep
        self.monotonic = monotonic
        self.now = now
        self.timeout = max(0.0, float(timeout))
        self.max_attempts = max(1, int(max_attempts))
        self.poll_interval = max(0.0, float(poll_interval))
        self.correlation_id = correlation_id or self.now().strftime("STARTUP-%Y%m%d-%H%M%S")

    def resolve(self) -> StartupResolutionResult:
        started = self.monotonic()
        deadline = started + self.timeout
        attempts = 0
        trace: list[StartupTraceEvent] = []
        path: list[str] = [
            StartupState.STARTING.value,
            StartupState.LOGIN_STATE.value,
        ]
        last: OverlayObservation | None = None
        home_fingerprint = ""
        home_frames = 0
        overlay_phase_started = False

        def append_path(value: str) -> None:
            if not path or path[-1] != value:
                path.append(value)

        def login_as_overlay(
            observation: LoginObservation,
        ) -> OverlayObservation:
            return OverlayObservation(
                page_type=observation.state.value,
                overlay_type=(
                    OverlayType.ACCOUNT_SECURITY
                    if observation.state is LoginState.LOGIN_REQUIRED
                    else OverlayType.UNKNOWN
                ),
                confidence=observation.confidence,
                screenshot_hash=observation.screenshot_hash,
                page_fingerprint=observation.page_fingerprint,
                reason=observation.reason,
            )

        def event(
            observation: OverlayObservation,
            state: StartupState,
            *,
            action_taken: bool = False,
            guard_result: str = "NOT_REQUESTED",
            postcondition: str = "NOT_CHECKED",
        ) -> None:
            trace.append(StartupTraceEvent(
                timestamp=self.now().isoformat(timespec="milliseconds"),
                page_type=observation.page_type,
                overlay_type=observation.overlay_type.value,
                confidence=observation.confidence,
                allowed_action=(observation.action.type if observation.action else "OBSERVE_ONLY"),
                action_taken=action_taken,
                screenshot_hash=observation.screenshot_hash,
                page_fingerprint=observation.page_fingerprint,
                state=state.value,
                reason=observation.reason,
                attempt_count=attempts,
                correlation_id=self.correlation_id,
                guard_result=guard_result,
                postcondition=postcondition,
            ))

        def finish(state: StartupState, status: str, reason: str) -> StartupResolutionResult:
            return StartupResolutionResult(
                state=state,
                status=status,
                path=tuple(path),
                attempt_count=attempts,
                reason=reason,
                screenshot_hash=last.screenshot_hash if last else "",
                page_fingerprint=last.page_fingerprint if last else "",
                trace=tuple(trace),
                correlation_id=self.correlation_id,
                irreversible_actions=0,
            )

        while attempts < self.max_attempts and self.monotonic() < deadline:
            attempts += 1
            frame = self.frame_provider()
            login = classify_login_frame(frame)

            if login.state is LoginState.LOGIN_REQUIRED:
                last = login_as_overlay(login)
                append_path(StartupState.BLOCKED.value)
                event(last, StartupState.BLOCKED)
                return finish(
                    StartupState.BLOCKED,
                    "BLOCKED",
                    "BLOCKED_MANUAL_AUTH_REQUIRED",
                )
            if login.state in {LoginState.LOGIN_FAILED, LoginState.SERVER_ERROR}:
                last = login_as_overlay(login)
                append_path(StartupState.BLOCKED.value)
                event(last, StartupState.BLOCKED)
                return finish(StartupState.BLOCKED, "BLOCKED", login.reason)
            if login.state is LoginState.SESSION_READY:
                last = login_as_overlay(login)
                append_path(StartupState.SESSION_READY.value)
                event(last, StartupState.SESSION_READY)
                entry_observation = classify_session_entry_frame(frame)
                if entry_observation.state is not SessionEntryState.ENTRY_GATE_REQUIRED:
                    append_path(StartupState.BLOCKED.value)
                    return finish(
                        StartupState.BLOCKED,
                        "BLOCKED",
                        entry_observation.reason,
                    )
                entry = EntryChainResolver(
                    frame_provider=self.frame_provider,
                    tap=self.tap,
                    sleep=self.sleep,
                    monotonic=self.monotonic,
                    now=self.now,
                    timeout=max(0.0, deadline - self.monotonic()),
                    max_attempts=max(1, self.max_attempts - attempts + 1),
                    max_entry_steps=3,
                    poll_interval=self.poll_interval,
                    correlation_id=self.correlation_id,
                    initial_observation=entry_observation,
                ).resolve()
                attempts += max(0, entry.attempt_count - 1)
                for entry_state in entry.path:
                    if entry_state != SessionEntryState.SESSION_READY.value:
                        append_path(entry_state)
                event(
                    last,
                    StartupState.ENTRY_CONFIRMING,
                    action_taken=entry.action_executed,
                    guard_result=("ALLOWED" if entry.action_allowed else "DENIED"),
                    postcondition=entry.reason,
                )
                if entry.state is SessionEntryState.HOME_READY:
                    last = OverlayObservation(
                        page_type="HOME",
                        overlay_type=OverlayType.HOME_READY,
                        confidence="HIGH",
                        screenshot_hash=entry.screenshot_hash,
                        page_fingerprint=entry.page_fingerprint,
                        reason=entry.reason,
                    )
                    append_path(StartupState.HOME_READY.value)
                    event(
                        last,
                        StartupState.HOME_READY,
                        postcondition="two_consistent_home_frames",
                    )
                    return finish(
                        StartupState.HOME_READY,
                        "PASS",
                        entry.reason,
                    )
                if entry.state is SessionEntryState.BLOCKED:
                    append_path(StartupState.BLOCKED.value)
                    return finish(StartupState.BLOCKED, "BLOCKED", entry.reason)
                append_path(StartupState.ENTRY_FAILED.value)
                return finish(StartupState.ENTRY_FAILED, "FAILED", entry.reason)
            if login.state in {
                LoginState.LOGIN_LOADING,
                LoginState.SESSION_VALIDATING,
                LoginState.SERVER_CONNECTING,
            }:
                last = login_as_overlay(login)
                append_path(login.state.value)
                event(last, StartupState.LOGIN_STATE)
                self.sleep(
                    min(
                        self.poll_interval,
                        max(0.0, deadline - self.monotonic()),
                    )
                )
                continue

            if not overlay_phase_started:
                append_path(StartupState.OVERLAY_RESOLUTION.value)
                overlay_phase_started = True
            last = classify_startup_frame(frame)

            if last.overlay_type is OverlayType.HOME_READY:
                home_frames = home_frames + 1 if last.page_fingerprint == home_fingerprint else 1
                home_fingerprint = last.page_fingerprint
                if home_frames >= 2:
                    append_path(StartupState.HOME_READY.value)
                    event(last, StartupState.HOME_READY, postcondition="two_consistent_home_frames")
                    return finish(StartupState.HOME_READY, "PASS", "home_ready_confirmed")
                append_path("HOME_CANDIDATE")
                event(last, StartupState.WAITING_UI, postcondition="home_frame_1_of_2")
                self.sleep(min(self.poll_interval, max(0.0, deadline - self.monotonic())))
                continue

            home_frames = 0
            home_fingerprint = ""
            if last.overlay_type in {
                OverlayType.CONSUMABLE_CONFIRMATION,
                OverlayType.PURCHASE_CONFIRMATION,
                OverlayType.ACCOUNT_SECURITY,
                OverlayType.READ_ONLY_INFORMATION,
            } or (last.overlay_type is OverlayType.REWARD_RELATED and last.action is None):
                append_path(StartupState.BLOCKED.value)
                event(last, StartupState.BLOCKED)
                return finish(StartupState.BLOCKED, "BLOCKED", last.reason)

            if last.action is not None:
                append_path(StartupState.SAFE_OVERLAY.value)
                append_path(StartupState.DISMISS_PENDING.value)
                event(last, StartupState.DISMISS_PENDING)
                try:
                    allowed = self.tap(
                        last.action.point,
                        intent=ActionIntent(
                            "dialog_cancel", "cancel",
                            f"{self.correlation_id}:{last.action.evidence_id}",
                        ),
                    )
                except (PermissionError, RuntimeError) as error:
                    append_path(StartupState.BLOCKED.value)
                    event(last, StartupState.BLOCKED, guard_result=type(error).__name__)
                    return finish(StartupState.BLOCKED, "BLOCKED", "overlay_guard_denied")
                if allowed is False:
                    append_path(StartupState.BLOCKED.value)
                    event(last, StartupState.BLOCKED, guard_result="DENIED")
                    return finish(StartupState.BLOCKED, "BLOCKED", "overlay_guard_denied")
                event(last, StartupState.DISMISS_PENDING, action_taken=True, guard_result="ALLOWED")
                self.sleep(min(self.poll_interval, max(0.0, deadline - self.monotonic())))
                if attempts >= self.max_attempts or self.monotonic() >= deadline:
                    break
                attempts += 1
                post = classify_startup_frame(self.frame_provider())
                event(post, StartupState.DISMISS_PENDING, postcondition="overlay_reclassified")
                if post.overlay_type is last.overlay_type and post.page_fingerprint == last.page_fingerprint:
                    last = post
                    append_path(StartupState.FAILED.value)
                    return finish(StartupState.FAILED, "FAILED", "overlay_persisted_after_dismiss")
                last = post
                if post.overlay_type is OverlayType.HOME_READY:
                    home_frames = 1
                    home_fingerprint = post.page_fingerprint
                    append_path("HOME_CANDIDATE")
                continue

            append_path(StartupState.WAITING_UI.value)
            event(last, StartupState.WAITING_UI)
            self.sleep(min(self.poll_interval, max(0.0, deadline - self.monotonic())))

        append_path(StartupState.TIMEOUT.value)
        return finish(StartupState.TIMEOUT, "BLOCKED", "startup_deadline_or_attempt_limit")


__all__ = [
    "OverlayAction",
    "OverlayObservation",
    "OverlayType",
    "StartupResolutionResult",
    "StartupResolver",
    "StartupState",
    "StartupTraceEvent",
    "classify_startup_frame",
]
