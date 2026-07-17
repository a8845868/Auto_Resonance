"""Extensible shop catalog, purchase plan, and reset scheduling."""

from __future__ import annotations

import json
import os
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from core.services.task_schedule_state import next_daily_reset


ROOT = Path(__file__).resolve().parents[2]
CATALOG_PATH = ROOT / "resources" / "shop" / "catalog.json"
PLAN_PATH = ROOT / "config" / "shop_purchase_plan.json"
ATTEMPT_PATH = ROOT / "config" / "shop_purchase_attempts.json"
PERIOD_LABELS = {
    "daily": "每日",
    "weekly": "每周",
    "monthly": "每月",
}
QUANTITY_MODES = {"one", "max"}
_ATTEMPT_LOCK = threading.RLock()


class ShopAttemptAlreadyActive(RuntimeError):
    """Raised when an item already has a write-ahead lock for this cycle."""

    def __init__(self, item: "ShopItem", entry: dict[str, Any]):
        self.item = item
        self.entry = dict(entry)
        super().__init__(
            f"商品 {item.name} 已在当前{item.period_label}刷新周期处理，"
            f"锁定至 {entry.get('blocked_until', '未知')}"
        )


def _strict_bool(value: object, default: bool = False) -> bool:
    """Accept JSON booleans only; never treat a non-empty string as enabled."""
    return value if isinstance(value, bool) else default


@dataclass(frozen=True)
class CurrencyDefinition:
    key: str
    name: str
    icon: str


@dataclass(frozen=True)
class ShopItem:
    id: str
    shop_id: str
    name: str
    period: str
    max_limit: int
    price: int
    currency: str
    icon: str

    @property
    def period_label(self) -> str:
        return PERIOD_LABELS.get(self.period, self.period)


@dataclass(frozen=True)
class ShopDefinition:
    id: str
    name: str
    short_name: str
    adapter: str
    automation_supported: bool
    description: str
    accent: str
    items: tuple[ShopItem, ...]


@dataclass(frozen=True)
class ShopCatalog:
    version: int
    currencies: dict[str, CurrencyDefinition]
    shops: tuple[ShopDefinition, ...]

    @property
    def items(self) -> tuple[ShopItem, ...]:
        return tuple(item for shop in self.shops for item in shop.items)

    def shop(self, shop_id: str) -> ShopDefinition:
        for shop in self.shops:
            if shop.id == shop_id:
                return shop
        raise KeyError(shop_id)

    def item(self, item_id: str) -> ShopItem:
        for item in self.items:
            if item.id == item_id:
                return item
        raise KeyError(item_id)


@dataclass(frozen=True)
class ConfiguredPurchase:
    shop: ShopDefinition
    item: ShopItem
    quantity: str


def load_shop_catalog(path: Path = CATALOG_PATH) -> ShopCatalog:
    raw = json.loads(path.read_text(encoding="utf-8"))
    currencies = {
        key: CurrencyDefinition(key=key, name=value["name"], icon=value["icon"])
        for key, value in raw.get("currencies", {}).items()
    }
    shops: list[ShopDefinition] = []
    seen_items: set[str] = set()
    for value in raw.get("shops", []):
        shop_id = str(value["id"])
        items: list[ShopItem] = []
        for item_value in value.get("items", []):
            item_id = str(item_value["id"])
            if item_id in seen_items:
                raise ValueError(f"重复的商店商品 ID: {item_id}")
            seen_items.add(item_id)
            period = str(item_value["period"])
            if period not in PERIOD_LABELS:
                raise ValueError(f"未知商店刷新周期: {period}")
            currency = str(item_value["currency"])
            if currency not in currencies:
                raise ValueError(f"未知商店货币: {currency}")
            items.append(
                ShopItem(
                    id=item_id,
                    shop_id=shop_id,
                    name=str(item_value["name"]),
                    period=period,
                    max_limit=max(1, int(item_value["max_limit"])),
                    price=max(0, int(item_value["price"])),
                    currency=currency,
                    icon=str(item_value.get("icon", "")),
                )
            )
        shops.append(
            ShopDefinition(
                id=shop_id,
                name=str(value["name"]),
                short_name=str(value.get("short_name", value["name"])),
                adapter=str(value["adapter"]),
                automation_supported=_strict_bool(
                    value.get("automation_supported"), False
                ),
                description=str(value.get("description", "")),
                accent=str(value.get("accent", "#d5a04d")),
                items=tuple(items),
            )
        )
    return ShopCatalog(
        version=max(1, int(raw.get("version", 1))),
        currencies=currencies,
        shops=tuple(shops),
    )


def default_shop_plan(catalog: ShopCatalog | None = None) -> dict[str, Any]:
    catalog = catalog or load_shop_catalog()
    return {
        "version": 1,
        "enabled": False,
        "capture_evidence": True,
        "shops": {
            shop.id: {
                "enabled": bool(shop.automation_supported),
                "items": {
                    item.id: {"enabled": False, "quantity": "one"}
                    for item in shop.items
                },
            }
            for shop in catalog.shops
        },
    }


def normalize_shop_plan(
    value: object,
    catalog: ShopCatalog | None = None,
) -> dict[str, Any]:
    catalog = catalog or load_shop_catalog()
    raw = value if isinstance(value, dict) else {}
    raw_shops = raw.get("shops") if isinstance(raw.get("shops"), dict) else {}
    result = default_shop_plan(catalog)
    result["enabled"] = _strict_bool(raw.get("enabled"), False)
    result["capture_evidence"] = _strict_bool(
        raw.get("capture_evidence"), True
    )
    for shop in catalog.shops:
        raw_shop = raw_shops.get(shop.id, {})
        if not isinstance(raw_shop, dict):
            continue
        target_shop = result["shops"][shop.id]
        target_shop["enabled"] = (
            _strict_bool(raw_shop.get("enabled"), False)
            and shop.automation_supported
        )
        raw_items = raw_shop.get("items")
        if not isinstance(raw_items, dict):
            continue
        for item in shop.items:
            raw_rule = raw_items.get(item.id, {})
            if not isinstance(raw_rule, dict):
                continue
            quantity = str(raw_rule.get("quantity", "one"))
            if quantity not in QUANTITY_MODES:
                quantity = "one"
            target_shop["items"][item.id] = {
                "enabled": _strict_bool(raw_rule.get("enabled"), False),
                "quantity": quantity,
            }
    return result


def load_shop_plan(
    path: Path = PLAN_PATH,
    catalog: ShopCatalog | None = None,
) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        value = {}
    return normalize_shop_plan(value, catalog)


def save_shop_plan(
    plan: object,
    path: Path = PLAN_PATH,
    catalog: ShopCatalog | None = None,
) -> dict[str, Any]:
    normalized = normalize_shop_plan(plan, catalog)
    _atomic_write_json(path, normalized)
    return normalized


def _atomic_write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(
        f"{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    )
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(path)


def configured_purchases(
    plan: object | None = None,
    catalog: ShopCatalog | None = None,
    require_global_enabled: bool = True,
) -> list[ConfiguredPurchase]:
    catalog = catalog or load_shop_catalog()
    normalized = normalize_shop_plan(
        load_shop_plan(catalog=catalog) if plan is None else plan,
        catalog,
    )
    if require_global_enabled and not normalized["enabled"]:
        return []
    purchases: list[ConfiguredPurchase] = []
    for shop in catalog.shops:
        shop_plan = normalized["shops"][shop.id]
        if not shop.automation_supported or not shop_plan["enabled"]:
            continue
        for item in shop.items:
            rule = shop_plan["items"][item.id]
            if rule["enabled"]:
                purchases.append(
                    ConfiguredPurchase(
                        shop=shop,
                        item=item,
                        quantity=rule["quantity"],
                    )
                )
    return purchases


def shop_plan_enabled(
    plan: object | None = None,
    catalog: ShopCatalog | None = None,
) -> bool:
    return bool(configured_purchases(plan, catalog, require_global_enabled=True))


def next_weekly_shop_reset(now: datetime | None = None) -> datetime:
    from core.services.server_calendar import SERVER_CLOCK

    current = now or datetime.now()
    was_naive = current.tzinfo is None or current.utcoffset() is None
    aware = (
        current.replace(tzinfo=datetime.now().astimezone().tzinfo).astimezone(
            SERVER_CLOCK.timezone
        )
        if was_naive
        else current.astimezone(SERVER_CLOCK.timezone)
    )
    target = SERVER_CLOCK.next_weekly_reset(aware)
    return target.replace(tzinfo=None) if was_naive else target


def next_monthly_shop_reset(now: datetime | None = None) -> datetime:
    now = now or datetime.now()
    current_reset = now.replace(day=1, hour=5, minute=0, second=0, microsecond=0)
    if current_reset > now:
        return current_reset
    if now.month == 12:
        return now.replace(
            year=now.year + 1,
            month=1,
            day=1,
            hour=5,
            minute=0,
            second=0,
            microsecond=0,
        )
    return now.replace(
        month=now.month + 1,
        day=1,
        hour=5,
        minute=0,
        second=0,
        microsecond=0,
    )


def next_shop_reset(
    now: datetime | None = None,
    plan: object | None = None,
    catalog: ShopCatalog | None = None,
) -> datetime:
    now = now or datetime.now()
    purchases = configured_purchases(
        plan,
        catalog,
        require_global_enabled=False,
    )
    periods = {purchase.item.period for purchase in purchases}
    candidates = []
    if "daily" in periods or not periods:
        candidates.append(next_daily_reset(now))
    if "weekly" in periods:
        candidates.append(next_weekly_shop_reset(now))
    if "monthly" in periods:
        candidates.append(next_monthly_shop_reset(now))
    return min(candidates)


def next_item_shop_reset(period: str, now: datetime | None = None) -> datetime:
    now = now or datetime.now()
    if period == "daily":
        return next_daily_reset(now)
    if period == "weekly":
        return next_weekly_shop_reset(now)
    if period == "monthly":
        return next_monthly_shop_reset(now)
    raise ValueError(f"未知商店刷新周期: {period}")


def load_shop_attempts(path: Path = ATTEMPT_PATH) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        raw = {}
    raw_items = raw.get("items") if isinstance(raw, dict) else None
    items = {
        str(item_id): dict(entry)
        for item_id, entry in (raw_items or {}).items()
        if isinstance(entry, dict)
    }
    return {"version": 1, "items": items}


def active_shop_attempt(
    item: ShopItem,
    now: datetime | None = None,
    path: Path = ATTEMPT_PATH,
) -> dict[str, Any] | None:
    now = now or datetime.now()
    entry = load_shop_attempts(path)["items"].get(item.id)
    if not entry or entry.get("period") != item.period:
        return None
    try:
        blocked_until = datetime.fromisoformat(str(entry["blocked_until"]))
    except (KeyError, TypeError, ValueError):
        return None
    return entry if blocked_until > now else None


def record_shop_attempt(
    item: ShopItem,
    quantity_mode: str,
    *,
    quantity: int = 0,
    cost: int = 0,
    status: str = "prepared",
    now: datetime | None = None,
    path: Path = ATTEMPT_PATH,
) -> dict[str, Any]:
    """Atomically block one item/reset cycle before the irreversible tap.

    The runtime lease already prevents two controller processes from operating
    the emulator.  The in-process lock also makes the read/check/write sequence
    indivisible for callers in different threads.  An active entry is never
    overwritten; callers must use :func:`update_shop_attempt` after submission.
    """
    now = now or datetime.now()
    with _ATTEMPT_LOCK:
        state = load_shop_attempts(path)
        existing = state["items"].get(item.id)
        if existing and existing.get("period") == item.period:
            try:
                blocked_until = datetime.fromisoformat(
                    str(existing["blocked_until"])
                )
            except (KeyError, TypeError, ValueError):
                blocked_until = now
            if blocked_until > now:
                raise ShopAttemptAlreadyActive(item, existing)
        entry = {
            "item_id": item.id,
            "period": item.period,
            "quantity_mode": quantity_mode,
            "quantity": max(0, int(quantity)),
            "cost": max(0, int(cost)),
            "status": str(status),
            "attempted_at": now.isoformat(timespec="seconds"),
            "blocked_until": next_item_shop_reset(item.period, now).isoformat(
                timespec="seconds"
            ),
        }
        state["items"][item.id] = entry
        _atomic_write_json(path, state)
        return dict(entry)


def update_shop_attempt(
    item_id: str,
    status: str,
    result: object = None,
    *,
    now: datetime | None = None,
    path: Path = ATTEMPT_PATH,
) -> dict[str, Any] | None:
    with _ATTEMPT_LOCK:
        state = load_shop_attempts(path)
        entry = state["items"].get(str(item_id))
        if not entry:
            return None
        entry["status"] = str(status)
        entry["updated_at"] = (now or datetime.now()).isoformat(timespec="seconds")
        if result is not None:
            entry["result"] = result
        _atomic_write_json(path, state)
        return dict(entry)
