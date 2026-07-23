"""Deadline-driven startup and city-entry observation coordinator.

The coordinator deliberately separates observation from physical dispatch.  A
long loading or UNKNOWN interval may consume many observations, while every
known overlay fingerprint and the city-entry action remain independently
bounded.  Callers must explicitly provide and enable action dispatchers; the
default mode is observation-only.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections import Counter, deque
from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import Enum
from typing import Callable, Iterable, Mapping

from core.services.read_only_policy import ActionIntent


POSTCONDITION_SHADOW_MODE_DEFAULT = False


class CoordinatorState(str, Enum):
    EMULATOR_OFFLINE = "EMULATOR_OFFLINE"
    EMULATOR_STARTING = "EMULATOR_STARTING"
    EMULATOR_READY = "EMULATOR_READY"
    GAME_PROCESS_STARTING = "GAME_PROCESS_STARTING"
    GAME_LOADING = "GAME_LOADING"
    RESOURCE_CHECKING = "RESOURCE_CHECKING"
    RESOURCE_DOWNLOADING = "RESOURCE_DOWNLOADING"
    SIGNIN_VISIBLE = "SIGNIN_VISIBLE"
    ANNOUNCEMENT_VISIBLE = "ANNOUNCEMENT_VISIBLE"
    ACTIVITY_OVERLAY = "ACTIVITY_OVERLAY"
    HOME_READY = "HOME_READY"
    CITY_ENTRY_VISIBLE = "CITY_ENTRY_VISIBLE"
    CITY_TRANSITION = "CITY_TRANSITION"
    CITY_DETAIL = "CITY_DETAIL"
    UPDATE_REQUIRED = "UPDATE_REQUIRED"
    MAINTENANCE = "MAINTENANCE"
    NETWORK_ERROR = "NETWORK_ERROR"
    UNKNOWN_RECOVERABLE = "UNKNOWN_RECOVERABLE"
    UNKNOWN_MANUAL_REQUIRED = "UNKNOWN_MANUAL_REQUIRED"


class OverlayKind(str, Enum):
    SIGNIN = "SIGNIN"
    ANNOUNCEMENT = "ANNOUNCEMENT"
    ACTIVITY = "ACTIVITY_OVERLAY"
    OPTIONAL_UPDATE = "OPTIONAL_UPDATE_PROMPT"
    FORCED_UPDATE = "FORCED_UPDATE"
    MAINTENANCE = "MAINTENANCE"
    NETWORK_ERROR = "NETWORK_ERROR"


class Actionability(str, Enum):
    OBSERVE_ONLY = "OBSERVE_ONLY"
    SAFE_ACTION_AVAILABLE = "SAFE_ACTION_AVAILABLE"
    BLOCKED_BY_OVERLAY = "BLOCKED_BY_OVERLAY"
    MANUAL_REQUIRED = "MANUAL_REQUIRED"


class RecoveryStage(str, Enum):
    OBSERVE_ONLY = "OBSERVE_ONLY"
    WAIT_FOR_SETTLE = "WAIT_FOR_SETTLE"
    CLASSIFY_OVERLAY = "CLASSIFY_OVERLAY"
    SAFE_DISMISS_KNOWN_OVERLAY = "SAFE_DISMISS_KNOWN_OVERLAY"
    REOBSERVE = "REOBSERVE"
    SAFE_BACK_ONCE = "SAFE_BACK_ONCE"
    REDISCOVER_WINDOW_ADB = "REDISCOVER_WINDOW_ADB"
    MANUAL_REQUIRED = "MANUAL_REQUIRED"


@dataclass(frozen=True)
class StartupDeadlines:
    emulator_startup: float = 180.0
    game_process_startup: float = 120.0
    game_loading_resource: float = 300.0
    home_ready: float = 120.0
    city_entry_postcondition: float = 30.0
    unknown_observation_only: float = 20.0


@dataclass(frozen=True)
class StartupBudgets:
    observation_sample_budget: int = 900
    obstruction_action_budget: int = 1
    physical_dispatch_budget: int = 1
    recovery_escalation_budget: int = 8


@dataclass(frozen=True)
class StateContract:
    entry_evidence: tuple[str, ...]
    minimum_dwell_seconds: float
    deadline_seconds: float
    allowed_read_observations: tuple[str, ...]
    allowed_safe_actions: tuple[str, ...]
    forbidden_actions: tuple[str, ...]
    next_states: tuple[str, ...]
    recovery_strategy: str


def build_state_contracts(
    deadlines: StartupDeadlines = StartupDeadlines(),
) -> dict[CoordinatorState, StateContract]:
    observe = ("capture", "ocr", "image_match", "process_probe", "adb_probe")
    forbidden = ("reward_claim", "trade", "consume", "depart", "blind_tap")

    def contract(
        evidence: Iterable[str], deadline: float, next_states: Iterable[CoordinatorState],
        *, dwell: float = 0.0, actions: Iterable[str] = (), recovery: str = "OBSERVE_ONLY",
    ) -> StateContract:
        return StateContract(
            tuple(evidence), dwell, deadline, observe, tuple(actions), forbidden,
            tuple(state.value for state in next_states), recovery,
        )

    return {
        CoordinatorState.EMULATOR_OFFLINE: contract(
            ("emulator_process_absent",), deadlines.emulator_startup,
            (CoordinatorState.EMULATOR_STARTING, CoordinatorState.UNKNOWN_MANUAL_REQUIRED),
            actions=("launch_emulator",), recovery="MANUAL_REQUIRED",
        ),
        CoordinatorState.EMULATOR_STARTING: contract(
            ("launch_requested",), deadlines.emulator_startup,
            (CoordinatorState.EMULATOR_READY, CoordinatorState.UNKNOWN_MANUAL_REQUIRED),
            recovery="REDISCOVER_WINDOW_ADB",
        ),
        CoordinatorState.EMULATOR_READY: contract(
            ("adb_ready",), deadlines.game_process_startup,
            (CoordinatorState.GAME_PROCESS_STARTING, CoordinatorState.GAME_LOADING),
            actions=("launch_game",), recovery="REDISCOVER_WINDOW_ADB",
        ),
        CoordinatorState.GAME_PROCESS_STARTING: contract(
            ("game_launch_requested",), deadlines.game_process_startup,
            (CoordinatorState.GAME_LOADING, CoordinatorState.UNKNOWN_MANUAL_REQUIRED),
            recovery="REDISCOVER_WINDOW_ADB",
        ),
        CoordinatorState.GAME_LOADING: contract(
            ("ocr_loading", "image_loading"), deadlines.game_loading_resource,
            (CoordinatorState.RESOURCE_CHECKING, CoordinatorState.HOME_READY,
             CoordinatorState.UNKNOWN_RECOVERABLE), dwell=0.5,
            recovery="WAIT_FOR_SETTLE",
        ),
        CoordinatorState.RESOURCE_CHECKING: contract(
            ("ocr_resource_check",), deadlines.game_loading_resource,
            (CoordinatorState.RESOURCE_DOWNLOADING, CoordinatorState.HOME_READY,
             CoordinatorState.UPDATE_REQUIRED), recovery="WAIT_FOR_SETTLE",
        ),
        CoordinatorState.RESOURCE_DOWNLOADING: contract(
            ("ocr_resource_download_progress",), deadlines.game_loading_resource,
            (CoordinatorState.HOME_READY, CoordinatorState.UPDATE_REQUIRED),
            recovery="WAIT_FOR_SETTLE",
        ),
        CoordinatorState.SIGNIN_VISIBLE: contract(
            ("ocr_signin", "unique_close_anchor"), deadlines.home_ready,
            (CoordinatorState.HOME_READY, CoordinatorState.UNKNOWN_RECOVERABLE),
            actions=("dismiss_signin",), recovery="SAFE_DISMISS_KNOWN_OVERLAY",
        ),
        CoordinatorState.ANNOUNCEMENT_VISIBLE: contract(
            ("ocr_announcement", "unique_close_anchor"), deadlines.home_ready,
            (CoordinatorState.HOME_READY, CoordinatorState.UNKNOWN_RECOVERABLE),
            actions=("dismiss_announcement",), recovery="SAFE_DISMISS_KNOWN_OVERLAY",
        ),
        CoordinatorState.ACTIVITY_OVERLAY: contract(
            ("ocr_activity_overlay", "unique_close_anchor"), deadlines.home_ready,
            (CoordinatorState.HOME_READY, CoordinatorState.UNKNOWN_RECOVERABLE),
            actions=("dismiss_activity_overlay",), recovery="SAFE_DISMISS_KNOWN_OVERLAY",
        ),
        CoordinatorState.HOME_READY: contract(
            ("home_anchor_categories",), deadlines.home_ready,
            (CoordinatorState.CITY_ENTRY_VISIBLE, CoordinatorState.SIGNIN_VISIBLE,
             CoordinatorState.ANNOUNCEMENT_VISIBLE, CoordinatorState.ACTIVITY_OVERLAY),
            recovery="OBSERVE_ONLY",
        ),
        CoordinatorState.CITY_ENTRY_VISIBLE: contract(
            ("home_base", "stable_city_entry_anchor"), deadlines.home_ready,
            (CoordinatorState.CITY_TRANSITION, CoordinatorState.CITY_DETAIL),
            actions=("enter_city",), recovery="REOBSERVE",
        ),
        CoordinatorState.CITY_TRANSITION: contract(
            ("post_dispatch_transition",), deadlines.city_entry_postcondition,
            (CoordinatorState.CITY_DETAIL, CoordinatorState.UNKNOWN_MANUAL_REQUIRED),
            recovery="REOBSERVE",
        ),
        CoordinatorState.CITY_DETAIL: contract(
            ("two_independent_city_categories",), deadlines.city_entry_postcondition,
            (), recovery="OBSERVE_ONLY",
        ),
        CoordinatorState.UPDATE_REQUIRED: contract(
            ("forced_update",), deadlines.home_ready,
            (CoordinatorState.UNKNOWN_MANUAL_REQUIRED,), recovery="MANUAL_REQUIRED",
        ),
        CoordinatorState.MAINTENANCE: contract(
            ("maintenance_notice",), deadlines.home_ready,
            (CoordinatorState.UNKNOWN_MANUAL_REQUIRED,), recovery="MANUAL_REQUIRED",
        ),
        CoordinatorState.NETWORK_ERROR: contract(
            ("network_error",), deadlines.unknown_observation_only,
            (CoordinatorState.UNKNOWN_RECOVERABLE, CoordinatorState.UNKNOWN_MANUAL_REQUIRED),
            actions=("safe_back_once",), recovery="REDISCOVER_WINDOW_ADB",
        ),
        CoordinatorState.UNKNOWN_RECOVERABLE: contract(
            ("insufficient_or_conflicting_evidence",), deadlines.unknown_observation_only,
            (CoordinatorState.HOME_READY, CoordinatorState.CITY_DETAIL,
             CoordinatorState.UNKNOWN_MANUAL_REQUIRED), recovery="OBSERVE_ONLY",
        ),
        CoordinatorState.UNKNOWN_MANUAL_REQUIRED: contract(
            ("state_deadline_expired",), 0.0, (), recovery="MANUAL_REQUIRED",
        ),
    }


STATE_CONTRACTS = build_state_contracts()


@dataclass(frozen=True)
class OverlayHandler:
    name: str
    overlay: OverlayKind
    recognition_evidence: tuple[str, ...]
    trusted_anchor_texts: tuple[str, ...]
    involves_reward_or_resource: bool
    permit_requirement: str
    action_identity_prefix: str
    postcondition: str
    max_actions_per_fingerprint: int = 1


HANDLER_REGISTRY: Mapping[OverlayKind, OverlayHandler] = {
    OverlayKind.SIGNIN: OverlayHandler(
        "SIGNIN_HANDLER", OverlayKind.SIGNIN, ("ocr_signin", "unique_close_anchor"),
        ("关闭", "退出"), True, "dialog_cancel", "startup:signin:close",
        "SIGNIN_absent", 1,
    ),
    OverlayKind.ANNOUNCEMENT: OverlayHandler(
        "ANNOUNCEMENT_HANDLER", OverlayKind.ANNOUNCEMENT,
        ("ocr_announcement", "unique_close_anchor"), ("关闭", "返回"), False,
        "dialog_cancel", "startup:announcement:close", "ANNOUNCEMENT_absent", 1,
    ),
    OverlayKind.ACTIVITY: OverlayHandler(
        "ACTIVITY_OVERLAY_HANDLER", OverlayKind.ACTIVITY,
        ("ocr_activity_overlay", "unique_close_anchor"), ("关闭", "返回"), False,
        "dialog_cancel", "startup:activity:close", "ACTIVITY_OVERLAY_absent", 1,
    ),
    OverlayKind.OPTIONAL_UPDATE: OverlayHandler(
        "UPDATE_HANDLER", OverlayKind.OPTIONAL_UPDATE,
        ("ocr_optional_update", "unique_close_anchor"), ("稍后", "以后再说", "关闭"),
        True, "dialog_cancel", "startup:optional-update:close",
        "OPTIONAL_UPDATE_PROMPT_absent", 1,
    ),
    OverlayKind.FORCED_UPDATE: OverlayHandler(
        "UPDATE_HANDLER", OverlayKind.FORCED_UPDATE, ("ocr_forced_update",), (), True,
        "manual", "startup:forced-update", "MANUAL_REQUIRED", 0,
    ),
    OverlayKind.NETWORK_ERROR: OverlayHandler(
        "NETWORK_ERROR_HANDLER", OverlayKind.NETWORK_ERROR,
        ("ocr_network_error",), ("返回",), False, "page_back",
        "startup:network-error:back", "NETWORK_ERROR_absent", 1,
    ),
}


@dataclass(frozen=True)
class PageClassification:
    base_page: str
    overlays: tuple[OverlayKind, ...]
    confidence: float
    evidence_categories: tuple[str, ...]
    actionability: Actionability
    blocking_reason: str
    screenshot_hash: str
    capture_sequence: str
    page_fingerprint: str
    city_entry_anchor: tuple[int, int, int, int] | None = None
    safe_close_anchor: tuple[int, int, int, int] | None = None


def _bbox(item: Mapping[str, object]) -> tuple[int, int, int, int] | None:
    points = item.get("position") or ()
    if not isinstance(points, (list, tuple)) or len(points) < 3:
        return None
    xs = [int(point[0]) for point in points]
    ys = [int(point[1]) for point in points]
    return min(xs), min(ys), max(xs), max(ys)


def _center(bounds: tuple[int, int, int, int]) -> tuple[int, int]:
    x1, y1, x2, y2 = bounds
    return ((x1 + x2) // 2, (y1 + y2) // 2)


def _frame_hash(frame: object) -> str:
    raw = str(getattr(frame, "raw_frame_hash", "") or "")
    if raw:
        return raw
    image = getattr(frame, "image", None)
    if image is not None:
        return hashlib.sha256(image.tobytes()).hexdigest()
    return ""


def _visual_evidence(frame: object) -> set[str]:
    value = getattr(frame, "visual_evidence", ()) or ()
    if isinstance(value, Mapping):
        return {str(key) for key, present in value.items() if present}
    return {str(item) for item in value}


def classify_startup_frame(frame: object) -> PageClassification:
    """Classify base page and overlays independently without performing input."""

    items = list(frame.ocr())
    texts = [str(item.get("text", "")).replace(" ", "") for item in items]
    joined = "|".join(texts)
    visual = _visual_evidence(frame)
    evidence: set[str] = set()

    home_markers = {
        marker for marker in ("访问城市", "作战终端", "启程", "整备列车")
        if marker in joined
    }
    if "home_hud_template" in visual:
        home_markers.add("image_home_hud")
    if len(home_markers) >= 2:
        evidence.add("home_anchor_categories")

    city_groups = {
        "city_title": any(marker in joined for marker in ("城市详情", "当前城市", "城市发展度", "城市等级")),
        "city_functions": any(marker in joined for marker in ("城市设施", "城市手册", "交易所", "商会", "休息区")),
        "city_map": any(marker in joined for marker in ("城市地图", "地图区域")),
        "city_visual": bool({"city_layout", "city_map_template"} & visual),
    }
    city_evidence = {name for name, present in city_groups.items() if present}
    evidence.update(city_evidence)

    entry_candidates = [
        bounds for item, text in zip(items, texts)
        if "访问城市" in text and (bounds := _bbox(item)) is not None
    ]
    if len(entry_candidates) == 1:
        evidence.add("unique_city_entry_anchor")
    elif len(entry_candidates) > 1:
        evidence.add("conflicting_city_entry_anchors")

    close_candidates = [
        bounds for item, text in zip(items, texts)
        if text in {"关闭", "退出", "返回", "稍后", "以后再说"}
        and (bounds := _bbox(item)) is not None
    ]
    close_anchor = close_candidates[0] if len(close_candidates) == 1 else None
    if close_anchor is not None:
        evidence.add("unique_close_anchor")

    overlays: list[OverlayKind] = []
    if any(marker in joined for marker in ("每日签到奖励", "签到奖励", "每日签到")):
        overlays.append(OverlayKind.SIGNIN)
        evidence.add("ocr_signin")
    if any(marker in joined for marker in ("登录公告", "游戏公告", "公告", "资讯")):
        overlays.append(OverlayKind.ANNOUNCEMENT)
        evidence.add("ocr_announcement")
    if any(marker in joined for marker in ("活动详情", "活动说明", "活动窗口", "活动介绍")):
        overlays.append(OverlayKind.ACTIVITY)
        evidence.add("ocr_activity_overlay")
    if any(marker in joined for marker in ("版本过低", "必须更新", "强制更新", "前往更新")):
        overlays.append(OverlayKind.FORCED_UPDATE)
        evidence.add("ocr_forced_update")
    elif any(marker in joined for marker in ("发现新版本", "可选更新", "更新提示")):
        overlays.append(OverlayKind.OPTIONAL_UPDATE)
        evidence.add("ocr_optional_update")
    if any(marker in joined for marker in ("维护中", "服务器维护", "停服维护")):
        overlays.append(OverlayKind.MAINTENANCE)
        evidence.add("maintenance_notice")
    if any(marker in joined for marker in ("网络异常", "连接失败", "网络错误", "重新连接")):
        overlays.append(OverlayKind.NETWORK_ERROR)
        evidence.add("ocr_network_error")
    overlay_priority = {
        OverlayKind.FORCED_UPDATE: 0,
        OverlayKind.MAINTENANCE: 1,
        OverlayKind.NETWORK_ERROR: 2,
        OverlayKind.SIGNIN: 3,
        OverlayKind.ANNOUNCEMENT: 4,
        OverlayKind.ACTIVITY: 5,
        OverlayKind.OPTIONAL_UPDATE: 6,
    }
    overlays.sort(key=overlay_priority.__getitem__)

    if len(city_evidence) >= 2 and home_markers:
        base_page = CoordinatorState.UNKNOWN_RECOVERABLE.value
        blocking_reason = "conflicting_home_and_city_anchors"
        evidence.add("conflicting_anchor_categories")
    elif len(city_evidence) >= 2:
        base_page = CoordinatorState.CITY_DETAIL.value
        blocking_reason = ""
    elif len(home_markers) >= 2:
        base_page = CoordinatorState.HOME_READY.value
        blocking_reason = ""
    elif any(marker in joined for marker in ("正在校验资源", "资源检查中", "检查资源")):
        base_page = CoordinatorState.RESOURCE_CHECKING.value
        blocking_reason = "resource_check_wait_only"
        evidence.add("ocr_resource_check")
    elif any(marker in joined for marker in ("正在下载资源", "资源下载中", "下载进度")):
        base_page = CoordinatorState.RESOURCE_DOWNLOADING.value
        blocking_reason = "resource_download_wait_only"
        evidence.add("ocr_resource_download_progress")
    elif any(marker in joined for marker in ("加载中", "正在加载", "连接服务器")):
        base_page = CoordinatorState.GAME_LOADING.value
        blocking_reason = "game_loading_wait_only"
        evidence.add("ocr_loading")
    elif any(marker in joined for marker in (
        "点击屏幕进入游戏", "点击任意位置进入游戏", "更新已经完成",
        "更新已完成",
    )):
        base_page = CoordinatorState.UNKNOWN_MANUAL_REQUIRED.value
        blocking_reason = "manual_game_entry_required"
        evidence.add("manual_game_entry_anchor")
    elif not texts:
        # A blank frame is ambiguous until this coordinator has committed the
        # one city-entry action.  CITY_TRANSITION is post-action-only.
        base_page = CoordinatorState.UNKNOWN_RECOVERABLE.value
        blocking_reason = "blank_frame_without_committed_transition"
        evidence.add("blank_transition_frame")
    else:
        base_page = CoordinatorState.UNKNOWN_RECOVERABLE.value
        blocking_reason = "insufficient_page_evidence"

    claim_control = any(
        ("领取" in text and "已领取" not in text)
        for text in texts
    )
    dangerous = claim_control or any(marker in joined for marker in (
        "确认购买", "确认支付", "充值", "使用道具", "确认消耗", "账号授权",
    ))
    if dangerous:
        actionability = Actionability.MANUAL_REQUIRED
        blocking_reason = "reward_resource_or_account_action_present"
    elif any(overlay in {OverlayKind.FORCED_UPDATE, OverlayKind.MAINTENANCE} for overlay in overlays):
        actionability = Actionability.MANUAL_REQUIRED
        blocking_reason = "forced_update_or_maintenance"
    elif overlays:
        handler = HANDLER_REGISTRY.get(overlays[0])
        if handler and handler.max_actions_per_fingerprint and close_anchor is not None:
            actionability = Actionability.SAFE_ACTION_AVAILABLE
            blocking_reason = "known_overlay_blocks_base_action"
        else:
            actionability = Actionability.BLOCKED_BY_OVERLAY
            blocking_reason = "overlay_has_no_trusted_safe_action"
    else:
        actionability = Actionability.OBSERVE_ONLY

    screenshot_hash = _frame_hash(frame)
    sequence = str(
        getattr(frame, "backend_monotonic_sequence", "")
        or getattr(frame, "capture_sequence", "")
        or getattr(frame, "backend_capture_id", "")
        or getattr(frame, "capture_id", "")
    )
    fingerprint = hashlib.sha256(json.dumps({
        "base_page": base_page,
        "overlays": [overlay.value for overlay in overlays],
        "evidence": sorted(evidence),
        "entry_anchor": entry_candidates[0] if len(entry_candidates) == 1 else None,
        "close_anchor": close_anchor,
    }, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:24]
    confidence = 0.95 if base_page != CoordinatorState.UNKNOWN_RECOVERABLE.value else 0.35
    if overlays and actionability is Actionability.BLOCKED_BY_OVERLAY:
        confidence = min(confidence, 0.6)
    return PageClassification(
        base_page=base_page,
        overlays=tuple(overlays),
        confidence=confidence,
        evidence_categories=tuple(sorted(evidence)),
        actionability=actionability,
        blocking_reason=blocking_reason,
        screenshot_hash=screenshot_hash,
        capture_sequence=sequence,
        page_fingerprint=fingerprint,
        city_entry_anchor=entry_candidates[0] if len(entry_candidates) == 1 else None,
        safe_close_anchor=close_anchor,
    )


@dataclass(frozen=True)
class CoordinatorTraceEvent:
    timestamp: str
    state: str
    base_page: str
    overlays: tuple[str, ...]
    confidence: float
    evidence_categories: tuple[str, ...]
    actionability: str
    blocking_reason: str
    screenshot_hash: str
    capture_sequence: str
    page_fingerprint: str
    fresh: bool
    observation_count: int
    physical_dispatch_count: int
    obstruction_action_count: int
    elapsed_seconds: float
    state_deadline_seconds: float
    recovery_stage: str
    next_escalation: str
    action_identity: str = ""


@dataclass(frozen=True)
class CoordinatorResult:
    state: CoordinatorState
    status: str
    reason: str
    path: tuple[str, ...]
    observation_count: int
    physical_dispatch_count: int
    obstruction_action_count: int
    stale_frame_count: int
    irreversible_actions: int
    gui_available: bool
    trace: tuple[CoordinatorTraceEvent, ...]
    correlation_id: str

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["state"] = self.state.value
        payload["trace"] = [asdict(event) for event in self.trace]
        return payload


def _candidate_state(classification: PageClassification) -> CoordinatorState:
    if classification.overlays:
        priority = classification.overlays[0]
        return {
            OverlayKind.SIGNIN: CoordinatorState.SIGNIN_VISIBLE,
            OverlayKind.ANNOUNCEMENT: CoordinatorState.ANNOUNCEMENT_VISIBLE,
            OverlayKind.ACTIVITY: CoordinatorState.ACTIVITY_OVERLAY,
            OverlayKind.OPTIONAL_UPDATE: CoordinatorState.ACTIVITY_OVERLAY,
            OverlayKind.FORCED_UPDATE: CoordinatorState.UPDATE_REQUIRED,
            OverlayKind.MAINTENANCE: CoordinatorState.MAINTENANCE,
            OverlayKind.NETWORK_ERROR: CoordinatorState.NETWORK_ERROR,
        }[priority]
    if classification.base_page == CoordinatorState.HOME_READY.value:
        return (
            CoordinatorState.CITY_ENTRY_VISIBLE
            if classification.city_entry_anchor is not None
            else CoordinatorState.HOME_READY
        )
    try:
        return CoordinatorState(classification.base_page)
    except ValueError:
        return CoordinatorState.UNKNOWN_RECOVERABLE


class StartupCoordinator:
    """Observe startup until HOME/CITY is stable or a state deadline expires."""

    def __init__(
        self,
        *,
        frame_provider: Callable[[], object],
        overlay_dispatch: Callable[..., object] | None = None,
        city_entry_dispatch: Callable[..., object] | None = None,
        safe_back_dispatch: Callable[..., object] | None = None,
        post_dispatch_shadow_observer: object | None = None,
        allow_overlay_actions: bool = False,
        allow_city_entry: bool = False,
        enable_postcondition_shadow: bool = POSTCONDITION_SHADOW_MODE_DEFAULT,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = lambda: datetime.now().astimezone(),
        sampling_interval: float = 0.65,
        window_size: int = 5,
        budgets: StartupBudgets = StartupBudgets(),
        deadlines: StartupDeadlines = StartupDeadlines(),
        correlation_id: str | None = None,
    ):
        self.frame_provider = frame_provider
        self.overlay_dispatch = overlay_dispatch
        self.city_entry_dispatch = city_entry_dispatch
        self.safe_back_dispatch = safe_back_dispatch
        self.post_dispatch_shadow_observer = post_dispatch_shadow_observer
        self.allow_overlay_actions = bool(allow_overlay_actions)
        self.allow_city_entry = bool(allow_city_entry)
        # Fail closed: only the literal boolean True enables the shadow hook.
        # Strings such as "true"/"false", integers, None, and arbitrary
        # configuration objects remain disabled.
        self.enable_postcondition_shadow = enable_postcondition_shadow is True
        if self.enable_postcondition_shadow:
            if self.post_dispatch_shadow_observer is None:
                raise ValueError("postcondition_shadow_observer_missing")
            if getattr(self.post_dispatch_shadow_observer, "mode", "") != "SHADOW_ONLY":
                raise ValueError("postcondition_observer_must_be_shadow_only")
        self.postcondition_shadow_result: object | None = None
        self.sleep = sleep
        self.monotonic = monotonic
        self.now = now
        self.sampling_interval = max(0.0, float(sampling_interval))
        self.window_size = max(3, min(5, int(window_size)))
        self.budgets = budgets
        self.deadlines = deadlines
        self.contracts = build_state_contracts(deadlines)
        self.correlation_id = correlation_id or self.now().strftime(
            "STARTUP-COORD-%Y%m%d-%H%M%S"
        )

    @staticmethod
    def _consensus(states: deque[CoordinatorState]) -> CoordinatorState | None:
        if len(states) >= 3:
            recent = list(states)[-3:]
            state, count = Counter(recent).most_common(1)[0]
            if count >= 2:
                return state
        if len(states) >= 5:
            state, count = Counter(states).most_common(1)[0]
            if count >= 3:
                return state
        return None

    def resolve(self) -> CoordinatorResult:
        started = self.monotonic()
        state_started = started
        current = CoordinatorState.UNKNOWN_RECOVERABLE
        path = [CoordinatorState.EMULATOR_READY.value, CoordinatorState.GAME_LOADING.value]
        trace: list[CoordinatorTraceEvent] = []
        window: deque[CoordinatorState] = deque(maxlen=self.window_size)
        observations = 0
        physical_dispatches = 0
        obstruction_actions = 0
        stale_frames = 0
        last_hash = ""
        last_sequence = ""
        action_counts: Counter[str] = Counter()
        city_dispatched = False
        postcondition_shadow_active = False

        def add_path(state: CoordinatorState) -> None:
            if not path or path[-1] != state.value:
                path.append(state.value)

        def deadline_for(state: CoordinatorState) -> float:
            return self.contracts[state].deadline_seconds

        def finish(state: CoordinatorState, status: str, reason: str) -> CoordinatorResult:
            add_path(state)
            return CoordinatorResult(
                state, status, reason, tuple(path), observations, physical_dispatches,
                obstruction_actions, stale_frames, 0, True, tuple(trace),
                self.correlation_id,
            )

        while observations < max(1, self.budgets.observation_sample_budget):
            try:
                frame = self.frame_provider()
            except StopIteration:
                return finish(
                    CoordinatorState.UNKNOWN_MANUAL_REQUIRED, "BLOCKED",
                    "observation_source_exhausted",
                )
            except Exception as error:  # noqa: BLE001 - capture remains fail closed
                return finish(
                    CoordinatorState.UNKNOWN_MANUAL_REQUIRED, "BLOCKED",
                    f"capture_failed:{type(error).__name__}",
                )
            observations += 1
            if postcondition_shadow_active:
                try:
                    self.postcondition_shadow_result = (
                        self.post_dispatch_shadow_observer.observe_frame(frame)
                    )
                except Exception as error:  # noqa: BLE001 - shadow cannot escape
                    fail_closed = getattr(
                        self.post_dispatch_shadow_observer, "fail_closed", None
                    )
                    if callable(fail_closed):
                        self.postcondition_shadow_result = fail_closed(error)
                    postcondition_shadow_active = False
            classification = classify_startup_frame(frame)
            candidate = _candidate_state(classification)
            if (
                city_dispatched
                and "blank_transition_frame" in classification.evidence_categories
            ):
                candidate = CoordinatorState.CITY_TRANSITION
            fresh = bool(classification.screenshot_hash) and (
                classification.screenshot_hash != last_hash
            )
            if classification.capture_sequence and last_sequence:
                fresh = fresh and classification.capture_sequence != last_sequence
            if fresh:
                window.append(candidate)
            else:
                stale_frames += 1
            if classification.screenshot_hash:
                last_hash = classification.screenshot_hash
            if classification.capture_sequence:
                last_sequence = classification.capture_sequence

            stable = self._consensus(window)
            may_replace_post_dispatch = stable in {
                CoordinatorState.CITY_DETAIL,
                CoordinatorState.UPDATE_REQUIRED,
                CoordinatorState.MAINTENANCE,
            }
            if (
                stable is not None
                and stable is not current
                and (not city_dispatched or may_replace_post_dispatch)
            ):
                current = stable
                state_started = self.monotonic()
                add_path(current)
            elapsed = max(0.0, self.monotonic() - started)
            state_elapsed = max(0.0, self.monotonic() - state_started)
            recovery = (
                RecoveryStage.CLASSIFY_OVERLAY
                if classification.overlays else
                RecoveryStage.REOBSERVE
                if city_dispatched else
                RecoveryStage.WAIT_FOR_SETTLE
                if candidate in {
                    CoordinatorState.GAME_LOADING,
                    CoordinatorState.RESOURCE_CHECKING,
                    CoordinatorState.RESOURCE_DOWNLOADING,
                } else RecoveryStage.OBSERVE_ONLY
            )
            next_escalation = self.contracts[current].recovery_strategy
            action_identity = ""

            if stable in {
                CoordinatorState.UPDATE_REQUIRED, CoordinatorState.MAINTENANCE,
            }:
                trace.append(self._event(
                    classification, candidate, fresh, observations, physical_dispatches,
                    obstruction_actions, elapsed, deadline_for(current),
                    RecoveryStage.MANUAL_REQUIRED, "MANUAL_REQUIRED",
                ))
                return finish(stable, "BLOCKED", classification.blocking_reason)

            if stable is CoordinatorState.UNKNOWN_MANUAL_REQUIRED:
                trace.append(self._event(
                    classification, candidate, fresh, observations, physical_dispatches,
                    obstruction_actions, elapsed, deadline_for(current),
                    RecoveryStage.MANUAL_REQUIRED, "MANUAL_REQUIRED",
                ))
                return finish(
                    CoordinatorState.UNKNOWN_MANUAL_REQUIRED,
                    "BLOCKED",
                    classification.blocking_reason or "manual_required",
                )

            if stable in {
                CoordinatorState.SIGNIN_VISIBLE,
                CoordinatorState.ANNOUNCEMENT_VISIBLE,
                CoordinatorState.ACTIVITY_OVERLAY,
                CoordinatorState.NETWORK_ERROR,
            } and classification.overlays:
                overlay = classification.overlays[0]
                handler = HANDLER_REGISTRY.get(overlay)
                if classification.actionability is Actionability.MANUAL_REQUIRED:
                    trace.append(self._event(
                        classification, candidate, fresh, observations,
                        physical_dispatches, obstruction_actions, elapsed,
                        deadline_for(current), RecoveryStage.MANUAL_REQUIRED,
                        "MANUAL_REQUIRED",
                    ))
                    return finish(
                        CoordinatorState.UNKNOWN_MANUAL_REQUIRED, "BLOCKED",
                        classification.blocking_reason,
                    )
                if (
                    handler is not None
                    and self.allow_overlay_actions
                    and self.overlay_dispatch is not None
                    and classification.safe_close_anchor is not None
                ):
                    action_identity = (
                        f"{handler.action_identity_prefix}:{classification.page_fingerprint}"
                    )
                    limit = min(
                        handler.max_actions_per_fingerprint,
                        max(0, self.budgets.obstruction_action_budget),
                    )
                    if action_counts[action_identity] < limit:
                        try:
                            allowed = self.overlay_dispatch(
                                _center(classification.safe_close_anchor),
                                intent=ActionIntent(
                                    handler.permit_requirement,
                                    (
                                        "top_left_back"
                                        if handler.permit_requirement == "page_back"
                                        else "cancel"
                                    ),
                                    action_identity,
                                ),
                            )
                        except (PermissionError, RuntimeError):
                            allowed = False
                        action_counts[action_identity] += 1
                        obstruction_actions += 1
                        recovery = RecoveryStage.SAFE_DISMISS_KNOWN_OVERLAY
                        next_escalation = RecoveryStage.REOBSERVE.value
                        if allowed is False:
                            trace.append(self._event(
                                classification, candidate, fresh, observations,
                                physical_dispatches, obstruction_actions, elapsed,
                                deadline_for(current), recovery, "MANUAL_REQUIRED",
                                action_identity,
                            ))
                            return finish(
                                CoordinatorState.UNKNOWN_MANUAL_REQUIRED, "BLOCKED",
                                "overlay_guard_denied",
                            )

            if stable is CoordinatorState.CITY_DETAIL:
                trace.append(self._event(
                    classification, candidate, fresh, observations, physical_dispatches,
                    obstruction_actions, elapsed, deadline_for(current), recovery,
                    next_escalation, action_identity,
                ))
                return finish(CoordinatorState.CITY_DETAIL, "PASS", "city_detail_stable")

            if stable in {CoordinatorState.HOME_READY, CoordinatorState.CITY_ENTRY_VISIBLE}:
                if classification.overlays:
                    pass
                elif not self.allow_city_entry:
                    trace.append(self._event(
                        classification, candidate, fresh, observations,
                        physical_dispatches, obstruction_actions, elapsed,
                        deadline_for(current), recovery, next_escalation,
                    ))
                    return finish(CoordinatorState.HOME_READY, "PASS", "home_ready_stable")
                elif stable is CoordinatorState.CITY_ENTRY_VISIBLE and not city_dispatched:
                    if (
                        self.city_entry_dispatch is None
                        or classification.city_entry_anchor is None
                        or physical_dispatches >= self.budgets.physical_dispatch_budget
                    ):
                        trace.append(self._event(
                            classification, candidate, fresh, observations,
                            physical_dispatches, obstruction_actions, elapsed,
                            deadline_for(current), RecoveryStage.MANUAL_REQUIRED,
                            "MANUAL_REQUIRED",
                        ))
                        return finish(
                            CoordinatorState.UNKNOWN_MANUAL_REQUIRED, "BLOCKED",
                            "city_entry_dispatch_unavailable_or_budget_exhausted",
                        )
                    action_identity = f"startup:enter-city:{self.correlation_id}"
                    try:
                        allowed = self.city_entry_dispatch(
                            _center(classification.city_entry_anchor),
                            intent=ActionIntent(
                                "city_entry_navigation", "city_entry", action_identity,
                            ),
                        )
                    except (PermissionError, RuntimeError):
                        allowed = False
                    if allowed is False:
                        trace.append(self._event(
                            classification, candidate, fresh, observations,
                            physical_dispatches, obstruction_actions, elapsed,
                            deadline_for(current), recovery, next_escalation,
                            action_identity,
                        ))
                        return finish(
                            CoordinatorState.UNKNOWN_MANUAL_REQUIRED, "BLOCKED",
                            "city_entry_guard_denied",
                        )
                    physical_dispatches += 1
                    city_dispatched = True
                    if self.enable_postcondition_shadow:
                        try:
                            self.post_dispatch_shadow_observer.start_after_dispatch()
                            postcondition_shadow_active = True
                        except Exception as error:  # noqa: BLE001 - shadow only
                            fail_closed = getattr(
                                self.post_dispatch_shadow_observer,
                                "fail_closed",
                                None,
                            )
                            if callable(fail_closed):
                                self.postcondition_shadow_result = fail_closed(error)
                            postcondition_shadow_active = False
                    window.clear()
                    current = CoordinatorState.CITY_TRANSITION
                    state_started = self.monotonic()
                    add_path(current)
                    recovery = RecoveryStage.REOBSERVE
                    next_escalation = RecoveryStage.MANUAL_REQUIRED.value

            trace.append(self._event(
                classification, candidate, fresh, observations, physical_dispatches,
                obstruction_actions, elapsed, deadline_for(current), recovery,
                next_escalation, action_identity,
            ))

            if state_elapsed >= deadline_for(current):
                return finish(
                    CoordinatorState.UNKNOWN_MANUAL_REQUIRED, "BLOCKED",
                    f"state_deadline_expired:{current.value}",
                )
            self.sleep(self.sampling_interval)

        return finish(
            CoordinatorState.UNKNOWN_MANUAL_REQUIRED, "BLOCKED",
            "observation_sample_budget_exhausted",
        )

    def _event(
        self,
        classification: PageClassification,
        state: CoordinatorState,
        fresh: bool,
        observations: int,
        physical_dispatches: int,
        obstruction_actions: int,
        elapsed: float,
        state_deadline: float,
        recovery: RecoveryStage,
        next_escalation: str,
        action_identity: str = "",
    ) -> CoordinatorTraceEvent:
        return CoordinatorTraceEvent(
            timestamp=self.now().isoformat(timespec="milliseconds"),
            state=state.value,
            base_page=classification.base_page,
            overlays=tuple(overlay.value for overlay in classification.overlays),
            confidence=classification.confidence,
            evidence_categories=classification.evidence_categories,
            actionability=classification.actionability.value,
            blocking_reason=classification.blocking_reason,
            screenshot_hash=classification.screenshot_hash,
            capture_sequence=classification.capture_sequence,
            page_fingerprint=classification.page_fingerprint,
            fresh=fresh,
            observation_count=observations,
            physical_dispatch_count=physical_dispatches,
            obstruction_action_count=obstruction_actions,
            elapsed_seconds=elapsed,
            state_deadline_seconds=state_deadline,
            recovery_stage=recovery.value,
            next_escalation=next_escalation,
            action_identity=action_identity,
        )


__all__ = [
    "Actionability", "CoordinatorResult", "CoordinatorState",
    "CoordinatorTraceEvent", "HANDLER_REGISTRY", "OverlayHandler",
    "OverlayKind", "PageClassification", "RecoveryStage", "STATE_CONTRACTS",
    "POSTCONDITION_SHADOW_MODE_DEFAULT",
    "StartupBudgets", "StartupCoordinator", "StartupDeadlines",
    "StateContract", "build_state_contracts", "classify_startup_frame",
]
