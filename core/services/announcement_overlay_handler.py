"""Read-only announcement detection and safe blank-region extraction."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Mapping, Sequence

import cv2 as cv
import numpy as np

from core.services.startup_coordinator import OverlayKind, classify_startup_frame


def _frame_bgr(frame: object) -> np.ndarray:
    image = getattr(frame, "image", frame)
    if isinstance(image, np.ndarray):
        if image.ndim == 2:
            return cv.cvtColor(image, cv.COLOR_GRAY2BGR)
        return image.copy()
    convert = getattr(image, "convert", None)
    if callable(convert):
        rgb = np.asarray(convert("RGB"))
        return cv.cvtColor(rgb, cv.COLOR_RGB2BGR)
    raise TypeError("announcement_frame_pixels_unavailable")


def _bbox(item: Mapping[str, object] | object) -> tuple[int, int, int, int] | None:
    direct = item.get("bbox") if isinstance(item, Mapping) else getattr(item, "bbox", None)
    if isinstance(direct, Sequence) and len(direct) == 4:
        return tuple(map(int, direct))
    points = item.get("position") if isinstance(item, Mapping) else getattr(item, "position", None)
    if not isinstance(points, Sequence) or len(points) < 3:
        return None
    try:
        xs = [int(float(point[0])) for point in points]
        ys = [int(float(point[1])) for point in points]
    except (TypeError, ValueError, IndexError):
        return None
    return min(xs), min(ys), max(xs), max(ys)


def _overlap(
    first: tuple[int, int, int, int], second: tuple[int, int, int, int]
) -> int:
    left = max(first[0], second[0])
    top = max(first[1], second[1])
    right = min(first[2], second[2])
    bottom = min(first[3], second[3])
    return max(0, right - left) * max(0, bottom - top)


@dataclass(frozen=True)
class SafeBlankRegion:
    bbox: tuple[int, int, int, int]
    point: tuple[int, int]
    area: int
    edge_density: float


@dataclass(frozen=True)
class AnnouncementSafetyMap:
    overlay_bbox: tuple[int, int, int, int]
    dialog_bbox: tuple[int, int, int, int]
    forbidden_bboxes: tuple[tuple[int, int, int, int], ...]
    candidates: tuple[SafeBlankRegion, ...]


class AnnouncementSafeRegionSelector:
    def __init__(
        self,
        *,
        minimum_region_area: int = 600,
        maximum_edge_density: float = 0.02,
        exclusion_margin: int = 6,
        screen_inset: int = 12,
        minimum_candidate_distance: int = 80,
    ) -> None:
        self.minimum_region_area = int(minimum_region_area)
        self.maximum_edge_density = float(maximum_edge_density)
        self.exclusion_margin = int(exclusion_margin)
        self.screen_inset = int(screen_inset)
        self.minimum_candidate_distance = int(minimum_candidate_distance)

    @staticmethod
    def _clamp(
        bbox: tuple[int, int, int, int], width: int, height: int
    ) -> tuple[int, int, int, int]:
        return (
            max(0, min(width, bbox[0])),
            max(0, min(height, bbox[1])),
            max(0, min(width, bbox[2])),
            max(0, min(height, bbox[3])),
        )

    def _expanded(
        self, bbox: tuple[int, int, int, int], width: int, height: int
    ) -> tuple[int, int, int, int]:
        margin = self.exclusion_margin
        return self._clamp(
            (bbox[0] - margin, bbox[1] - margin, bbox[2] + margin, bbox[3] + margin),
            width,
            height,
        )

    def select(
        self,
        frame: object,
        *,
        overlay_bbox: tuple[int, int, int, int],
        dialog_bbox: tuple[int, int, int, int],
        ocr_bboxes: Sequence[tuple[int, int, int, int]] = (),
        template_bboxes: Sequence[tuple[int, int, int, int]] = (),
    ) -> AnnouncementSafetyMap:
        image = _frame_bgr(frame)
        height, width = image.shape[:2]
        overlay = self._clamp(overlay_bbox, width, height)
        dialog = self._clamp(dialog_bbox, width, height)
        forbidden = tuple(
            self._expanded(item, width, height)
            for item in (tuple(ocr_bboxes) + tuple(template_bboxes))
        ) + (dialog,)

        allowed = np.zeros((height, width), dtype=np.uint8)
        allowed[overlay[1] : overlay[3], overlay[0] : overlay[2]] = 1
        search_top = max(overlay[1] + self.screen_inset, dialog[3] + 5)
        search_bottom = overlay[3] - self.screen_inset
        allowed[:search_top, :] = 0
        allowed[search_bottom:, :] = 0
        allowed[:, : overlay[0] + self.screen_inset] = 0
        allowed[:, overlay[2] - self.screen_inset :] = 0
        for left, top, right, bottom in forbidden:
            allowed[top:bottom, left:right] = 0

        gray = cv.cvtColor(image, cv.COLOR_BGR2GRAY)
        edges = cv.Canny(gray, 30, 90)
        high_edges = cv.dilate(edges, np.ones((5, 5), np.uint8), iterations=1) > 0
        allowed[high_edges] = 0

        component_count, labels, stats, _ = cv.connectedComponentsWithStats(
            allowed, connectivity=8
        )
        candidates: list[SafeBlankRegion] = []
        integral = cv.integral((edges > 0).astype(np.uint8))
        for label in range(1, component_count):
            area = int(stats[label, cv.CC_STAT_AREA])
            if area < self.minimum_region_area:
                continue
            component = (labels == label).astype(np.uint8)
            left = int(stats[label, cv.CC_STAT_LEFT])
            top = int(stats[label, cv.CC_STAT_TOP])
            box_width = int(stats[label, cv.CC_STAT_WIDTH])
            box_height = int(stats[label, cv.CC_STAT_HEIGHT])
            available = component.copy()
            for _ in range(2):
                distance = cv.distanceTransform(available, cv.DIST_L2, 5)
                _, maximum, _, maximum_at = cv.minMaxLoc(distance)
                point = (int(maximum_at[0]), int(maximum_at[1]))
                if maximum < 4.0 or not available[point[1], point[0]]:
                    break
                radius = 4
                x1, y1 = max(0, point[0] - radius), max(0, point[1] - radius)
                x2, y2 = min(width, point[0] + radius + 1), min(height, point[1] + radius + 1)
                edge_count = (
                    integral[y2, x2]
                    - integral[y1, x2]
                    - integral[y2, x1]
                    + integral[y1, x1]
                )
                density = float(edge_count) / float((x2 - x1) * (y2 - y1))
                point_box = (point[0], point[1], point[0] + 1, point[1] + 1)
                if density <= self.maximum_edge_density and not any(
                    _overlap(point_box, bounds) for bounds in forbidden
                ):
                    candidates.append(
                        SafeBlankRegion(
                            bbox=(left, top, left + box_width, top + box_height),
                            point=point,
                            area=area,
                            edge_density=density,
                        )
                    )
                cv.circle(
                    available,
                    point,
                    self.minimum_candidate_distance,
                    0,
                    thickness=-1,
                )
        selected: list[SafeBlankRegion] = []
        for candidate in sorted(candidates, key=lambda item: (item.area, -item.edge_density), reverse=True):
            if any(
                np.hypot(
                    candidate.point[0] - existing.point[0],
                    candidate.point[1] - existing.point[1],
                )
                < self.minimum_candidate_distance
                for existing in selected
            ):
                continue
            selected.append(candidate)
            if len(selected) == 2:
                break
        return AnnouncementSafetyMap(overlay, dialog, forbidden, tuple(selected))


class AnnouncementOverlayHandler:
    """Pure classifier/geometry helper; it never dispatches or owns a budget."""

    def __init__(
        self,
        *,
        classifier: Callable[[object], object] = classify_startup_frame,
        safe_region_selector: AnnouncementSafeRegionSelector | None = None,
    ) -> None:
        self.classifier = classifier
        self.safe_region_selector = safe_region_selector or AnnouncementSafeRegionSelector()

    def is_announcement(self, frame: object) -> bool:
        return OverlayKind.ANNOUNCEMENT in self.classifier(frame).overlays

    @staticmethod
    def resolve_dialog_bounds(frame: object) -> tuple[int, int, int, int] | None:
        explicit = getattr(frame, "announcement_dialog_bbox", None)
        if isinstance(explicit, Sequence) and len(explicit) == 4:
            return tuple(map(int, explicit))
        metadata = getattr(frame, "scenario_metadata", None)
        if isinstance(metadata, Mapping):
            explicit = metadata.get("dialog_bounds")
            if isinstance(explicit, Sequence) and len(explicit) == 4:
                return tuple(map(int, explicit))

        image = _frame_bgr(frame)
        height, width = image.shape[:2]
        edges = cv.Canny(cv.cvtColor(image, cv.COLOR_BGR2GRAY), 30, 90) > 0
        vertical = edges.sum(axis=0)
        horizontal = edges.sum(axis=1)
        left_candidates = np.flatnonzero(
            (vertical >= height * 0.25)
            & (np.arange(width) >= width * 0.03)
            & (np.arange(width) <= width * 0.25)
        )
        right_candidates = np.flatnonzero(
            (vertical >= height * 0.25)
            & (np.arange(width) >= width * 0.70)
            & (np.arange(width) <= width * 0.97)
        )
        top_candidates = np.flatnonzero(
            (horizontal >= width * 0.25)
            & (np.arange(height) >= height * 0.02)
            & (np.arange(height) <= height * 0.35)
        )
        bottom_candidates = np.flatnonzero(
            (horizontal >= width * 0.25)
            & (np.arange(height) >= height * 0.55)
            & (np.arange(height) <= height * 0.94)
        )
        if not all(
            len(values)
            for values in (
                left_candidates,
                right_candidates,
                top_candidates,
                bottom_candidates,
            )
        ):
            return None
        dialog = (
            int(left_candidates.min()),
            int(top_candidates.min()),
            int(right_candidates.max() + 1),
            int(bottom_candidates.max() + 1),
        )
        if (
            dialog[2] - dialog[0] < width * 0.55
            or dialog[3] - dialog[1] < height * 0.55
            or dialog[3] > height * 0.95
        ):
            return None
        return dialog

    @staticmethod
    def _ocr_bboxes(frame: object) -> tuple[tuple[int, int, int, int], ...]:
        ocr = getattr(frame, "ocr", None)
        if not callable(ocr):
            return ()
        return tuple(bounds for item in ocr() if (bounds := _bbox(item)) is not None)

    def resolve_safe_regions(self, frame: object) -> AnnouncementSafetyMap | None:
        image = _frame_bgr(frame)
        height, width = image.shape[:2]
        dialog = self.resolve_dialog_bounds(frame)
        if dialog is None:
            return None
        return self.safe_region_selector.select(
            frame,
            overlay_bbox=(0, 0, width, height),
            dialog_bbox=dialog,
            ocr_bboxes=self._ocr_bboxes(frame),
        )


__all__ = [
    "AnnouncementOverlayHandler",
    "AnnouncementSafetyMap",
    "AnnouncementSafeRegionSelector",
    "SafeBlankRegion",
    "_bbox",
    "_frame_bgr",
]
