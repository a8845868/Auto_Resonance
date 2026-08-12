from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import re
from typing import Any, Iterable

from app.utils.constants import ROOT_PATH


PASSENGER_RESOURCE_PATH = Path(ROOT_PATH) / "resources" / "passenger"
CATALOG_PATH = PASSENGER_RESOURCE_PATH / "furniture_catalog.json"
DEFAULT_LAYOUT_PATH = PASSENGER_RESOURCE_PATH / "optimal_layout.json"
STATE_PATH = Path(ROOT_PATH) / "config" / "passenger_layout_state.json"
SCORE_TARGETS = {
    "comfort": 42_000,
    "food": 7_000,
    "entertainment": 7_000,
    "pets": 7_000,
    "aquarium": 7_000,
    "plants": 7_000,
    "medical": 7_000,
}
SCORE_LABELS = {
    "comfort": "舒适",
    "food": "美味",
    "entertainment": "娱乐",
    "pets": "宠物",
    "aquarium": "水族",
    "plants": "绿植",
    "medical": "医疗",
}


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def load_furniture_catalog() -> dict[str, dict]:
    payload = _read_json(CATALOG_PATH)
    return {row["key"]: row for row in payload.get("items", [])}


def load_default_layout() -> dict:
    return _read_json(DEFAULT_LAYOUT_PATH)


def default_layout_state() -> dict:
    return {
        "version": 1,
        "active_layout": "default",
        "warehouse": {},
        "placements": {},
        "custom_layout": None,
        "fixed_furniture_initialized": False,
    }


def load_layout_state(path: Path = STATE_PATH) -> dict:
    state = default_layout_state()
    try:
        saved = _read_json(path)
    except (OSError, ValueError, TypeError):
        saved = {}
    if isinstance(saved, dict):
        state.update(saved)
    legacy_inventory = state.pop("inventory", {})
    warehouse = state.get("warehouse") or legacy_inventory
    state["warehouse"] = {str(k): max(0, int(v)) for k, v in warehouse.items()}
    state["placements"] = {str(k): max(0, int(v)) for k, v in state.get("placements", {}).items()}
    if not state.get("fixed_furniture_initialized"):
        catalog = load_furniture_catalog()
        layout = load_default_layout()
        for slot in layout.get("slots", []):
            item = catalog.get(slot.get("item"), {})
            if item.get("source") == "列车固定家具":
                state["placements"][slot["id"]] = max(0, int(slot.get("count", 0)))
        state["fixed_furniture_initialized"] = True
    return state


def save_layout_state(state: dict, path: Path = STATE_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def active_layout(state: dict) -> dict:
    if state.get("active_layout") == "custom" and state.get("custom_layout"):
        return deepcopy(state["custom_layout"])
    return load_default_layout()


def clone_as_custom(state: dict) -> dict:
    layout = active_layout(state)
    layout["id"] = "custom"
    layout["name"] = "我的自定义结构"
    layout["editable"] = True
    state["custom_layout"] = layout
    state["active_layout"] = "custom"
    return state


def required_counts(layout: dict) -> dict[str, int]:
    required: dict[str, int] = {}
    for slot in layout.get("slots", []):
        count = max(0, int(slot.get("count", 0)))
        if count:
            required[slot["item"]] = required.get(slot["item"], 0) + count
    return required


def placed_counts(layout: dict, placements: dict[str, int]) -> dict[str, int]:
    placed: dict[str, int] = {}
    for slot in layout.get("slots", []):
        count = min(max(0, int(slot.get("count", 0))), max(0, int(placements.get(slot["id"], 0))))
        placed[slot["item"]] = placed.get(slot["item"], 0) + count
    placed["goldfish_large"] = min(placed.get("goldfish_large", 0), placed.get("large_tank", 0))
    placed["goldfish_small"] = min(placed.get("goldfish_small", 0), placed.get("small_tank", 0))
    return placed


def calculate_layout_summary(state: dict, layout: dict | None = None, catalog: dict[str, dict] | None = None) -> dict:
    layout = layout or active_layout(state)
    catalog = catalog or load_furniture_catalog()
    needed = required_counts(layout)
    placed = placed_counts(layout, state.get("placements", {}))
    warehouse = state.get("warehouse", {})
    rows = []
    scores = {key: 0.0 for key in SCORE_TARGETS}
    all_keys = sorted(set(needed) | set(warehouse), key=lambda key: catalog.get(key, {}).get("name", key))
    for key in all_keys:
        item = catalog.get(key, {"key": key, "name": key, "source": "未收录", "scores": {}})
        required = needed.get(key, 0)
        installed = min(required, max(0, int(placed.get(key, 0))))
        warehouse_count = max(0, int(warehouse.get(key, 0)))
        owned = warehouse_count + installed
        for score_key, per_unit in item.get("scores", {}).items():
            if score_key in scores:
                scores[score_key] += installed * float(per_unit)
        rows.append({
            "key": key,
            "name": item["name"],
            "source": item.get("source", ""),
            "required": required,
            "owned": owned,
            "placed": installed,
            "missing": max(0, required - owned),
            "warehouse": warehouse_count,
        })
    score_rows = {
        key: {
            "label": SCORE_LABELS[key],
            "value": round(scores[key], 2),
            "target": target,
            "ready": scores[key] >= target,
        }
        for key, target in SCORE_TARGETS.items()
    }
    return {
        "rows": rows,
        "scores": score_rows,
        "missing_total": sum(row["missing"] for row in rows),
        "required_total": sum(row["required"] for row in rows),
        "placed_total": sum(row["placed"] for row in rows),
        "all_scores_ready": all(row["ready"] for row in score_rows.values()),
    }


def auto_place_owned(state: dict, layout: dict | None = None) -> dict:
    layout = layout or active_layout(state)
    remaining = {key: max(0, int(value)) for key, value in state.get("warehouse", {}).items()}
    placements = dict(state.get("placements", {}))
    for slot in layout.get("slots", []):
        key = slot["item"]
        required = max(0, int(slot.get("count", 0)))
        current = min(required, max(0, int(placements.get(slot["id"], 0))))
        count = min(required - current, remaining.get(key, 0))
        placements[slot["id"]] = current + count
        remaining[key] = max(0, remaining.get(key, 0) - count)
    state["placements"] = placements
    state["warehouse"] = remaining
    return state


def toggle_slot(state: dict, slot_id: str, layout: dict | None = None) -> dict:
    layout = layout or active_layout(state)
    slot = next((item for item in layout.get("slots", []) if item["id"] == slot_id), None)
    if not slot:
        return state
    current = max(0, int(state.setdefault("placements", {}).get(slot_id, 0)))
    warehouse = state.setdefault("warehouse", {})
    key = slot["item"]
    if current:
        state["placements"][slot_id] = 0
        warehouse[key] = max(0, int(warehouse.get(key, 0))) + current
        return state
    available = max(0, int(warehouse.get(key, 0)))
    state["placements"][slot_id] = min(max(0, int(slot.get("count", 0))), available)
    warehouse[key] = max(0, available - state["placements"][slot_id])
    return state


def normalize_furniture_name(value: str) -> str:
    return re.sub(r"[\s·（）()“”\"'LvVv0-9]", "", str(value)).lower()


def furniture_inventory_from_assets(assets: Iterable[Any], catalog: dict[str, dict] | None = None) -> dict[str, int]:
    catalog = catalog or load_furniture_catalog()
    aliases = {normalize_furniture_name(row["name"]): key for key, row in catalog.items()}
    found: dict[str, int] = {}
    for asset in assets:
        name = getattr(asset, "name", "")
        count = getattr(asset, "count", 0)
        normalized = normalize_furniture_name(name)
        key = aliases.get(normalized)
        if not key:
            candidates = [(alias, item_key) for alias, item_key in aliases.items() if alias and (alias in normalized or normalized in alias)]
            if candidates:
                key = max(candidates, key=lambda pair: len(pair[0]))[1]
        if key:
            found[key] = max(found.get(key, 0), max(0, int(count)))
    return found
