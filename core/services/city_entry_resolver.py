"""Bounded, evidence-driven HOME to CITY_DETAIL navigation."""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from enum import Enum
from typing import Callable, Iterable

from core.services.read_only_policy import ActionIntent


class CityEntryState(str, Enum):
    HOME_READY = "HOME_READY"
    CITY_ENTRY_VISIBLE = "CITY_ENTRY_VISIBLE"
    CITY_ENTRY_CONFIRMING = "CITY_ENTRY_CONFIRMING"
    CITY_TRANSITION = "CITY_TRANSITION"
    CITY_DETAIL = "CITY_DETAIL"
    UNKNOWN = "UNKNOWN"
    TIMEOUT = "TIMEOUT"
    FAILED = "FAILED"


@dataclass(frozen=True)
class CityEntryObservation:
    visible: bool
    anchor_bbox: tuple[int, int, int, int] | None
    confidence: str
    page_fingerprint: str
    screenshot_hash: str
    capture_id: str
    state: CityEntryState
    evidence: tuple[str, ...]
    back_anchor_bbox: tuple[int, int, int, int] | None
    text_count: int
    reason: str
    timestamp: str


@dataclass(frozen=True)
class CityEntryExecution:
    allowed: bool
    executed: bool
    guard_result: str


@dataclass(frozen=True)
class CityEntryTraceEvent:
    correlation_id: str
    timestamp: str
    state: str
    page_fingerprint: str
    screenshot_hash: str
    capture_id: str
    action: str
    guard_result: str
    postcondition: str
    status: str
    reason: str
    attempt_count: int


@dataclass(frozen=True)
class CityEntryResult:
    state: CityEntryState
    status: str
    reason: str
    attempt_count: int
    action_count: int
    action_allowed: bool
    action_executed: bool
    screenshot_hash: str
    page_fingerprint: str
    trace: tuple[CityEntryTraceEvent, ...]
    correlation_id: str
    irreversible_actions: int = 0

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["state"] = self.state.value
        payload["trace"] = [asdict(event) for event in self.trace]
        return payload


def _items(frame: object) -> list[dict]:
    if isinstance(frame, list):
        return list(frame)
    if hasattr(frame, "ocr"):
        return list(frame.ocr())
    return []


def _text(item: dict) -> str:
    return str(item.get("text", "")).replace(" ", "")


def _bbox(item: dict) -> tuple[int, int, int, int] | None:
    points = item.get("position") or ()
    if len(points) < 3:
        return None
    xs = [int(point[0]) for point in points]
    ys = [int(point[1]) for point in points]
    return min(xs), min(ys), max(xs), max(ys)


def _trusted_candidates(
    items: Iterable[dict],
    predicate: Callable[[str], bool],
) -> list[tuple[int, int, int, int]]:
    candidates = []
    for item in items:
        if not predicate(_text(item)):
            continue
        if float(item.get("score", 1.0)) < 0.8:
            continue
        bounds = _bbox(item)
        if bounds is not None:
            candidates.append(bounds)
    return candidates


def _contains(texts: list[str], markers: Iterable[str]) -> bool:
    return any(marker in text for marker in markers for text in texts)


def _frame_hash(frame: object) -> str:
    supplied = str(getattr(frame, "raw_frame_hash", ""))
    if supplied:
        return supplied
    image = getattr(frame, "image", None)
    return hashlib.sha256(image.tobytes()).hexdigest() if image is not None else ""


def _center(bounds: tuple[int, int, int, int]) -> tuple[int, int]:
    x1, y1, x2, y2 = bounds
    return int(round((x1 + x2) / 2)), int(round((y1 + y2) / 2))


def observe_city_entry_frame(
    frame: object,
    *,
    now: Callable[[], datetime] = lambda: datetime.now().astimezone(),
) -> CityEntryObservation:
    items = _items(frame)
    texts = [_text(item) for item in items]
    joined = "|".join(texts)
    screenshot_hash = _frame_hash(frame)
    capture_id = str(
        getattr(frame, "source_capture_id", "")
        or getattr(frame, "capture_id", "")
    )
    captured_at = getattr(frame, "captured_at", None) or now()

    entry_candidates = _trusted_candidates(
        items, lambda text: "访问城市" in text
    )
    back_candidates = _trusted_candidates(
        items, lambda text: text in {"返回", "退出城市"}
    )
    anchor_bbox = entry_candidates[0] if len(entry_candidates) == 1 else None
    back_anchor_bbox = back_candidates[0] if len(back_candidates) == 1 else None

    dangerous_reason = ""
    if _contains(texts, ("请输入密码", "账号密码", "验证码", "授权登录")):
        dangerous_reason = "LOGIN_REQUIRED"
    elif _contains(texts, ("领取奖励", "点击领取", "立即领取", "签到领取")):
        dangerous_reason = "REWARD_PAGE_BLOCKED"
    elif _contains(texts, ("确认购买", "确认支付", "充值", "支付订单")):
        dangerous_reason = "PURCHASE_PAGE_BLOCKED"
    elif _contains(texts, ("使用道具", "确认消耗", "使用便当", "使用气泡水")):
        dangerous_reason = "CONSUMABLE_PAGE_BLOCKED"
    elif _contains(texts, ("触碰空白区域退出", "未知弹窗")):
        dangerous_reason = "UNKNOWN_OVERLAY_BLOCKED"
    elif _contains(texts, ("你想要什么", "什么都行", "NPC对话")):
        dangerous_reason = "NPC_DIALOG_BLOCKED"

    home_evidence = tuple(
        marker
        for marker in ("访问城市", "作战终端", "启程", "整备列车")
        if marker in joined
    )
    city_checks = (
        (
            "city_title",
            _contains(
                texts,
                ("城市详情", "当前城市", "城市发展度", "城市等级"),
            ),
        ),
        (
            "city_functions",
            _contains(
                texts,
                ("城市设施", "城市手册", "交易所", "商会", "休息区"),
            ),
        ),
        (
            "npc_area",
            _contains(texts, ("交流", "NPC区域", "进入设施", "城市NPC")),
        ),
        (
            "city_map",
            _contains(texts, ("城市地图", "地图区域")),
        ),
    )
    city_evidence = tuple(name for name, present in city_checks if present)

    if dangerous_reason:
        state = CityEntryState.UNKNOWN
        reason = dangerous_reason
        evidence = (dangerous_reason,)
        confidence = "UNKNOWN"
    elif len(entry_candidates) > 1:
        state = CityEntryState.UNKNOWN
        reason = "city_entry_anchor_untrusted"
        evidence = ("multiple_city_entry_anchors",)
        confidence = "UNKNOWN"
    elif len(city_evidence) >= 2:
        state = CityEntryState.CITY_DETAIL
        reason = "city_detail_evidence_confirmed"
        evidence = city_evidence
        confidence = "HIGH"
    elif len(home_evidence) >= 2 and anchor_bbox is not None:
        state = CityEntryState.CITY_ENTRY_VISIBLE
        reason = "home_ready_city_entry_visible"
        evidence = home_evidence
        confidence = "HIGH"
    elif len(home_evidence) >= 2:
        state = CityEntryState.HOME_READY
        reason = "home_ready_without_city_entry"
        evidence = home_evidence
        confidence = "HIGH"
    elif not texts:
        state = CityEntryState.CITY_TRANSITION
        reason = "city_transition_without_committed_page"
        evidence = ("blank_transition",)
        confidence = "LOW"
    else:
        state = CityEntryState.UNKNOWN
        reason = "city_page_evidence_unknown"
        evidence = ()
        confidence = "UNKNOWN"

    fingerprint = hashlib.sha256(
        json.dumps(
            {"state": state.value, "evidence": sorted(set(evidence))},
            ensure_ascii=False,
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()[:24]
    return CityEntryObservation(
        visible=anchor_bbox is not None,
        anchor_bbox=anchor_bbox,
        confidence=confidence,
        page_fingerprint=fingerprint,
        screenshot_hash=screenshot_hash,
        capture_id=capture_id,
        state=state,
        evidence=tuple(evidence),
        back_anchor_bbox=back_anchor_bbox,
        text_count=len(texts),
        reason=reason,
        timestamp=captured_at.isoformat(timespec="milliseconds"),
    )


class CityEntryResolver:
    _BLOCKING_REASONS = frozenset(
        {
            "LOGIN_REQUIRED",
            "REWARD_PAGE_BLOCKED",
            "PURCHASE_PAGE_BLOCKED",
            "CONSUMABLE_PAGE_BLOCKED",
            "UNKNOWN_OVERLAY_BLOCKED",
            "NPC_DIALOG_BLOCKED",
        }
    )

    def __init__(
        self,
        *,
        frame_provider: Callable[[], object],
        tap: Callable[..., object],
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = lambda: datetime.now().astimezone(),
        timeout: float = 30.0,
        max_attempts: int = 10,
        stall_frames: int = 5,
        poll_interval: float = 0.5,
        correlation_id: str | None = None,
        initial_observation: CityEntryObservation | None = None,
    ):
        self.frame_provider = frame_provider
        self.tap = tap
        self.sleep = sleep
        self.monotonic = monotonic
        self.now = now
        self.timeout = max(0.0, float(timeout))
        self.max_attempts = max(1, int(max_attempts))
        self.stall_frames = max(2, int(stall_frames))
        self.poll_interval = max(0.0, float(poll_interval))
        self.correlation_id = correlation_id or self.now().strftime(
            "CITYENTRY-%Y%m%d-%H%M%S"
        )
        self.initial_observation = initial_observation

    def _resolve_execution(self, raw: object) -> CityEntryExecution:
        if isinstance(raw, CityEntryExecution):
            return raw
        return CityEntryExecution(
            allowed=bool(raw),
            executed=bool(raw),
            guard_result="ALLOWED" if raw else "DENIED",
        )

    def _run(
        self,
        *,
        mode: str,
    ) -> CityEntryResult:
        deadline = self.monotonic() + self.timeout
        attempts = 0
        action_count = 0
        action_allowed = False
        action_executed = False
        trace: list[CityEntryTraceEvent] = []
        last: CityEntryObservation | None = self.initial_observation

        def record(
            observation: CityEntryObservation,
            *,
            state: CityEntryState | None = None,
            action: str = "OBSERVE_ONLY",
            guard_result: str = "NOT_REQUESTED",
            postcondition: str = "NOT_CHECKED",
            status: str = "PENDING",
            reason: str | None = None,
        ) -> None:
            trace.append(
                CityEntryTraceEvent(
                    correlation_id=self.correlation_id,
                    timestamp=observation.timestamp,
                    state=(state or observation.state).value,
                    page_fingerprint=observation.page_fingerprint,
                    screenshot_hash=observation.screenshot_hash,
                    capture_id=observation.capture_id,
                    action=action,
                    guard_result=guard_result,
                    postcondition=postcondition,
                    status=status,
                    reason=reason or observation.reason,
                    attempt_count=attempts,
                )
            )

        def finish(
            state: CityEntryState,
            status: str,
            reason: str,
        ) -> CityEntryResult:
            return CityEntryResult(
                state=state,
                status=status,
                reason=reason,
                attempt_count=attempts,
                action_count=action_count,
                action_allowed=action_allowed,
                action_executed=action_executed,
                screenshot_hash=last.screenshot_hash if last else "",
                page_fingerprint=last.page_fingerprint if last else "",
                trace=tuple(trace),
                correlation_id=self.correlation_id,
            )

        if self.monotonic() >= deadline:
            return finish(CityEntryState.TIMEOUT, "BLOCKED", "navigation_timeout")
        if last is None:
            last = observe_city_entry_frame(self.frame_provider(), now=self.now)
        attempts += 1
        record(last)

        if last.reason in self._BLOCKING_REASONS:
            return finish(CityEntryState.UNKNOWN, "BLOCKED", last.reason)

        if mode == "enter":
            if last.state is CityEntryState.HOME_READY:
                return finish(
                    CityEntryState.HOME_READY,
                    "BLOCKED",
                    "city_entry_anchor_missing",
                )
            if last.state is not CityEntryState.CITY_ENTRY_VISIBLE:
                return finish(
                    CityEntryState.UNKNOWN,
                    "BLOCKED",
                    last.reason,
                )
            bounds = last.anchor_bbox
            action_key = "city_entry_navigation"
            target = "city_entry"
            expected = "CITY_DETAIL"
        else:
            if last.state is not CityEntryState.CITY_DETAIL:
                return finish(
                    CityEntryState.UNKNOWN,
                    "BLOCKED",
                    "safe_back_requires_city_detail",
                )
            bounds = last.back_anchor_bbox
            if bounds is None:
                return finish(
                    CityEntryState.CITY_DETAIL,
                    "BLOCKED",
                    "back_anchor_missing",
                )
            action_key = "page_back"
            target = "top_left_back"
            expected = "HOME_READY"

        action_count += 1
        try:
            execution = self._resolve_execution(
                self.tap(
                    _center(bounds),
                    intent=ActionIntent(
                        action_key,
                        target,
                        f"{self.correlation_id}:{mode}",
                    ),
                )
            )
        except (PermissionError, RuntimeError) as error:
            execution = CityEntryExecution(False, False, type(error).__name__)
        action_allowed = execution.allowed
        action_executed = execution.executed
        record(
            last,
            state=CityEntryState.CITY_ENTRY_CONFIRMING,
            action=action_key,
            guard_result=execution.guard_result,
            postcondition=expected,
            reason=f"{mode}_action_requested",
        )
        if not execution.executed:
            reason = (
                "guard_denied_city_entry"
                if mode == "enter"
                else "guard_denied_page_back"
            )
            return finish(CityEntryState.FAILED, "BLOCKED", reason)

        last_hash = last.screenshot_hash
        last_fingerprint = last.page_fingerprint
        stable = 0
        for _ in range(max(0, self.max_attempts - attempts)):
            if self.monotonic() >= deadline:
                break
            self.sleep(
                min(
                    self.poll_interval,
                    max(0.0, deadline - self.monotonic()),
                )
            )
            if self.monotonic() >= deadline:
                break
            last = observe_city_entry_frame(self.frame_provider(), now=self.now)
            attempts += 1
            same_frame = bool(
                last.screenshot_hash and last.screenshot_hash == last_hash
            )
            same_page = last.page_fingerprint == last_fingerprint
            stable = stable + 1 if (same_frame or same_page) else 1
            last_hash = last.screenshot_hash
            last_fingerprint = last.page_fingerprint

            if last.reason in self._BLOCKING_REASONS:
                record(last, status="BLOCKED", reason=last.reason)
                return finish(CityEntryState.UNKNOWN, "BLOCKED", last.reason)
            if mode == "enter" and last.state is CityEntryState.CITY_DETAIL:
                record(
                    last,
                    postcondition="CITY_DETAIL",
                    status="PASS",
                    reason="city_detail_verified",
                )
                return finish(
                    CityEntryState.CITY_DETAIL,
                    "PASS",
                    "city_detail_verified",
                )
            if mode == "back" and last.state in {
                CityEntryState.HOME_READY,
                CityEntryState.CITY_ENTRY_VISIBLE,
            }:
                record(
                    last,
                    state=CityEntryState.HOME_READY,
                    postcondition="HOME_READY",
                    status="PASS",
                    reason="home_ready_restored",
                )
                return finish(
                    CityEntryState.HOME_READY,
                    "PASS",
                    "home_ready_restored",
                )
            if last.state is CityEntryState.CITY_TRANSITION:
                record(
                    last,
                    postcondition=expected,
                    reason="navigation_transition_pending",
                )
            elif last.state is CityEntryState.UNKNOWN:
                record(
                    last,
                    postcondition=expected,
                    status="FAILED",
                    reason="NAVIGATION_POSTCONDITION_FAILED",
                )
                return finish(
                    CityEntryState.FAILED,
                    "FAILED",
                    "NAVIGATION_POSTCONDITION_FAILED",
                )
            else:
                record(last, postcondition=expected)

            if stable >= self.stall_frames:
                record(
                    last,
                    postcondition=expected,
                    status="FAILED",
                    reason="NAVIGATION_STALLED",
                )
                return finish(
                    CityEntryState.FAILED,
                    "FAILED",
                    "NAVIGATION_STALLED",
                )

        if self.monotonic() >= deadline:
            return finish(CityEntryState.TIMEOUT, "BLOCKED", "navigation_timeout")
        return finish(
            CityEntryState.FAILED,
            "FAILED",
            "NAVIGATION_POSTCONDITION_FAILED",
        )

    def enter_city(self) -> CityEntryResult:
        return self._run(mode="enter")

    def safe_back_to_home(self) -> CityEntryResult:
        return self._run(mode="back")


__all__ = [
    "CityEntryExecution",
    "CityEntryObservation",
    "CityEntryResolver",
    "CityEntryResult",
    "CityEntryState",
    "CityEntryTraceEvent",
    "observe_city_entry_frame",
]
