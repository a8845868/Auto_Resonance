"""Bounded, evidence-driven city and exchange read-only navigation."""

from __future__ import annotations

import hashlib
import json
import math
import time
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Callable, Iterable

from core.control.nemu_capture import (
    CaptureSessionRecoveryResult,
    NemuCaptureError,
)
from core.services.capture_recovery import (
    CaptureRecoveryPolicy,
    DEFAULT_CAPTURE_RECOVERY_POLICY,
)
from core.services.dispatch_outcome import (
    DispatchStatus,
    physical_input_count_from_dispatch_error,
    receipt_from_dispatch_error,
)
from core.services.navigation_evidence import (
    CoordinateChain,
    DerivedObservationProvenanceContract,
    NavigationAttemptEvidence,
    record_navigation_attempt,
)
from core.services.read_only_policy import ActionIntent
from core.services.navigation_parent_control import (
    confirm_fresh_parent_control,
    resolve_navigation_parent_control,
)
from core.services.city_entry_postcondition import CITY_ENTRY_POSTCONDITION_POLICY
from core.services.runtime_navigation_kernel import UiState, normalize_legacy_state
from core.services.runtime_fault_telemetry import (
    RuntimeFaultEvent,
    record_runtime_fault,
)


CITY_ENTRY_TRANSITION_TIMEOUT_SECONDS = 30.0
CITY_ENTRY_OBSERVATION_INTERVAL_SECONDS = 0.5
CITY_ENTRY_MINIMUM_GRACE_SECONDS = 2.0
CITY_PARENT_CONTROL_FRESH_OBSERVATION_LIMIT = 2


class CityNavigationState(str, Enum):
    HOME_READY = "HOME_READY"
    HOME_CITY_ENTRY_CONTROL_VISIBLE = "HOME_CITY_ENTRY_CONTROL_VISIBLE"
    CITY_ENTRY_VISIBLE = "HOME_CITY_ENTRY_CONTROL_VISIBLE"
    CITY_TRANSITION = "CITY_TRANSITION"
    CITY_MAP = "CITY_MAP"
    CITY_DETAIL_VISIBLE = "CITY_DETAIL_VISIBLE"
    CITY_DETAIL = "CITY_DETAIL_VISIBLE"
    NPC_DIALOG = "NPC_DIALOG"
    EXCHANGE_NPC_VISIBLE = "EXCHANGE_NPC_VISIBLE"
    EXCHANGE_MENU = "EXCHANGE_MENU"
    EXCHANGE_BUY = "EXCHANGE_BUY"
    EXCHANGE_SELL = "EXCHANGE_SELL"
    UNKNOWN = "UNKNOWN"
    TIMEOUT = "TIMEOUT"
    FAILED = "FAILED"


@dataclass(frozen=True)
class CityEntryObservation:
    visible: bool
    anchor_bbox: tuple[int, int, int, int] | None
    confidence: str
    page_fingerprint: str
    source_capture_id: str
    candidate_count: int = 0
    candidate_score: float | None = None


@dataclass(frozen=True)
class CityPageObservation:
    state: CityNavigationState
    page_fingerprint: str
    screenshot_hash: str
    confidence: str
    timestamp: str
    source_capture_id: str
    city_entry: CityEntryObservation
    exchange_anchor_bbox: tuple[int, int, int, int] | None
    buy_anchor_bbox: tuple[int, int, int, int] | None
    sell_anchor_bbox: tuple[int, int, int, int] | None
    evidence: tuple[str, ...]
    text_count: int
    reason: str
    foreign_page_reason: str | None = None

    def to_ui_state(self) -> UiState:
        return normalize_legacy_state(
            self.state.value,
            phase="CITY_NAVIGATION",
            confidence=self.confidence,
            evidence=self.evidence,
            frame_hash=self.screenshot_hash,
            capture_id=self.source_capture_id,
        )


@dataclass(frozen=True)
class CityNavigationEvent:
    correlation_id: str
    timestamp: str
    state: CityNavigationState
    confidence: str
    page_fingerprint: str
    screenshot_hash: str
    action: str
    guard_result: str
    postcondition: str
    status: str
    reason: str
    elapsed_since_dispatch_seconds: float | None = None
    transition_classification: str = "NOT_APPLICABLE"


@dataclass(frozen=True)
class CityNavigationResult:
    state: CityNavigationState
    status: str
    reason: str
    attempt_count: int
    trace: tuple[CityNavigationEvent, ...]
    irreversible_actions: int = 0
    entry_opened: bool = False
    station_confirmed: bool = False
    station_id: str | None = None
    terminal: bool = True
    evidence_attempt_id: str = ""
    evidence: NavigationAttemptEvidence | None = None
    dispatch_count: int = 0
    post_observation_count: int = 0
    transition_elapsed_seconds: float = 0.0
    transition_result: str = "NOT_RUN"
    last_observed_state: str = ""
    post_canonical_leaf_state: str = ""
    post_context_state: str = ""
    city_entry_verified: bool = False
    exact_expected_leaf_match: bool = False
    gate_postcondition_policy_id: str = ""
    physical_input_count: int = 0
    capture_failure: NemuCaptureError | None = None
    capture_failure_stage: str = ""
    session_recovery_count: int = 0
    same_action_retry: int = 0
    retry_exhausted: bool = False
    postcondition_observation_failed: bool = False
    stale_frame_reused: bool = False
    runtime_fault_recorded: bool = False
    session_lifecycle_conflict: str = "NOT_PROVEN"
    session_generation_before_recovery: int | None = None
    session_generation_after_recovery: int | None = None


class KnownNavigationBlock(RuntimeError):
    """Typed bridge from navigation to a task-level safe block."""

    def __init__(self, result: CityNavigationResult) -> None:
        self.result = result
        super().__init__(result.reason)


@dataclass(frozen=True)
class StationDetectionResult:
    result: str
    station_id: str | None
    confidence: str
    evidence_ids: tuple[str, ...]
    candidate_count: int
    reason: str


def detect_current_station(
    frame: object,
    station_ids: Iterable[str],
) -> StationDetectionResult:
    """Resolve exactly one known station from full-frame OCR without defaults."""

    try:
        texts = tuple(_text(item) for item in _items(frame) if _text(item))
    except Exception as error:  # noqa: BLE001 - detector failure is explicit
        return StationDetectionResult(
            "ERROR", None, "UNKNOWN", ("station_ocr_error",), 0,
            f"station_detector_error:{type(error).__name__}",
        )
    known = tuple(dict.fromkeys(str(value).replace(" ", "") for value in station_ids if value))
    exact = {station for station in known if station in texts}
    matches = exact or {
        station
        for station in known
        if any(station in text for text in texts)
    }
    if not matches:
        return StationDetectionResult(
            "NO_MATCH", None, "UNKNOWN", ("station_name_absent",), 0,
            "station_detector_no_match",
        )
    if len(matches) != 1:
        return StationDetectionResult(
            "AMBIGUOUS", None, "UNKNOWN", ("multiple_station_names",),
            len(matches), "station_detector_ambiguous",
        )
    station_id = next(iter(matches))
    evidence_id = hashlib.sha256(station_id.encode("utf-8")).hexdigest()[:16]
    return StationDetectionResult(
        "PASS", station_id, "HIGH", (f"station_name_sha256:{evidence_id}",),
        1, "station_confirmed",
    )


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


def _unique_bbox(items: Iterable[dict], predicate: Callable[[str], bool]):
    candidates = [
        bounds for item in items
        if predicate(_text(item)) and (bounds := _bbox(item)) is not None
    ]
    return candidates[0] if len(candidates) == 1 else None


def _center(bounds: tuple[int, int, int, int]) -> tuple[int, int]:
    x1, y1, x2, y2 = bounds
    return int(round((x1 + x2) / 2)), int(round((y1 + y2) / 2))


def _frame_hash(frame: object) -> str:
    supplied = str(getattr(frame, "raw_frame_hash", ""))
    if supplied:
        return supplied
    image = getattr(frame, "image", None)
    return hashlib.sha256(image.tobytes()).hexdigest() if image is not None else ""


def _contains(texts: list[str], markers: Iterable[str]) -> bool:
    return any(marker in text for marker in markers for text in texts)


def _exchange_evidence(texts: list[str], side: str) -> tuple[str, ...]:
    if side == "BUY":
        checks = (
            ("buy_title", _contains(texts, ("预计买入", "买入标题")) or any(text == "买入" for text in texts)),
            ("goods_list", _contains(texts, ("商品列表", "交易品列表", "交易品", "载货量"))),
            ("buy_button", _contains(texts, ("买入按钮", "全部买入")) or any(text == "买入" for text in texts)),
            ("buy_price", _contains(texts, ("购买价格", "买入价格", "买入总价", "含税"))),
        )
    else:
        checks = (
            ("sell_title", _contains(texts, ("预计卖出", "卖出标题")) or any(text == "卖出" for text in texts)),
            ("goods_area", _contains(texts, ("商品出售区域", "交易品列表", "交易品", "载货量"))),
            ("sell_button", _contains(texts, ("卖出按钮", "全部卖出")) or any(text == "卖出" for text in texts)),
            ("sell_price", _contains(texts, ("出售价格", "卖出价格", "卖出总价", "含税"))),
        )
    return tuple(name for name, present in checks if present)


def observe_city_frame(
    frame: object,
    *,
    now: Callable[[], datetime] = lambda: datetime.now().astimezone(),
) -> CityPageObservation:
    items = _items(frame)
    texts = [_text(item) for item in items]
    joined = "|".join(texts)
    screenshot_hash = _frame_hash(frame)
    source_capture_id = str(getattr(frame, "source_capture_id", ""))
    captured_at = getattr(frame, "captured_at", None) or now()
    timestamp = captured_at.isoformat(timespec="milliseconds")
    evidence: list[str] = []

    city_entry_candidates = [
        (bounds, float(item.get("score", 1.0)))
        for item in items
        if "访问城市" in _text(item) and (bounds := _bbox(item)) is not None
    ]
    city_entry_bbox = (
        city_entry_candidates[0][0] if len(city_entry_candidates) == 1 else None
    )
    exchange_bbox = _unique_bbox(items, lambda text: "交易所" in text)
    buy_bbox = _unique_bbox(items, lambda text: "我要买" in text)
    sell_bbox = _unique_bbox(items, lambda text: "我要卖" in text)

    buy_evidence = _exchange_evidence(texts, "BUY")
    sell_evidence = _exchange_evidence(texts, "SELL")
    menu_evidence = tuple(
        marker for marker in ("交易所", "你想要什么", "我要买", "我要卖", "交易品投资", "私人仓库")
        if marker in joined
    )
    home_evidence = tuple(
        marker for marker in ("访问城市", "作战终端", "启程", "整备列车")
        if marker in joined
    )
    city_title = _contains(texts, ("当前城市", "城市详情", "城市设施", "城市手册", "城市发展度", "CITY"))
    city_map = _contains(texts, ("城市设施", "城市手册", "交易所", "商会", "休息区"))
    npc_area = _contains(texts, ("NPC", "交流", "对话", "进入", "交易所"))
    city_categories = tuple(
        name for name, present in (
            ("city_title", city_title), ("city_map", city_map), ("npc_area", npc_area),
        ) if present
    )

    if len(buy_evidence) == 4 and len(sell_evidence) == 4:
        state = CityNavigationState.UNKNOWN
        reason = "conflicting_exchange_page_evidence"
        evidence.extend(("buy_sell_conflict",))
    elif len(buy_evidence) == 4:
        state = CityNavigationState.EXCHANGE_BUY
        reason = "buy_page_evidence_confirmed"
        evidence.extend(buy_evidence)
    elif len(sell_evidence) == 4:
        state = CityNavigationState.EXCHANGE_SELL
        reason = "sell_page_evidence_confirmed"
        evidence.extend(sell_evidence)
    elif len(menu_evidence) >= 3 and "我要买" in joined and "我要卖" in joined:
        state = CityNavigationState.EXCHANGE_MENU
        reason = "exchange_menu_evidence_confirmed"
        evidence.extend(menu_evidence)
    elif _contains(texts, ("你想要什么", "研究报告", "什么都行")):
        state = CityNavigationState.NPC_DIALOG
        reason = "npc_dialog_evidence_confirmed"
        evidence.append("npc_dialog")
    elif city_title and _contains(texts, ("城市地图", "地图")):
        state = CityNavigationState.CITY_MAP
        reason = "city_map_evidence_confirmed"
        evidence.extend(("city_title", "map_element"))
    elif len(city_categories) >= 2 and len(home_evidence) >= 2:
        state = CityNavigationState.UNKNOWN
        reason = "conflicting_home_city_page_evidence"
        evidence.extend(("home_city_conflict",))
    elif len(city_categories) >= 2 and exchange_bbox is not None:
        state = CityNavigationState.EXCHANGE_NPC_VISIBLE
        reason = "city_exchange_anchor_confirmed"
        evidence.extend(city_categories)
    elif len(city_categories) >= 2:
        state = CityNavigationState.CITY_DETAIL
        reason = "city_detail_evidence_confirmed"
        evidence.extend(city_categories)
    elif len(home_evidence) >= 2 and city_entry_bbox is not None:
        state = CityNavigationState.CITY_ENTRY_VISIBLE
        reason = "home_ready_city_entry_visible"
        evidence.extend(home_evidence)
    elif len(home_evidence) >= 2:
        state = CityNavigationState.HOME_READY
        reason = "home_ready_without_unique_city_entry"
        evidence.extend(home_evidence)
    else:
        state = CityNavigationState.UNKNOWN
        reason = "page_evidence_unknown"

    fingerprint = hashlib.sha256(
        json.dumps(
            {"state": state.value, "evidence": sorted(set(evidence))},
            ensure_ascii=False,
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()[:24]
    confidence = "HIGH" if state is not CityNavigationState.UNKNOWN else "UNKNOWN"
    city_entry = CityEntryObservation(
        visible=city_entry_bbox is not None,
        anchor_bbox=city_entry_bbox,
        confidence="HIGH" if city_entry_bbox is not None else "UNKNOWN",
        page_fingerprint=fingerprint,
        source_capture_id=source_capture_id,
        candidate_count=len(city_entry_candidates),
        candidate_score=(
            city_entry_candidates[0][1]
            if len(city_entry_candidates) == 1 else None
        ),
    )
    return CityPageObservation(
        state=state,
        page_fingerprint=fingerprint,
        screenshot_hash=screenshot_hash,
        confidence=confidence,
        timestamp=timestamp,
        source_capture_id=source_capture_id,
        city_entry=city_entry,
        exchange_anchor_bbox=exchange_bbox,
        buy_anchor_bbox=buy_bbox,
        sell_anchor_bbox=sell_bbox,
        evidence=tuple(evidence),
        text_count=len(texts),
        reason=reason,
        foreign_page_reason=_explicit_foreign_page_reason(texts),
    )


def _explicit_foreign_page_reason(texts: Iterable[str]) -> str | None:
    """Return a committed non-city page only from multiple specific cues."""

    joined = "|".join(str(text) for text in texts)
    cue_sets = (
        (
            "inventory",
            ("装备", "载货", "素材", "私人仓库", "建材"),
            3,
        ),
        (
            "action_summary",
            ("行动汇总", "任务结果", "队列结果", "已完成任务"),
            2,
        ),
        (
            "external_browser",
            ("http://", "https://", "Chrome", "浏览器", "网页"),
            2,
        ),
        (
            "login",
            ("点击屏幕进入游戏", "账号登录", "验证码", "用户协议"),
            2,
        ),
        (
            "announcement",
            ("资讯", "公告", "触碰空白区域退出"),
            2,
        ),
    )
    for page_name, markers, required in cue_sets:
        if sum(marker in joined for marker in markers) >= required:
            return page_name
    return None


class CityNavigationAdapter:
    def __init__(
        self,
        *,
        frame_provider: Callable[[], object],
        tap: Callable[..., object],
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = lambda: datetime.now().astimezone(),
        timeout: float = CITY_ENTRY_TRANSITION_TIMEOUT_SECONDS,
        max_attempts: int = 10,
        stall_frames: int = 5,
        cancellation: Callable[[], bool] | None = None,
        correlation_id: str | None = None,
        geometry_provider: Callable[[], object] | None = None,
        evidence_recorder: Callable[[NavigationAttemptEvidence], object] | None = None,
        station_ids: Iterable[str] = (),
        station_detector: Callable[[object, Iterable[str]], StationDetectionResult] = detect_current_station,
        require_station_confirmation: bool = False,
        dispatch_backend: str = "device_control",
        city_entry_transition_timeout_seconds: float | None = None,
        city_entry_observation_interval_seconds: float = CITY_ENTRY_OBSERVATION_INTERVAL_SECONDS,
        city_entry_minimum_grace_seconds: float = CITY_ENTRY_MINIMUM_GRACE_SECONDS,
        city_parent_control_fresh_observation_limit: int = CITY_PARENT_CONTROL_FRESH_OBSERVATION_LIMIT,
        session_recoverer: Callable[[], CaptureSessionRecoveryResult] | None = None,
        capture_recovery_policy: CaptureRecoveryPolicy = DEFAULT_CAPTURE_RECOVERY_POLICY,
        runtime_fault_recorder: Callable[[RuntimeFaultEvent], object] | None = record_runtime_fault,
    ):
        self.frame_provider = frame_provider
        self.tap = tap
        self.sleep = sleep
        self.monotonic = monotonic
        self.now = now
        self.timeout = max(0.0, float(timeout))
        self.max_attempts = max(1, int(max_attempts))
        self.stall_frames = max(2, int(stall_frames))
        self.cancellation = cancellation or (lambda: False)
        self.correlation_id = correlation_id or now().strftime("CITYNAV-%Y%m%d-%H%M%S")
        self.geometry_provider = geometry_provider
        self.evidence_recorder = evidence_recorder or record_navigation_attempt
        self.station_ids = tuple(station_ids)
        self.station_detector = station_detector
        self.require_station_confirmation = bool(require_station_confirmation)
        self.dispatch_backend = str(dispatch_backend)
        self.city_entry_transition_timeout_seconds = max(
            0.0,
            float(
                self.timeout
                if city_entry_transition_timeout_seconds is None
                else city_entry_transition_timeout_seconds
            ),
        )
        self.city_entry_observation_interval_seconds = max(
            0.001, float(city_entry_observation_interval_seconds)
        )
        self.city_entry_minimum_grace_seconds = max(
            0.0, float(city_entry_minimum_grace_seconds)
        )
        self.city_parent_control_fresh_observation_limit = max(
            1, int(city_parent_control_fresh_observation_limit)
        )
        self.session_recoverer = session_recoverer
        self.capture_recovery_policy = capture_recovery_policy
        self.runtime_fault_recorder = runtime_fault_recorder

    def enter_city(self) -> CityNavigationResult:
        deadline = self.monotonic() + self.timeout
        attempts = 0
        trace: list[CityNavigationEvent] = []
        evidence: NavigationAttemptEvidence | None = None
        evidence_recorded = False
        entry_opened = False
        station_confirmed = False
        station_id: str | None = None
        dispatch_count = 0
        post_observation_count = 0
        dispatch_started: float | None = None
        last_observed_state = ""
        post_canonical_leaf_state = ""
        post_context_state = ""
        city_entry_verified = False
        exact_expected_leaf_match = False
        gate_postcondition_policy_id = CITY_ENTRY_POSTCONDITION_POLICY.policy_id
        physical_input_count = 0
        capture_failure: NemuCaptureError | None = None
        capture_failure_stage = ""
        session_recovery_count = 0
        same_action_retry = 0
        retry_exhausted = False
        postcondition_observation_failed = False
        stale_frame_reused = False
        runtime_fault_recorded = False
        session_lifecycle_conflict = "NOT_PROVEN"
        session_generation_before_recovery: int | None = None
        session_generation_after_recovery: int | None = None

        def record(
            observation,
            action,
            guard,
            postcondition,
            status,
            reason,
            *,
            state=None,
            transition_classification="NOT_APPLICABLE",
        ):
            elapsed = (
                max(0.0, self.monotonic() - dispatch_started)
                if dispatch_started is not None
                else None
            )
            trace.append(CityNavigationEvent(
                correlation_id=self.correlation_id,
                timestamp=observation.timestamp,
                state=state or observation.state,
                confidence=observation.confidence,
                page_fingerprint=observation.page_fingerprint,
                screenshot_hash=observation.screenshot_hash,
                action=action,
                guard_result=guard,
                postcondition=postcondition,
                status=status,
                reason=reason,
                elapsed_since_dispatch_seconds=(
                    round(elapsed, 6) if elapsed is not None else None
                ),
                transition_classification=transition_classification,
            ))

        def persist_evidence() -> None:
            nonlocal evidence_recorded
            if evidence is not None and not evidence_recorded:
                self.evidence_recorder(evidence)
                evidence_recorded = True

        def finish(state, status, reason, *, transition_result=None):
            persist_evidence()
            elapsed = (
                max(0.0, self.monotonic() - dispatch_started)
                if dispatch_started is not None
                else 0.0
            )
            resolved_transition_result = transition_result
            if resolved_transition_result is None:
                if dispatch_started is None:
                    resolved_transition_result = "NOT_RUN"
                elif status == "PASS":
                    resolved_transition_result = "PASS"
                elif reason == "city_entry_cancelled":
                    resolved_transition_result = "CANCELLED"
                elif reason == "city_entry_postcondition_timeout":
                    resolved_transition_result = "TIMEOUT"
                else:
                    resolved_transition_result = "FAIL"
            return CityNavigationResult(
                state=state,
                status=status,
                reason=reason,
                attempt_count=attempts,
                trace=tuple(trace),
                irreversible_actions=0,
                entry_opened=entry_opened,
                station_confirmed=station_confirmed,
                station_id=station_id,
                terminal=True,
                evidence_attempt_id=evidence.attempt_id if evidence else "",
                evidence=evidence,
                dispatch_count=dispatch_count,
                post_observation_count=post_observation_count,
                transition_elapsed_seconds=round(elapsed, 6),
                transition_result=resolved_transition_result,
                last_observed_state=last_observed_state,
                post_canonical_leaf_state=post_canonical_leaf_state,
                post_context_state=post_context_state,
                city_entry_verified=city_entry_verified,
                exact_expected_leaf_match=exact_expected_leaf_match,
                gate_postcondition_policy_id=gate_postcondition_policy_id,
                physical_input_count=physical_input_count,
                capture_failure=capture_failure,
                capture_failure_stage=capture_failure_stage,
                session_recovery_count=session_recovery_count,
                same_action_retry=same_action_retry,
                retry_exhausted=retry_exhausted,
                postcondition_observation_failed=postcondition_observation_failed,
                stale_frame_reused=stale_frame_reused,
                runtime_fault_recorded=runtime_fault_recorded,
                session_lifecycle_conflict=session_lifecycle_conflict,
                session_generation_before_recovery=(
                    session_generation_before_recovery
                ),
                session_generation_after_recovery=(
                    session_generation_after_recovery
                ),
            )

        def record_capture_fault(
            failure: NemuCaptureError,
            *,
            recovery_attempted: bool,
            recovery_result: str,
        ) -> None:
            nonlocal runtime_fault_recorded
            if self.runtime_fault_recorder is None:
                return
            try:
                self.runtime_fault_recorder(RuntimeFaultEvent(
                    task_id="city_entry_navigation",
                    attempt_id=self.correlation_id,
                    backend=failure.backend,
                    session_generation=failure.session_generation,
                    native_return_code=failure.native_return_code,
                    failure_stage=failure.failure_stage,
                    recovery_attempted=recovery_attempted,
                    recovery_result=recovery_result,
                    capture_call_index=failure.capture_call_index,
                    session_lifecycle_conflict=(
                        failure.session_lifecycle_conflict
                    ),
                ))
                runtime_fault_recorded = True
            except OSError:
                runtime_fault_recorded = False

        def acquire_pre_dispatch():
            nonlocal attempts, capture_failure, capture_failure_stage
            nonlocal session_recovery_count, retry_exhausted
            nonlocal session_lifecycle_conflict
            nonlocal session_generation_before_recovery
            nonlocal session_generation_after_recovery

            while True:
                if self.cancellation():
                    return finish(
                        CityNavigationState.FAILED,
                        "BLOCKED",
                        "city_entry_cancelled",
                    )
                if self.monotonic() >= deadline:
                    return finish(
                        CityNavigationState.TIMEOUT,
                        "BLOCKED",
                        "navigation_deadline_or_attempt_limit",
                    )
                try:
                    planned_frame = self.frame_provider()
                except NemuCaptureError as error:
                    failure = error.with_failure_stage("INITIAL_CAPTURE")
                    recovery_attempted = False
                    recovery_result = "NOT_ALLOWED"
                    if (
                        self.session_recoverer is not None
                        and self.capture_recovery_policy.allows(
                            failure,
                            dispatch_count=dispatch_count,
                            recovery_count=session_recovery_count,
                        )
                    ):
                        recovery_attempted = True
                        recovered = self.session_recoverer()
                        session_recovery_count += 1
                        session_generation_before_recovery = (
                            recovered.previous_session_generation
                        )
                        session_generation_after_recovery = (
                            recovered.current_session_generation
                        )
                        recovery_result = recovered.reason
                        record_capture_fault(
                            failure,
                            recovery_attempted=True,
                            recovery_result=recovery_result,
                        )
                        if recovered.success:
                            continue
                    capture_failure = failure
                    capture_failure_stage = failure.failure_stage
                    session_lifecycle_conflict = (
                        failure.session_lifecycle_conflict
                    )
                    retry_exhausted = bool(
                        recovery_attempted
                        or session_recovery_count
                        >= self.capture_recovery_policy.max_pre_dispatch_session_recovery
                    )
                    if not recovery_attempted:
                        record_capture_fault(
                            failure,
                            recovery_attempted=False,
                            recovery_result=recovery_result,
                        )
                    return finish(
                        CityNavigationState.FAILED,
                        "BLOCKED_SAFETY",
                        "initial_capture_failed",
                    )

                before = observe_city_frame(planned_frame, now=self.now)
                attempts += 1
                if before.city_entry.candidate_count != 1:
                    record(before, "OBSERVE_CITY_ENTRY", "NOT_REQUESTED", "NOT_CHECKED", "BLOCKED", "city_entry_candidate_not_unique")
                    return finish(before.state, "BLOCKED", "city_entry_candidate_not_unique")
                if before.state is CityNavigationState.HOME_READY:
                    record(before, "OBSERVE_CITY_ENTRY", "NOT_REQUESTED", "NOT_CHECKED", "BLOCKED", "city_entry_anchor_missing")
                    return finish(CityNavigationState.HOME_READY, "BLOCKED", "city_entry_anchor_missing")
                if before.state is not CityNavigationState.CITY_ENTRY_VISIBLE:
                    record(before, "OBSERVE_CITY_ENTRY", "NOT_REQUESTED", "NOT_CHECKED", "BLOCKED", "unknown_page_blocks_action")
                    return finish(CityNavigationState.UNKNOWN, "BLOCKED", "unknown_page_blocks_action")

                bounds = before.city_entry.anchor_bbox
                if bounds is None:
                    return finish(CityNavigationState.HOME_READY, "BLOCKED", "city_entry_anchor_missing")
                planned_image = getattr(planned_frame, "image", planned_frame)
                initial_parent = resolve_navigation_parent_control(
                    planned_image,
                    semantic_id="visit_city",
                    anchor_bbox=bounds,
                    source_capture_id=before.source_capture_id,
                    source_frame_sha256=before.screenshot_hash,
                )
                if not initial_parent.resolved:
                    record(before, "enter_city", "NOT_REQUESTED", "NOT_CHECKED", "BLOCKED", "city_parent_control_unresolved")
                    return finish(before.state, "BLOCKED", "city_parent_control_unresolved")

                parent_observations = [initial_parent]
                restart_pre_dispatch = False
                for fresh_index in range(
                    self.city_parent_control_fresh_observation_limit
                ):
                    if self.cancellation():
                        return finish(CityNavigationState.FAILED, "BLOCKED", "city_entry_cancelled")
                    if self.monotonic() >= deadline:
                        return finish(CityNavigationState.TIMEOUT, "BLOCKED", "city_entry_postcondition_timeout")
                    try:
                        fresh_frame = self.frame_provider()
                    except NemuCaptureError as error:
                        failure = error.with_failure_stage(
                            "FRESH_CONFIRMATION_CAPTURE"
                        )
                        recovery_attempted = False
                        recovery_result = "NOT_ALLOWED"
                        if (
                            self.session_recoverer is not None
                            and self.capture_recovery_policy.allows(
                                failure,
                                dispatch_count=dispatch_count,
                                recovery_count=session_recovery_count,
                            )
                        ):
                            recovery_attempted = True
                            recovered = self.session_recoverer()
                            session_recovery_count += 1
                            session_generation_before_recovery = (
                                recovered.previous_session_generation
                            )
                            session_generation_after_recovery = (
                                recovered.current_session_generation
                            )
                            recovery_result = recovered.reason
                            record_capture_fault(
                                failure,
                                recovery_attempted=True,
                                recovery_result=recovery_result,
                            )
                            if recovered.success:
                                # Never reuse observations across sessions.
                                restart_pre_dispatch = True
                                break
                        capture_failure = failure
                        capture_failure_stage = failure.failure_stage
                        session_lifecycle_conflict = (
                            failure.session_lifecycle_conflict
                        )
                        retry_exhausted = bool(
                            recovery_attempted
                            or session_recovery_count
                            >= self.capture_recovery_policy.max_pre_dispatch_session_recovery
                        )
                        if not recovery_attempted:
                            record_capture_fault(
                                failure,
                                recovery_attempted=False,
                                recovery_result=recovery_result,
                            )
                        return finish(
                            CityNavigationState.FAILED,
                            "BLOCKED_SAFETY",
                            "fresh_capture_failed",
                        )

                    fresh = observe_city_frame(fresh_frame, now=self.now)
                    attempts += 1
                    if (
                        before.source_capture_id
                        and fresh.source_capture_id
                        and before.source_capture_id == fresh.source_capture_id
                    ):
                        record(fresh, "enter_city", "NOT_REQUESTED", "NOT_CHECKED", "BLOCKED", "stale_frame_action")
                        return finish(CityNavigationState.FAILED, "BLOCKED", "stale_frame_action")
                    if fresh.city_entry.candidate_count != 1 or fresh.city_entry.anchor_bbox is None:
                        record(fresh, "enter_city", "NOT_REQUESTED", "NOT_CHECKED", "BLOCKED", "city_entry_candidate_not_unique")
                        return finish(fresh.state, "BLOCKED", "city_entry_candidate_not_unique")
                    if fresh.state is not CityNavigationState.CITY_ENTRY_VISIBLE:
                        record(fresh, "enter_city", "NOT_REQUESTED", "NOT_CHECKED", "BLOCKED", "city_entry_candidate_mismatch")
                        return finish(fresh.state, "BLOCKED", "city_entry_candidate_mismatch")

                    image = getattr(fresh_frame, "image", fresh_frame)
                    fresh_parent = resolve_navigation_parent_control(
                        image,
                        semantic_id="visit_city",
                        anchor_bbox=fresh.city_entry.anchor_bbox,
                        source_capture_id=fresh.source_capture_id,
                        source_frame_sha256=fresh.screenshot_hash,
                    )
                    confirmed_parent = None
                    for prior in reversed(parent_observations):
                        confirmed_parent = confirm_fresh_parent_control(
                            prior, fresh_parent
                        )
                        if confirmed_parent is not None:
                            break
                    if (
                        confirmed_parent is not None
                        and confirmed_parent.safe_hit_point is not None
                    ):
                        return fresh, image, confirmed_parent

                    parent_observations.append(fresh_parent)
                    more_observations_allowed = (
                        fresh_index + 1
                        < self.city_parent_control_fresh_observation_limit
                        and attempts < self.max_attempts
                        and self.monotonic() < deadline
                    )
                    if more_observations_allowed:
                        record(
                            fresh,
                            "OBSERVE_CITY_PARENT_CONTROL",
                            "NOT_REQUESTED",
                            "NOT_CHECKED",
                            "PENDING",
                            "city_parent_control_confirmation_pending",
                        )
                        self.sleep(self.city_entry_observation_interval_seconds)
                        continue

                    record(fresh, "enter_city", "NOT_REQUESTED", "NOT_CHECKED", "BLOCKED", "city_parent_control_unstable")
                    return finish(fresh.state, "BLOCKED", "city_parent_control_unstable")

                if restart_pre_dispatch:
                    continue

        acquired = acquire_pre_dispatch()
        if isinstance(acquired, CityNavigationResult):
            return acquired
        fresh, image, confirmed_parent = acquired
        if image is None or not hasattr(image, "shape") or len(image.shape) < 2:
            return finish(CityNavigationState.FAILED, "BLOCKED", "city_entry_coordinate_chain_incomplete")
        capture_height, capture_width = map(int, image.shape[:2])
        point = confirmed_parent.safe_hit_point
        render_size = (capture_width, capture_height)
        device_size = render_size
        if self.geometry_provider is not None:
            geometry = self.geometry_provider()
            device_size = render_size = (
                int(getattr(geometry, "physical_width")),
                int(getattr(geometry, "physical_height")),
            )
        try:
            chain = CoordinateChain.from_capture_point(
                point,
                capture_size=(capture_width, capture_height),
                render_client_size=render_size,
                device_size=device_size,
                source_coordinate_space="CAPTURE_PIXELS",
            )
        except (TypeError, ValueError):
            return finish(CityNavigationState.FAILED, "BLOCKED", "city_entry_coordinate_chain_incomplete")
        if not chain.complete:
            return finish(CityNavigationState.FAILED, "BLOCKED", "city_entry_coordinate_chain_incomplete")

        evidence = NavigationAttemptEvidence(
            task_name="city_entry",
            entry_name="visit_city",
            pre_state="HOME_READY",
            pre_frame_sha256=fresh.screenshot_hash,
            coordinate_chain=chain,
            candidate_type="visual_parent_control",
            candidate_bbox=confirmed_parent.parent_control_bbox,
            candidate_score=fresh.city_entry.candidate_score,
            candidate_count=fresh.city_entry.candidate_count,
            dispatch_backend=self.dispatch_backend,
            random_offset_enabled=False,
            random_offset_requested=False,
            actual_dispatched_point=chain.device_point,
        )
        record(fresh, "enter_city", "PENDING", "city_evidence_categories_at_least_2", "PENDING", "city_entry_freshly_confirmed")
        try:
            allowed = self.tap(
                point,
                random_offset=False,
                intent=ActionIntent(
                    "city_entry_navigation", "city_entry", evidence.attempt_id,
                ),
            )
        except (PermissionError, RuntimeError) as error:
            physical_input_count = physical_input_count_from_dispatch_error(error)
            dispatch_count = physical_input_count
            error_receipt = receipt_from_dispatch_error(error)
            dispatch_result = str(
                getattr(error_receipt, "delivery_status", "")
                or f"dispatch_exception:{type(error).__name__}"
            )
            evidence.mark_dispatch(
                requested=True, acknowledged=False, result=dispatch_result
            )
            record(fresh, "enter_city", type(error).__name__, "NOT_CHECKED", "BLOCKED", "city_entry_dispatch_not_acknowledged")
            return finish(CityNavigationState.FAILED, "BLOCKED", "city_entry_dispatch_not_acknowledged")
        if allowed is False:
            evidence.mark_dispatch(requested=True, acknowledged=False, result="dispatch_rejected")
            record(fresh, "enter_city", "DENIED", "NOT_CHECKED", "BLOCKED", "city_entry_dispatch_not_acknowledged")
            return finish(CityNavigationState.FAILED, "BLOCKED", "city_entry_dispatch_not_acknowledged")
        evidence.mark_dispatch(
            requested=True,
            acknowledged=True,
            result=str(getattr(allowed, "delivery_status", "") or "call_returned"),
        )
        dispatch_count = 1
        physical_input_count = 1
        dispatch_started = self.monotonic()
        action_reason = (
            "city_entry_guard_postcondition_unverified"
            if getattr(allowed, "status", None)
            is DispatchStatus.DISPATCHED_UNVERIFIED
            else "city_entry_action_executed"
        )
        record(
            fresh,
            "enter_city",
            "ALLOWED",
            "PENDING",
            "PENDING",
            action_reason,
            transition_classification="PENDING",
        )

        transition_deadline = dispatch_started + self.city_entry_transition_timeout_seconds
        transition_observation_limit = max(
            self.max_attempts,
            int(
                math.ceil(
                    self.city_entry_transition_timeout_seconds
                    / self.city_entry_observation_interval_seconds
                )
            )
            + 1,
        )
        while post_observation_count < transition_observation_limit:
            if self.cancellation():
                return finish(CityNavigationState.FAILED, "BLOCKED", "city_entry_cancelled")
            if self.monotonic() >= transition_deadline:
                break
            self.sleep(
                min(
                    self.city_entry_observation_interval_seconds,
                    max(0.0, transition_deadline - self.monotonic()),
                )
            )
            if self.monotonic() >= transition_deadline:
                break
            try:
                post_frame = self.frame_provider()
            except NemuCaptureError as error:
                capture_failure = error.with_failure_stage(
                    "POST_DISPATCH_CAPTURE"
                )
                capture_failure_stage = capture_failure.failure_stage
                session_lifecycle_conflict = (
                    capture_failure.session_lifecycle_conflict
                )
                postcondition_observation_failed = True
                retry_exhausted = True
                record_capture_fault(
                    capture_failure,
                    recovery_attempted=False,
                    recovery_result="FORBIDDEN_AFTER_DISPATCH",
                )
                return finish(
                    CityNavigationState.FAILED,
                    "POSTCONDITION_UNOBSERVABLE",
                    "post_dispatch_capture_failed",
                    transition_result="UNOBSERVABLE",
                )
            except StopIteration:
                # Deterministic fixture/provider exhaustion remains a precise
                # transition failure; unexpected RuntimeError still escapes.
                return finish(
                    CityNavigationState.FAILED,
                    "FAILED",
                    "city_entry_transition_capture_failed",
                )
            try:
                observation = observe_city_frame(post_frame, now=self.now)
            except Exception:  # noqa: BLE001 - detector failure is not a page
                return finish(
                    CityNavigationState.FAILED,
                    "FAILED",
                    "city_entry_transition_detection_failed",
                )
            attempts += 1
            post_observation_count += 1
            last_observed_state = observation.state.value
            elapsed_since_dispatch = max(0.0, self.monotonic() - dispatch_started)

            frame_is_fresh = bool(
                observation.source_capture_id
                and observation.source_capture_id != fresh.source_capture_id
            )
            frame_changed = bool(
                observation.screenshot_hash
                and observation.screenshot_hash != fresh.screenshot_hash
            )
            postcondition = CITY_ENTRY_POSTCONDITION_POLICY.evaluate(
                observation.state,
                frame_is_fresh=frame_is_fresh,
                frame_changed=frame_changed,
                evidence_invariant_check=evidence.evidence_invariant_check,
            )
            if postcondition.city_entry_verified:
                post_canonical_leaf_state = postcondition.post_canonical_leaf_state
                post_context_state = postcondition.post_context_state
                city_entry_verified = True
                exact_expected_leaf_match = postcondition.exact_expected_leaf_match
                trusted_city_observation = evidence.add_post_observation(
                    frame=post_frame,
                    state=observation.state.value,
                    positive_cues=(observation.reason, *observation.evidence),
                    negative_cues=(),
                    reason_codes=postcondition.reason_codes,
                    postcondition_result="PASS_TO_STATION_DETECTOR",
                    trusted_postcondition_observed=True,
                    source_capture_id=observation.source_capture_id,
                    capture_sequence=getattr(post_frame, "capture_sequence", None),
                    session_generation=getattr(
                        post_frame,
                        "session_generation",
                        getattr(post_frame, "backend_generation", None),
                    ),
                )
                entry_opened = True
                if not self.require_station_confirmation:
                    record(
                        observation,
                        "OBSERVE_CITY_POSTCONDITION",
                        "ALLOWED",
                        "VERIFIED",
                        "PASS",
                        "city_postcondition_verified",
                        transition_classification="SUCCESS",
                    )
                    return finish(observation.state, "PASS", "city_postcondition_verified")
                try:
                    station = self.station_detector(post_frame, self.station_ids)
                except Exception as error:  # noqa: BLE001 - explicit detector outcome
                    station = StationDetectionResult(
                        "ERROR", None, "UNKNOWN", ("station_detector_exception",), 0,
                        f"station_detector_error:{type(error).__name__}",
                    )
                evidence.mark_station_detection(
                    result=station.result,
                    station_id=station.station_id,
                    confidence=station.confidence,
                    evidence_ids=station.evidence_ids,
                )
                if station.result == "PASS" and station.station_id:
                    station_confirmed = True
                    station_id = station.station_id
                    station_provenance = DerivedObservationProvenanceContract.resolve(
                        parent_observation=trusted_city_observation,
                        frame=post_frame,
                        new_capture_performed=False,
                    )
                    evidence.add_post_observation(
                        frame=post_frame,
                        state=observation.state.value,
                        positive_cues=("station_confirmed", *station.evidence_ids),
                        reason_codes=("city_and_station_verified",),
                        postcondition_result="PASS",
                        source_capture_id=station_provenance.source_capture_id,
                        capture_sequence=station_provenance.capture_sequence,
                        session_generation=station_provenance.session_generation,
                    )
                    record(
                        observation,
                        "OBSERVE_STATION",
                        "ALLOWED",
                        "VERIFIED",
                        "PASS",
                        "station_confirmed",
                        transition_classification="SUCCESS",
                    )
                    return finish(observation.state, "PASS", "station_confirmed")
                if station.result == "AMBIGUOUS":
                    evidence.add_post_observation(
                        frame=post_frame,
                        state=observation.state.value,
                        negative_cues=("multiple_station_names",),
                        reason_codes=("station_detector_ambiguous",),
                        postcondition_result="FAIL",
                    )
                    record(
                        observation,
                        "OBSERVE_STATION",
                        "ALLOWED",
                        "FAILED",
                        "FAILED",
                        "station_detector_ambiguous",
                        transition_classification="SUCCESS",
                    )
                    return finish(
                        observation.state,
                        "FAILED",
                        "station_detector_ambiguous",
                        transition_result="PASS",
                    )
                if station.result == "ERROR":
                    evidence.add_post_observation(
                        frame=post_frame,
                        state=observation.state.value,
                        negative_cues=("station_detector_error",),
                        reason_codes=("station_detector_error",),
                        postcondition_result="FAIL",
                    )
                    record(
                        observation,
                        "OBSERVE_STATION",
                        "ALLOWED",
                        "FAILED",
                        "FAILED",
                        "station_detector_error",
                        transition_classification="SUCCESS",
                    )
                    return finish(
                        observation.state,
                        "FAILED",
                        "station_detector_error",
                        transition_result="PASS",
                    )
                record(
                    observation,
                    "OBSERVE_STATION",
                    "ALLOWED",
                    "FAILED",
                    "FAILED",
                    "station_detector_no_match",
                    transition_classification="SUCCESS",
                )
                return finish(
                    observation.state,
                    "FAILED",
                    "station_detector_no_match",
                    transition_result="PASS",
                )
            if observation.state.value in CITY_ENTRY_POSTCONDITION_POLICY.accepted_leaf_states:
                evidence.add_post_observation(
                    frame=post_frame,
                    state=observation.state.value,
                    positive_cues=(observation.reason, *observation.evidence),
                    negative_cues=postcondition.reason_codes,
                    reason_codes=("city_entry_policy_evidence_failed",),
                    postcondition_result="FAIL",
                    source_capture_id=observation.source_capture_id,
                )
                record(
                    observation,
                    "OBSERVE_CITY_POSTCONDITION",
                    "ALLOWED",
                    "FAILED",
                    "FAILED",
                    "city_entry_postcondition_evidence_failed",
                    state=CityNavigationState.FAILED,
                    transition_classification="EXPLICIT_FAILURE",
                )
                return finish(
                    CityNavigationState.FAILED,
                    "FAILED",
                    "city_entry_postcondition_evidence_failed",
                    transition_result="EXPLICIT_FAILURE",
                )
            pending_reason_codes = ["postcondition_pending"]
            if elapsed_since_dispatch < self.city_entry_minimum_grace_seconds:
                pending_reason_codes.append("within_minimum_grace")

            if observation.foreign_page_reason:
                foreign_reason = observation.foreign_page_reason
                evidence.add_post_observation(
                    frame=post_frame,
                    state=observation.state.value,
                    positive_cues=(),
                    negative_cues=(foreign_reason,),
                    reason_codes=("explicit_foreign_page",),
                    postcondition_result="FAIL",
                )
                record(
                    observation,
                    "OBSERVE_CITY_POSTCONDITION",
                    "ALLOWED",
                    "FAILED",
                    "FAILED",
                    "city_entry_unexpected_page",
                    state=CityNavigationState.FAILED,
                    transition_classification="EXPLICIT_FAILURE",
                )
                return finish(
                    CityNavigationState.FAILED,
                    "FAILED",
                    "city_entry_unexpected_page",
                    transition_result="EXPLICIT_FAILURE",
                )
            if observation.state in {
                CityNavigationState.HOME_READY,
                CityNavigationState.CITY_ENTRY_VISIBLE,
            }:
                # A successfully dispatched tap does not guarantee that the
                # next 500 ms capture has already left HOME.  OCR can also
                # momentarily lose the unique `访问城市` anchor while the rest
                # of the HOME evidence remains intact.  Keep observing within
                # the bounded transition deadline, but never redispatch.
                pending_reason_codes.append("home_or_city_entry_still_visible")
                pending_reason = "home_page_still_visible_after_dispatch"
            elif observation.state is CityNavigationState.UNKNOWN:
                # UNKNOWN is absence of a committed page classification, not
                # proof of a foreign page.  Text and changing frame hashes are
                # expected during animation/loading and remain observable until
                # the transition deadline.  The adapter never redispatches.
                pending_reason_codes.append("unknown_transition_recoverable")
                pending_reason = (
                    "city_transition_without_committed_page"
                    if observation.text_count == 0
                    else "city_transition_with_uncommitted_page_evidence"
                )
            else:
                foreign_reason = f"known_non_city_state:{observation.state.value}"
                evidence.add_post_observation(
                    frame=post_frame,
                    state=observation.state.value,
                    positive_cues=(),
                    negative_cues=(foreign_reason,),
                    reason_codes=("explicit_foreign_page",),
                    postcondition_result="FAIL",
                )
                record(
                    observation,
                    "OBSERVE_CITY_POSTCONDITION",
                    "ALLOWED",
                    "FAILED",
                    "FAILED",
                    "city_entry_unexpected_page",
                    state=CityNavigationState.FAILED,
                    transition_classification="EXPLICIT_FAILURE",
                )
                return finish(
                    CityNavigationState.FAILED,
                    "FAILED",
                    "city_entry_unexpected_page",
                    transition_result="EXPLICIT_FAILURE",
                )

            evidence.add_post_observation(
                frame=post_frame,
                state=observation.state.value,
                positive_cues=(observation.reason, *observation.evidence),
                negative_cues=(),
                reason_codes=tuple(pending_reason_codes),
                postcondition_result="PENDING",
                source_capture_id=observation.source_capture_id,
            )
            record(
                observation,
                "WAIT_CITY_TRANSITION",
                "ALLOWED",
                "PENDING",
                "PENDING",
                pending_reason,
                state=CityNavigationState.CITY_TRANSITION,
                transition_classification="PENDING",
            )

        return finish(
            CityNavigationState.TIMEOUT,
            "BLOCKED",
            "city_entry_postcondition_timeout",
            transition_result="TIMEOUT",
        )


class ExchangeEntryAdapter:
    def __init__(
        self,
        *,
        frame_provider: Callable[[], object] | None = None,
        tap: Callable[..., object] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = lambda: datetime.now().astimezone(),
        timeout: float = 30.0,
        max_attempts: int = 10,
        stall_frames: int = 5,
        cancellation: Callable[[], bool] | None = None,
        correlation_id: str | None = None,
    ):
        self.frame_provider = frame_provider
        self.tap = tap
        self.sleep = sleep
        self.monotonic = monotonic
        self.now = now
        self.timeout = max(0.0, float(timeout))
        self.max_attempts = max(1, int(max_attempts))
        self.stall_frames = max(2, int(stall_frames))
        self.cancellation = cancellation or (lambda: False)
        self.correlation_id = correlation_id or now().strftime("CITYNAV-%Y%m%d-%H%M%S")

    @staticmethod
    def page_matches(observation: CityPageObservation, action: str) -> bool:
        selected = str(action).upper()
        return observation.state is (
            CityNavigationState.EXCHANGE_BUY
            if selected == "BUY" else CityNavigationState.EXCHANGE_SELL
        )

    def _navigate(
        self,
        *,
        source_states: frozenset[CityNavigationState],
        target_state: CityNavigationState,
        anchor_name: str,
        action_key: str,
        action_name: str,
    ) -> CityNavigationResult:
        if self.frame_provider is None or self.tap is None:
            raise RuntimeError("exchange_adapter_runtime_not_configured")
        deadline = self.monotonic() + self.timeout
        attempts = 0
        trace: list[CityNavigationEvent] = []

        def record(observation, action, guard, postcondition, status, reason, *, state=None):
            trace.append(CityNavigationEvent(
                self.correlation_id, observation.timestamp,
                state or observation.state, observation.confidence,
                observation.page_fingerprint, observation.screenshot_hash,
                action, guard, postcondition, status, reason,
            ))

        def finish(state, status, reason):
            return CityNavigationResult(state, status, reason, attempts, tuple(trace), 0)

        if self.cancellation():
            return finish(CityNavigationState.FAILED, "BLOCKED", "cancelled")
        if self.monotonic() >= deadline:
            return finish(CityNavigationState.TIMEOUT, "BLOCKED", "navigation_deadline_or_attempt_limit")
        before = observe_city_frame(self.frame_provider(), now=self.now)
        attempts += 1
        if before.state not in source_states:
            record(before, action_name, "NOT_REQUESTED", "NOT_CHECKED", "BLOCKED", "navigation_source_not_confirmed")
            return finish(before.state, "BLOCKED", "navigation_source_not_confirmed")
        bounds = {
            "交易所": before.exchange_anchor_bbox,
            "buy_navigation": before.buy_anchor_bbox,
            "sell_navigation": before.sell_anchor_bbox,
        }[anchor_name]
        if bounds is None:
            record(before, action_name, "NOT_REQUESTED", "NOT_CHECKED", "BLOCKED", "navigation_anchor_not_unique")
            return finish(before.state, "BLOCKED", "navigation_anchor_not_unique")
        point = _center(bounds)
        record(before, action_name, "PENDING", target_state.value, "PENDING", "navigation_anchor_observed")
        try:
            allowed = self.tap(
                point,
                intent=ActionIntent(
                    action_key, anchor_name, f"{self.correlation_id}:{action_name}",
                ),
            )
        except (PermissionError, RuntimeError) as error:
            record(before, action_name, type(error).__name__, "NOT_CHECKED", "BLOCKED", "guard_denied_navigation")
            return finish(CityNavigationState.FAILED, "BLOCKED", "guard_denied_navigation")
        if allowed is False:
            record(before, action_name, "DENIED", "NOT_CHECKED", "BLOCKED", "guard_denied_navigation")
            return finish(CityNavigationState.FAILED, "BLOCKED", "guard_denied_navigation")
        record(before, action_name, "ALLOWED", "PENDING", "PENDING", "navigation_action_executed")

        last_hash = before.screenshot_hash
        last_fingerprint = before.page_fingerprint
        stable = 0
        for _ in range(max(0, self.max_attempts - attempts)):
            if self.cancellation():
                return finish(CityNavigationState.FAILED, "BLOCKED", "cancelled")
            if self.monotonic() >= deadline:
                break
            self.sleep(min(0.5, max(0.0, deadline - self.monotonic())))
            if self.monotonic() >= deadline:
                break
            observation = observe_city_frame(self.frame_provider(), now=self.now)
            attempts += 1
            same_frame = bool(observation.screenshot_hash and observation.screenshot_hash == last_hash)
            same_page = observation.page_fingerprint == last_fingerprint
            stable = stable + 1 if (same_frame or same_page) else 1
            last_hash = observation.screenshot_hash
            last_fingerprint = observation.page_fingerprint

            if observation.state is target_state:
                record(observation, "OBSERVE_NAVIGATION_POSTCONDITION", "ALLOWED", "VERIFIED", "PASS", "navigation_postcondition_verified")
                return finish(target_state, "PASS", "navigation_postcondition_verified")
            transitional = (
                observation.state is CityNavigationState.NPC_DIALOG
                and target_state is CityNavigationState.EXCHANGE_MENU
            ) or (
                observation.state is CityNavigationState.UNKNOWN
                and observation.text_count == 0
            )
            if transitional:
                transition_state = (
                    observation.state
                    if observation.state is CityNavigationState.NPC_DIALOG
                    else CityNavigationState.CITY_TRANSITION
                )
                record(observation, "WAIT_NAVIGATION_TRANSITION", "ALLOWED", "PENDING", "PENDING", "navigation_transition", state=transition_state)
            else:
                record(observation, "OBSERVE_NAVIGATION_POSTCONDITION", "ALLOWED", "FAILED", "FAILED", "NAVIGATION_POSTCONDITION_FAILED")
                return finish(CityNavigationState.FAILED, "FAILED", "NAVIGATION_POSTCONDITION_FAILED")
            if stable >= self.stall_frames:
                record(observation, "WAIT_NAVIGATION_TRANSITION", "ALLOWED", "FAILED", "FAILED", "NAVIGATION_STALLED", state=CityNavigationState.FAILED)
                return finish(CityNavigationState.FAILED, "FAILED", "NAVIGATION_STALLED")
        return finish(CityNavigationState.TIMEOUT, "BLOCKED", "navigation_deadline_or_attempt_limit")

    def open_menu(self) -> CityNavigationResult:
        return self._navigate(
            source_states=frozenset({
                CityNavigationState.CITY_MAP,
                CityNavigationState.CITY_DETAIL,
                CityNavigationState.EXCHANGE_NPC_VISIBLE,
            }),
            target_state=CityNavigationState.EXCHANGE_MENU,
            anchor_name="交易所",
            action_key="navigation_anchor",
            action_name="enter_exchange",
        )

    def open_action(self, action: str) -> CityNavigationResult:
        selected = str(action).upper()
        if selected not in {"BUY", "SELL"}:
            raise ValueError(f"unsupported exchange action: {action}")
        return self._navigate(
            source_states=frozenset({CityNavigationState.EXCHANGE_MENU}),
            target_state=(
                CityNavigationState.EXCHANGE_BUY
                if selected == "BUY" else CityNavigationState.EXCHANGE_SELL
            ),
            anchor_name="buy_navigation" if selected == "BUY" else "sell_navigation",
            action_key=(
                "exchange_buy_navigation"
                if selected == "BUY" else "exchange_sell_navigation"
            ),
            action_name=f"open_exchange_{selected.lower()}",
        )


__all__ = [
    "CityEntryObservation",
    "CityNavigationAdapter",
    "CityNavigationEvent",
    "CityNavigationResult",
    "CityNavigationState",
    "CityPageObservation",
    "ExchangeEntryAdapter",
    "StationDetectionResult",
    "detect_current_station",
    "observe_city_frame",
]
