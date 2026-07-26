"""Bounded, evidence-backed navigation to the action-summary page.

This adapter stops as soon as the page is proved.  It never selects a stage,
starts a sweep, consumes resources, or retries a dispatched action.
"""

from __future__ import annotations

import time
import hashlib
import re
import unicodedata
import cv2 as cv
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
from core.services.runtime_navigation_kernel import (
    Confidence,
    DEFAULT_CAPABILITIES,
    InteractionTarget,
    PageKind,
    PagePerception,
    PageSignature,
    PROVEN_NAVIGATION_CONTRACTS,
    RuntimeNavigationKernel,
    TransitionClassification,
    TransitionClassifier,
    UiState,
    confirm_fresh_capability,
    confirm_fresh_target,
    normalize_legacy_state,
)
from core.services.screen_state import (
    ResidentHomeState,
    is_inventory_screen,
    resident_home_state,
)


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
    confidence: Confidence = Confidence.UNKNOWN
    source_frame: object | None = field(default=None, compare=False, repr=False)

    def to_ui_state(self) -> UiState:
        page = (
            "UNKNOWN"
            if self.state is ActionSummaryState.OPTIONAL_OVERLAY_VISIBLE
            else self.state.value
        )
        overlays = (
            ("OPTIONAL_OVERLAY_VISIBLE",)
            if self.state is ActionSummaryState.OPTIONAL_OVERLAY_VISIBLE
            else ()
        )
        return normalize_legacy_state(
            page,
            overlays=overlays,
            phase="ACTION_SUMMARY_NAVIGATION",
            confidence=self.confidence,
            evidence=self.positive_cues,
            frame_hash=self.frame_hash,
            capture_id=self.capture_id,
        )


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
    hit_target_resolutions: list["HitTargetResolutionEvidence"] = field(default_factory=list)
    first_stage_result: str = "NOT_RUN"
    global_prep_candidate_resolutions: list["GlobalPrepResolutionEvidence"] = field(default_factory=list)
    global_prep_hit_target_resolutions: list["GlobalPrepHitTargetEvidence"] = field(default_factory=list)
    global_prep_stage_result: str = "NOT_RUN"
    action_summary_entry_resolutions: list["ActionSummaryEntryResolutionEvidence"] = field(default_factory=list)


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


@dataclass(frozen=True)
class ActionTerminalHitTarget:
    label_bbox: tuple[int, int, int, int]
    label_center: tuple[int, int]
    container_bbox: tuple[int, int, int, int]
    container_detection_method: str
    container_confidence: float
    icon_bbox: tuple[int, int, int, int]
    icon_relation_to_label: str
    hit_target_bbox: tuple[int, int, int, int]
    hit_target_point: tuple[int, int]
    hit_target_reason: str
    occlusion_detected: bool
    occlusion_bboxes: tuple[tuple[int, int, int, int], ...]

    def to_dict(self) -> dict:
        return {
            "label_bbox": self.label_bbox,
            "label_center": self.label_center,
            "container_bbox": self.container_bbox,
            "container_detection_method": self.container_detection_method,
            "container_confidence": self.container_confidence,
            "icon_bbox": self.icon_bbox,
            "icon_relation_to_label": self.icon_relation_to_label,
            "hit_target_bbox": self.hit_target_bbox,
            "hit_target_point": self.hit_target_point,
            "hit_target_reason": self.hit_target_reason,
            "occlusion_detected": self.occlusion_detected,
            "occlusion_bboxes": self.occlusion_bboxes,
        }


@dataclass(frozen=True)
class HitTargetResolutionEvidence:
    phase: str
    label_bbox: tuple[int, int, int, int]
    visual_component_count: int
    parent_container_count: int
    trusted_expanded_region: tuple[float, float, float, float]
    target: ActionTerminalHitTarget | None
    rejected_reasons: tuple[str, ...]

    def to_dict(self) -> dict:
        return {
            "phase": self.phase,
            "label_bbox": self.label_bbox,
            "visual_component_count": self.visual_component_count,
            "parent_container_count": self.parent_container_count,
            "trusted_expanded_region": self.trusted_expanded_region,
            "target": self.target.to_dict() if self.target else None,
            "rejected_reasons": self.rejected_reasons,
        }


@dataclass(frozen=True)
class GlobalPrepCandidate:
    candidate_type: str
    bbox: tuple[int, int, int, int]
    score: float
    evidence_ids: tuple[str, ...]
    source_indices: tuple[int, ...]

    @property
    def point(self) -> tuple[int, int]:
        return _center(self.bbox)


@dataclass(frozen=True)
class GlobalPrepResolutionEvidence:
    phase: str
    raw_item_count: int
    exact_match_count: int
    broad_match_count: int
    fragment_match_count: int
    merged_candidate_count: int
    deduplicated_candidate_count: int
    region_filtered_candidate_count: int
    safe_candidate_count: int
    candidate_region: tuple[float, float, float, float]
    candidate: GlobalPrepCandidate | None
    rejected_reasons: tuple[str, ...]
    failure_class: str | None

    def to_dict(self) -> dict:
        return {
            "phase": self.phase,
            "raw_item_count": self.raw_item_count,
            "exact_match_count": self.exact_match_count,
            "broad_match_count": self.broad_match_count,
            "fragment_match_count": self.fragment_match_count,
            "merged_candidate_count": self.merged_candidate_count,
            "deduplicated_candidate_count": self.deduplicated_candidate_count,
            "region_filtered_candidate_count": self.region_filtered_candidate_count,
            "safe_candidate_count": self.safe_candidate_count,
            "candidate_region": self.candidate_region,
            "candidate_type": self.candidate.candidate_type if self.candidate else None,
            "candidate_bbox": self.candidate.bbox if self.candidate else None,
            "candidate_score": self.candidate.score if self.candidate else None,
            "candidate_evidence_ids": self.candidate.evidence_ids if self.candidate else (),
            "rejected_reasons": self.rejected_reasons,
            "failure_class": self.failure_class,
        }


@dataclass(frozen=True)
class GlobalPrepHitTarget:
    label_bbox: tuple[int, int, int, int]
    label_center: tuple[int, int]
    candidate_type: str
    candidate_evidence_ids: tuple[str, ...]
    parent_container_bbox: tuple[int, int, int, int]
    container_detection_method: str
    icon_bbox: tuple[int, int, int, int] | None
    icon_relation_to_label: str
    hit_target_bbox: tuple[int, int, int, int]
    hit_target_point: tuple[int, int]
    occlusion_detected: bool
    occlusion_bboxes: tuple[tuple[int, int, int, int], ...]

    def to_dict(self) -> dict:
        return {
            "label_bbox": self.label_bbox,
            "label_center": self.label_center,
            "candidate_type": self.candidate_type,
            "candidate_evidence_ids": self.candidate_evidence_ids,
            "parent_container_bbox": self.parent_container_bbox,
            "container_detection_method": self.container_detection_method,
            "icon_bbox": self.icon_bbox,
            "icon_relation_to_label": self.icon_relation_to_label,
            "hit_target_bbox": self.hit_target_bbox,
            "hit_target_point": self.hit_target_point,
            "occlusion_detected": self.occlusion_detected,
            "occlusion_bboxes": self.occlusion_bboxes,
        }


@dataclass(frozen=True)
class GlobalPrepHitTargetEvidence:
    phase: str
    visual_parent_count: int
    target: GlobalPrepHitTarget | None
    rejected_reasons: tuple[str, ...]

    def to_dict(self) -> dict:
        return {
            "phase": self.phase,
            "visual_parent_count": self.visual_parent_count,
            "target": self.target.to_dict() if self.target else None,
            "rejected_reasons": self.rejected_reasons,
        }


@dataclass(frozen=True)
class ActionSummaryEntryResolutionEvidence:
    phase: str
    exact_match_count: int
    region_filtered_candidate_count: int
    parent_container_count: int
    target: InteractionTarget | None
    rejected_reasons: tuple[str, ...]
    failure_class: str | None

    def to_dict(self) -> dict:
        target = self.target
        return {
            "phase": self.phase,
            "exact_match_count": self.exact_match_count,
            "region_filtered_candidate_count": self.region_filtered_candidate_count,
            "parent_container_count": self.parent_container_count,
            "target": (
                {
                    "semantic_id": target.semantic_id,
                    "candidate_count": target.candidate_count,
                    "label_bbox": target.label_bbox,
                    "parent_bbox": target.parent_bbox,
                    "hit_target_bbox": target.hit_target_bbox,
                    "hit_target_point": target.hit_target_point,
                    "capture_size": target.capture_size,
                    "method": target.method,
                    "occluded": target.occluded,
                }
                if target is not None
                else None
            ),
            "rejected_reasons": self.rejected_reasons,
            "failure_class": self.failure_class,
        }


_SIEGE_PAGE_LABELS = (
    "特殊订单", "利刃行动", "挑灯看剑", "武器材质分析", "骑士小说",
    "我思我在", "所知所闻", "大的！", "总体围剿",
)
_EXCHANGE_FOREIGN_MARKERS = (
    "交易所", "我要买", "我要卖", "全部买入", "全部卖出", "买入价格", "卖出价格",
)
_BROWSER_FOREIGN_MARKERS = ("http//", "https//", "Chrome", "浏览器", "网页")
_LOGIN_FOREIGN_MARKERS = ("点击屏幕进入游戏", "账号登录", "验证码", "用户协议")


_ACTION_PAGE_KERNEL = RuntimeNavigationKernel(
    (
        PageSignature(
            "action_summary.page",
            "ACTION_SUMMARY_VISIBLE",
            PageKind.TRUSTED,
            required_all=frozenset({"action_summary_layout"}),
            capabilities=DEFAULT_CAPABILITIES["ACTION_SUMMARY_VISIBLE"],
            priority=120,
        ),
        # The real Global Prep page contains both its title and the Action
        # Summary entry.  This multi-cue signature must outrank every generic
        # word that may also occur in the descriptive copy (for example
        # ``材料``).
        PageSignature(
            "global_prep.page.multi_cue",
            "GLOBAL_PREP_PAGE",
            PageKind.TRUSTED,
            required_all=frozenset({
                "global_prep_page_title", "action_summary_entry_unique",
            }),
            capabilities=DEFAULT_CAPABILITIES["GLOBAL_PREP_PAGE"],
            priority=115,
        ),
        # Compatibility with cropped/legacy frames that expose only the
        # unique entry.  The cue is still specific and cannot be synthesized
        # by one broad descriptive keyword.
        PageSignature(
            "global_prep.page.entry_only",
            "GLOBAL_PREP_PAGE",
            PageKind.TRUSTED,
            required_all=frozenset({"action_summary_entry_unique"}),
            capabilities=DEFAULT_CAPABILITIES["GLOBAL_PREP_PAGE"],
            priority=105,
            confidence=Confidence.MEDIUM,
        ),
        PageSignature(
            "activity_overview.page",
            "ACTIVITY_OVERVIEW_VISIBLE",
            PageKind.TRUSTED,
            required_all=frozenset({"global_prep_entry_unique"}),
            forbidden=frozenset({"action_summary_entry_unique"}),
            capabilities=DEFAULT_CAPABILITIES["ACTIVITY_OVERVIEW_VISIBLE"],
            priority=95,
        ),
        PageSignature(
            "resident_home.page",
            "HOME_READY",
            PageKind.TRUSTED,
            required_all=frozenset({"resident_home_ready"}),
            capabilities=DEFAULT_CAPABILITIES["HOME_READY"],
            priority=90,
        ),
        PageSignature(
            "blank_exit.overlay",
            "BLANK_EXIT_OVERLAY",
            PageKind.OVERLAY,
            required_all=frozenset({"blank_exit_overlay"}),
            priority=90,
        ),
        PageSignature(
            "announcement.overlay",
            "ANNOUNCEMENT_OVERLAY",
            PageKind.OVERLAY,
            required_all=frozenset({"announcement_overlay"}),
            priority=80,
        ),
        PageSignature(
            "checkin.overlay",
            "CHECKIN_OVERLAY",
            PageKind.OVERLAY,
            required_all=frozenset({"checkin_overlay"}),
            priority=80,
        ),
        PageSignature(
            "inventory.foreign",
            "INVENTORY",
            PageKind.FOREIGN,
            required_all=frozenset({"inventory_category_rail"}),
            priority=70,
        ),
        PageSignature(
            "exchange.foreign",
            "EXCHANGE_PAGE",
            PageKind.FOREIGN,
            required_all=frozenset({"exchange_transaction_layout"}),
            priority=70,
        ),
        PageSignature(
            "external_browser.foreign",
            "EXTERNAL_BROWSER",
            PageKind.FOREIGN,
            required_all=frozenset({"external_browser_layout"}),
            priority=70,
        ),
        PageSignature(
            "login.foreign",
            "LOGIN_PAGE",
            PageKind.FOREIGN,
            required_all=frozenset({"login_layout"}),
            priority=70,
        ),
    )
)


# Derived from two read-only 1280x720 HOME frames on 2026-07-26.  The terminal
# button bbox was (1149,394)-(1232,422) and (1148,391)-(1232,423); the legacy
# measured point (1180,415) is inside the same region.  The quest copy containing
# the same words was above y=298, outside this normalized target region.
ACTION_TERMINAL_REGION_NORMALIZED = (0.86, 0.52, 0.99, 0.62)
ACTION_TERMINAL_PARENT_REGION_NORMALIZED = (0.83, 0.50, 1.0, 0.66)
GLOBAL_PREP_LABEL_REGION_NORMALIZED = (0.005, 0.32, 0.18, 0.48)
GLOBAL_PREP_PARENT_REGION_NORMALIZED = (0.005, 0.30, 0.20, 0.52)
ACTION_SUMMARY_ENTRY_LABEL_REGION_NORMALIZED = (0.62, 0.28, 0.90, 0.58)
ACTION_SUMMARY_ENTRY_PARENT_REGION_NORMALIZED = (0.48, 0.20, 0.96, 0.72)
_ACTION_SUMMARY_ENTRY_TEXT = "\u884c\u52a8\u6c47\u603b"
_ACTION_TERMINAL_TEXT = "作战终端"
_ACTION_TERMINAL_FRAGMENTS = ("作战", "终端")
_GLOBAL_PREP_TEXT = "全域整备"
_GLOBAL_PREP_FRAGMENTS = ("全域", "整备")
_OCR_SEPARATOR = re.compile(r"[\s|｜:：·•._\-—]+")


def _intersection_area(
    first: tuple[int, int, int, int], second: tuple[int, int, int, int]
) -> int:
    return max(0, min(first[2], second[2]) - max(first[0], second[0])) * max(
        0, min(first[3], second[3]) - max(first[1], second[1])
    )


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


def resolve_action_terminal_hit_target(
    frame: object,
    candidate: ActionTerminalCandidate,
    *,
    phase: str,
) -> tuple[ActionTerminalHitTarget | None, HitTargetResolutionEvidence]:
    """Bind the semantic label to one visual icon/card and derive one safe point.

    The HOME shortcut is a right-edge card: a circular high-contrast icon sits
    immediately left of the OCR label.  The label remains the semantic anchor,
    while the click is derived from the icon's visual component.  No coordinate
    fallback is used when that relationship cannot be proved uniquely.
    """

    image = getattr(frame, "image", None)
    label = candidate.bbox
    rejected: list[str] = []
    if image is None or not hasattr(image, "shape") or len(image.shape) < 2:
        evidence = HitTargetResolutionEvidence(
            phase, label, 0, 0, ACTION_TERMINAL_PARENT_REGION_NORMALIZED,
            None, ("capture_pixels_missing",),
        )
        return None, evidence
    height, width = map(int, image.shape[:2])
    label_height = max(1, label[3] - label[1])
    search = (
        max(0, label[0] - round(2.0 * label_height)),
        max(0, label[1] - round(0.5 * label_height)),
        label[0],
        min(height, label[3] + round(0.75 * label_height)),
    )
    if search[2] <= search[0] or search[3] <= search[1]:
        evidence = HitTargetResolutionEvidence(
            phase, label, 0, 0, ACTION_TERMINAL_PARENT_REGION_NORMALIZED,
            None, ("icon_search_region_invalid",),
        )
        return None, evidence

    roi = image[search[1]:search[3], search[0]:search[2]]
    gray = cv.cvtColor(roi, cv.COLOR_BGR2GRAY) if len(roi.shape) == 3 else roi
    edges = cv.Canny(gray, 60, 160)
    edges = cv.dilate(
        edges, cv.getStructuringElement(cv.MORPH_ELLIPSE, (3, 3)), iterations=1,
    )
    contours, _ = cv.findContours(edges, cv.RETR_EXTERNAL, cv.CHAIN_APPROX_SIMPLE)
    components: list[tuple[tuple[int, int, int, int], float]] = []
    min_side = max(6, round(label_height * 0.65))
    max_side = max(min_side, round(label_height * 2.2))
    label_center = _center(label)
    trusted_left, trusted_top, trusted_right, trusted_bottom = (
        ACTION_TERMINAL_PARENT_REGION_NORMALIZED
    )
    for contour in contours:
        x, y, component_width, component_height = cv.boundingRect(contour)
        if not (
            min_side <= component_width <= max_side
            and min_side <= component_height <= max_side
        ):
            continue
        aspect = component_width / float(max(1, component_height))
        if not 0.65 <= aspect <= 1.45:
            continue
        bounds = (
            search[0] + x, search[1] + y,
            search[0] + x + component_width, search[1] + y + component_height,
        )
        center = _center(bounds)
        if center[0] >= label[0] or abs(center[1] - label_center[1]) > label_height:
            continue
        if label[0] - bounds[2] > label_height * 0.75:
            continue
        if not (
            trusted_left <= center[0] / width <= trusted_right
            and trusted_top <= center[1] / height <= trusted_bottom
        ):
            rejected.append("icon_component_outside_trusted_region")
            continue
        fill = cv.contourArea(contour) / float(max(1, component_width * component_height))
        if fill < 0.05:
            continue
        confidence = min(1.0, 0.55 + fill + max(0.0, 0.2 - abs(aspect - 1.0)))
        components.append((bounds, round(confidence, 6)))

    if len(components) != 1:
        rejected.append(
            "visual_parent_missing" if not components else "visual_parent_not_unique"
        )
        evidence = HitTargetResolutionEvidence(
            phase, label, len(components), len(components),
            ACTION_TERMINAL_PARENT_REGION_NORMALIZED, None,
            tuple(dict.fromkeys(rejected)),
        )
        return None, evidence

    icon_bbox, confidence = components[0]
    margin_y = max(2, round(label_height * 0.25))
    container = (
        icon_bbox[0],
        max(0, min(icon_bbox[1], label[1]) - margin_y),
        min(width, label[2] + max(label_height, label[2] - label[0])),
        min(height, max(icon_bbox[3], label[3]) + margin_y),
    )
    if not (
        container[0] / width >= trusted_left
        and container[1] / height >= trusted_top
        and container[2] / width <= trusted_right
        and container[3] / height <= trusted_bottom
    ):
        evidence = HitTargetResolutionEvidence(
            phase, label, 1, 0, ACTION_TERMINAL_PARENT_REGION_NORMALIZED,
            None, ("parent_container_outside_trusted_region",),
        )
        return None, evidence

    inset_x = max(2, round((icon_bbox[2] - icon_bbox[0]) * 0.25))
    inset_y = max(2, round((icon_bbox[3] - icon_bbox[1]) * 0.25))
    hit_bbox = (
        icon_bbox[0] + inset_x, icon_bbox[1] + inset_y,
        icon_bbox[2] - inset_x, icon_bbox[3] - inset_y,
    )
    if hit_bbox[0] >= hit_bbox[2] or hit_bbox[1] >= hit_bbox[3]:
        evidence = HitTargetResolutionEvidence(
            phase, label, 1, 0, ACTION_TERMINAL_PARENT_REGION_NORMALIZED,
            None, ("safe_icon_inset_empty",),
        )
        return None, evidence

    items = _items(frame)
    modal_markers = (
        "触碰空白区域退出", "每日签到奖励", "资讯", "公告",
    )
    modal_present = any(
        marker in _normalized_text(item.get("text", ""))
        for marker in modal_markers for item in items
    )
    occlusions: list[tuple[int, int, int, int]] = []
    for index, item in enumerate(items):
        bounds = _bbox(item)
        if bounds is None or index in candidate.source_indices:
            continue
        if _intersection_area(bounds, hit_bbox) > 0:
            occlusions.append(bounds)
        container_overlap = _intersection_area(bounds, container)
        container_area = max(1, (container[2] - container[0]) * (container[3] - container[1]))
        if container_overlap / container_area >= 0.35:
            rejected.append("parent_overlaps_adjacent_recognized_control")
    if modal_present:
        rejected.append("known_modal_present")
    occluded = modal_present or bool(occlusions)
    target = ActionTerminalHitTarget(
        label_bbox=label,
        label_center=label_center,
        container_bbox=container,
        container_detection_method="left_icon_edge_component_plus_text_alignment",
        container_confidence=confidence,
        icon_bbox=icon_bbox,
        icon_relation_to_label="LEFT_ALIGNED_SAME_HORIZONTAL_CARD",
        hit_target_bbox=hit_bbox,
        hit_target_point=_center(hit_bbox),
        hit_target_reason="safe_inset_of_unique_parent_icon_away_from_label_and_card_edges",
        occlusion_detected=occluded,
        occlusion_bboxes=tuple(occlusions),
    )
    parent_count = 1
    if occluded or "parent_overlaps_adjacent_recognized_control" in rejected:
        evidence = HitTargetResolutionEvidence(
            phase, label, 1, parent_count,
            ACTION_TERMINAL_PARENT_REGION_NORMALIZED, target,
            tuple(dict.fromkeys(rejected)),
        )
        return None, evidence
    evidence = HitTargetResolutionEvidence(
        phase, label, 1, parent_count,
        ACTION_TERMINAL_PARENT_REGION_NORMALIZED, target,
        tuple(dict.fromkeys(rejected)),
    )
    return target, evidence


def resolve_global_prep_candidate(
    frame: object, *, phase: str
) -> tuple[GlobalPrepCandidate | None, GlobalPrepResolutionEvidence]:
    """Resolve the exact left-rail Global Prep label without substring authorization."""

    items = _items(frame)
    image = getattr(frame, "image", None)
    if image is None or not hasattr(image, "shape") or len(image.shape) < 2:
        evidence = GlobalPrepResolutionEvidence(
            phase, len(items), 0, 0, 0, 0, 0, 0, 0,
            GLOBAL_PREP_LABEL_REGION_NORMALIZED, None,
            ("capture_dimensions_missing",), "BBOX_INVALID",
        )
        return None, evidence
    height, width = map(int, image.shape[:2])
    parsed = [
        (
            index,
            _normalized_text(item.get("text", "")),
            _bbox(item),
            float(item.get("score", 0.0) or 0.0),
        )
        for index, item in enumerate(items)
    ]
    exact = [entry for entry in parsed if entry[1] == _GLOBAL_PREP_TEXT]
    broad = [entry for entry in parsed if _GLOBAL_PREP_TEXT in entry[1] and entry[1] != _GLOBAL_PREP_TEXT]
    fragments = [entry for entry in parsed if entry[1] in _GLOBAL_PREP_FRAGMENTS]
    candidates: list[GlobalPrepCandidate] = []
    rejected: list[str] = []

    def valid(bounds: tuple[int, int, int, int] | None) -> bool:
        if bounds is None:
            return False
        left, top, right, bottom = bounds
        if not (0 <= left < right <= width and 0 <= top < bottom <= height):
            return False
        box_width, box_height = right - left, bottom - top
        area = box_width * box_height / float(width * height)
        return 0.00025 <= area <= 0.015 and 1.2 <= box_width / box_height <= 8.0

    for index, text, bounds, score in exact:
        if not valid(bounds):
            rejected.append("exact_bbox_invalid")
            continue
        assert bounds is not None
        candidates.append(GlobalPrepCandidate(
            "exact_ocr", bounds, score,
            (_semantic_evidence_id(text, bounds),), (index,),
        ))

    merged_count = 0
    left_fragments = [entry for entry in fragments if entry[1] == "全域"]
    right_fragments = [entry for entry in fragments if entry[1] == "整备"]
    for left_item in left_fragments:
        for right_item in right_fragments:
            li, lt, lb, ls = left_item
            ri, rt, rb, rs = right_item
            if lb is None or rb is None:
                rejected.append("fragment_bbox_missing")
                continue
            gap = rb[0] - lb[2]
            max_gap = max(6, round(max(lb[3] - lb[1], rb[3] - rb[1]) * 1.25))
            if gap < -2 or gap > max_gap or _vertical_overlap_ratio(lb, rb) < 0.6:
                rejected.append("fragment_geometry_mismatch")
                continue
            merged = (min(lb[0], rb[0]), min(lb[1], rb[1]), max(lb[2], rb[2]), max(lb[3], rb[3]))
            if not valid(merged):
                rejected.append("merged_bbox_invalid")
                continue
            intervening = any(
                index not in (li, ri) and bounds is not None
                and lb[2] <= _center(bounds)[0] <= rb[0]
                and _vertical_overlap_ratio(lb, bounds) >= 0.5
                for index, _, bounds, _ in parsed
            )
            if intervening:
                rejected.append("fragment_crosses_other_item")
                continue
            merged_count += 1
            candidates.append(GlobalPrepCandidate(
                "merged_ocr_fragments", merged, min(ls, rs),
                (_semantic_evidence_id(lt, lb), _semantic_evidence_id(rt, rb)),
                (li, ri),
            ))

    deduplicated: list[GlobalPrepCandidate] = []
    for candidate in sorted(candidates, key=lambda value: value.score, reverse=True):
        duplicate_index = next(
            (index for index, existing in enumerate(deduplicated) if _iou(candidate.bbox, existing.bbox) >= 0.75),
            None,
        )
        if duplicate_index is None:
            deduplicated.append(candidate)
        else:
            existing = deduplicated[duplicate_index]
            deduplicated[duplicate_index] = GlobalPrepCandidate(
                existing.candidate_type,
                existing.bbox,
                max(existing.score, candidate.score),
                tuple(dict.fromkeys(existing.evidence_ids + candidate.evidence_ids)),
                tuple(dict.fromkeys(existing.source_indices + candidate.source_indices)),
            )
            rejected.append("duplicate_bbox_collapsed")

    region = []
    rleft, rtop, rright, rbottom = GLOBAL_PREP_LABEL_REGION_NORMALIZED
    for candidate in deduplicated:
        center = candidate.point
        if rleft <= center[0] / width <= rright and rtop <= center[1] / height <= rbottom:
            region.append(candidate)
        else:
            rejected.append("candidate_outside_global_prep_region")
    safe: list[GlobalPrepCandidate] = []
    for candidate in region:
        point = candidate.point
        overlap = any(
            index not in candidate.source_indices and bounds is not None
            and bounds[0] <= point[0] < bounds[2] and bounds[1] <= point[1] < bounds[3]
            for index, _, bounds, _ in parsed
        )
        if overlap:
            rejected.append("candidate_center_overlaps_other_ocr")
        else:
            safe.append(candidate)
    selected = safe[0] if len(safe) == 1 else None
    failure = None
    if selected is None:
        if len(safe) > 1 or len(region) > 1 or len(deduplicated) > 1:
            failure = "OCR_MULTIPLE_MATCHES"
        elif exact and not candidates:
            failure = "BBOX_INVALID"
        elif fragments and merged_count == 0:
            failure = "OCR_FRAGMENTED"
        elif not candidates:
            failure = "OCR_NO_MATCH"
        elif not region:
            failure = "REGION_FILTER_REJECTED"
        else:
            failure = "SAFE_POINT_REJECTED"
    evidence = GlobalPrepResolutionEvidence(
        phase, len(items), len(exact), len(broad), len(fragments), merged_count,
        len(deduplicated), len(region), len(safe),
        GLOBAL_PREP_LABEL_REGION_NORMALIZED, selected,
        tuple(dict.fromkeys(rejected)), failure,
    )
    return selected, evidence


def resolve_global_prep_hit_target(
    frame: object, candidate: GlobalPrepCandidate, *, phase: str
) -> tuple[GlobalPrepHitTarget | None, GlobalPrepHitTargetEvidence]:
    """Bind the label to one enclosing left-rail visual card and use its inset."""

    image = getattr(frame, "image", None)
    if image is None or not hasattr(image, "shape") or len(image.shape) < 2:
        return None, GlobalPrepHitTargetEvidence(
            phase, 0, None, ("capture_pixels_missing",),
        )
    height, width = map(int, image.shape[:2])
    label = candidate.bbox
    label_height = max(1, label[3] - label[1])
    search = (
        max(0, label[0] - label_height),
        max(0, label[1] - 4 * label_height),
        min(width, label[2] + 8 * label_height),
        min(height, label[3] + label_height),
    )
    roi = image[search[1]:search[3], search[0]:search[2]]
    gray = cv.cvtColor(roi, cv.COLOR_BGR2GRAY) if len(roi.shape) == 3 else roi
    edges = cv.Canny(gray, 50, 150)
    contours, _ = cv.findContours(edges, cv.RETR_LIST, cv.CHAIN_APPROX_SIMPLE)
    raw_parents: list[tuple[int, int, int, int]] = []
    label_center = candidate.point
    for contour in contours:
        x, y, card_width, card_height = cv.boundingRect(contour)
        bounds = (
            search[0] + x, search[1] + y,
            search[0] + x + card_width, search[1] + y + card_height,
        )
        if not (
            5 * label_height <= card_width <= 13 * label_height
            and 3 * label_height <= card_height <= 7 * label_height
            and 1.5 <= card_width / float(max(1, card_height)) <= 4.5
            and bounds[0] <= label_center[0] < bounds[2]
            and bounds[1] <= label_center[1] < bounds[3]
        ):
            continue
        fill = cv.contourArea(contour) / float(max(1, card_width * card_height))
        if fill >= 0.4:
            raw_parents.append(bounds)
    parents: list[tuple[int, int, int, int]] = []
    for bounds in sorted(raw_parents, key=lambda value: (value[2] - value[0]) * (value[3] - value[1]), reverse=True):
        if not any(_iou(bounds, existing) >= 0.8 for existing in parents):
            parents.append(bounds)
    rejected: list[str] = []
    if len(parents) != 1:
        rejected.append("visual_parent_missing" if not parents else "visual_parent_not_unique")
        return None, GlobalPrepHitTargetEvidence(
            phase, len(parents), None, tuple(rejected),
        )
    parent = parents[0]
    pleft, ptop, pright, pbottom = GLOBAL_PREP_PARENT_REGION_NORMALIZED
    if not (
        parent[0] / width >= pleft and parent[1] / height >= ptop
        and parent[2] / width <= pright and parent[3] / height <= pbottom
    ):
        return None, GlobalPrepHitTargetEvidence(
            phase, 0, None, ("parent_outside_trusted_region",),
        )
    inset_x = max(3, round((parent[2] - parent[0]) * 0.20))
    inset_y = max(3, round((parent[3] - parent[1]) * 0.20))
    hit_bbox = (
        parent[0] + inset_x, parent[1] + inset_y,
        parent[2] - inset_x, parent[3] - inset_y,
    )
    items = _items(frame)
    modal_markers = ("触碰空白区域退出", "每日签到奖励", "资讯", "公告")
    modal_present = any(
        marker in _normalized_text(item.get("text", ""))
        for marker in modal_markers for item in items
    )
    occlusions = tuple(
        bounds for index, item in enumerate(items)
        if index not in candidate.source_indices
        and (bounds := _bbox(item)) is not None
        and _intersection_area(bounds, hit_bbox) > 0
    )
    target = GlobalPrepHitTarget(
        label, label_center, candidate.candidate_type, candidate.evidence_ids,
        parent, "enclosing_high_contrast_left_rail_card_contour",
        None, "LABEL_INSIDE_CARD_ART", hit_bbox, _center(hit_bbox),
        modal_present or bool(occlusions), occlusions,
    )
    if target.occlusion_detected:
        rejected.append("global_prep_target_occluded")
        return None, GlobalPrepHitTargetEvidence(
            phase, 1, target, tuple(rejected),
        )
    return target, GlobalPrepHitTargetEvidence(phase, 1, target, ())


def resolve_action_summary_entry_target(
    frame: object, *, phase: str
) -> tuple[InteractionTarget | None, ActionSummaryEntryResolutionEvidence]:
    """Bind the unique Action Summary label to its enclosing action card.

    The semantic OCR label proves identity but is never used as the physical
    point. The target is a deterministic lower action band inside the single
    enclosing right-side card.
    """

    items = _items(frame)
    image = getattr(frame, "image", None)
    if image is None or not hasattr(image, "shape") or len(image.shape) < 2:
        evidence = ActionSummaryEntryResolutionEvidence(
            phase, 0, 0, 0, None, ("capture_pixels_missing",),
            "CAPTURE_PIXELS_MISSING",
        )
        return None, evidence
    height, width = map(int, image.shape[:2])
    exact = [
        (index, bounds)
        for index, item in enumerate(items)
        if _normalized_text(item.get("text", "")) == _ACTION_SUMMARY_ENTRY_TEXT
        and (bounds := _bbox(item)) is not None
    ]
    if len(exact) != 1:
        evidence = ActionSummaryEntryResolutionEvidence(
            phase, len(exact), 0, 0, None,
            ("semantic_anchor_missing" if not exact else "semantic_anchor_not_unique",),
            "SEMANTIC_ANCHOR_NOT_UNIQUE",
        )
        return None, evidence
    _, label = exact[0]
    label_center = _center(label)
    lleft, ltop, lright, lbottom = ACTION_SUMMARY_ENTRY_LABEL_REGION_NORMALIZED
    in_region = (
        lleft <= label_center[0] / width <= lright
        and ltop <= label_center[1] / height <= lbottom
    )
    if not in_region:
        evidence = ActionSummaryEntryResolutionEvidence(
            phase, 1, 0, 0, None, ("semantic_anchor_outside_trusted_region",),
            "SEMANTIC_ANCHOR_REGION_REJECTED",
        )
        return None, evidence

    pleft, ptop, pright, pbottom = ACTION_SUMMARY_ENTRY_PARENT_REGION_NORMALIZED
    search = (
        max(0, round(pleft * width)),
        max(0, round(ptop * height)),
        min(width, round(pright * width)),
        min(height, round(pbottom * height)),
    )
    roi = image[search[1]:search[3], search[0]:search[2]]
    gray = cv.cvtColor(roi, cv.COLOR_BGR2GRAY) if len(roi.shape) == 3 else roi
    edges = cv.Canny(gray, 30, 100)
    contours, _ = cv.findContours(edges, cv.RETR_LIST, cv.CHAIN_APPROX_SIMPLE)
    raw_parents: list[tuple[int, int, int, int]] = []
    for contour in contours:
        x, y, card_width, card_height = cv.boundingRect(contour)
        bounds = (
            search[0] + x,
            search[1] + y,
            search[0] + x + card_width,
            search[1] + y + card_height,
        )
        if not (
            0.25 * width <= card_width <= 0.48 * width
            and 0.18 * height <= card_height <= 0.38 * height
            and 1.5 <= card_width / float(max(1, card_height)) <= 3.5
            and bounds[0] <= label_center[0] < bounds[2]
            and bounds[1] <= label_center[1] < bounds[3]
        ):
            continue
        perimeter = cv.arcLength(contour, True)
        expected_perimeter = 2.0 * (card_width + card_height)
        if perimeter >= 0.55 * expected_perimeter:
            raw_parents.append(bounds)
    parents: list[tuple[int, int, int, int]] = []
    for bounds in sorted(
        raw_parents,
        key=lambda value: (value[2] - value[0]) * (value[3] - value[1]),
        reverse=True,
    ):
        area = max(1, (bounds[2] - bounds[0]) * (bounds[3] - bounds[1]))
        if not any(
            _iou(bounds, existing) >= 0.80
            or _intersection_area(bounds, existing)
            / float(min(
                area,
                max(1, (existing[2] - existing[0]) * (existing[3] - existing[1])),
            ))
            >= 0.90
            for existing in parents
        ):
            parents.append(bounds)
    if len(parents) != 1:
        evidence = ActionSummaryEntryResolutionEvidence(
            phase, 1, 1, len(parents), None,
            ("visual_parent_missing" if not parents else "visual_parent_not_unique",),
            "PARENT_CONTROL_NOT_UNIQUE",
        )
        return None, evidence

    parent = parents[0]
    parent_width = parent[2] - parent[0]
    parent_height = parent[3] - parent[1]
    hit_bbox = (
        parent[0] + round(parent_width * 0.38),
        parent[1] + round(parent_height * 0.62),
        parent[2] - round(parent_width * 0.06),
        parent[3] - round(parent_height * 0.08),
    )
    modal_markers = (
        "\u89e6\u78b0\u7a7a\u767d\u533a\u57df\u9000\u51fa",
        "\u6bcf\u65e5\u7b7e\u5230\u5956\u52b1",
        "\u8d44\u8baf",
        "\u516c\u544a",
    )
    modal_present = any(
        marker in _normalized_text(item.get("text", ""))
        for marker in modal_markers
        for item in items
    )
    target = InteractionTarget(
        semantic_id="ACTION_SUMMARY_ENTRY",
        candidate_count=1,
        label_bbox=label,
        parent_bbox=parent,
        hit_target_bbox=hit_bbox,
        hit_target_point=_center(hit_bbox),
        capture_size=(width, height),
        method="enclosing_right_action_card_contour",
        occluded=modal_present,
    )
    if target.occluded:
        evidence = ActionSummaryEntryResolutionEvidence(
            phase, 1, 1, 1, target, ("target_occluded",), "TARGET_OCCLUDED"
        )
        return None, evidence
    evidence = ActionSummaryEntryResolutionEvidence(
        phase, 1, 1, 1, target, (), None
    )
    return target, evidence


def _same_global_prep_identity(
    initial: GlobalPrepHitTarget,
    fresh: GlobalPrepHitTarget,
    *,
    initial_size: tuple[int, int],
    fresh_size: tuple[int, int],
) -> bool:
    return (
        initial.candidate_type == fresh.candidate_type
        and abs(initial.hit_target_point[0] / initial_size[0] - fresh.hit_target_point[0] / fresh_size[0]) <= 0.03
        and abs(initial.hit_target_point[1] / initial_size[1] - fresh.hit_target_point[1] / fresh_size[1]) <= 0.03
    )


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


def _same_normalized_hit_target_identity(
    initial: ActionTerminalHitTarget,
    fresh: ActionTerminalHitTarget,
    *,
    initial_size: tuple[int, int],
    fresh_size: tuple[int, int],
) -> bool:
    initial_point = initial.hit_target_point
    fresh_point = fresh.hit_target_point
    return (
        abs(initial_point[0] / initial_size[0] - fresh_point[0] / fresh_size[0]) <= 0.03
        and abs(initial_point[1] / initial_size[1] - fresh_point[1] / fresh_size[1]) <= 0.03
        and initial.container_detection_method == fresh.container_detection_method
    )


def _items(frame: object) -> tuple[dict, ...]:
    ocr = getattr(frame, "ocr", None)
    return tuple(ocr()) if callable(ocr) else ()


def _matches(items: Sequence[Mapping[str, object]], marker: str) -> list[dict]:
    normalized_marker = _normalized_text(marker)
    return [
        dict(item)
        for item in items
        if normalized_marker in _normalized_text(item.get("text", ""))
    ]


def _exact_matches(items: Sequence[Mapping[str, object]], marker: str) -> list[dict]:
    normalized_marker = _normalized_text(marker)
    return [
        dict(item)
        for item in items
        if _normalized_text(item.get("text", "")) == normalized_marker
    ]


def observe_action_summary(frame: object) -> ActionSummaryObservation:
    """Classify navigation pages through composable, ordered signatures.

    Specific trusted pages are resolved before overlays and strongly committed
    foreign pages.  Generic words such as ``材料`` never decide a page by
    themselves.
    """

    items = _items(frame)
    texts = tuple(_normalized_text(item.get("text", "")) for item in items)
    joined = "|".join(texts)
    capture_id = str(getattr(frame, "source_capture_id", "") or "")

    task_count = sum(any(label in text for text in texts) for label in _SIEGE_PAGE_LABELS)
    challenge_count = sum("进入挑战" in text for text in texts)
    has_old_title = "利刃围剿" in joined
    has_blank_exit = "触碰空白区域退出" in joined
    facts: set[str] = set()
    # One historical title is deliberately insufficient. A page needs a list
    # structure and a stable action affordance as independent cues.
    if (has_old_title or task_count >= 2) and task_count >= 2 and challenge_count >= 1:
        facts.add("action_summary_layout")

    image = getattr(frame, "image", None)
    height, width = (
        map(int, image.shape[:2])
        if image is not None and hasattr(image, "shape") and len(image.shape) >= 2
        else (0, 0)
    )
    action_entries = [
        item for item in _exact_matches(items, "行动汇总")
        if (bounds := _bbox(item)) is not None and width > 0 and height > 0
        and ACTION_SUMMARY_ENTRY_LABEL_REGION_NORMALIZED[0]
        <= _center(bounds)[0] / width
        <= ACTION_SUMMARY_ENTRY_LABEL_REGION_NORMALIZED[2]
        and ACTION_SUMMARY_ENTRY_LABEL_REGION_NORMALIZED[1]
        <= _center(bounds)[1] / height
        <= ACTION_SUMMARY_ENTRY_LABEL_REGION_NORMALIZED[3]
    ]
    if len(action_entries) == 1:
        facts.add("action_summary_entry_unique")
    global_prep_matches = [
        item for item in _exact_matches(items, "全域整备")
        if (bounds := _bbox(item)) is not None and width > 0 and height > 0
    ]
    left_rail_global_prep = [
        item for item in global_prep_matches
        if (bounds := _bbox(item)) is not None
        and GLOBAL_PREP_LABEL_REGION_NORMALIZED[0] <= _center(bounds)[0] / width
        <= GLOBAL_PREP_LABEL_REGION_NORMALIZED[2]
        and GLOBAL_PREP_LABEL_REGION_NORMALIZED[1] <= _center(bounds)[1] / height
        <= GLOBAL_PREP_LABEL_REGION_NORMALIZED[3]
    ]
    page_title_global_prep = [
        item for item in global_prep_matches
        if (bounds := _bbox(item)) is not None
        and 0.50 <= _center(bounds)[0] / width <= 0.80
        and 0.05 <= _center(bounds)[1] / height <= 0.22
    ]
    if len(left_rail_global_prep) == 1:
        facts.add("global_prep_entry_unique")
    if len(page_title_global_prep) == 1:
        facts.add("global_prep_page_title")

    home = resident_home_state(list(items))
    if home is ResidentHomeState.HOME_READY:
        facts.add("resident_home_ready")
    elif home is ResidentHomeState.ANNOUNCEMENT_OVERLAY:
        facts.add("announcement_overlay")
    elif home is ResidentHomeState.CHECKIN_OVERLAY:
        facts.add("checkin_overlay")
    # Overlay identity is orthogonal to the base page. Background OCR can
    # still expose HOME or activity cues, so derive these facts independently.
    announcement_cues = sum(marker in joined for marker in ("资讯", "公告"))
    if announcement_cues >= 2 or (announcement_cues == 1 and has_blank_exit):
        facts.add("announcement_overlay")
    if any(marker in joined for marker in ("每日签到奖励", "签到奖励")):
        facts.add("checkin_overlay")
    if has_blank_exit:
        facts.add("blank_exit_overlay")

    # Foreign pages require a layout or multiple independent cues.  A single
    # common noun cannot override a more specific trusted signature.
    if is_inventory_screen(list(items)):
        facts.add("inventory_category_rail")
    if sum(marker in joined for marker in _EXCHANGE_FOREIGN_MARKERS) >= 2:
        facts.add("exchange_transaction_layout")
    if sum(marker in joined for marker in _BROWSER_FOREIGN_MARKERS) >= 2:
        facts.add("external_browser_layout")
    if sum(marker in joined for marker in _LOGIN_FOREIGN_MARKERS) >= 2:
        facts.add("login_layout")

    normalized = _ACTION_PAGE_KERNEL.classify(
        PagePerception.from_facts(
            facts,
            frame_hash=frame_sha256(frame),
            capture_id=capture_id,
        ),
        phase="ACTION_SUMMARY_NAVIGATION",
    )
    if normalized.overlays:
        return ActionSummaryObservation(
            ActionSummaryState.OPTIONAL_OVERLAY_VISIBLE,
            normalized.frame_hash,
            capture_id,
            items,
            tuple(f"overlay:{name.casefold()}" for name in normalized.overlays),
            confidence=normalized.confidence,
            source_frame=frame,
        )
    state_map = {
        "HOME_READY": ActionSummaryState.HOME_READY,
        "ACTIVITY_OVERVIEW_VISIBLE": ActionSummaryState.ACTIVITY_OVERVIEW_VISIBLE,
        "GLOBAL_PREP_PAGE": ActionSummaryState.ACTION_SUMMARY_ENTRY_VISIBLE,
        "ACTION_SUMMARY_VISIBLE": ActionSummaryState.ACTION_SUMMARY_VISIBLE,
    }
    if normalized.base_page in state_map:
        return ActionSummaryObservation(
            state_map[normalized.base_page],
            normalized.frame_hash,
            capture_id,
            items,
            normalized.evidence,
            confidence=normalized.confidence,
            source_frame=frame,
        )
    if not normalized.is_unknown:
        return ActionSummaryObservation(
            ActionSummaryState.FOREIGN_PAGE,
            normalized.frame_hash,
            capture_id,
            items,
            (),
            tuple(f"foreign:{cue}" for cue in normalized.evidence),
            confidence=normalized.confidence,
            source_frame=frame,
        )
    return ActionSummaryObservation(
        ActionSummaryState.UNKNOWN, normalized.frame_hash, capture_id, items, (),
        ("recognized_state_absent",), confidence=normalized.confidence,
        source_frame=frame,
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
        stop_after_first_stage: bool = False,
        stop_after_global_prep_stage: bool = False,
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
        self.stop_after_first_stage = bool(stop_after_first_stage)
        self.stop_after_global_prep_stage = bool(stop_after_global_prep_stage)
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

    def _wait_first_stage_after_dispatch(
        self,
        evidence: NavigationAttemptEvidence,
        *,
        dispatch_started: float,
        pre_capture_id: str,
        timeline: list[dict],
    ) -> tuple[ActionSummaryObservation | None, str]:
        """Classify touch effect for the one-action diagnostic probe only."""

        deadline = dispatch_started + self.postcondition_timeout
        last_capture_id = pre_capture_id
        stable_changed_hash = ""
        stable_changed_count = 0
        any_changed = False
        while self.monotonic() < deadline:
            if self.cancellation():
                return None, "CANCELLED"
            self.sleep(min(self.poll_interval, max(0.0, deadline - self.monotonic())))
            if self.monotonic() >= deadline:
                break
            try:
                frame = self.frame_provider()
                observed = observe_action_summary(frame)
            except Exception:  # noqa: BLE001 - exact diagnostic stop class
                return None, "POST_OBSERVATION_FAILURE"
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
            frame_changed = bool(
                observed.frame_hash and observed.frame_hash != evidence.pre_frame_sha256
            )
            target_page_changed = observed.state is not ActionSummaryState.HOME_READY
            evidence.mark_post_effect(
                frame_changed=frame_changed,
                target_page_changed=target_page_changed,
            )
            any_changed = any_changed or frame_changed
            classification = "PENDING"
            if observed.state is ActionSummaryState.ACTIVITY_OVERVIEW_VISIBLE:
                classification = "EXPECTED_PAGE"
            elif observed.state is ActionSummaryState.FOREIGN_PAGE:
                classification = "KNOWN_FOREIGN_PAGE"
            elif frame_changed and observed.state is ActionSummaryState.UNKNOWN:
                if observed.frame_hash == stable_changed_hash:
                    stable_changed_count += 1
                else:
                    stable_changed_hash = observed.frame_hash
                    stable_changed_count = 1
                if stable_changed_count >= 2:
                    classification = "STABLE_CHANGED_UNKNOWN"
            else:
                stable_changed_hash = ""
                stable_changed_count = 0
            evidence.add_post_observation(
                frame=frame,
                state=observed.state.value,
                positive_cues=observed.positive_cues,
                negative_cues=observed.negative_cues,
                reason_codes=(f"first_stage_{classification.casefold()}",),
                postcondition_result=classification,
            )
            timeline.append({
                "attempt_id": evidence.attempt_id,
                "post_observation_index": len(evidence.post_observations),
                "elapsed_since_dispatch_seconds": round(elapsed, 6),
                "post_state": observed.state.value,
                "transition_classification": classification,
                "post_frame_sha256": observed.frame_hash,
                "frame_changed": frame_changed,
                "home_anchor_changed": target_page_changed,
                "action_terminal_label_disappeared": target_page_changed,
                "positive_cues": list(observed.positive_cues),
                "negative_cues": list(observed.negative_cues),
                "reason_codes": [f"first_stage_{classification.casefold()}"],
            })
            if classification != "PENDING":
                return observed, classification
        return None, (
            "TRANSITION_TIMEOUT" if any_changed else "NO_TOUCH_EFFECT_OBSERVED"
        )

    def _wait_global_prep_after_dispatch(
        self,
        evidence: NavigationAttemptEvidence,
        *,
        dispatch_started: float,
        pre_capture_id: str,
        timeline: list[dict],
    ) -> tuple[ActionSummaryObservation | None, str]:
        deadline = dispatch_started + self.postcondition_timeout
        last_capture_id = pre_capture_id
        transition = TransitionClassifier(
            PROVEN_NAVIGATION_CONTRACTS["OPEN_GLOBAL_PREP"],
            source_state=normalize_legacy_state(
                "ACTIVITY_OVERVIEW_VISIBLE",
                phase="OPEN_GLOBAL_PREP",
                frame_hash=evidence.pre_frame_sha256,
                capture_id=pre_capture_id,
            ),
            stable_unknown_frames=2,
        )
        while self.monotonic() < deadline:
            if self.cancellation():
                return None, "CANCELLED"
            self.sleep(min(self.poll_interval, max(0.0, deadline - self.monotonic())))
            if self.monotonic() >= deadline:
                break
            try:
                frame = self.frame_provider()
                observed = observe_action_summary(frame)
            except Exception:  # noqa: BLE001 - precise diagnostic boundary
                return None, "POST_OBSERVATION_FAILURE"
            elapsed = max(0.0, self.monotonic() - dispatch_started)
            if observed.capture_id and observed.capture_id == last_capture_id:
                timeline.append({
                    "attempt_id": evidence.attempt_id,
                    "post_observation_index": len(evidence.post_observations),
                    "elapsed_since_dispatch_seconds": round(elapsed, 6),
                    "post_state": observed.state.value,
                    "transition_classification": "STALE",
                    "post_frame_sha256": observed.frame_hash,
                    "reason_codes": ["stale_post_frame_ignored"],
                })
                continue
            last_capture_id = observed.capture_id or last_capture_id
            target_page_changed = observed.state is not ActionSummaryState.ACTIVITY_OVERVIEW_VISIBLE
            frame_changed = bool(
                observed.frame_hash and observed.frame_hash != evidence.pre_frame_sha256
            )
            evidence.mark_post_effect(
                frame_changed=frame_changed,
                target_page_changed=target_page_changed,
            )
            transition_decision = transition.observe(observed.to_ui_state())
            if transition_decision.classification is TransitionClassification.EXPECTED_POST_STATE:
                classification = (
                    "ACTION_SUMMARY_VISIBLE"
                    if observed.state is ActionSummaryState.ACTION_SUMMARY_VISIBLE
                    else "EXPECTED_NEXT_PAGE"
                )
            elif transition_decision.classification is TransitionClassification.OPTIONAL_OVERLAY_REACHED:
                classification = "KNOWN_OPTIONAL_OVERLAY"
            elif transition_decision.classification is TransitionClassification.STABLE_CHANGED_UNKNOWN:
                classification = "STABLE_CHANGED_UNKNOWN"
            elif transition_decision.classification is TransitionClassification.KNOWN_FOREIGN_PAGE:
                classification = "KNOWN_FOREIGN_PAGE"
            else:
                classification = "PENDING"
            evidence.add_post_observation(
                frame=frame, state=observed.state.value,
                positive_cues=observed.positive_cues,
                negative_cues=observed.negative_cues,
                reason_codes=(f"global_prep_{classification.casefold()}",),
                postcondition_result=classification,
            )
            timeline.append({
                "attempt_id": evidence.attempt_id,
                "post_observation_index": len(evidence.post_observations),
                "elapsed_since_dispatch_seconds": round(elapsed, 6),
                "post_state": observed.state.value,
                "transition_classification": classification,
                "post_frame_sha256": observed.frame_hash,
                "frame_changed": frame_changed,
                "global_prep_label_disappeared": target_page_changed,
                "positive_cues": list(observed.positive_cues),
                "negative_cues": list(observed.negative_cues),
                "reason_codes": [f"global_prep_{classification.casefold()}"],
            })
            if classification != "PENDING":
                return observed, classification
        timeout = transition.timeout().classification
        return None, (
            "NO_TOUCH_EFFECT_OBSERVED"
            if timeout is TransitionClassification.NO_TOUCH_EFFECT_OBSERVED
            else "TRANSITION_TIMEOUT"
        )

    def navigate(self) -> ActionSummaryResult:
        evidences: list[NavigationAttemptEvidence] = []
        timeline: list[dict] = []
        candidate_resolutions: list[CandidateResolutionEvidence] = []
        hit_target_resolutions: list[HitTargetResolutionEvidence] = []
        global_prep_candidate_resolutions: list[GlobalPrepResolutionEvidence] = []
        global_prep_hit_target_resolutions: list[GlobalPrepHitTargetEvidence] = []
        action_summary_entry_resolutions: list[ActionSummaryEntryResolutionEvidence] = []
        dispatches = 0
        contract_dispatches: dict[str, int] = {}
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
        start_at_global_prep = bool(
            self.stop_after_global_prep_stage
            and current.state is ActionSummaryState.ACTIVITY_OVERVIEW_VISIBLE
        )
        if self.stop_after_global_prep_stage and not start_at_global_prep:
            return ActionSummaryResult(
                False, current.state, "global_prep_precondition_failed",
                0, 0, [], [],
            )
        start_stage = {
            ActionSummaryState.HOME_READY: 0,
            ActionSummaryState.ACTIVITY_OVERVIEW_VISIBLE: 1,
            ActionSummaryState.ACTION_SUMMARY_ENTRY_VISIBLE: 2,
        }.get(current.state)
        if start_stage is None:
            return ActionSummaryResult(
                False, current.state, "trusted_start_precondition_failed", 0, 0, [], []
            )
        if current.state is ActionSummaryState.HOME_READY:
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
            initial_hit_target, initial_hit_resolution = resolve_action_terminal_hit_target(
                initial_frame, initial_terminal, phase="initial"
            )
            hit_target_resolutions.append(initial_hit_resolution)
            if initial_hit_target is None:
                reason = (
                    "action_terminal_target_occluded"
                    if initial_hit_resolution.target
                    and initial_hit_resolution.target.occlusion_detected
                    else "action_terminal_hit_target_not_unique"
                )
                return ActionSummaryResult(
                    False, current.state, reason, 0, 0, [], [],
                    candidate_resolutions, hit_target_resolutions,
                )
            initial_hit_target_size = initial_terminal_size

        stages = (
            (ActionSummaryState.HOME_READY, "作战终端", {ActionSummaryState.ACTIVITY_OVERVIEW_VISIBLE}, "open_action_entry"),
            (ActionSummaryState.ACTIVITY_OVERVIEW_VISIBLE, "全域整备", {ActionSummaryState.ACTION_SUMMARY_ENTRY_VISIBLE, ActionSummaryState.OPTIONAL_OVERLAY_VISIBLE, ActionSummaryState.ACTION_SUMMARY_VISIBLE}, "open_activity_overview"),
            (ActionSummaryState.ACTION_SUMMARY_ENTRY_VISIBLE, "行动汇总", {ActionSummaryState.ACTION_SUMMARY_VISIBLE}, "open_action_summary"),
        )
        stage_index = start_stage
        while True:
            if current.state is ActionSummaryState.ACTION_SUMMARY_VISIBLE:
                return ActionSummaryResult(
                    True, current.state, "action_summary_visible", dispatches,
                    dispatches, evidences, timeline, candidate_resolutions,
                    global_prep_candidate_resolutions=global_prep_candidate_resolutions,
                    global_prep_hit_target_resolutions=global_prep_hit_target_resolutions,
                    action_summary_entry_resolutions=action_summary_entry_resolutions,
                )
            if current.state is ActionSummaryState.OPTIONAL_OVERLAY_VISIBLE:
                marker = "safe_blank"
                accepted = {ActionSummaryState.ACTION_SUMMARY_ENTRY_VISIBLE, ActionSummaryState.ACTION_SUMMARY_VISIBLE}
                entry_name = "dismiss_known_optional_overlay"
                candidate_resolver = self._overlay_candidate
            elif stage_index == 1 and current.state is ActionSummaryState.ACTIVITY_OVERVIEW_VISIBLE:
                source_frame = current.source_frame
                if source_frame is None:
                    return ActionSummaryResult(
                        False, current.state, "global_prep_initial_frame_missing",
                        dispatches, dispatches, evidences, timeline,
                        candidate_resolutions, hit_target_resolutions,
                        global_prep_candidate_resolutions=global_prep_candidate_resolutions,
                        global_prep_hit_target_resolutions=global_prep_hit_target_resolutions,
                    )
                initial_global, initial_global_resolution = resolve_global_prep_candidate(
                    source_frame, phase="initial"
                )
                global_prep_candidate_resolutions.append(initial_global_resolution)
                if initial_global is None:
                    return ActionSummaryResult(
                        False, current.state,
                        "global_prep_candidate_not_unique" if initial_global_resolution.failure_class == "OCR_MULTIPLE_MATCHES" else "global_prep_candidate_no_match",
                        dispatches, dispatches, evidences, timeline,
                        candidate_resolutions, hit_target_resolutions,
                        global_prep_candidate_resolutions=global_prep_candidate_resolutions,
                    )
                initial_global_hit, initial_global_hit_resolution = resolve_global_prep_hit_target(
                    source_frame, initial_global, phase="initial"
                )
                global_prep_hit_target_resolutions.append(initial_global_hit_resolution)
                if initial_global_hit is None:
                    reason = (
                        "global_prep_target_occluded"
                        if initial_global_hit_resolution.target
                        and initial_global_hit_resolution.target.occlusion_detected
                        else "global_prep_parent_not_unique"
                    )
                    return ActionSummaryResult(
                        False, current.state, reason, dispatches, dispatches,
                        evidences, timeline, candidate_resolutions,
                        hit_target_resolutions,
                        global_prep_candidate_resolutions=global_prep_candidate_resolutions,
                        global_prep_hit_target_resolutions=global_prep_hit_target_resolutions,
                    )
                marker = "全域整备"
                accepted = {
                    ActionSummaryState.ACTION_SUMMARY_ENTRY_VISIBLE,
                    ActionSummaryState.OPTIONAL_OVERLAY_VISIBLE,
                    ActionSummaryState.ACTION_SUMMARY_VISIBLE,
                }
                entry_name = "open_activity_overview"
                candidate_resolver = None
            elif stage_index == 2 and current.state is ActionSummaryState.ACTION_SUMMARY_ENTRY_VISIBLE:
                source_frame = current.source_frame
                if source_frame is None:
                    return ActionSummaryResult(
                        False, current.state, "action_summary_initial_frame_missing",
                        dispatches, dispatches, evidences, timeline,
                        candidate_resolutions,
                        global_prep_candidate_resolutions=global_prep_candidate_resolutions,
                        global_prep_hit_target_resolutions=global_prep_hit_target_resolutions,
                        action_summary_entry_resolutions=action_summary_entry_resolutions,
                    )
                initial_summary_target, initial_summary_resolution = (
                    resolve_action_summary_entry_target(source_frame, phase="initial")
                )
                action_summary_entry_resolutions.append(initial_summary_resolution)
                if initial_summary_target is None:
                    return ActionSummaryResult(
                        False, current.state,
                        "action_summary_parent_target_not_unique",
                        dispatches, dispatches, evidences, timeline,
                        candidate_resolutions,
                        global_prep_candidate_resolutions=global_prep_candidate_resolutions,
                        global_prep_hit_target_resolutions=global_prep_hit_target_resolutions,
                        action_summary_entry_resolutions=action_summary_entry_resolutions,
                    )
                marker = "行动汇总"
                accepted = {ActionSummaryState.ACTION_SUMMARY_VISIBLE}
                entry_name = "open_action_summary"
                candidate_resolver = None
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
                fresh_hit_target, fresh_hit_resolution = resolve_action_terminal_hit_target(
                    planned_frame, fresh_terminal, phase="fresh"
                )
                hit_target_resolutions.append(fresh_hit_resolution)
                if fresh_hit_target is None:
                    reason = (
                        "action_terminal_target_occluded"
                        if fresh_hit_resolution.target
                        and fresh_hit_resolution.target.occlusion_detected
                        else "action_terminal_fresh_confirmation_failed"
                    )
                    return ActionSummaryResult(
                        False, planned.state, reason, dispatches, dispatches,
                        evidences, timeline, candidate_resolutions,
                        hit_target_resolutions,
                    )
                if not _same_normalized_hit_target_identity(
                    initial_hit_target,
                    fresh_hit_target,
                    initial_size=initial_hit_target_size,
                    fresh_size=self._frame_size(planned_frame),
                ):
                    return ActionSummaryResult(
                        False, planned.state,
                        "action_terminal_fresh_confirmation_failed",
                        dispatches, dispatches, evidences, timeline,
                        candidate_resolutions, hit_target_resolutions,
                    )
                candidate = (
                    fresh_hit_target.hit_target_point,
                    fresh_hit_target.hit_target_bbox,
                    f"{fresh_terminal.candidate_type}+parent_visual_icon",
                    fresh_hit_target.container_confidence,
                    fresh_hit_resolution.parent_container_count,
                )
            elif stage_index == 1 and current.state is ActionSummaryState.ACTIVITY_OVERVIEW_VISIBLE:
                fresh_global, fresh_global_resolution = resolve_global_prep_candidate(
                    planned_frame, phase="fresh"
                )
                global_prep_candidate_resolutions.append(fresh_global_resolution)
                if fresh_global is None:
                    return ActionSummaryResult(
                        False, planned.state, "global_prep_fresh_confirmation_failed",
                        dispatches, dispatches, evidences, timeline,
                        candidate_resolutions, hit_target_resolutions,
                        global_prep_candidate_resolutions=global_prep_candidate_resolutions,
                        global_prep_hit_target_resolutions=global_prep_hit_target_resolutions,
                    )
                fresh_global_hit, fresh_global_hit_resolution = resolve_global_prep_hit_target(
                    planned_frame, fresh_global, phase="fresh"
                )
                global_prep_hit_target_resolutions.append(fresh_global_hit_resolution)
                if fresh_global_hit is None:
                    reason = (
                        "global_prep_target_occluded"
                        if fresh_global_hit_resolution.target
                        and fresh_global_hit_resolution.target.occlusion_detected
                        else "global_prep_fresh_confirmation_failed"
                    )
                    return ActionSummaryResult(
                        False, planned.state, reason, dispatches, dispatches,
                        evidences, timeline, candidate_resolutions,
                        hit_target_resolutions,
                        global_prep_candidate_resolutions=global_prep_candidate_resolutions,
                        global_prep_hit_target_resolutions=global_prep_hit_target_resolutions,
                    )
                if not _same_global_prep_identity(
                    initial_global_hit, fresh_global_hit,
                    initial_size=self._frame_size(source_frame),
                    fresh_size=self._frame_size(planned_frame),
                ):
                    return ActionSummaryResult(
                        False, planned.state, "global_prep_fresh_confirmation_failed",
                        dispatches, dispatches, evidences, timeline,
                        candidate_resolutions, hit_target_resolutions,
                        global_prep_candidate_resolutions=global_prep_candidate_resolutions,
                        global_prep_hit_target_resolutions=global_prep_hit_target_resolutions,
                    )
                candidate = (
                    fresh_global_hit.hit_target_point,
                    fresh_global_hit.hit_target_bbox,
                    f"{fresh_global.candidate_type}+parent_visual_card",
                    fresh_global.score,
                    fresh_global_hit_resolution.visual_parent_count,
                )
            elif stage_index == 2 and current.state is ActionSummaryState.ACTION_SUMMARY_ENTRY_VISIBLE:
                fresh_summary_target, fresh_summary_resolution = (
                    resolve_action_summary_entry_target(planned_frame, phase="fresh")
                )
                action_summary_entry_resolutions.append(fresh_summary_resolution)
                if fresh_summary_target is None:
                    return ActionSummaryResult(
                        False, planned.state,
                        "action_summary_fresh_parent_target_not_unique",
                        dispatches, dispatches, evidences, timeline,
                        candidate_resolutions,
                        global_prep_candidate_resolutions=global_prep_candidate_resolutions,
                        global_prep_hit_target_resolutions=global_prep_hit_target_resolutions,
                        action_summary_entry_resolutions=action_summary_entry_resolutions,
                    )
                target_confirmation = confirm_fresh_target(
                    initial_summary_target, fresh_summary_target
                )
                if not target_confirmation.allowed:
                    return ActionSummaryResult(
                        False, planned.state,
                        f"action_summary_{target_confirmation.reason}",
                        dispatches, dispatches, evidences, timeline,
                        candidate_resolutions,
                        global_prep_candidate_resolutions=global_prep_candidate_resolutions,
                        global_prep_hit_target_resolutions=global_prep_hit_target_resolutions,
                        action_summary_entry_resolutions=action_summary_entry_resolutions,
                    )
                candidate = (
                    fresh_summary_target.hit_target_point,
                    fresh_summary_target.hit_target_bbox,
                    "exact_ocr+parent_visual_action_card",
                    1.0,
                    fresh_summary_target.candidate_count,
                )
            else:
                assert candidate_resolver is not None
                candidate = candidate_resolver(planned_frame, planned)
            if candidate is None:
                return ActionSummaryResult(
                    False, planned.state, "candidate_not_unique_or_safe",
                    dispatches, dispatches, evidences, timeline,
                    candidate_resolutions,
                )
            point, bounds, candidate_type, candidate_score, candidate_count = candidate
            contract_key = {
                "open_action_entry": "OPEN_ACTION_TERMINAL",
                "open_activity_overview": "OPEN_GLOBAL_PREP",
                "open_action_summary": "OPEN_ACTION_SUMMARY",
            }.get(entry_name)
            if contract_key is not None:
                contract = PROVEN_NAVIGATION_CONTRACTS[contract_key]
                contract_decision = confirm_fresh_capability(
                    current.to_ui_state(),
                    planned.to_ui_state(),
                    contract,
                    dispatch_count=contract_dispatches.get(contract_key, 0),
                )
                if not contract_decision.allowed:
                    return ActionSummaryResult(
                        False, planned.state,
                        f"action_contract_{contract_decision.reason}",
                        dispatches, dispatches, evidences, timeline,
                        candidate_resolutions,
                        global_prep_candidate_resolutions=global_prep_candidate_resolutions,
                        global_prep_hit_target_resolutions=global_prep_hit_target_resolutions,
                        action_summary_entry_resolutions=action_summary_entry_resolutions,
                    )
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
            if contract_key is not None:
                contract_dispatches[contract_key] = (
                    contract_dispatches.get(contract_key, 0) + 1
                )
            dispatches += 1
            started = self.monotonic()
            if self.stop_after_first_stage and entry_name == "open_action_entry":
                observed, first_stage_result = self._wait_first_stage_after_dispatch(
                    evidence,
                    dispatch_started=started,
                    pre_capture_id=planned.capture_id,
                    timeline=timeline,
                )
                self.budget.record_result(
                    decision,
                    "PASS" if first_stage_result == "EXPECTED_PAGE" else "FAIL",
                )
                self.evidence_recorder(evidence)
                reason = {
                    "EXPECTED_PAGE": "first_stage_expected_page",
                    "STABLE_CHANGED_UNKNOWN": "action_terminal_stable_changed_unknown",
                    "NO_TOUCH_EFFECT_OBSERVED": "action_terminal_no_touch_effect_observed",
                    "KNOWN_FOREIGN_PAGE": "action_terminal_known_foreign_page",
                    "TRANSITION_TIMEOUT": "action_terminal_transition_timeout",
                }.get(first_stage_result, "external_runtime_blocker")
                state = observed.state if observed is not None else planned.state
                return ActionSummaryResult(
                    first_stage_result == "EXPECTED_PAGE",
                    state,
                    reason,
                    dispatches,
                    dispatches,
                    evidences,
                    timeline,
                    candidate_resolutions,
                    hit_target_resolutions,
                    "PASS" if first_stage_result == "EXPECTED_PAGE" else first_stage_result,
                )
            if self.stop_after_global_prep_stage and entry_name == "open_activity_overview":
                observed, global_result = self._wait_global_prep_after_dispatch(
                    evidence,
                    dispatch_started=started,
                    pre_capture_id=planned.capture_id,
                    timeline=timeline,
                )
                successful = global_result in {
                    "EXPECTED_NEXT_PAGE", "KNOWN_OPTIONAL_OVERLAY",
                    "ACTION_SUMMARY_VISIBLE",
                }
                self.budget.record_result(decision, "PASS" if successful else "FAIL")
                self.evidence_recorder(evidence)
                reason = {
                    "EXPECTED_NEXT_PAGE": "global_prep_expected_next_page",
                    "KNOWN_OPTIONAL_OVERLAY": "global_prep_optional_overlay_reached",
                    "ACTION_SUMMARY_VISIBLE": "global_prep_action_summary_reached",
                    "STABLE_CHANGED_UNKNOWN": "global_prep_stable_changed_unknown",
                    "NO_TOUCH_EFFECT_OBSERVED": "global_prep_no_touch_effect_observed",
                    "KNOWN_FOREIGN_PAGE": "global_prep_known_foreign_page",
                    "TRANSITION_TIMEOUT": "global_prep_transition_timeout",
                }.get(global_result, "external_runtime_blocker")
                stage_result = {
                    "EXPECTED_NEXT_PAGE": "PASS",
                    "KNOWN_OPTIONAL_OVERLAY": "OPTIONAL_OVERLAY_REACHED",
                    "ACTION_SUMMARY_VISIBLE": "ACTION_SUMMARY_REACHED",
                    "STABLE_CHANGED_UNKNOWN": "STABLE_CHANGED_UNKNOWN",
                    "NO_TOUCH_EFFECT_OBSERVED": "NO_TOUCH_EFFECT",
                }.get(global_result, "FAIL")
                return ActionSummaryResult(
                    successful,
                    observed.state if observed is not None else planned.state,
                    reason, dispatches, dispatches, evidences, timeline,
                    candidate_resolutions, hit_target_resolutions,
                    global_prep_candidate_resolutions=global_prep_candidate_resolutions,
                    global_prep_hit_target_resolutions=global_prep_hit_target_resolutions,
                    global_prep_stage_result=stage_result,
                )
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
            stage_index = {
                ActionSummaryState.HOME_READY: 0,
                ActionSummaryState.ACTIVITY_OVERVIEW_VISIBLE: 1,
                ActionSummaryState.ACTION_SUMMARY_ENTRY_VISIBLE: 2,
            }.get(current.state, stage_index)


__all__ = [
    "ACTION_TERMINAL_PARENT_REGION_NORMALIZED",
    "ACTION_TERMINAL_REGION_NORMALIZED",
    "GLOBAL_PREP_LABEL_REGION_NORMALIZED",
    "GLOBAL_PREP_PARENT_REGION_NORMALIZED", "ActionSummaryNavigator",
    "ActionSummaryObservation", "ActionSummaryResult", "ActionSummaryState",
    "ActionTerminalCandidate", "ActionTerminalHitTarget",
    "CandidateResolutionEvidence", "HitTargetResolutionEvidence",
    "GlobalPrepCandidate", "GlobalPrepHitTarget",
    "GlobalPrepHitTargetEvidence", "GlobalPrepResolutionEvidence",
    "ActionSummaryEntryResolutionEvidence",
    "ACTION_SUMMARY_ENTRY_LABEL_REGION_NORMALIZED",
    "ACTION_SUMMARY_ENTRY_PARENT_REGION_NORMALIZED",
    "observe_action_summary", "resolve_action_terminal_candidate",
    "resolve_action_terminal_hit_target",
    "resolve_global_prep_candidate", "resolve_global_prep_hit_target",
    "resolve_action_summary_entry_target",
]
