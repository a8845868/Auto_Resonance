"""Resolve the HOME backpack cube without expanding it to the toolbar.

The lower-left ``资产`` label is a balance display.  It is deliberately not
part of this resolver and cannot contribute target authority.  The white cube
glyph in the top-right toolbar is itself the interactive backpack control.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Callable, Sequence

import cv2 as cv
import numpy as np

from core.services.navigation_evidence import frame_sha256


_TEMPLATE_PATH = (
    Path(__file__).resolve().parents[2]
    / "resources"
    / "inventory"
    / "home_backpack_cube_template_v1.json"
)
_FORBIDDEN_LEGACY_POINT = (1138, 94)


@dataclass(frozen=True, slots=True)
class HomeAssetsBalanceDisplay:
    """Semantic declaration for the non-interactive HOME balance label."""

    semantic_id: str = "iron_coin_balance_display"
    currency_id: str = "iron_coin"
    can_open_inventory: bool = False


@dataclass(frozen=True, slots=True)
class HomeBackpackCubeCandidate:
    point: tuple[int, int]
    bbox: tuple[int, int, int, int]
    score: float


@dataclass(frozen=True, slots=True)
class HomeBackpackCubeControl:
    semantic_id: str
    source_capture_id: str
    source_frame_sha256: str
    capture_size: tuple[int, int]
    cube_bbox: tuple[int, int, int, int] | None
    safe_hit_bbox: tuple[int, int, int, int] | None
    safe_hit_point: tuple[int, int] | None
    candidate_score: float
    candidate_count: int
    template_id: str
    template_sha256: str
    resolved: bool
    reason_codes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _BackpackTemplate:
    template_id: str
    pixels: np.ndarray
    semantic_hash: str
    minimum_score: float
    scale_factors: tuple[float, ...]
    roi_ratios: tuple[float, float, float, float]
    safe_inset_ratio: float


def _white_icon_mask(image: np.ndarray) -> np.ndarray:
    hsv = cv.cvtColor(image, cv.COLOR_BGR2HSV)
    mask = cv.inRange(hsv, np.array([0, 0, 170]), np.array([180, 90, 255]))
    return cv.morphologyEx(mask, cv.MORPH_OPEN, np.ones((2, 2), dtype=np.uint8))


@lru_cache(maxsize=1)
def backpack_cube_template() -> _BackpackTemplate:
    document = json.loads(_TEMPLATE_PATH.read_text(encoding="utf-8"))
    source_path = _TEMPLATE_PATH.with_name(str(document["source_filename"]))
    source = source_path.read_bytes()
    if hashlib.sha256(source).hexdigest() != document["source_payload_sha256"]:
        raise ValueError("backpack_cube_template_source_hash_mismatch")
    encoded = np.frombuffer(source, dtype=np.uint8)
    image = cv.imdecode(encoded, cv.IMREAD_COLOR)
    if image is None:
        raise ValueError("backpack_cube_template_decode_failed")
    height, width = image.shape[:2]
    if [width, height] != document["source_dimensions"]:
        raise ValueError("backpack_cube_template_source_shape_mismatch")

    crop = image[
        int(height * 0.16):int(height * 0.87),
        int(width * 0.16):int(width * 0.87),
    ]
    mask = _white_icon_mask(crop)
    ys, xs = np.where(mask > 0)
    if not len(xs):
        raise ValueError("backpack_cube_template_empty")
    normalized = np.ascontiguousarray(
        mask[ys.min():ys.max() + 1, xs.min():xs.max() + 1]
    )
    if list(normalized.shape[::-1]) != document["normalized_dimensions"]:
        raise ValueError("backpack_cube_template_normalized_shape_mismatch")
    digest = hashlib.sha256(normalized.tobytes()).hexdigest()
    if digest != document["normalized_sha256"]:
        raise ValueError("backpack_cube_template_normalized_hash_mismatch")
    if document.get("privacy") != "MINIMAL_UI_ICON_CROP_NO_ACCOUNT_DATA":
        raise ValueError("backpack_cube_template_privacy_contract_missing")
    return _BackpackTemplate(
        template_id=str(document["template_id"]),
        pixels=normalized,
        semantic_hash=digest,
        minimum_score=float(document["minimum_score"]),
        scale_factors=tuple(float(value) for value in document["scale_factors"]),
        roi_ratios=tuple(float(value) for value in document["roi_ratios"]),
        safe_inset_ratio=float(document["safe_inset_ratio"]),
    )


def _center(bounds: tuple[int, int, int, int]) -> tuple[int, int]:
    return ((bounds[0] + bounds[2]) // 2, (bounds[1] + bounds[3]) // 2)


def _inside(point: tuple[int, int], bounds: tuple[int, int, int, int]) -> bool:
    return bounds[0] <= point[0] < bounds[2] and bounds[1] <= point[1] < bounds[3]


def _candidate_clusters(
    image: np.ndarray,
    template: _BackpackTemplate,
) -> list[HomeBackpackCubeCandidate]:
    height, width = image.shape[:2]
    rx0, ry0, rx1, ry1 = template.roi_ratios
    roi = (
        max(0, int(round(width * rx0))),
        max(0, int(round(height * ry0))),
        min(width, int(round(width * rx1))),
        min(height, int(round(height * ry1))),
    )
    x0, y0, x1, y1 = roi
    search = _white_icon_mask(image[y0:y1, x0:x1])
    raw: list[HomeBackpackCubeCandidate] = []
    for scale in template.scale_factors:
        glyph_width = max(1, int(round(template.pixels.shape[1] * scale)))
        glyph_height = max(1, int(round(template.pixels.shape[0] * scale)))
        if glyph_width >= search.shape[1] or glyph_height >= search.shape[0]:
            continue
        resized = cv.resize(
            template.pixels,
            (glyph_width, glyph_height),
            interpolation=cv.INTER_NEAREST,
        )
        scores = cv.matchTemplate(search, resized, cv.TM_CCOEFF_NORMED)
        work = np.nan_to_num(scores, nan=-1.0, posinf=-1.0, neginf=-1.0)
        for _ in range(8):
            _low, score, _low_at, location = cv.minMaxLoc(work)
            if score < template.minimum_score:
                break
            left, top = location
            bbox = (
                x0 + left,
                y0 + top,
                x0 + left + glyph_width,
                y0 + top + glyph_height,
            )
            raw.append(HomeBackpackCubeCandidate(_center(bbox), bbox, float(score)))
            work[
                max(0, top - glyph_height // 2):min(
                    work.shape[0], top + glyph_height // 2 + 1
                ),
                max(0, left - glyph_width // 2):min(
                    work.shape[1], left + glyph_width // 2 + 1
                ),
            ] = -1.0

    distinct: list[HomeBackpackCubeCandidate] = []
    for candidate in sorted(raw, key=lambda value: value.score, reverse=True):
        radius = max(12, min(
            candidate.bbox[2] - candidate.bbox[0],
            candidate.bbox[3] - candidate.bbox[1],
        ) // 2)
        if any(
            abs(candidate.point[0] - kept.point[0]) <= radius
            and abs(candidate.point[1] - kept.point[1]) <= radius
            for kept in distinct
        ):
            continue
        distinct.append(candidate)
    return distinct


def find_home_backpack_cube_candidates(
    image: np.ndarray,
) -> list[HomeBackpackCubeCandidate]:
    if not isinstance(image, np.ndarray) or image.ndim != 3 or image.size == 0:
        return []
    return _candidate_clusters(image, backpack_cube_template())


def _safe_inset(
    bounds: tuple[int, int, int, int], ratio: float
) -> tuple[int, int, int, int] | None:
    width = bounds[2] - bounds[0]
    height = bounds[3] - bounds[1]
    inset_x = max(2, int(round(width * ratio)))
    inset_y = max(2, int(round(height * ratio)))
    safe = (
        bounds[0] + inset_x,
        bounds[1] + inset_y,
        bounds[2] - inset_x,
        bounds[3] - inset_y,
    )
    return safe if safe[2] > safe[0] and safe[3] > safe[1] else None


def resolve_home_backpack_cube_control(
    frame: object,
    *,
    candidate_resolver: Callable[[np.ndarray], Sequence[HomeBackpackCubeCandidate]] = (
        find_home_backpack_cube_candidates
    ),
) -> HomeBackpackCubeControl:
    image = getattr(frame, "image", None)
    capture_id = str(getattr(frame, "source_capture_id", "") or "").strip()
    digest = frame_sha256(frame)
    template = backpack_cube_template()
    base = {
        "semantic_id": "home_backpack_cube_control",
        "source_capture_id": capture_id,
        "source_frame_sha256": digest,
        "capture_size": (
            (int(image.shape[1]), int(image.shape[0]))
            if isinstance(image, np.ndarray) and image.ndim >= 2
            else (0, 0)
        ),
        "template_id": template.template_id,
        "template_sha256": template.semantic_hash,
    }
    if (
        not isinstance(image, np.ndarray)
        or image.ndim != 3
        or image.size == 0
    ):
        return HomeBackpackCubeControl(
            **base, cube_bbox=None, safe_hit_bbox=None, safe_hit_point=None,
            candidate_score=0.0, candidate_count=0, resolved=False,
            reason_codes=("frame_pixels_invalid",),
        )
    if not capture_id or not re.fullmatch(r"[0-9a-f]{64}", digest or ""):
        return HomeBackpackCubeControl(
            **base, cube_bbox=None, safe_hit_bbox=None, safe_hit_point=None,
            candidate_score=0.0, candidate_count=0, resolved=False,
            reason_codes=("capture_provenance_missing",),
        )

    height, width = image.shape[:2]
    candidates = [
        candidate for candidate in candidate_resolver(image)
        if 0 <= candidate.bbox[0] < candidate.bbox[2] <= width
        and 0 <= candidate.bbox[1] < candidate.bbox[3] <= height
        and candidate.score >= template.minimum_score
        and 12 <= candidate.bbox[2] - candidate.bbox[0] <= width * 0.08
        and 12 <= candidate.bbox[3] - candidate.bbox[1] <= height * 0.16
        and candidate.bbox[0] >= width * template.roi_ratios[0]
        and candidate.bbox[2] <= width * template.roi_ratios[2]
        and candidate.bbox[1] >= height * template.roi_ratios[1]
        and candidate.bbox[3] <= height * template.roi_ratios[3]
    ]
    if len(candidates) != 1:
        reason = "backpack_cube_missing" if not candidates else "backpack_cube_ambiguous"
        return HomeBackpackCubeControl(
            **base, cube_bbox=None, safe_hit_bbox=None, safe_hit_point=None,
            candidate_score=max((value.score for value in candidates), default=0.0),
            candidate_count=len(candidates), resolved=False, reason_codes=(reason,),
        )

    candidate = candidates[0]
    safe = _safe_inset(candidate.bbox, template.safe_inset_ratio)
    point = _center(safe) if safe is not None else None
    if (
        safe is None
        or point is None
        or not _inside(point, candidate.bbox)
        or _inside(_FORBIDDEN_LEGACY_POINT, safe)
        or point == _FORBIDDEN_LEGACY_POINT
    ):
        return HomeBackpackCubeControl(
            **base, cube_bbox=candidate.bbox, safe_hit_bbox=None,
            safe_hit_point=None, candidate_score=candidate.score,
            candidate_count=1, resolved=False,
            reason_codes=("backpack_cube_safe_region_invalid",),
        )
    return HomeBackpackCubeControl(
        **base, cube_bbox=candidate.bbox, safe_hit_bbox=safe,
        safe_hit_point=point, candidate_score=candidate.score,
        candidate_count=1, resolved=True,
        reason_codes=("unique_backpack_cube", "safe_region_inside_cube"),
    )


def confirm_fresh_home_backpack_cube(
    initial: HomeBackpackCubeControl,
    fresh: HomeBackpackCubeControl,
    *,
    max_center_drift: int = 8,
) -> HomeBackpackCubeControl | None:
    """Return only the second fresh-frame target when both controls agree."""

    if not initial.resolved or not fresh.resolved:
        return None
    if (
        not initial.source_capture_id
        or not fresh.source_capture_id
        or initial.source_capture_id == fresh.source_capture_id
    ):
        return None
    if initial.template_sha256 != fresh.template_sha256:
        return None
    if initial.capture_size != fresh.capture_size:
        return None
    if initial.cube_bbox is None or fresh.cube_bbox is None:
        return None
    first_center = _center(initial.cube_bbox)
    fresh_center = _center(fresh.cube_bbox)
    if (
        abs(first_center[0] - fresh_center[0]) > max_center_drift
        or abs(first_center[1] - fresh_center[1]) > max_center_drift
    ):
        return None
    first_size = (
        initial.cube_bbox[2] - initial.cube_bbox[0],
        initial.cube_bbox[3] - initial.cube_bbox[1],
    )
    fresh_size = (
        fresh.cube_bbox[2] - fresh.cube_bbox[0],
        fresh.cube_bbox[3] - fresh.cube_bbox[1],
    )
    if any(abs(left - right) > 8 for left, right in zip(first_size, fresh_size)):
        return None
    return fresh


__all__ = [
    "HomeAssetsBalanceDisplay",
    "HomeBackpackCubeCandidate",
    "HomeBackpackCubeControl",
    "backpack_cube_template",
    "confirm_fresh_home_backpack_cube",
    "find_home_backpack_cube_candidates",
    "resolve_home_backpack_cube_control",
]
