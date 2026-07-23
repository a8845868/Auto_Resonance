"""Scenario-driven personal runtime with one injected episode action budget."""

from __future__ import annotations

import hashlib
import re
import time
from dataclasses import asdict, dataclass
from enum import Enum
from typing import Callable, Mapping

from core.services.announcement_overlay_handler import (
    AnnouncementOverlayHandler,
    AnnouncementSafeRegionSelector,
    SafeBlankRegion,
    _bbox,
    _frame_bgr,
)
from core.services.personal_action_budget import EpisodeActionBudget
from core.services.personal_city_target import PersonalCityTarget, resolve_personal_city_anchor
from core.services.runtime_mode import RuntimeMode, resolve_runtime_mode


class RuntimeState(str, Enum):
    RESOURCE_UPDATE_REQUIRED = "RESOURCE_UPDATE_REQUIRED"
    RESOURCE_UPDATE_DOWNLOADING = "RESOURCE_UPDATE_DOWNLOADING"
    ANNOUNCEMENT_VISIBLE = "ANNOUNCEMENT_VISIBLE"
    DAILY_CHECKIN = "DAILY_CHECKIN"
    SESSION_ENTRY = "SESSION_ENTRY"
    HOME_READY = "HOME_READY"
    CITY_DETAIL = "CITY_DETAIL"
    LOGIN_REQUIRED = "LOGIN_REQUIRED"
    UNKNOWN = "UNKNOWN"


class RuntimeAction(str, Enum):
    CONFIRM_RESOURCE_UPDATE = "CONFIRM_RESOURCE_UPDATE"
    DISMISS_ANNOUNCEMENT = "DISMISS_ANNOUNCEMENT"
    DISMISS_DAILY_CHECKIN = "DISMISS_DAILY_CHECKIN"
    ENTER_SESSION = "ENTER_SESSION"
    ENTER_CITY = "ENTER_CITY"
    OBSERVE_ONLY = "OBSERVE_ONLY"
    STOP = "STOP"


@dataclass(frozen=True)
class EpisodePolicy:
    minimum_action_interval_seconds: float = 0.5
    maximum_observations: int = 120
    episode_timeout_seconds: float = 120.0
    resource_update_timeout_seconds: float = 600.0
    resource_update_stall_timeout_seconds: float = 120.0

    def validate(self) -> None:
        if self.minimum_action_interval_seconds < 0:
            raise ValueError("episode_action_interval_invalid")
        if (
            self.maximum_observations < 1
            or self.episode_timeout_seconds <= 0
            or self.resource_update_timeout_seconds <= 0
            or self.resource_update_stall_timeout_seconds <= 0
        ):
            raise ValueError("episode_observation_policy_invalid")


@dataclass(frozen=True)
class DetectedRuntimeState:
    state: RuntimeState
    frame_dimensions: tuple[int, int]
    confidence: float
    ocr_bboxes: tuple[tuple[int, int, int, int], ...]
    ocr_texts: tuple[str, ...]
    overlay_bbox: tuple[int, int, int, int] | None = None
    dialog_bbox: tuple[int, int, int, int] | None = None
    announcement_candidates: tuple[SafeBlankRegion, ...] = ()
    city_entry_bbox: tuple[int, int, int, int] | None = None
    resource_size_mb: float | None = None
    resource_confirm_bbox: tuple[int, int, int, int] | None = None
    resource_progress_percent: float | None = None
    evidence: tuple[str, ...] = ()
    frame_hash: str = ""


@dataclass(frozen=True)
class PlannedRuntimeAction:
    action: RuntimeAction
    state: RuntimeState
    capture_point: tuple[int, int] | None
    target_bbox: tuple[int, int, int, int] | None
    expected_postcondition: RuntimeState | None
    reason: str


@dataclass(frozen=True)
class CoordinateMapping:
    capture_point: tuple[int, int]
    normalized_render_point: tuple[int, int]
    render_client_point: tuple[int, int]
    screen_point: tuple[int, int]
    capture_to_normalized_scale: tuple[float, float]
    normalized_to_client_scale: tuple[float, float]


@dataclass(frozen=True)
class CoordinateTransform:
    capture_size: tuple[int, int]
    reference_render_size: tuple[int, int]
    render_client_size: tuple[int, int]
    render_client_screen_origin: tuple[int, int]

    def map(self, point: tuple[int, int]) -> CoordinateMapping:
        capture_width, capture_height = self.capture_size
        reference_width, reference_height = self.reference_render_size
        client_width, client_height = self.render_client_size
        if min(
            capture_width,
            capture_height,
            reference_width,
            reference_height,
            client_width,
            client_height,
        ) <= 0:
            raise ValueError("coordinate_geometry_invalid")
        capture_scale = (
            reference_width / capture_width,
            reference_height / capture_height,
        )
        normalized = (
            round(point[0] * capture_scale[0]),
            round(point[1] * capture_scale[1]),
        )
        client_scale = (client_width / reference_width, client_height / reference_height)
        client = (
            round(normalized[0] * client_scale[0]),
            round(normalized[1] * client_scale[1]),
        )
        screen = (
            self.render_client_screen_origin[0] + client[0],
            self.render_client_screen_origin[1] + client[1],
        )
        return CoordinateMapping(point, normalized, client, screen, capture_scale, client_scale)


def normalize_capture_point(
    point: tuple[int, int],
    frame_dimensions: tuple[int, int],
    reference: tuple[int, int] = (853, 480),
) -> tuple[int, int]:
    return (
        round(point[0] * reference[0] / frame_dimensions[0]),
        round(point[1] * reference[1] / frame_dimensions[1]),
    )


class StateDetector:
    """Pixels/OCR to one state; this class never dispatches input."""

    HOME_MARKERS = ("访问城市", "作战终端", "启程", "整备列车")
    CITY_MARKERS = ("市政厅", "交易所", "商会", "休息区", "城市设施")
    RESOURCE_TEXT_MARKERS = ("需要下载资源包", "需要更新资源", "需要下载资源")
    RESOURCE_BLOCKING_MARKERS = (
        "购买",
        "支付",
        "充值",
        "付费",
        "奖励",
        "领取",
        "交易",
        "买入",
        "卖出",
    )
    RESOURCE_SIZE_PATTERN = re.compile(r"(?P<size>\d+(?:\.\d+)?)\s*MB", re.IGNORECASE)
    RESOURCE_PROGRESS_PATTERN = re.compile(r"(?P<progress>\d{1,3}(?:\.\d+)?)\s*%")

    def __init__(
        self,
        *,
        announcement_handler: AnnouncementOverlayHandler | None = None,
        safe_region_selector: AnnouncementSafeRegionSelector | None = None,
        city_target: PersonalCityTarget = PersonalCityTarget(),
    ) -> None:
        city_target.validate()
        selector = safe_region_selector or AnnouncementSafeRegionSelector()
        self.announcement_handler = announcement_handler or AnnouncementOverlayHandler(
            safe_region_selector=selector
        )
        self.city_target = city_target

    @staticmethod
    def _items(frame: object) -> list[dict]:
        ocr = getattr(frame, "ocr", None)
        return list(ocr()) if callable(ocr) else []

    @staticmethod
    def _text(item: Mapping[str, object]) -> str:
        return str(item.get("text", "")).strip()

    @classmethod
    def _daily_dialog_bounds(
        cls, items: list[dict], width: int, height: int
    ) -> tuple[int, int, int, int] | None:
        content = [
            bounds
            for item in items
            if "空白区域退出" not in cls._text(item)
            and (bounds := _bbox(item)) is not None
        ]
        if not content:
            return None
        dialog = (
            max(0, min(item[0] for item in content) - 40),
            max(0, min(item[1] for item in content) - 14),
            min(width, max(item[2] for item in content) + 40),
            min(height, max(item[3] for item in content) + 12),
        )
        if (
            dialog[2] - dialog[0] < width * 0.55
            or dialog[3] - dialog[1] < height * 0.55
            or dialog[3] > height * 0.94
        ):
            return None
        return dialog

    def detect(self, frame: object) -> DetectedRuntimeState:
        image = _frame_bgr(frame)
        height, width = image.shape[:2]
        items = self._items(frame)
        texts = tuple(self._text(item) for item in items)
        bboxes = tuple(bounds for item in items if (bounds := _bbox(item)) is not None)
        frame_hash = hashlib.sha256(image.tobytes()).hexdigest()
        metadata = getattr(frame, "scenario_metadata", {})
        overlay = None
        dialog = None
        if isinstance(metadata, Mapping):
            raw_overlay = metadata.get("overlay_bounds")
            raw_dialog = metadata.get("dialog_bounds")
            if isinstance(raw_overlay, (list, tuple)) and len(raw_overlay) == 4:
                overlay = tuple(map(int, raw_overlay))
            if isinstance(raw_dialog, (list, tuple)) and len(raw_dialog) == 4:
                dialog = tuple(map(int, raw_dialog))

        resource_items = [
            item
            for item in items
            if any(marker in self._text(item) for marker in self.RESOURCE_TEXT_MARKERS)
        ]
        size_matches = [
            match
            for text in texts
            for match in self.RESOURCE_SIZE_PATTERN.finditer(text)
        ]
        confirm_items = [
            item for item in items if self._text(item).replace(" ", "") == "确认" and _bbox(item)
        ]
        progress_matches = [
            match
            for text in texts
            for match in self.RESOURCE_PROGRESS_PATTERN.finditer(text)
        ]
        blocking_cues = tuple(
            marker
            for marker in self.RESOURCE_BLOCKING_MARKERS
            if any(marker in text for text in texts)
        )
        if (
            len(resource_items) == 1
            and size_matches
            and progress_matches
            and not blocking_cues
        ):
            resource_size = float(size_matches[0].group("size"))
            progress = float(progress_matches[0].group("progress"))
            confirm_bbox = _bbox(confirm_items[0]) if len(confirm_items) == 1 else None
            if (
                len(confirm_items) == 1
                and progress <= 0
                and confirm_bbox is not None
                and 0 <= confirm_bbox[0] < confirm_bbox[2] <= width
                and 0 <= confirm_bbox[1] < confirm_bbox[3] <= height
            ):
                return DetectedRuntimeState(
                    RuntimeState.RESOURCE_UPDATE_REQUIRED,
                    (width, height),
                    1.0,
                    bboxes,
                    texts,
                    resource_size_mb=resource_size,
                    resource_confirm_bbox=confirm_bbox,
                    resource_progress_percent=progress,
                    evidence=(
                        "resource_update_text_match_count=1",
                        f"resource_size_mb={resource_size:g}",
                        "confirm_button_match_count=1",
                        "progress_indicator_present",
                        "risk_cues_absent",
                    ),
                    frame_hash=frame_hash,
                )
            if len(confirm_items) == 0 or progress > 0:
                return DetectedRuntimeState(
                    RuntimeState.RESOURCE_UPDATE_DOWNLOADING,
                    (width, height),
                    1.0,
                    bboxes,
                    texts,
                    resource_size_mb=resource_size,
                    resource_progress_percent=progress,
                    evidence=(
                        "resource_update_text_match_count=1",
                        f"resource_size_mb={resource_size:g}",
                        f"download_progress={progress:g}",
                        "risk_cues_absent",
                    ),
                    frame_hash=frame_hash,
                )

        daily = (
            any("每日签到奖励" in text for text in texts)
            and any("空白区域退出" in text for text in texts)
            and any("已领取" in text for text in texts)
        )
        if daily:
            overlay = overlay or (0, 0, width, height)
            dialog = dialog or self._daily_dialog_bounds(items, width, height)
            candidates: tuple[SafeBlankRegion, ...] = ()
            if dialog is not None:
                candidates = self.announcement_handler.safe_region_selector.select(
                    frame,
                    overlay_bbox=overlay,
                    dialog_bbox=dialog,
                    ocr_bboxes=bboxes,
                ).candidates
            return DetectedRuntimeState(
                RuntimeState.DAILY_CHECKIN,
                (width, height),
                1.0,
                bboxes,
                texts,
                overlay,
                dialog,
                candidates,
                evidence=("daily_checkin_title", "reward_already_claimed", "blank_exit_instruction"),
                frame_hash=frame_hash,
            )

        if self.announcement_handler.is_announcement(frame):
            safety = self.announcement_handler.resolve_safe_regions(frame)
            return DetectedRuntimeState(
                RuntimeState.ANNOUNCEMENT_VISIBLE,
                (width, height),
                1.0,
                bboxes,
                texts,
                safety.overlay_bbox if safety else (0, 0, width, height),
                safety.dialog_bbox if safety else self.announcement_handler.resolve_dialog_bounds(frame),
                safety.candidates if safety else (),
                evidence=("startup_overlay_classifier", "announcement_overlay"),
                frame_hash=frame_hash,
            )

        masked_account = any(re.search(r"\d{2,}\*+\d{2,}", text) for text in texts)
        session_items = [
            item for item in items if "点击屏幕进入游戏" in self._text(item) and _bbox(item)
        ]
        if masked_account and len(session_items) == 1:
            return DetectedRuntimeState(
                RuntimeState.SESSION_ENTRY,
                (width, height),
                1.0,
                bboxes,
                texts,
                city_entry_bbox=_bbox(session_items[0]),
                evidence=("masked_account", "explicit_enter_game_anchor"),
                frame_hash=frame_hash,
            )
        login_markers = sum(
            any(marker in text for marker in ("健康游戏", "登录", "用户协议", "隐私政策"))
            for text in texts
        )
        if masked_account and login_markers:
            return DetectedRuntimeState(
                RuntimeState.LOGIN_REQUIRED,
                (width, height),
                1.0,
                bboxes,
                texts,
                evidence=("masked_account", "login_page_marker"),
                frame_hash=frame_hash,
            )

        try:
            city_anchor = resolve_personal_city_anchor(items, self.city_target)
        except ValueError:
            city_anchor = None
        home_count = sum(any(marker in text for marker in self.HOME_MARKERS) for text in texts)
        if city_anchor is not None and home_count >= 2:
            return DetectedRuntimeState(
                RuntimeState.HOME_READY,
                (width, height),
                1.0,
                bboxes,
                texts,
                city_entry_bbox=city_anchor,
                evidence=("unique_visit_city", "exact_lanxin", "home_markers"),
                frame_hash=frame_hash,
            )
        visit_present = any(self.city_target.anchor_text in text for text in texts)
        city_label_present = any(text.replace(" ", "") == self.city_target.city_id for text in texts)
        city_count = sum(any(marker in text for marker in self.CITY_MARKERS) for text in texts)
        if city_label_present and city_count >= 2 and not visit_present:
            return DetectedRuntimeState(
                RuntimeState.CITY_DETAIL,
                (width, height),
                1.0,
                bboxes,
                texts,
                evidence=("exact_lanxin", "city_facilities"),
                frame_hash=frame_hash,
            )
        return DetectedRuntimeState(
            RuntimeState.UNKNOWN,
            (width, height),
            0.0,
            bboxes,
            texts,
            evidence=("insufficient_state_evidence",),
            frame_hash=frame_hash,
        )


class ResourceUpdateHandler:
    """Plan at most one exact resource-update confirmation per episode."""

    def __init__(self, *, authorized_resource_size_mb: float | None = None) -> None:
        self.authorized_resource_size_mb = authorized_resource_size_mb

    def plan(
        self, detected: DetectedRuntimeState, budget: EpisodeActionBudget
    ) -> PlannedRuntimeAction:
        already_dispatched = budget.actions_by_action_type["CONFIRM_RESOURCE_UPDATE"] > 0
        if (
            self.authorized_resource_size_mb is not None
            and detected.resource_size_mb != self.authorized_resource_size_mb
        ):
            return PlannedRuntimeAction(
                RuntimeAction.OBSERVE_ONLY,
                detected.state,
                None,
                detected.resource_confirm_bbox,
                None,
                "resource_update_size_not_authorized",
            )
        if already_dispatched:
            return PlannedRuntimeAction(
                RuntimeAction.OBSERVE_ONLY,
                detected.state,
                None,
                detected.resource_confirm_bbox,
                RuntimeState.RESOURCE_UPDATE_DOWNLOADING,
                "resource_update_confirmation_already_attempted",
            )
        if detected.resource_confirm_bbox is None:
            return PlannedRuntimeAction(
                RuntimeAction.OBSERVE_ONLY,
                detected.state,
                None,
                None,
                None,
                "resource_update_unique_confirm_unavailable",
            )
        bbox = detected.resource_confirm_bbox
        return PlannedRuntimeAction(
            RuntimeAction.CONFIRM_RESOURCE_UPDATE,
            detected.state,
            (round((bbox[0] + bbox[2]) / 2), round((bbox[1] + bbox[3]) / 2)),
            bbox,
            RuntimeState.RESOURCE_UPDATE_DOWNLOADING,
            "unique_resource_update_confirm_selected",
        )


class ActionPlanner:
    def __init__(self, resource_update_handler: ResourceUpdateHandler | None = None) -> None:
        self.resource_update_handler = resource_update_handler or ResourceUpdateHandler()

    @staticmethod
    def _center(bbox: tuple[int, int, int, int]) -> tuple[int, int]:
        return (round((bbox[0] + bbox[2]) / 2), round((bbox[1] + bbox[3]) / 2))

    @staticmethod
    def _candidate(
        detected: DetectedRuntimeState, budget: EpisodeActionBudget
    ) -> SafeBlankRegion | None:
        return next(
            (
                item
                for item in detected.announcement_candidates
                if normalize_capture_point(item.point, detected.frame_dimensions)
                not in budget.executed_points
            ),
            None,
        )

    def plan(
        self, detected: DetectedRuntimeState, *, budget: EpisodeActionBudget
    ) -> PlannedRuntimeAction:
        if detected.state is RuntimeState.RESOURCE_UPDATE_REQUIRED:
            return self.resource_update_handler.plan(detected, budget)
        if detected.state is RuntimeState.RESOURCE_UPDATE_DOWNLOADING:
            return PlannedRuntimeAction(
                RuntimeAction.OBSERVE_ONLY,
                detected.state,
                None,
                None,
                None,
                "resource_update_download_observation_only",
            )
        if detected.state is RuntimeState.ANNOUNCEMENT_VISIBLE:
            candidate = self._candidate(detected, budget)
            if candidate is None:
                return PlannedRuntimeAction(
                    RuntimeAction.OBSERVE_ONLY,
                    detected.state,
                    None,
                    None,
                    None,
                    "announcement_safe_blank_region_unavailable",
                )
            return PlannedRuntimeAction(
                RuntimeAction.DISMISS_ANNOUNCEMENT,
                detected.state,
                candidate.point,
                candidate.bbox,
                RuntimeState.HOME_READY,
                "safe_blank_region_selected",
            )
        if detected.state is RuntimeState.DAILY_CHECKIN:
            candidate = self._candidate(detected, budget)
            if candidate is None:
                return PlannedRuntimeAction(
                    RuntimeAction.OBSERVE_ONLY,
                    detected.state,
                    None,
                    None,
                    None,
                    "daily_checkin_safe_blank_region_unavailable",
                )
            return PlannedRuntimeAction(
                RuntimeAction.DISMISS_DAILY_CHECKIN,
                detected.state,
                candidate.point,
                candidate.bbox,
                RuntimeState.HOME_READY,
                "claimed_daily_checkin_blank_region_selected",
            )
        if detected.state is RuntimeState.SESSION_ENTRY and detected.city_entry_bbox:
            return PlannedRuntimeAction(
                RuntimeAction.ENTER_SESSION,
                detected.state,
                self._center(detected.city_entry_bbox),
                detected.city_entry_bbox,
                RuntimeState.HOME_READY,
                "explicit_enter_game_anchor_selected",
            )
        if detected.state is RuntimeState.HOME_READY and detected.city_entry_bbox:
            return PlannedRuntimeAction(
                RuntimeAction.ENTER_CITY,
                detected.state,
                self._center(detected.city_entry_bbox),
                detected.city_entry_bbox,
                RuntimeState.CITY_DETAIL,
                "unique_lanxin_entry_selected",
            )
        if detected.state is RuntimeState.CITY_DETAIL:
            return PlannedRuntimeAction(
                RuntimeAction.STOP,
                detected.state,
                None,
                None,
                RuntimeState.CITY_DETAIL,
                "city_detail_stop_boundary",
            )
        return PlannedRuntimeAction(
            RuntimeAction.OBSERVE_ONLY,
            detected.state,
            None,
            None,
            None,
            "unknown_state_observation_only",
        )


@dataclass(frozen=True)
class ExecutionResult:
    dispatched: bool
    mapping: CoordinateMapping | None
    reason: str


class ActionExecutor:
    def __init__(
        self,
        click: Callable[[tuple[int, int]], object],
        *,
        pre_dispatch_guard: Callable[[], bool] | None = None,
    ) -> None:
        self.click = click
        self.pre_dispatch_guard = pre_dispatch_guard

    def execute(
        self, plan: PlannedRuntimeAction, *, transform: CoordinateTransform
    ) -> ExecutionResult:
        if plan.capture_point is None:
            return ExecutionResult(False, None, "action_point_missing")
        if self.pre_dispatch_guard is not None and not self.pre_dispatch_guard():
            return ExecutionResult(False, None, "target_identity_guard_failed")
        mapping = transform.map(plan.capture_point)
        result = self.click(mapping.render_client_point)
        return ExecutionResult(result is not False, mapping, "input_dispatched")


class PostconditionVerifier:
    @staticmethod
    def verify(plan: PlannedRuntimeAction, detected: DetectedRuntimeState) -> bool:
        if plan.action is RuntimeAction.CONFIRM_RESOURCE_UPDATE:
            return detected.state is not RuntimeState.RESOURCE_UPDATE_REQUIRED
        return plan.expected_postcondition is not None and detected.state is plan.expected_postcondition


class RecoveryPolicy:
    def may_continue(
        self,
        previous: PlannedRuntimeAction,
        current: DetectedRuntimeState,
        budget: EpisodeActionBudget,
    ) -> bool:
        if current.state is RuntimeState.UNKNOWN:
            return True
        if previous.action is RuntimeAction.CONFIRM_RESOURCE_UPDATE:
            return current.state in {
                RuntimeState.RESOURCE_UPDATE_REQUIRED,
                RuntimeState.RESOURCE_UPDATE_DOWNLOADING,
            }
        if previous.action in {
            RuntimeAction.DISMISS_ANNOUNCEMENT,
            RuntimeAction.DISMISS_DAILY_CHECKIN,
        }:
            return self._has_distinct_candidate(current, budget)
        if previous.action is RuntimeAction.ENTER_SESSION:
            return current.state in {
                RuntimeState.ANNOUNCEMENT_VISIBLE,
                RuntimeState.DAILY_CHECKIN,
                RuntimeState.HOME_READY,
            }
        if previous.action is RuntimeAction.ENTER_CITY:
            return current.state is RuntimeState.HOME_READY
        return False

    @staticmethod
    def _has_distinct_candidate(
        current: DetectedRuntimeState, budget: EpisodeActionBudget
    ) -> bool:
        return any(
            normalize_capture_point(item.point, current.frame_dimensions)
            not in budget.executed_points
            for item in current.announcement_candidates
        )


class RunRecorder:
    def __init__(self) -> None:
        self.events: list[dict] = []

    def record(self, event: str, **details: object) -> None:
        self.events.append({"sequence": len(self.events) + 1, "event": event, **details})


@dataclass(frozen=True)
class EpisodeResult:
    status: str
    final_state: RuntimeState
    action_count: int
    observation_count: int
    reason: str
    events: tuple[dict, ...]


class PersonalAutomationEpisode:
    def __init__(
        self,
        *,
        frame_provider: Callable[[], object],
        detector: StateDetector,
        planner: ActionPlanner,
        executor: ActionExecutor,
        transform_provider: Callable[[DetectedRuntimeState], CoordinateTransform],
        budget: EpisodeActionBudget,
        verifier: PostconditionVerifier | None = None,
        recovery: RecoveryPolicy | None = None,
        recorder: RunRecorder | None = None,
        policy: EpisodePolicy = EpisodePolicy(),
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        resource_update_recover_package: Callable[[], object] | None = None,
        resource_update_package_running: Callable[[], bool] | None = None,
        mode: RuntimeMode | str | None = None,
    ) -> None:
        policy.validate()
        if resolve_runtime_mode(mode) is RuntimeMode.AUDIT:
            raise PermissionError("audit_mode_requires_controlled_authority_runner")
        if not isinstance(budget, EpisodeActionBudget):
            raise TypeError("shared_episode_action_budget_required")
        self.frame_provider = frame_provider
        self.detector = detector
        self.planner = planner
        self.executor = executor
        self.transform_provider = transform_provider
        self.budget = budget
        self.verifier = verifier or PostconditionVerifier()
        self.recovery = recovery or RecoveryPolicy()
        self.recorder = recorder or RunRecorder()
        self.policy = policy
        self.sleep = sleep
        self.monotonic = monotonic
        self.resource_update_recover_package = resource_update_recover_package
        self.resource_update_package_running = resource_update_package_running
        self._started = False

    def run(self) -> EpisodeResult:
        if self._started:
            raise RuntimeError("episode_instance_cannot_restart")
        self._started = True
        started = self.monotonic()
        episode_deadline = started + self.policy.episode_timeout_seconds
        resource_deadline: float | None = None
        resource_last_progress: float | None = None
        resource_last_progress_at: float | None = None
        resource_confirmation_dispatched = False
        resource_package_recovery_attempted = False
        observations = 0
        last_state = RuntimeState.UNKNOWN
        previous_plan: PlannedRuntimeAction | None = None
        while observations < self.policy.maximum_observations:
            now = self.monotonic()
            active_deadline = max(episode_deadline, resource_deadline or episode_deadline)
            if now >= active_deadline:
                break
            if (
                resource_confirmation_dispatched
                and self.resource_update_package_running is not None
                and not self.resource_update_package_running()
                and not resource_package_recovery_attempted
                and self.resource_update_recover_package is not None
            ):
                resource_package_recovery_attempted = True
                self.recorder.record(
                    "resource_package_recovery",
                    reason="package_not_running",
                    attempt=1,
                )
                self.resource_update_recover_package()
                self.sleep(self.policy.minimum_action_interval_seconds)
                continue
            try:
                frame = self.frame_provider()
            except Exception as exc:
                if (
                    resource_confirmation_dispatched
                    and not resource_package_recovery_attempted
                    and self.resource_update_recover_package is not None
                ):
                    resource_package_recovery_attempted = True
                    self.recorder.record(
                        "resource_package_recovery",
                        error_type=type(exc).__name__,
                        attempt=1,
                    )
                    self.resource_update_recover_package()
                    self.sleep(self.policy.minimum_action_interval_seconds)
                    continue
                raise
            detected = self.detector.detect(frame)
            last_state = detected.state
            observations += 1
            if detected.state in {
                RuntimeState.RESOURCE_UPDATE_REQUIRED,
                RuntimeState.RESOURCE_UPDATE_DOWNLOADING,
            }:
                if resource_deadline is None:
                    resource_deadline = self.monotonic() + self.policy.resource_update_timeout_seconds
                progress = detected.resource_progress_percent
                if progress is not None and (
                    resource_last_progress is None or progress > resource_last_progress
                ):
                    resource_last_progress = progress
                    resource_last_progress_at = self.monotonic()
                elif (
                    resource_confirmation_dispatched
                    and resource_last_progress_at is not None
                    and self.monotonic() - resource_last_progress_at
                    >= self.policy.resource_update_stall_timeout_seconds
                ):
                    return EpisodeResult(
                        "BLOCKED",
                        detected.state,
                        self.budget.total_actions,
                        observations,
                        "resource_update_progress_stalled",
                        tuple(self.recorder.events),
                    )
            self.recorder.record(
                "observation",
                state=detected.state.value,
                frame_hash=detected.frame_hash,
                action_count=self.budget.total_actions,
                resource_size_mb=detected.resource_size_mb,
                resource_progress_percent=detected.resource_progress_percent,
                resource_confirm_bbox=detected.resource_confirm_bbox,
            )
            if detected.state is RuntimeState.CITY_DETAIL:
                return EpisodeResult(
                    "PASS",
                    detected.state,
                    self.budget.total_actions,
                    observations,
                    "city_detail_reached",
                    tuple(self.recorder.events),
                )
            if previous_plan is not None and self.verifier.verify(previous_plan, detected):
                previous_plan = None
            elif previous_plan is not None and not self.recovery.may_continue(
                previous_plan, detected, self.budget
            ):
                return EpisodeResult(
                    "BLOCKED",
                    detected.state,
                    self.budget.total_actions,
                    observations,
                    "postcondition_failed_no_bounded_recovery",
                    tuple(self.recorder.events),
                )

            plan = self.planner.plan(detected, budget=self.budget)
            self.recorder.record(
                "plan",
                state=plan.state.value,
                action=plan.action.value,
                point=plan.capture_point,
                target_bbox=plan.target_bbox,
                reason=plan.reason,
            )
            if plan.action is RuntimeAction.STOP:
                return EpisodeResult(
                    "BLOCKED",
                    detected.state,
                    self.budget.total_actions,
                    observations,
                    plan.reason,
                    tuple(self.recorder.events),
                )
            if plan.action is RuntimeAction.OBSERVE_ONLY:
                self.sleep(self.policy.minimum_action_interval_seconds)
                continue
            transform = self.transform_provider(detected)
            assert plan.capture_point is not None
            mapping = transform.map(plan.capture_point)
            decision = self.budget.authorize(
                state=plan.state.value,
                action_type=plan.action.value,
                normalized_point=mapping.normalized_render_point,
            )
            if not decision.allowed:
                self.recorder.record("budget_rejection", reason=decision.reason_code)
                return EpisodeResult(
                    "BLOCKED",
                    detected.state,
                    self.budget.total_actions,
                    observations,
                    decision.reason_code,
                    tuple(self.recorder.events),
                )
            try:
                execution = self.executor.execute(plan, transform=transform)
            except Exception as exc:
                self.budget.record_dispatch(decision)
                self.budget.record_result(decision, "DELIVERY_UNKNOWN")
                self.recorder.record(
                    "execution",
                    action=plan.action.value,
                    dispatched="UNKNOWN",
                    error_type=type(exc).__name__,
                    action_count=self.budget.total_actions,
                )
                return EpisodeResult(
                    "BLOCKED",
                    detected.state,
                    self.budget.total_actions,
                    observations,
                    "action_delivery_unknown",
                    tuple(self.recorder.events),
                )
            if not execution.dispatched:
                self.budget.record_result(decision, "DISPATCH_REJECTED")
                return EpisodeResult(
                    "BLOCKED",
                    detected.state,
                    self.budget.total_actions,
                    observations,
                    execution.reason,
                    tuple(self.recorder.events),
                )
            self.budget.record_dispatch(decision)
            self.budget.record_result(decision, "DISPATCHED")
            if plan.action is RuntimeAction.CONFIRM_RESOURCE_UPDATE:
                resource_confirmation_dispatched = True
                if resource_last_progress_at is None:
                    resource_last_progress_at = self.monotonic()
            self.recorder.record(
                "execution",
                action=plan.action.value,
                dispatched=True,
                mapping=asdict(execution.mapping) if execution.mapping else None,
                action_count=self.budget.total_actions,
            )
            previous_plan = plan
            self.sleep(self.policy.minimum_action_interval_seconds)
        reason = (
            "resource_update_timeout"
            if resource_deadline is not None and self.monotonic() >= resource_deadline
            else "episode_deadline_or_observation_limit"
        )
        return EpisodeResult(
            "BLOCKED",
            last_state,
            self.budget.total_actions,
            observations,
            reason,
            tuple(self.recorder.events),
        )


__all__ = [
    "ActionExecutor",
    "ActionPlanner",
    "CoordinateMapping",
    "CoordinateTransform",
    "DetectedRuntimeState",
    "EpisodePolicy",
    "EpisodeResult",
    "PersonalAutomationEpisode",
    "PlannedRuntimeAction",
    "PostconditionVerifier",
    "RecoveryPolicy",
    "ResourceUpdateHandler",
    "RunRecorder",
    "RuntimeAction",
    "RuntimeState",
    "StateDetector",
    "normalize_capture_point",
]
