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

    def validate(self) -> None:
        if self.city_id != "岚心城":
            raise ValueError("personal_city_target_mismatch")
        if self.package_id != "com.hermes.goda" or self.instance_index != 0:
            raise ValueError("personal_city_runtime_identity_mismatch")
        if self.selector_type != "RESOLVER_BOUND_TARGET":
            raise ValueError("personal_city_selector_mismatch")
        if self.anchor_text != "访问城市":
            raise ValueError("personal_city_anchor_mismatch")


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
    target: PersonalCityTarget = PersonalCityTarget(),
) -> tuple[int, int, int, int]:
    target.validate()
    anchors = [
        bounds
        for item in items
        if target.anchor_text in _text(item) and (bounds := _bbox(item)) is not None
    ]
    labels = [item for item in items if _text(item) == target.city_id]
    if len(anchors) != 1:
        raise ValueError(
            "personal_city_anchor_missing" if not anchors else "personal_city_anchor_ambiguous"
        )
    if not labels:
        raise ValueError("personal_city_label_missing")
    return anchors[0]


__all__ = ["PersonalCityTarget", "resolve_personal_city_anchor"]
