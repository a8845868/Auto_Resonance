"""Atomic, idempotent runtime ledger for confirmed weekly trade facts."""

from __future__ import annotations

import json
import os
import threading
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any

from core.services.runtime_control import RUNTIME_DIR
from core.services.server_calendar import SERVER_CLOCK


SCHEMA_VERSION = 1
LEDGER_PATH = RUNTIME_DIR / "trade-ledger.json"
_LOCK = threading.RLock()


class ProgressSource(str, Enum):
    GAME_OBSERVED = "GAME_OBSERVED"
    LEDGER_CONFIRMED = "LEDGER_CONFIRMED"
    RECONCILED = "RECONCILED"
    USER_CORRECTED = "USER_CORRECTED"
    ESTIMATED = "ESTIMATED"
    UNKNOWN = "UNKNOWN"


class TradeEventType(str, Enum):
    LEG_STARTED = "LEG_STARTED"
    ARRIVAL_CONFIRMED = "ARRIVAL_CONFIRMED"
    PURCHASE_CONFIRMED = "PURCHASE_CONFIRMED"
    PURCHASE_BOOK_CONFIRMED = "PURCHASE_BOOK_CONFIRMED"
    SALE_CONFIRMED = "SALE_CONFIRMED"
    LEG_COMPLETED = "LEG_COMPLETED"
    ROUND_TRIP_COMPLETED = "ROUND_TRIP_COMPLETED"
    RECONCILIATION = "RECONCILIATION"
    MANUAL_CORRECTION = "MANUAL_CORRECTION"


@dataclass(frozen=True)
class TradeEvent:
    event_id: str
    server_week_id: str
    route_id: str
    cycle_id: str
    leg_id: str
    event_type: TradeEventType
    origin: str
    destination: str
    observed_at: datetime
    confirmed_by: str
    purchase_book_delta: int = 0
    fatigue_delta: int = 0
    profit_delta: int = 0
    metadata_version: int = 1
    metadata: dict[str, Any] | None = None


@dataclass(frozen=True)
class TradeWeekState:
    schema_version: int
    server_week_id: str
    source: ProgressSource
    confidence: str
    confirmed_round_trips: int | None
    confirmed_legs: int
    current_partial_cycle: dict[str, Any] | None
    purchase_books_used: int
    fatigue_used_for_trade: int
    last_confirmed_transaction_at: datetime | None
    last_reconciled_at: datetime | None
    ledger_sequence: int


def _read(path: Path) -> dict[str, Any]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
        document = {}
    if not isinstance(document, dict) or document.get("schema_version") != SCHEMA_VERSION:
        return {"schema_version": SCHEMA_VERSION, "events": []}
    if not isinstance(document.get("events"), list):
        document["events"] = []
    return document


def _write(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(document, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _serialize(event: TradeEvent) -> dict[str, Any]:
    if event.observed_at.tzinfo is None or event.observed_at.utcoffset() is None:
        raise ValueError("trade event timestamps must be timezone-aware")
    payload = asdict(event)
    payload["event_type"] = event.event_type.value
    payload["observed_at"] = event.observed_at.isoformat(timespec="seconds")
    return payload


def append_trade_event(path: Path, event: TradeEvent) -> bool:
    payload = _serialize(event)
    with _LOCK:
        document = _read(path)
        if any(item.get("event_id") == event.event_id for item in document["events"]):
            return False
        document["events"].append(payload)
        _write(path, document)
    return True


def reconcile_trade_baseline(
    path: Path,
    confirmed_round_trips: int,
    *,
    observed_at: datetime,
    source: ProgressSource = ProgressSource.USER_CORRECTED,
) -> bool:
    event = TradeEvent(
        event_id=f"manual-{uuid.uuid4().hex}",
        server_week_id=SERVER_CLOCK.server_week_id(observed_at),
        route_id="manual-baseline",
        cycle_id="manual-baseline",
        leg_id="",
        event_type=TradeEventType.MANUAL_CORRECTION,
        origin="",
        destination="",
        observed_at=observed_at,
        confirmed_by=source.value,
        metadata={"confirmed_round_trips": max(0, int(confirmed_round_trips))},
    )
    return append_trade_event(path, event)


def load_trade_week_state(
    path: Path = LEDGER_PATH,
    *,
    now: datetime | None = None,
) -> TradeWeekState:
    current = now or SERVER_CLOCK.server_now()
    week_id = SERVER_CLOCK.server_week_id(current)
    with _LOCK:
        all_events = _read(path)["events"]
    events = [item for item in all_events if item.get("server_week_id") == week_id]
    round_events = [
        item for item in events if item.get("event_type") == TradeEventType.ROUND_TRIP_COMPLETED.value
    ]
    correction_events = [
        item
        for item in events
        if item.get("event_type")
        in {TradeEventType.RECONCILIATION.value, TradeEventType.MANUAL_CORRECTION.value}
    ]
    baseline = 0
    counted_round_events = round_events
    source = ProgressSource.UNKNOWN
    last_reconciled_at = None
    if correction_events:
        latest = max(correction_events, key=lambda item: item.get("observed_at", ""))
        baseline = max(0, int((latest.get("metadata") or {}).get("confirmed_round_trips", 0)))
        source = ProgressSource(latest.get("confirmed_by", ProgressSource.RECONCILED.value))
        last_reconciled_at = datetime.fromisoformat(latest["observed_at"])
        counted_round_events = [
            item
            for item in round_events
            if item.get("observed_at", "") > latest.get("observed_at", "")
        ]
    elif events:
        source = ProgressSource.LEDGER_CONFIRMED

    completed_cycles = {item.get("cycle_id") for item in round_events}
    leg_events = [
        item for item in events if item.get("event_type") == TradeEventType.LEG_COMPLETED.value
    ]
    partial_by_cycle: dict[str, list[dict[str, Any]]] = {}
    for item in leg_events:
        cycle_id = str(item.get("cycle_id", ""))
        if cycle_id and cycle_id not in completed_cycles:
            partial_by_cycle.setdefault(cycle_id, []).append(item)
    current_partial = None
    if partial_by_cycle:
        cycle_id, legs = max(
            partial_by_cycle.items(), key=lambda item: item[1][-1].get("observed_at", "")
        )
        current_partial = {
            "cycle_id": cycle_id,
            "route_id": legs[-1].get("route_id", ""),
            "confirmed_legs": len({leg.get("event_id") for leg in legs}),
            "last_destination": legs[-1].get("destination", ""),
        }
    transaction_events = [
        item
        for item in events
        if item.get("event_type")
        in {
            TradeEventType.PURCHASE_CONFIRMED.value,
            TradeEventType.PURCHASE_BOOK_CONFIRMED.value,
            TradeEventType.SALE_CONFIRMED.value,
            TradeEventType.LEG_COMPLETED.value,
            TradeEventType.ROUND_TRIP_COMPLETED.value,
        }
    ]
    last_transaction = (
        datetime.fromisoformat(transaction_events[-1]["observed_at"])
        if transaction_events
        else None
    )
    confirmed = None if source is ProgressSource.UNKNOWN else baseline + len(counted_round_events)
    return TradeWeekState(
        schema_version=SCHEMA_VERSION,
        server_week_id=week_id,
        source=source,
        confidence="UNKNOWN" if source is ProgressSource.UNKNOWN else "HIGH",
        confirmed_round_trips=confirmed,
        confirmed_legs=len(leg_events),
        current_partial_cycle=current_partial,
        purchase_books_used=sum(
            max(0, int(item.get("purchase_book_delta", 0)))
            for item in events
            if item.get("event_type") == TradeEventType.PURCHASE_BOOK_CONFIRMED.value
        ),
        fatigue_used_for_trade=sum(max(0, int(item.get("fatigue_delta", 0))) for item in events),
        last_confirmed_transaction_at=last_transaction,
        last_reconciled_at=last_reconciled_at,
        ledger_sequence=len(events),
    )
