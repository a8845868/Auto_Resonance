"""Single authoritative inventory page classifier."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Mapping, Sequence

from core.services.navigation_evidence import frame_sha256


class InventoryPageState(str, Enum):
    INVENTORY_PAGE_VISIBLE = "INVENTORY_PAGE_VISIBLE"
    INVENTORY_DETAIL_VISIBLE = "INVENTORY_DETAIL_VISIBLE"
    NOT_INVENTORY = "NOT_INVENTORY"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class InventoryPageObservation:
    state: InventoryPageState
    source_frame_sha256: str
    source_capture_id: str
    evidence: tuple[str, ...]
    confidence: str


def _items(frame_or_items: object) -> list[Mapping[str, object]]:
    if isinstance(frame_or_items, list):
        return [item for item in frame_or_items if isinstance(item, Mapping)]
    ocr = getattr(frame_or_items, "ocr", None)
    if callable(ocr):
        return [item for item in ocr() if isinstance(item, Mapping)]
    return []


def observe_inventory_page(frame_or_items: object) -> InventoryPageObservation:
    items = _items(frame_or_items)
    texts = ["".join(str(item.get("text", "")).split()) for item in items]
    digest = frame_sha256(frame_or_items) if not isinstance(frame_or_items, list) else ""
    capture_id = str(
        getattr(frame_or_items, "source_capture_id", "")
        or getattr(frame_or_items, "backend_capture_id", "")
    )
    has_blank_exit = any("触碰空白区域退出" in text for text in texts)
    has_item_metadata = any(marker in text for text in texts for marker in ("拥有:", "拥有：", "获取途径"))
    if has_blank_exit and has_item_metadata:
        return InventoryPageObservation(
            InventoryPageState.INVENTORY_DETAIL_VISIBLE, digest, capture_id,
            ("detail_blank_exit", "detail_item_metadata"), "HIGH",
        )
    categories = ("道具", "材料", "装备", "载货", "冰箱", "武装", "凭钥柜", "信物", "私人仓库")
    matched = tuple(category for category in categories if any(category in text for text in texts))
    if "道具" in matched and len(matched) >= 3:
        return InventoryPageObservation(
            InventoryPageState.INVENTORY_PAGE_VISIBLE, digest, capture_id,
            tuple(f"category:{value}" for value in matched), "HIGH",
        )
    if not texts:
        return InventoryPageObservation(InventoryPageState.UNKNOWN, digest, capture_id, ("ocr_empty",), "UNKNOWN")
    return InventoryPageObservation(InventoryPageState.NOT_INVENTORY, digest, capture_id, ("inventory_signature_absent",), "HIGH")


__all__ = ["InventoryPageObservation", "InventoryPageState", "observe_inventory_page"]
