"""Resolve semantic OCR anchors to real, visually bounded parent controls."""

from __future__ import annotations

import math
from dataclasses import dataclass

import cv2 as cv
import numpy as np


Box = tuple[int, int, int, int]


@dataclass(frozen=True, slots=True)
class ParentControlCandidateDiagnostic:
    candidate_id: str
    bbox: Box
    center: tuple[int, int]
    contains_anchor: bool
    overlaps_anchor: bool
    distance_to_anchor_px: float
    width: int
    height: int
    area: int
    aspect_ratio: float
    edge_support: float
    background_contrast: float
    fill_uniformity: float
    icon_relation: str
    chevron_relation: str
    row_relation: str
    candidate_method: str
    accepted: bool
    reason_codes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CityParentControlDiagnostic:
    source_capture_id: str
    source_frame_sha256: str
    capture_width: int
    capture_height: int
    anchor_candidate_count: int
    anchor_bbox: Box
    anchor_center: tuple[int, int]
    anchor_confidence: str
    parent_search_roi: Box
    visual_component_count: int
    visual_component_diagnostics: tuple[ParentControlCandidateDiagnostic, ...]
    icon_candidate_count: int
    icon_candidate_bboxes: tuple[Box, ...]
    chevron_candidate_count: int
    chevron_candidate_bboxes: tuple[Box, ...]
    background_region_count: int
    background_region_bboxes: tuple[Box, ...]
    row_boundary_candidates: tuple[Box, ...]
    separator_candidates: tuple[Box, ...]
    accepted_parent_candidate_count: int
    rejected_parent_candidate_count: int
    rejection_reason_codes: tuple[str, ...]
    resolution_status: str


@dataclass(frozen=True, slots=True)
class NavigationParentControlObservation:
    semantic_id: str
    anchor_bbox: Box
    parent_control_bbox: Box | None
    safe_hit_bbox: Box | None
    safe_hit_point: tuple[int, int] | None
    parent_detection_method: str
    candidate_count: int
    confidence: str
    source_capture_id: str
    source_frame_sha256: str
    reason_codes: tuple[str, ...]
    evidence_ids: tuple[str, ...]
    associated_icon_bbox: Box | None = None
    associated_chevron_bbox: Box | None = None
    diagnostic: CityParentControlDiagnostic | None = None

    @property
    def resolved(self) -> bool:
        return (
            self.candidate_count == 1
            and self.parent_control_bbox is not None
            and self.safe_hit_bbox is not None
            and self.safe_hit_point is not None
        )


def _center(bounds: Box) -> tuple[int, int]:
    return ((bounds[0] + bounds[2]) // 2, (bounds[1] + bounds[3]) // 2)


def _contains(outer: Box, inner: Box, margin: int = 1) -> bool:
    return (
        outer[0] <= inner[0] + margin
        and outer[1] <= inner[1] + margin
        and outer[2] >= inner[2] - margin
        and outer[3] >= inner[3] - margin
    )


def _overlaps(a: Box, b: Box) -> bool:
    return min(a[2], b[2]) > max(a[0], b[0]) and min(a[3], b[3]) > max(a[1], b[1])


def _iou(a: Box, b: Box) -> float:
    left, top = max(a[0], b[0]), max(a[1], b[1])
    right, bottom = min(a[2], b[2]), min(a[3], b[3])
    intersection = max(0, right - left) * max(0, bottom - top)
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - intersection
    return intersection / union if union else 0.0


def _parent_search_roi(image: np.ndarray, anchor: Box) -> Box:
    height, width = image.shape[:2]
    anchor_width = anchor[2] - anchor[0]
    anchor_height = anchor[3] - anchor[1]
    return (
        max(0, anchor[0] - max(80, round(anchor_width * 2.7))),
        max(0, anchor[1] - max(48, round(anchor_height * 4.0))),
        min(width, anchor[2] + max(40, anchor_width)),
        min(height, anchor[3] + max(48, round(anchor_height * 4.0))),
    )


def _edge_support(edges: np.ndarray, bounds: Box) -> float:
    x0, y0, x1, y1 = bounds
    border = np.zeros(edges.shape[:2], dtype=np.uint8)
    cv.rectangle(border, (x0, y0), (max(x0, x1 - 1), max(y0, y1 - 1)), 255, 2)
    pixels = int(np.count_nonzero(border))
    return float(np.count_nonzero((edges > 0) & (border > 0))) / max(1, pixels)


def _candidate_diagnostic(
    *,
    candidate_id: str,
    bounds: Box,
    anchor: Box,
    gray: np.ndarray,
    edges: np.ndarray,
    method: str,
    accepted: bool,
    reasons: tuple[str, ...],
    associated_chevron: Box | None = None,
) -> ParentControlCandidateDiagnostic:
    x0, y0, x1, y1 = bounds
    width, height = x1 - x0, y1 - y0
    local = gray[y0:y1, x0:x1]
    local_mean = float(local.mean()) if local.size else 0.0
    local_std = float(local.std()) if local.size else 255.0
    ring = gray[max(0, y0 - 3):min(gray.shape[0], y1 + 3), max(0, x0 - 3):min(gray.shape[1], x1 + 3)]
    ring_mean = float(ring.mean()) if ring.size else local_mean
    center = _center(bounds)
    return ParentControlCandidateDiagnostic(
        candidate_id=candidate_id,
        bbox=bounds,
        center=center,
        contains_anchor=_contains(bounds, anchor),
        overlaps_anchor=_overlaps(bounds, anchor),
        distance_to_anchor_px=round(math.dist(center, _center(anchor)), 3),
        width=width,
        height=height,
        area=width * height,
        aspect_ratio=round(width / max(1, height), 4),
        edge_support=round(_edge_support(edges, bounds), 6),
        background_contrast=round(abs(local_mean - ring_mean) / 255.0, 6),
        fill_uniformity=round(max(0.0, 1.0 - local_std / 128.0), 6),
        icon_relation="NONE",
        chevron_relation="RIGHT_ALIGNED" if associated_chevron else "NONE",
        row_relation="CONTAINS_ANCHOR" if _contains(bounds, anchor) else "NONE",
        candidate_method=method,
        accepted=accepted,
        reason_codes=reasons,
    )


def _visual_contour_candidates(image: np.ndarray, anchor: Box, edges: np.ndarray) -> list[Box]:
    height, width = image.shape[:2]
    contours, _ = cv.findContours(edges, cv.RETR_LIST, cv.CHAIN_APPROX_SIMPLE)
    anchor_area = max(1, (anchor[2] - anchor[0]) * (anchor[3] - anchor[1]))
    candidates: list[Box] = []
    for contour in contours:
        x, y, box_width, box_height = cv.boundingRect(contour)
        bounds = (x, y, x + box_width, y + box_height)
        area = box_width * box_height
        if not _contains(bounds, anchor):
            continue
        if area < anchor_area * 1.8 or box_width < 24 or box_height < 18:
            continue
        if area > width * height * 0.45 or box_width > width * 0.78 or box_height > height * 0.50:
            continue
        if min(
            anchor[0] - x,
            anchor[1] - y,
            x + box_width - anchor[2],
            y + box_height - anchor[3],
        ) < 2:
            continue
        candidates.append(bounds)
    distinct: list[Box] = []
    for candidate in sorted(candidates, key=lambda box: (box[2] - box[0]) * (box[3] - box[1])):
        if any(_iou(candidate, old) >= 0.88 for old in distinct):
            continue
        distinct.append(candidate)
    if distinct:
        smallest = distinct[0]
        distinct = [smallest] + [box for box in distinct[1:] if not _contains(box, smallest)]
    return distinct


def _group_horizontal_lines(lines: np.ndarray | None, roi: Box) -> list[Box]:
    if lines is None:
        return []
    rx0, ry0, rx1, _ = roi
    roi_width = rx1 - rx0
    right_tolerance = max(5, round(roi_width * 0.04))
    raw: list[tuple[int, int, int]] = []
    for x0, y0, x1, y1 in lines[:, 0, :]:
        if abs(int(y1) - int(y0)) > 3:
            continue
        left, right = sorted((int(x0) + rx0, int(x1) + rx0))
        if right < rx1 - right_tolerance:
            continue
        raw.append((left, int(round((int(y0) + int(y1)) / 2)) + ry0, right + 1))
    raw.sort(key=lambda item: item[1])
    groups: list[list[tuple[int, int, int]]] = []
    for line in raw:
        if not groups or line[1] - groups[-1][-1][1] > 5:
            groups.append([line])
        else:
            groups[-1].append(line)
    return [
        (min(item[0] for item in group), min(item[1] for item in group), max(item[2] for item in group), max(item[1] for item in group) + 1)
        for group in groups
    ]


def _row_boundaries(edges: np.ndarray, roi: Box, anchor: Box) -> tuple[list[Box], Box | None]:
    rx0, ry0, rx1, ry1 = roi
    roi_edges = edges[ry0:ry1, rx0:rx1]
    minimum_line_length = max(32, round((anchor[2] - anchor[0]) * 1.45))
    lines = cv.HoughLinesP(
        roi_edges,
        1,
        np.pi / 180,
        threshold=max(20, minimum_line_length // 3),
        minLineLength=minimum_line_length,
        maxLineGap=max(8, round((rx1 - rx0) * 0.04)),
    )
    boundaries = _group_horizontal_lines(lines, roi)
    above = [item for item in boundaries if item[3] <= anchor[1] - 1]
    below = [item for item in boundaries if item[1] >= anchor[3] + 1]
    if not above or not below:
        return boundaries, None
    top = max(above, key=lambda item: item[3])
    bottom = min(below, key=lambda item: item[1])
    parent = (
        min(top[0], bottom[0]),
        top[1],
        max(top[2], bottom[2]),
        bottom[3],
    )
    anchor_height = anchor[3] - anchor[1]
    row_height = parent[3] - parent[1]
    if row_height < max(30, round(anchor_height * 1.35)) or row_height > max(150, round(anchor_height * 4.0)):
        return boundaries, None
    if not _contains(parent, anchor, margin=0):
        return boundaries, None
    horizontal_margin = max(8, round((anchor[2] - anchor[0]) * 0.18))
    if anchor[0] - parent[0] < horizontal_margin or parent[2] - anchor[2] < horizontal_margin:
        return boundaries, None
    return boundaries, parent


def _chevron_candidates(gray: np.ndarray, parent: Box, anchor: Box) -> list[Box]:
    parent_width = parent[2] - parent[0]
    x0 = max(parent[0] + 2, anchor[2] + max(2, round((anchor[2] - anchor[0]) * 0.04)))
    x1 = parent[2] - max(3, round(parent_width * 0.015))
    y0, y1 = parent[1] + 3, parent[3] - 3
    if x1 <= x0 or y1 <= y0:
        return []
    dark = np.where(gray[y0:y1, x0:x1] < 140, 255, 0).astype(np.uint8)
    dark = cv.morphologyEx(dark, cv.MORPH_CLOSE, np.ones((5, 7), np.uint8))
    count, _, stats, _ = cv.connectedComponentsWithStats(dark)
    candidates: list[Box] = []
    anchor_center_y = _center(anchor)[1]
    for label in range(1, count):
        x, y, width, height, area = map(int, stats[label])
        bounds = (x0 + x, y0 + y, x0 + x + width, y0 + y + height)
        center_y = _center(bounds)[1]
        if area < 20 or width < 8 or height < 8:
            continue
        if width > max(65, round(parent_width * 0.28)) or height > round((parent[3] - parent[1]) * 0.72):
            continue
        if not (0.55 <= width / max(1, height) <= 3.2):
            continue
        if abs(center_y - anchor_center_y) > max(18, round((parent[3] - parent[1]) * 0.34)):
            continue
        if bounds[1] <= y0 + 1 or bounds[3] >= y1 - 1:
            continue
        candidates.append(bounds)
    distinct: list[Box] = []
    for candidate in sorted(candidates):
        if any(_iou(candidate, old) >= 0.75 for old in distinct):
            continue
        distinct.append(candidate)
    return distinct


def _icon_candidates(gray: np.ndarray, parent: Box, anchor: Box) -> list[Box]:
    parent_width = parent[2] - parent[0]
    x0 = parent[0] + max(3, round(parent_width * 0.02))
    x1 = anchor[0] - max(3, round((anchor[2] - anchor[0]) * 0.04))
    y0, y1 = parent[1] + 3, parent[3] - 3
    if x1 <= x0 or y1 <= y0:
        return []
    dark = np.where(gray[y0:y1, x0:x1] < 140, 255, 0).astype(np.uint8)
    dark = cv.morphologyEx(dark, cv.MORPH_CLOSE, np.ones((5, 5), np.uint8))
    count, _, stats, _ = cv.connectedComponentsWithStats(dark)
    candidates: list[Box] = []
    anchor_center_y = _center(anchor)[1]
    parent_height = parent[3] - parent[1]
    for label in range(1, count):
        x, y, width, height, area = map(int, stats[label])
        bounds = (x0 + x, y0 + y, x0 + x + width, y0 + y + height)
        if area < 35 or width < 9 or height < 9:
            continue
        if width > round(parent_height * 0.90) or height > round(parent_height * 0.90):
            continue
        if not (0.55 <= width / max(1, height) <= 1.85):
            continue
        if abs(_center(bounds)[1] - anchor_center_y) > max(18, round(parent_height * 0.34)):
            continue
        if bounds[1] <= y0 + 1 or bounds[3] >= y1 - 1:
            continue
        candidates.append(bounds)
    distinct: list[Box] = []
    for candidate in sorted(candidates):
        if any(_iou(candidate, old) >= 0.75 for old in distinct):
            continue
        distinct.append(candidate)
    return distinct


def _component_safe_region(parent: Box, component: Box) -> tuple[Box, tuple[int, int]] | None:
    parent_width, parent_height = parent[2] - parent[0], parent[3] - parent[1]
    parent_core = (
        parent[0] + max(4, round(parent_width * 0.025)),
        parent[1] + max(4, round(parent_height * 0.10)),
        parent[2] - max(4, round(parent_width * 0.025)),
        parent[3] - max(4, round(parent_height * 0.10)),
    )
    safe = (
        max(parent_core[0], component[0] - 8),
        max(parent_core[1], component[1] - 7),
        min(parent_core[2], component[2] + 8),
        min(parent_core[3], component[3] + 7),
    )
    if safe[0] >= safe[2] or safe[1] >= safe[3]:
        return None
    point = _center(component)
    if not (safe[0] <= point[0] < safe[2] and safe[1] <= point[1] < safe[3]):
        return None
    return safe, point


def _blank_safe_region(parent: Box, anchor: Box, edges: np.ndarray) -> tuple[Box, tuple[int, int]] | None:
    width, height = parent[2] - parent[0], parent[3] - parent[1]
    core = (
        parent[0] + max(4, round(width * 0.10)),
        parent[1] + max(4, round(height * 0.14)),
        parent[2] - max(4, round(width * 0.10)),
        parent[3] - max(4, round(height * 0.14)),
    )
    if core[0] >= core[2] or core[1] >= core[3]:
        return None
    radius = max(3, min(10, round(height * 0.08)))
    candidates: list[tuple[float, int, int, int]] = []
    normalized_candidates = (
        (0.18, 0.50), (0.82, 0.50),
        (0.25, 0.50), (0.75, 0.50),
        (0.18, 0.32), (0.82, 0.32),
        (0.25, 0.32), (0.75, 0.32),
        (0.18, 0.68), (0.82, 0.68),
        (0.25, 0.68), (0.75, 0.68),
    )
    for preference, (x_ratio, y_ratio) in enumerate(normalized_candidates):
        x = int(round(parent[0] + width * x_ratio))
        y = int(round(parent[1] + height * y_ratio))
        patch = (x - radius, y - radius, x + radius + 1, y + radius + 1)
        if not _contains(core, patch, margin=0) or _overlaps(patch, anchor):
            continue
        density = float(np.count_nonzero(edges[patch[1]:patch[3], patch[0]:patch[2]])) / max(1, (2 * radius + 1) ** 2)
        candidates.append((density, preference, x, y))
    if not candidates:
        return None
    density, _, x, y = min(candidates)
    if density > 0.12:
        return None
    safe = (x - radius, y - radius, x + radius + 1, y + radius + 1)
    return safe, (x, y)


def _legacy_inset_region(parent: Box) -> tuple[Box, tuple[int, int]] | None:
    """Preserve the already-proven parent-control contract for non-city callers."""

    inset_x = max(3, round((parent[2] - parent[0]) * 0.16))
    inset_y = max(3, round((parent[3] - parent[1]) * 0.18))
    safe = (
        parent[0] + inset_x,
        parent[1] + inset_y,
        parent[2] - inset_x,
        parent[3] - inset_y,
    )
    if safe[0] >= safe[2] or safe[1] >= safe[3]:
        return None
    return safe, _center(safe)


def _diagnostic(
    *,
    image: np.ndarray,
    anchor: Box,
    source_capture_id: str,
    source_frame_sha256: str,
    edges: np.ndarray,
    contour_candidates: list[Box],
    boundaries: list[Box],
    semantic_parent: Box | None,
    icons: list[Box],
    chevrons: list[Box],
    accepted_method: str | None,
) -> CityParentControlDiagnostic:
    gray = cv.cvtColor(image, cv.COLOR_BGR2GRAY) if image.ndim == 3 else image
    candidates: list[ParentControlCandidateDiagnostic] = []
    for index, bounds in enumerate(contour_candidates):
        accepted = accepted_method == "BUTTON_CONTAINER_CONTOUR" and len(contour_candidates) == 1
        candidates.append(_candidate_diagnostic(
            candidate_id=f"contour-{index}", bounds=bounds, anchor=anchor,
            gray=gray, edges=edges, method="BUTTON_CONTAINER_CONTOUR",
            accepted=accepted,
            reasons=("unique_visual_parent_control",) if accepted else ("visual_contour_not_authoritative",),
        ))
    if semantic_parent is not None:
        accepted = accepted_method == "SEMANTIC_CONTROL_SLOT" and len(chevrons) == 1
        reasons = (
            ("unique_row_action_region", "unique_associated_control_glyph")
            if accepted else
            (
                ("associated_chevron_ambiguous",)
                if len(chevrons) > 1 else
                (("associated_icon_ambiguous",) if len(icons) > 1 else ("associated_control_glyph_missing",))
            )
        )
        candidates.append(_candidate_diagnostic(
            candidate_id="semantic-slot-0", bounds=semantic_parent, anchor=anchor,
            gray=gray, edges=edges, method="SEMANTIC_CONTROL_SLOT",
            accepted=accepted, reasons=reasons,
            associated_chevron=chevrons[0] if len(chevrons) == 1 else None,
        ))
    rejected = [candidate for candidate in candidates if not candidate.accepted]
    return CityParentControlDiagnostic(
        source_capture_id=source_capture_id,
        source_frame_sha256=source_frame_sha256,
        capture_width=int(image.shape[1]),
        capture_height=int(image.shape[0]),
        anchor_candidate_count=1,
        anchor_bbox=anchor,
        anchor_center=_center(anchor),
        anchor_confidence="NOT_PROVIDED",
        parent_search_roi=_parent_search_roi(image, anchor),
        visual_component_count=len(candidates),
        visual_component_diagnostics=tuple(candidates),
        icon_candidate_count=len(icons),
        icon_candidate_bboxes=tuple(icons),
        chevron_candidate_count=len(chevrons),
        chevron_candidate_bboxes=tuple(chevrons),
        background_region_count=0,
        background_region_bboxes=(),
        row_boundary_candidates=tuple(boundaries),
        separator_candidates=tuple(boundaries),
        accepted_parent_candidate_count=sum(candidate.accepted for candidate in candidates),
        rejected_parent_candidate_count=len(rejected),
        rejection_reason_codes=tuple(sorted({reason for candidate in rejected for reason in candidate.reason_codes})),
        resolution_status="PASS" if any(candidate.accepted for candidate in candidates) else "BLOCKED",
    )


def resolve_navigation_parent_control(
    image: np.ndarray,
    *,
    semantic_id: str,
    anchor_bbox: Box,
    source_capture_id: str,
    source_frame_sha256: str,
) -> NavigationParentControlObservation:
    if not isinstance(image, np.ndarray) or image.ndim not in (2, 3):
        return NavigationParentControlObservation(
            semantic_id, anchor_bbox, None, None, None, "VISUAL_CONTOUR", 0,
            "UNKNOWN", source_capture_id, source_frame_sha256,
            ("parent_frame_missing",), (),
        )
    height, width = image.shape[:2]
    if not (0 <= anchor_bbox[0] < anchor_bbox[2] <= width and 0 <= anchor_bbox[1] < anchor_bbox[3] <= height):
        return NavigationParentControlObservation(
            semantic_id, anchor_bbox, None, None, None, "VISUAL_CONTOUR", 0,
            "UNKNOWN", source_capture_id, source_frame_sha256,
            ("anchor_bbox_out_of_bounds",), (),
        )
    gray = cv.cvtColor(image, cv.COLOR_BGR2GRAY) if image.ndim == 3 else image
    edges = cv.Canny(gray, 55, 150)
    edges = cv.morphologyEx(edges, cv.MORPH_CLOSE, np.ones((3, 3), np.uint8))
    contours = _visual_contour_candidates(image, anchor_bbox, edges)
    boundaries: list[Box] = []
    semantic_parent: Box | None = None
    icons: list[Box] = []
    chevrons: list[Box] = []
    if len(contours) == 1:
        parent = contours[0]
        safe_target = (
            _blank_safe_region(parent, anchor_bbox, edges)
            if semantic_id == "visit_city"
            else _legacy_inset_region(parent)
        )
        diagnostic = _diagnostic(
            image=image, anchor=anchor_bbox, source_capture_id=source_capture_id,
            source_frame_sha256=source_frame_sha256, edges=edges,
            contour_candidates=contours, boundaries=boundaries,
            semantic_parent=None, icons=icons, chevrons=chevrons,
            accepted_method="BUTTON_CONTAINER_CONTOUR" if safe_target else None,
        )
        if safe_target is None:
            return NavigationParentControlObservation(
                semantic_id, anchor_bbox, parent, None, None, "BUTTON_CONTAINER_CONTOUR", 1,
                "UNKNOWN", source_capture_id, source_frame_sha256,
                ("parent_safe_non_text_region_missing",), (), diagnostic=diagnostic,
            )
        safe, point = safe_target
        return NavigationParentControlObservation(
            semantic_id, anchor_bbox, parent, safe, point, "BUTTON_CONTAINER_CONTOUR", 1,
            "HIGH", source_capture_id, source_frame_sha256,
            ("unique_visual_parent_control", "safe_inset_core", "stable_non_text_region"),
            (f"parent:{source_frame_sha256[:16]}",), diagnostic=diagnostic,
        )
    if not contours and semantic_id == "visit_city":
        boundaries, semantic_parent = _row_boundaries(edges, _parent_search_roi(image, anchor_bbox), anchor_bbox)
        if semantic_parent is not None:
            icons = _icon_candidates(gray, semantic_parent, anchor_bbox)
            chevrons = _chevron_candidates(gray, semantic_parent, anchor_bbox)
    selected_component: Box | None = None
    selected_kind = ""
    if semantic_parent is not None and len(chevrons) == 1:
        selected_component = chevrons[0]
        selected_kind = "chevron"
    elif semantic_parent is not None and not chevrons and len(icons) == 1:
        selected_component = icons[0]
        selected_kind = "icon"
    if semantic_parent is not None and selected_component is not None:
        safe_target = _component_safe_region(semantic_parent, selected_component)
        diagnostic = _diagnostic(
            image=image, anchor=anchor_bbox, source_capture_id=source_capture_id,
            source_frame_sha256=source_frame_sha256, edges=edges,
            contour_candidates=contours, boundaries=boundaries,
            semantic_parent=semantic_parent, icons=icons, chevrons=chevrons,
            accepted_method="SEMANTIC_CONTROL_SLOT" if safe_target else None,
        )
        if safe_target is not None:
            safe, point = safe_target
            return NavigationParentControlObservation(
                semantic_id, anchor_bbox, semantic_parent, safe, point,
                "SEMANTIC_CONTROL_SLOT", 1, "HIGH",
                source_capture_id, source_frame_sha256,
                (
                    "unique_semantic_control_slot", "row_geometry_proven",
                    f"unique_associated_{selected_kind}", "safe_inset_core",
                ),
                (f"row:{source_frame_sha256[:16]}", f"{selected_kind}:{source_frame_sha256[:16]}"),
                associated_icon_bbox=icons[0] if selected_kind == "icon" else None,
                associated_chevron_bbox=chevrons[0] if selected_kind == "chevron" else None,
                diagnostic=diagnostic,
            )
    diagnostic = _diagnostic(
        image=image, anchor=anchor_bbox, source_capture_id=source_capture_id,
        source_frame_sha256=source_frame_sha256, edges=edges,
        contour_candidates=contours, boundaries=boundaries,
        semantic_parent=semantic_parent, icons=icons, chevrons=chevrons,
        accepted_method=None,
    )
    if contours:
        reason = "parent_control_ambiguous"
        count = len(contours)
    elif semantic_parent is None:
        reason = "parent_control_missing"
        count = 0
    elif len(chevrons) > 1:
        reason = "semantic_slot_chevron_ambiguous"
        count = len(chevrons)
    elif len(icons) > 1:
        reason = "semantic_slot_icon_ambiguous"
        count = len(icons)
    elif not chevrons and not icons:
        reason = "semantic_slot_control_glyph_missing"
        count = 0
    else:
        reason = "semantic_slot_control_glyph_unresolved"
        count = max(len(chevrons), len(icons))
    return NavigationParentControlObservation(
        semantic_id, anchor_bbox, None, None, None,
        "SEMANTIC_CONTROL_SLOT" if semantic_parent is not None else "VISUAL_CONTOUR",
        count, "UNKNOWN", source_capture_id, source_frame_sha256,
        (reason,), (), diagnostic=diagnostic,
    )


def confirm_fresh_parent_control(
    initial: NavigationParentControlObservation,
    fresh: NavigationParentControlObservation,
) -> NavigationParentControlObservation | None:
    if not initial.resolved or not fresh.resolved:
        return None
    if initial.semantic_id != fresh.semantic_id:
        return None
    if initial.parent_detection_method != fresh.parent_detection_method:
        return None
    if initial.source_capture_id and fresh.source_capture_id:
        if initial.source_capture_id == fresh.source_capture_id:
            return None
    elif initial.source_frame_sha256 == fresh.source_frame_sha256:
        return None
    if _iou(initial.anchor_bbox, fresh.anchor_bbox) < 0.75:
        return None
    assert initial.parent_control_bbox and fresh.parent_control_bbox
    assert initial.safe_hit_bbox and fresh.safe_hit_bbox
    assert initial.safe_hit_point and fresh.safe_hit_point
    if _iou(initial.parent_control_bbox, fresh.parent_control_bbox) < 0.82:
        return None
    if _iou(initial.safe_hit_bbox, fresh.safe_hit_bbox) < 0.75:
        return None
    if math.dist(initial.safe_hit_point, fresh.safe_hit_point) > 8.0:
        return None
    return fresh


__all__ = [
    "CityParentControlDiagnostic",
    "NavigationParentControlObservation",
    "ParentControlCandidateDiagnostic",
    "confirm_fresh_parent_control",
    "resolve_navigation_parent_control",
]
