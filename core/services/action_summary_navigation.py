"""Bounded, evidence-backed navigation to the action-summary page.

This adapter stops as soon as the page is proved.  It never selects a stage,
starts a sweep, consumes resources, or retries a dispatched action.
"""

from __future__ import annotations

import time
import hashlib
import re
import unicodedata
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Mapping, Sequence

from core.services.announcement_overlay_handler import AnnouncementSafeRegionSelector
from core.services.navigation_evidence import (
    CoordinateChain,
    NavigationAttemptEvidence,
    frame_sha256,
    record_navigation_attempt,
)
from core.services.personal_action_budget import EpisodeActionBudget
from core.services.read_only_policy import ActionIntent
from core.services.screen_state import ResidentHomeState, resident_home_state


class ActionSummaryState(str, Enum):
    HOME_READY = "HOME_READY"
    ACTIVITY_OVERVIEW_VISIBLE = "ACTIVITY_OVERVIEW_VISIBLE"
    OPTIONAL_OVERLAY_VISIBLE = "OPTIONAL_OVERLAY_VISIBLE"
    ACTION_SUMMARY_ENTRY_VISIBLE = "ACTION_SUMMARY_ENTRY_VISIBLE"
    ACTION_SUMMARY_VISIBLE = "ACTION_SUMMARY_VISIBLE"
    FOREIGN_PAGE = "FOREIGN_PAGE"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class ActionSummaryObservation:
    state: ActionSummaryState
    frame_hash: str
    capture_id: str
    items: tuple[dict, ...]
    positive_cues: tuple[str, ...]
    negative_cues: tuple[str, ...] = ()


@dataclass
class ActionSummaryResult:
    success: bool
    state: ActionSummaryState
    reason: str
    dispatch_count: int
    stage_count: int
    evidences: list[NavigationAttemptEvidence] = field(default_factory=list)
    timeline: list[dict] = field(default_factory=list)
    candidate_resolutions: list["CandidateResolutionEvidence"] = field(default_factory=list)


@dataclass(frozen=True)
class ActionTerminalCandidate:
    candidate_type: str
    bbox: tuple[int, int, int, int]
    score: float
    evidence_ids: tuple[str, ...]
    source_indices: tuple[int, ...]

    @property
    def point(self) -> tuple[int, int]:
        return _center(self.bbox)


@dataclass(frozen=True)
class CandidateResolutionEvidence:
    phase: str
    raw_item_count: int
    exact_match_count: int
    fragment_match_count: int
    merged_candidate_count: int
    deduplicated_candidate_count: int
    region_filtered_candidate_count: int
    safe_candidate_count: int
    candidate_region: tuple[float, float, float, float]
    candidate_type: str | None
    candidate_bbox: tuple[int, int, int, int] | None
    candidate_score: float | None
    candidate_evidence_ids: tuple[str, ...]
    rejected_candidate_reasons: tuple[str, ...]
    failure_class: str | None

    def to_dict(self) -> dict:
        return {
            "phase": self.phase,
            "raw_item_count": self.raw_item_count,
            "exact_match_count": self.exact_match_count,
            "fragment_match_count": self.fragment_match_count,
            "merged_candidate_count": self.merged_candidate_count,
            "deduplicated_candidate_count": self.deduplicated_candidate_count,
            "region_filtered_candidate_count": self.region_filtered_candidate_count,
            "safe_candidate_count": self.safe_candidate_count,
            "candidate_region": self.candidate_region,
            "candidate_type": self.candidate_type,
            "candidate_bbox": self.candidate_bbox,
            "candidate_score": self.candidate_score,
            "candidate_evidence_ids": self.candidate_evidence_ids,
            "rejected_candidate_reasons": self.rejected_candidate_reasons,
            "failure_class": self.failure_class,
        }


_SIEGE_PAGE_LABELS = (
    "特殊订单", "利刃行动", "挑灯看剑", "武器材质分析", "骑士小说",
    "我思我在", "所知所闻", "大的！", "总体围剿",
)
_FOREIGN_MARKERS = ("道具", "材料", "交易所", "买入", "卖出", "浏览器")
# Derived from two read-only 1280x720 HOME frames on 2026-07-26.  The terminal
# button bbox was (1149,394)-(1232,422) and (1148,391)-(1232,423); the legacy
# measured point (1180,415) is inside the same region.  The quest copy containing
# the same words was above y=298, outside this normalized target region.
ACTION_TERMINAL_REGION_NORMALIZED = (0.86, 0.52, 0.99, 0.62)
_ACTION_TERMINAL_TEXT = "作战终端"
_ACTION_TERMINAL_FRAGMENTS = ("作战", "终端")
_OCR_SEPARATOR = re.compile(r"[\s|｜:：·•._\-—]+")


def _bbox(item: Mapping[str, object]) -> tuple[int, int, int, int] | None:
    points = item.get("position")
    if not isinstance(points, Sequence) or len(points) < 3:
        return None
    try:
        xs = [int(float(point[0])) for point in points]
        ys = [int(float(point[1])) for point in points]
    except (TypeError, ValueError, IndexError):
        return None
    return min(xs), min(ys), max(xs), max(ys)


def _center(bounds: tuple[int, int, int, int]) -> tuple[int, int]:
    return ((bounds[0] + bounds[2]) // 2, (bounds[1] + bounds[3]) // 2)


def _normalized_text(value: object) -> str:
    return _OCR_SEPARATOR.sub("", unicodedata.normalize("NFKC", str(value)))


def _semantic_evidence_id(text: str, bbox: tuple[int, int, int, int]) -> str:
    payload = f"{text}|{','.join(map(str, bbox))}".encode("utf-8")
    return f"ocr:{hashlib.sha256(payload).hexdigest()[:20]}"


def _iou(
    first: tuple[int, int, int, int], second: tuple[int, int, int, int]
) -> float:
    left, top = max(first[0], second[0]), max(first[1], second[1])
    right, bottom = min(first[2], second[2]), min(first[3], second[3])
    intersection = max(0, right - left) * max(0, bottom - top)
    first_area = max(0, first[2] - first[0]) * max(0, first[3] - first[1])
    second_area = max(0, second[2] - second[0]) * max(0, second[3] - second[1])
    union = first_area + second_area - intersection
    return float(intersection) / float(union) if union else 0.0


def _valid_action_terminal_bbox(
    bbox: tuple[int, int, int, int], width: int, height: int
) -> bool:
    left, top, right, bottom = bbox
    if not (0 <= left < right <= width and 0 <= top < bottom <= height):
        return False
    box_width, box_height = right - left, bottom - top
    normalized_area = (box_width * box_height) / float(width * height)
    aspect = box_width / float(box_height)
    return 0.0005 <= normalized_area <= 0.02 and 1.4 <= aspect <= 8.0


def _inside_target_region(
    bbox: tuple[int, int, int, int], width: int, height: int
) -> bool:
    x, y = _center(bbox)
    left, top, right, bottom = ACTION_TERMINAL_REGION_NORMALIZED
    return left <= x / width <= right and top <= y / height <= bottom


def _vertical_overlap_ratio(
    first: tuple[int, int, int, int], second: tuple[int, int, int, int]
) -> float:
    overlap = max(0, min(first[3], second[3]) - max(first[1], second[1]))
    return overlap / float(max(1, min(first[3] - first[1], second[3] - second[1])))


def resolve_action_terminal_candidate(
    frame: object, *, phase: str
) -> tuple[ActionTerminalCandidate | None, CandidateResolutionEvidence]:
    """Resolve the HOME action-terminal button without substring guessing."""

    items = _items(frame)
    image = getattr(frame, "image", None)
    if image is None or not hasattr(image, "shape") or len(image.shape) < 2:
        evidence = CandidateResolutionEvidence(
            phase, len(items), 0, 0, 0, 0, 0, 0,
            ACTION_TERMINAL_REGION_NORMALIZED, None, None, None, (),
            ("capture_dimensions_missing",), "BBOX_INVALID",
        )
        return None, evidence
    height, width = map(int, image.shape[:2])
    parsed: list[tuple[int, str, tuple[int, int, int, int] | None, float]] = []
    for index, item in enumerate(items):
        parsed.append((
            index,
            _normalized_text(item.get("text", "")),
            _bbox(item),
            float(item.get("score", 0.0) or 0.0),
        ))
    exact_items = [entry for entry in parsed if entry[1] == _ACTION_TERMINAL_TEXT]
    fragment_items = [entry for entry in parsed if entry[1] in _ACTION_TERMINAL_FRAGMENTS]
    candidates: list[ActionTerminalCandidate] = []
    rejected: list[str] = []
    for index, text, bounds, score in exact_items:
        if bounds is None or not _valid_action_terminal_bbox(bounds, width, height):
            rejected.append("exact_bbox_invalid")
            continue
        candidates.append(ActionTerminalCandidate(
            "exact_ocr", bounds, score,
            (_semantic_evidence_id(text, bounds),), (index,),
        ))

    merged_count = 0
    left_fragments = [entry for entry in fragment_items if entry[1] == "作战"]
    right_fragments = [entry for entry in fragment_items if entry[1] == "终端"]
    for left_item in left_fragments:
        for right_item in right_fragments:
            left_index, left_text, left_box, left_score = left_item
            right_index, right_text, right_box, right_score = right_item
            if left_box is None or right_box is None:
                rejected.append("fragment_bbox_missing")
                continue
            gap = right_box[0] - left_box[2]
            max_gap = max(8, round(max(left_box[3] - left_box[1], right_box[3] - right_box[1]) * 1.5))
            if gap < -2 or gap > max_gap or _vertical_overlap_ratio(left_box, right_box) < 0.5:
                rejected.append("fragment_geometry_mismatch")
                continue
            combined = left_text + right_text
            if combined != _ACTION_TERMINAL_TEXT:
                rejected.append("fragment_text_not_exact")
                continue
            between = any(
                index not in (left_index, right_index)
                and bounds is not None
                and left_box[2] <= _center(bounds)[0] <= right_box[0]
                and _vertical_overlap_ratio(left_box, bounds) >= 0.5
                for index, _, bounds, _ in parsed
            )
            if between:
                rejected.append("fragment_crosses_other_item")
                continue
            merged_box = (
                min(left_box[0], right_box[0]), min(left_box[1], right_box[1]),
                max(left_box[2], right_box[2]), max(left_box[3], right_box[3]),
            )
            if not _valid_action_terminal_bbox(merged_box, width, height):
                rejected.append("merged_bbox_invalid")
                continue
            merged_count += 1
            candidates.append(ActionTerminalCandidate(
                "merged_ocr_fragments", merged_box, min(left_score, right_score),
                (
                    _semantic_evidence_id(left_text, left_box),
                    _semantic_evidence_id(right_text, right_box),
                ),
                (left_index, right_index),
            ))

    deduplicated: list[ActionTerminalCandidate] = []
    for candidate in sorted(
        candidates,
        key=lambda item: (item.candidate_type == "exact_ocr", item.score),
        reverse=True,
    ):
        duplicate_index = next(
            (index for index, existing in enumerate(deduplicated) if _iou(candidate.bbox, existing.bbox) >= 0.75),
            None,
        )
        if duplicate_index is None:
            deduplicated.append(candidate)
            continue
        existing = deduplicated[duplicate_index]
        deduplicated[duplicate_index] = ActionTerminalCandidate(
            existing.candidate_type,
            existing.bbox,
            max(existing.score, candidate.score),
            tuple(dict.fromkeys(existing.evidence_ids + candidate.evidence_ids)),
            tuple(dict.fromkeys(existing.source_indices + candidate.source_indices)),
        )
        rejected.append("duplicate_bbox_collapsed")

    region_candidates = [
        candidate for candidate in deduplicated
        if _inside_target_region(candidate.bbox, width, height)
    ]
    if deduplicated and not region_candidates:
        rejected.append("candidate_outside_target_region")
    safe_candidates: list[ActionTerminalCandidate] = []
    for candidate in region_candidates:
        point = candidate.point
        if not (0 <= point[0] < width and 0 <= point[1] < height):
            rejected.append("candidate_center_out_of_bounds")
            continue
        if not (candidate.bbox[0] <= point[0] < candidate.bbox[2] and candidate.bbox[1] <= point[1] < candidate.bbox[3]):
            rejected.append("candidate_center_outside_bbox")
            continue
        overlaps_other = any(
            index not in candidate.source_indices
            and bounds is not None
            and bounds[0] <= point[0] < bounds[2]
            and bounds[1] <= point[1] < bounds[3]
            and _normalized_text(text) not in (_ACTION_TERMINAL_TEXT,) + _ACTION_TERMINAL_FRAGMENTS
            for index, text, bounds, _ in parsed
        )
        if overlaps_other:
            rejected.append("candidate_center_overlaps_other_ocr")
            continue
        safe_candidates.append(candidate)

    candidate = safe_candidates[0] if len(safe_candidates) == 1 else None
    failure_class = None
    if candidate is None:
        if len(safe_candidates) > 1 or len(region_candidates) > 1 or len(deduplicated) > 1:
            failure_class = "OCR_MULTIPLE_MATCHES"
        elif exact_items and not candidates:
            failure_class = "BBOX_INVALID"
        elif fragment_items and merged_count == 0:
            failure_class = "OCR_FRAGMENTED"
        elif not candidates:
            failure_class = "OCR_NO_MATCH"
        elif not region_candidates:
            failure_class = "REGION_FILTER_REJECTED"
        else:
            failure_class = "SAFE_POINT_REJECTED"
    evidence = CandidateResolutionEvidence(
        phase=phase,
        raw_item_count=len(items),
        exact_match_count=len(exact_items),
        fragment_match_count=len(fragment_items),
        merged_candidate_count=merged_count,
        deduplicated_candidate_count=len(deduplicated),
        region_filtered_candidate_count=len(region_candidates),
        safe_candidate_count=len(safe_candidates),
        candidate_region=ACTION_TERMINAL_REGION_NORMALIZED,
        candidate_type=candidate.candidate_type if candidate else None,
        candidate_bbox=candidate.bbox if candidate else None,
        candidate_score=candidate.score if candidate else None,
        candidate_evidence_ids=candidate.evidence_ids if candidate else (),
        rejected_candidate_reasons=tuple(dict.fromkeys(rejected)),
        failure_class=failure_class,
    )
    return candidate, evidence


def _action_terminal_failure_reason(evidence: CandidateResolutionEvidence) -> str:
    return {
        "OCR_NO_MATCH": "action_terminal_ocr_no_match",
        "OCR_FRAGMENTED": "action_terminal_fragment_ambiguous",
        "OCR_MULTIPLE_MATCHES": "action_terminal_candidate_not_unique",
        "DUPLICATE_BOXES": "action_terminal_candidate_not_unique",
        "REGION_FILTER_REJECTED": "action_terminal_candidate_unsafe",
        "BBOX_INVALID": "action_terminal_candidate_unsafe",
        "SAFE_POINT_REJECTED": "action_terminal_candidate_unsafe",
    }.get(str(evidence.failure_class), "action_terminal_candidate_unsafe")


def _same_normalized_terminal_identity(
    initial: ActionTerminalCandidate,
    fresh: ActionTerminalCandidate,
    *,
    initial_size: tuple[int, int],
    fresh_size: tuple[int, int],
) -> bool:
    initial_center = initial.point
    fresh_center = fresh.point
    delta_x = abs(initial_center[0] / initial_size[0] - fresh_center[0] / fresh_size[0])
    delta_y = abs(initial_center[1] / initial_size[1] - fresh_center[1] / fresh_size[1])
    return delta_x <= 0.03 and delta_y <= 0.03


def _items(frame: object) -> tuple[dict, ...]:
    ocr = getattr(frame, "ocr", None)
    return tuple(ocr()) if callable(ocr) else ()


def _matches(items: Sequence[Mapping[str, object]], marker: str) -> list[dict]:
    return [dict(item) for item in items if marker in str(item.get("text", "")).replace(" ", "")]


def observe_action_summary(frame: object) -> ActionSummaryObservation:
    """Classify only source-backed states used by the old navigation chain."""

    items = _items(frame)
    texts = tuple(str(item.get("text", "")).replace(" ", "") for item in items)
    joined = "|".join(texts)
    capture_id = str(getattr(frame, "source_capture_id", "") or "")

    task_count = sum(any(label in text for text in texts) for label in _SIEGE_PAGE_LABELS)
    challenge_count = sum("进入挑战" in text for text in texts)
    has_old_title = "利刃围剿" in joined
    # One historical title is deliberately insufficient.  A page needs a
    # list structure and a stable action affordance as independent cues.
    if (has_old_title or task_count >= 2) and task_count >= 2 and challenge_count >= 1:
        return ActionSummaryObservation(
            ActionSummaryState.ACTION_SUMMARY_VISIBLE,
            frame_sha256(frame), capture_id, items,
            ("action_summary_list", "action_summary_action_region", "action_summary_layout"),
        )
    if any(marker in joined for marker in _FOREIGN_MARKERS):
        return ActionSummaryObservation(
            ActionSummaryState.FOREIGN_PAGE, frame_sha256(frame), capture_id, items,
            (), ("foreign_page_cue",),
        )
    home = resident_home_state(list(items))
    if home is ResidentHomeState.HOME_READY:
        return ActionSummaryObservation(
            ActionSummaryState.HOME_READY, frame_sha256(frame), capture_id, items,
            ("home_ready",),
        )
    if "触碰空白区域退出" in joined:
        return ActionSummaryObservation(
            ActionSummaryState.OPTIONAL_OVERLAY_VISIBLE,
            frame_sha256(frame), capture_id, items, ("known_blank_exit_overlay",),
        )
    if len(_matches(items, "行动汇总")) == 1:
        return ActionSummaryObservation(
            ActionSummaryState.ACTION_SUMMARY_ENTRY_VISIBLE,
            frame_sha256(frame), capture_id, items, ("action_summary_entry",),
        )
    if len(_matches(items, "全域整备")) == 1:
        return ActionSummaryObservation(
            ActionSummaryState.ACTIVITY_OVERVIEW_VISIBLE,
            frame_sha256(frame), capture_id, items, ("full_realm_card",),
        )
    return ActionSummaryObservation(
        ActionSummaryState.UNKNOWN, frame_sha256(frame), capture_id, items, (),
        ("recognized_state_absent",),
    )


class ActionSummaryNavigator:
    """Navigate four reviewed stages with at most one dispatch per stage."""

    def __init__(
        self,
        *,
        frame_provider: Callable[[], object],
        tap: Callable[..., object],
        geometry_provider: Callable[[], object] | None = None,
        evidence_recorder: Callable[[NavigationAttemptEvidence], object] = record_navigation_attempt,
        budget: EpisodeActionBudget | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        cancellation: Callable[[], bool] = lambda: False,
        postcondition_timeout: float = 4.0,
        poll_interval: float = 0.4,
        dispatch_backend: str = "device_control",
    ) -> None:
        self.frame_provider = frame_provider
        self.tap = tap
        self.geometry_provider = geometry_provider
        self.evidence_recorder = evidence_recorder
        self.budget = budget or EpisodeActionBudget(clock=monotonic)
        self.monotonic = monotonic
        self.sleep = sleep
        self.cancellation = cancellation
        self.postcondition_timeout = float(postcondition_timeout)
        self.poll_interval = float(poll_interval)
        self.dispatch_backend = dispatch_backend
        self.safe_selector = AnnouncementSafeRegionSelector()

    @staticmethod
    def _frame_size(frame: object) -> tuple[int, int]:
        image = getattr(frame, "image", None)
        if image is None or not hasattr(image, "shape") or len(image.shape) < 2:
            raise ValueError("action_summary_capture_pixels_missing")
        height, width = map(int, image.shape[:2])
        return width, height

    def _coordinate_chain(self, frame: object, point: tuple[int, int]) -> CoordinateChain:
        capture_size = self._frame_size(frame)
        render_size = capture_size
        if self.geometry_provider is not None:
            geometry = self.geometry_provider()
            render_size = (int(geometry.physical_width), int(geometry.physical_height))
        return CoordinateChain.from_capture_point(
            point,
            capture_size=capture_size,
            render_client_size=render_size,
            device_size=render_size,
            source_coordinate_space="CAPTURE_PIXELS",
        )

    def _candidate(
        self, frame: object, observation: ActionSummaryObservation, marker: str,
    ) -> tuple[tuple[int, int], tuple[int, int, int, int], str, float, int] | None:
        matches = [item for item in _matches(observation.items, marker) if _bbox(item)]
        if len(matches) != 1:
            return None
        bounds = _bbox(matches[0])
        assert bounds is not None
        score = float(matches[0].get("score", 1.0))
        return _center(bounds), bounds, f"ocr_{marker}", score, len(matches)

    def _overlay_candidate(
        self, frame: object, observation: ActionSummaryObservation,
    ) -> tuple[tuple[int, int], tuple[int, int, int, int], str, float, int] | None:
        width, height = self._frame_size(frame)
        bboxes = tuple(bounds for item in observation.items if (bounds := _bbox(item)))
        non_prompt = tuple(
            bounds for item in observation.items
            if "触碰空白区域退出" not in str(item.get("text", ""))
            and (bounds := _bbox(item))
        )
        if not non_prompt:
            return None
        dialog = (
            max(0, min(box[0] for box in non_prompt) - 30),
            max(0, min(box[1] for box in non_prompt) - 12),
            min(width, max(box[2] for box in non_prompt) + 30),
            min(height, max(box[3] for box in non_prompt) + 12),
        )
        safety = self.safe_selector.select(
            frame, overlay_bbox=(0, 0, width, height), dialog_bbox=dialog,
            ocr_bboxes=bboxes,
        )
        if len(safety.candidates) < 1:
            return None
        candidate = safety.candidates[0]
        return (
            candidate.point,
            candidate.bbox,
            "computed_safe_blank_region",
            round(1.0 - candidate.edge_density, 6),
            len(safety.candidates),
        )

    def _wait_after_dispatch(
        self,
        evidence: NavigationAttemptEvidence,
        *,
        accepted: set[ActionSummaryState],
        dispatch_started: float,
        pre_capture_id: str,
        timeline: list[dict],
    ) -> tuple[ActionSummaryObservation | None, str]:
        deadline = dispatch_started + self.postcondition_timeout
        last_capture_id = pre_capture_id
        while self.monotonic() < deadline:
            if self.cancellation():
                return None, "cancelled"
            self.sleep(min(self.poll_interval, max(0.0, deadline - self.monotonic())))
            if self.monotonic() >= deadline:
                break
            try:
                frame = self.frame_provider()
            except Exception:  # noqa: BLE001 - precise stop class
                return None, "post_capture_failure"
            try:
                observed = observe_action_summary(frame)
            except Exception:  # noqa: BLE001 - detector failure is not a page
                return None, "post_detector_failure"
            elapsed = max(0.0, self.monotonic() - dispatch_started)
            if observed.capture_id and observed.capture_id == last_capture_id:
                timeline.append({
                    "attempt_id": evidence.attempt_id,
                    "post_observation_index": len(evidence.post_observations),
                    "elapsed_since_dispatch_seconds": round(elapsed, 6),
                    "post_state": observed.state.value,
                    "transition_classification": "STALE",
                    "post_frame_sha256": observed.frame_hash,
                    "positive_cues": [],
                    "negative_cues": ["stale_capture"],
                    "reason_codes": ["stale_post_frame_ignored"],
                })
                continue
            last_capture_id = observed.capture_id or last_capture_id
            classification = "PASS" if observed.state in accepted else (
                "FAIL" if observed.state is ActionSummaryState.FOREIGN_PAGE else "PENDING"
            )
            evidence.add_post_observation(
                frame=frame, state=observed.state.value,
                positive_cues=observed.positive_cues,
                negative_cues=observed.negative_cues,
                reason_codes=(f"transition_{classification.lower()}",),
                postcondition_result=classification,
            )
            timeline.append({
                "attempt_id": evidence.attempt_id,
                "post_observation_index": len(evidence.post_observations),
                "elapsed_since_dispatch_seconds": round(elapsed, 6),
                "post_state": observed.state.value,
                "transition_classification": classification,
                "post_frame_sha256": observed.frame_hash,
                "positive_cues": list(observed.positive_cues),
                "negative_cues": list(observed.negative_cues),
                "reason_codes": [f"transition_{classification.lower()}"],
            })
            if classification != "PENDING":
                return observed, "postcondition_reached"
        return None, "postcondition_timeout"

    def navigate(self) -> ActionSummaryResult:
        evidences: list[NavigationAttemptEvidence] = []
        timeline: list[dict] = []
        candidate_resolutions: list[CandidateResolutionEvidence] = []
        dispatches = 0
        if self.cancellation():
            return ActionSummaryResult(False, ActionSummaryState.UNKNOWN, "cancelled", 0, 0, [], [])
        try:
            initial_frame = self.frame_provider()
        except Exception:  # noqa: BLE001 - precise precondition stop class
            return ActionSummaryResult(False, ActionSummaryState.UNKNOWN, "initial_capture_failure", 0, 0, [], [])
        try:
            current = observe_action_summary(initial_frame)
        except Exception:  # noqa: BLE001 - detector failure is not UNKNOWN
            return ActionSummaryResult(False, ActionSummaryState.UNKNOWN, "initial_detector_failure", 0, 0, [], [])
        if current.state is ActionSummaryState.ACTION_SUMMARY_VISIBLE:
            return ActionSummaryResult(True, current.state, "already_visible", 0, 0, [], [])
        if current.state is not ActionSummaryState.HOME_READY:
            return ActionSummaryResult(False, current.state, "home_ready_precondition_failed", 0, 0, [], [])
        initial_terminal, initial_resolution = resolve_action_terminal_candidate(
            initial_frame, phase="initial"
        )
        candidate_resolutions.append(initial_resolution)
        if initial_terminal is None:
            return ActionSummaryResult(
                False, current.state,
                _action_terminal_failure_reason(initial_resolution),
                0, 0, [], [], candidate_resolutions,
            )
        initial_terminal_size = self._frame_size(initial_frame)

        stages = (
            (ActionSummaryState.HOME_READY, "作战终端", {ActionSummaryState.ACTIVITY_OVERVIEW_VISIBLE}, "open_action_entry"),
            (ActionSummaryState.ACTIVITY_OVERVIEW_VISIBLE, "全域整备", {ActionSummaryState.ACTION_SUMMARY_ENTRY_VISIBLE, ActionSummaryState.OPTIONAL_OVERLAY_VISIBLE, ActionSummaryState.ACTION_SUMMARY_VISIBLE}, "open_activity_overview"),
            (ActionSummaryState.ACTION_SUMMARY_ENTRY_VISIBLE, "行动汇总", {ActionSummaryState.ACTION_SUMMARY_VISIBLE}, "open_action_summary"),
        )
        stage_index = 0
        while True:
            if current.state is ActionSummaryState.ACTION_SUMMARY_VISIBLE:
                return ActionSummaryResult(
                    True, current.state, "action_summary_visible", dispatches,
                    dispatches, evidences, timeline, candidate_resolutions,
                )
            if current.state is ActionSummaryState.OPTIONAL_OVERLAY_VISIBLE:
                marker = "safe_blank"
                accepted = {ActionSummaryState.ACTION_SUMMARY_ENTRY_VISIBLE, ActionSummaryState.ACTION_SUMMARY_VISIBLE}
                entry_name = "dismiss_known_optional_overlay"
                candidate_resolver = self._overlay_candidate
            else:
                if stage_index >= len(stages) or current.state is not stages[stage_index][0]:
                    return ActionSummaryResult(False, current.state, "unexpected_page", dispatches, dispatches, evidences, timeline, candidate_resolutions)
                _, marker, accepted, entry_name = stages[stage_index]
                candidate_resolver = lambda frame, obs, value=marker: self._candidate(frame, obs, value)

            if self.cancellation():
                return ActionSummaryResult(False, current.state, "cancelled", dispatches, dispatches, evidences, timeline, candidate_resolutions)
            try:
                planned_frame = self.frame_provider()
            except Exception:  # noqa: BLE001 - precise pre-dispatch stop class
                return ActionSummaryResult(False, current.state, "fresh_capture_failure", dispatches, dispatches, evidences, timeline, candidate_resolutions)
            try:
                planned = observe_action_summary(planned_frame)
            except Exception:  # noqa: BLE001 - detector failure is not UNKNOWN
                return ActionSummaryResult(False, current.state, "fresh_detector_failure", dispatches, dispatches, evidences, timeline, candidate_resolutions)
            if planned.state is not current.state:
                return ActionSummaryResult(False, planned.state, "fresh_confirmation_state_changed", dispatches, dispatches, evidences, timeline, candidate_resolutions)
            if current.capture_id and planned.capture_id and current.capture_id == planned.capture_id:
                return ActionSummaryResult(
                    False, planned.state, "stale_frame_action", dispatches,
                    dispatches, evidences, timeline, candidate_resolutions,
                )
            if stage_index == 0 and current.state is ActionSummaryState.HOME_READY:
                fresh_terminal, fresh_resolution = resolve_action_terminal_candidate(
                    planned_frame, phase="fresh"
                )
                candidate_resolutions.append(fresh_resolution)
                if fresh_terminal is None:
                    return ActionSummaryResult(
                        False, planned.state,
                        "action_terminal_fresh_confirmation_failed",
                        dispatches, dispatches, evidences, timeline,
                        candidate_resolutions,
                    )
                if not _same_normalized_terminal_identity(
                    initial_terminal,
                    fresh_terminal,
                    initial_size=initial_terminal_size,
                    fresh_size=self._frame_size(planned_frame),
                ):
                    return ActionSummaryResult(
                        False, planned.state,
                        "action_terminal_fresh_confirmation_failed",
                        dispatches, dispatches, evidences, timeline,
                        candidate_resolutions,
                    )
                candidate = (
                    fresh_terminal.point,
                    fresh_terminal.bbox,
                    fresh_terminal.candidate_type,
                    fresh_terminal.score,
                    fresh_resolution.safe_candidate_count,
                )
            else:
                candidate = candidate_resolver(planned_frame, planned)
            if candidate is None:
                return ActionSummaryResult(
                    False, planned.state, "candidate_not_unique_or_safe",
                    dispatches, dispatches, evidences, timeline,
                    candidate_resolutions,
                )
            point, bounds, candidate_type, candidate_score, candidate_count = candidate
            try:
                chain = self._coordinate_chain(planned_frame, point)
            except (TypeError, ValueError):
                return ActionSummaryResult(False, planned.state, "coordinate_chain_incomplete", dispatches, dispatches, evidences, timeline, candidate_resolutions)
            decision = self.budget.authorize(
                state=planned.state.value,
                action_type="ACTION_SUMMARY_NAVIGATION",
                normalized_point=chain.device_point,
            )
            if not decision.allowed or dispatches >= 4:
                return ActionSummaryResult(False, planned.state, decision.reason_code, dispatches, dispatches, evidences, timeline, candidate_resolutions)
            evidence = NavigationAttemptEvidence(
                task_name="action_summary_entry", entry_name=entry_name,
                pre_state=planned.state.value, pre_frame_sha256=planned.frame_hash,
                coordinate_chain=chain, candidate_type=candidate_type,
                candidate_bbox=bounds, candidate_score=candidate_score,
                candidate_count=candidate_count,
                dispatch_backend=self.dispatch_backend, random_offset_enabled=False,
                random_offset_requested=False, actual_dispatched_point=chain.device_point,
            )
            evidences.append(evidence)
            try:
                result = self.tap(
                    chain.device_point, random_offset=False,
                    intent=ActionIntent("action_summary_navigation", entry_name, evidence.attempt_id),
                )
            except Exception as error:  # noqa: BLE001 - record exact dispatch boundary
                evidence.mark_dispatch(requested=True, acknowledged=False, result=f"dispatch_exception:{type(error).__name__}")
                self.evidence_recorder(evidence)
                return ActionSummaryResult(False, planned.state, "dispatch_failure", dispatches, dispatches, evidences, timeline, candidate_resolutions)
            if result is False:
                evidence.mark_dispatch(requested=True, acknowledged=False, result="dispatch_rejected")
                self.evidence_recorder(evidence)
                return ActionSummaryResult(False, planned.state, "dispatch_failure", dispatches, dispatches, evidences, timeline, candidate_resolutions)
            evidence.mark_dispatch(requested=True, acknowledged=True, result="call_returned")
            self.budget.record_dispatch(decision)
            dispatches += 1
            started = self.monotonic()
            observed, wait_reason = self._wait_after_dispatch(
                evidence, accepted=accepted, dispatch_started=started,
                pre_capture_id=planned.capture_id, timeline=timeline,
            )
            self.budget.record_result(decision, "PASS" if observed and observed.state in accepted else "FAIL")
            self.evidence_recorder(evidence)
            if observed is None:
                return ActionSummaryResult(
                    False, planned.state, wait_reason, dispatches, dispatches,
                    evidences, timeline, candidate_resolutions,
                )
            if observed.state is ActionSummaryState.FOREIGN_PAGE:
                return ActionSummaryResult(
                    False, observed.state, "unexpected_page", dispatches,
                    dispatches, evidences, timeline, candidate_resolutions,
                )
            current = observed
            if current.state is not ActionSummaryState.OPTIONAL_OVERLAY_VISIBLE:
                stage_index += 1


__all__ = [
    "ACTION_TERMINAL_REGION_NORMALIZED", "ActionSummaryNavigator",
    "ActionSummaryObservation", "ActionSummaryResult", "ActionSummaryState",
    "ActionTerminalCandidate", "CandidateResolutionEvidence",
    "observe_action_summary", "resolve_action_terminal_candidate",
]
