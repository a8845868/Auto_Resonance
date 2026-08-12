"""Minimal immutable city target used by the personal runtime."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence


@dataclass(frozen=True)
class PersonalCityTarget:
    city_id: str = "岚心城"
    package_id: str = "com.hermes.goda"
    instance_index: int = 0
    selector_type: str = "RESOLVER_BOUND_TARGET"
    anchor_text: str = "访问城市"
    target_region_normalized: tuple[float, float, float, float] = (
        0.87,
        0.64,
        0.98,
        0.75,
    )

    def validate(self) -> None:
        if self.city_id != "岚心城":
            raise ValueError("personal_city_target_mismatch")
        if self.package_id != "com.hermes.goda" or self.instance_index != 0:
            raise ValueError("personal_city_runtime_identity_mismatch")
        if self.selector_type != "RESOLVER_BOUND_TARGET":
            raise ValueError("personal_city_selector_mismatch")
        if self.anchor_text != "访问城市":
            raise ValueError("personal_city_anchor_mismatch")
        left, top, right, bottom = self.target_region_normalized
        if not (0 <= left < right <= 1 and 0 <= top < bottom <= 1):
            raise ValueError("personal_city_target_region_invalid")


@dataclass(frozen=True)
class ResolvedPersonalCityTarget:
    city_label_bbox: tuple[int, int, int, int]
    visit_city_bbox: tuple[int, int, int, int]
    target_region: tuple[int, int, int, int]
    spatial_relation: str = "visit_above_city_label_same_target_region"


def _text(item: Mapping[str, object]) -> str:
    return str(item.get("text", "")).replace(" ", "")


def _bbox(item: Mapping[str, object]) -> tuple[int, int, int, int] | None:
    direct = item.get("bbox")
    if isinstance(direct, Sequence) and len(direct) == 4:
        return tuple(map(int, direct))
    points = item.get("position")
    if not isinstance(points, Sequence) or len(points) < 3:
        return None
    try:
        xs = [int(float(point[0])) for point in points]
        ys = [int(float(point[1])) for point in points]
    except (TypeError, ValueError, IndexError):
        return None
    return min(xs), min(ys), max(xs), max(ys)


def resolve_personal_city_anchor(
    items: Sequence[Mapping[str, object]],
    frame_dimensions: tuple[int, int],
    target: PersonalCityTarget = PersonalCityTarget(),
) -> tuple[int, int, int, int]:
    return resolve_personal_city_binding(items, frame_dimensions, target).visit_city_bbox


def resolve_personal_city_binding(
    items: Sequence[Mapping[str, object]],
    frame_dimensions: tuple[int, int],
    target: PersonalCityTarget = PersonalCityTarget(),
) -> ResolvedPersonalCityTarget:
    target.validate()
    width, height = frame_dimensions
    if width <= 0 or height <= 0:
        raise ValueError("personal_city_frame_dimensions_invalid")
    normalized = target.target_region_normalized
    region = (
        round(normalized[0] * width),
        round(normalized[1] * height),
        round(normalized[2] * width),
        round(normalized[3] * height),
    )

    def valid(bounds: tuple[int, int, int, int]) -> bool:
        return 0 <= bounds[0] < bounds[2] <= width and 0 <= bounds[1] < bounds[3] <= height

    def inside(bounds: tuple[int, int, int, int]) -> bool:
        x = (bounds[0] + bounds[2]) / 2
        y = (bounds[1] + bounds[3]) / 2
        return region[0] <= x <= region[2] and region[1] <= y <= region[3]

    anchor_items = [item for item in items if target.anchor_text in _text(item)]
    label_items = [item for item in items if _text(item) == target.city_id]
    if any(_bbox(item) is None for item in anchor_items):
        raise ValueError("personal_city_anchor_bbox_missing")
    if any(_bbox(item) is None for item in label_items):
        raise ValueError("personal_city_label_bbox_missing")
    anchors = [bounds for item in anchor_items if (bounds := _bbox(item)) and valid(bounds) and inside(bounds)]
    labels = [bounds for item in label_items if (bounds := _bbox(item)) and valid(bounds) and inside(bounds)]
    if len(anchors) != 1:
        raise ValueError(
            "personal_city_anchor_missing" if not anchors else "personal_city_anchor_ambiguous"
        )
    if len(labels) != 1:
        raise ValueError(
            "personal_city_label_missing" if not labels else "personal_city_label_ambiguous"
        )
    anchor = anchors[0]
    label = labels[0]
    anchor_center = ((anchor[0] + anchor[2]) / 2, (anchor[1] + anchor[3]) / 2)
    label_center = ((label[0] + label[2]) / 2, (label[1] + label[3]) / 2)
    if anchor_center[1] > label_center[1] or abs(anchor_center[0] - label_center[0]) > width * 0.06:
        raise ValueError("personal_city_spatial_relation_invalid")
    return ResolvedPersonalCityTarget(label, anchor, region)


__all__ = [
    "PersonalCityTarget",
    "ResolvedPersonalCityTarget",
    "resolve_personal_city_anchor",
    "resolve_personal_city_binding",
]
