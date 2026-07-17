"""Crash-safe, idempotent runtime ledger for confirmed trade facts."""

from __future__ import annotations

import json
import os
import threading
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Iterable

from core.services.runtime_control import RUNTIME_DIR
from core.services.server_calendar import SERVER_CLOCK


SCHEMA_VERSION = 2
SUPPORTED_SCHEMA_VERSIONS = {1, SCHEMA_VERSION}
LEDGER_PATH = RUNTIME_DIR / "trade-ledger.json"
_LOCK = threading.RLock()


class LedgerMigrationRequired(RuntimeError):
    """Raised before an unsupported or read-only ledger can be overwritten."""


class LedgerCorruptionError(RuntimeError):
    """Raised when a ledger exists but is not a valid ledger document."""


class ProgressSource(str, Enum):
    GAME_OBSERVED = "GAME_OBSERVED"
    LEDGER_CONFIRMED = "LEDGER_CONFIRMED"
    RECONCILED = "RECONCILED"
    USER_CORRECTED = "USER_CORRECTED"
    ESTIMATED = "ESTIMATED"
    UNKNOWN = "UNKNOWN"


class TradeEventType(str, Enum):
    LEG_PLANNED = "LEG_PLANNED"
    LEG_STARTED = "LEG_STARTED"
    BOOK_USE_CONFIRMED = "BOOK_USE_CONFIRMED"
    # Version-one ledgers used this longer name. Keep it readable, but all new
    # production writes use BOOK_USE_CONFIRMED at the irreversible boundary.
    PURCHASE_BOOK_CONFIRMED = "PURCHASE_BOOK_CONFIRMED"
    PURCHASE_CONFIRMED = "PURCHASE_CONFIRMED"
    DEPARTURE_REQUESTED = "DEPARTURE_REQUESTED"
    DEPARTURE_CONFIRMED = "DEPARTURE_CONFIRMED"
    ARRIVAL_CONFIRMED = "ARRIVAL_CONFIRMED"
    SALE_CONFIRMED = "SALE_CONFIRMED"
    LEG_COMPLETED = "LEG_COMPLETED"
    CYCLE_STARTED = "CYCLE_STARTED"
    OUTBOUND_COMPLETED = "OUTBOUND_COMPLETED"
    RETURN_COMPLETED = "RETURN_COMPLETED"
    CYCLE_COMPLETED = "CYCLE_COMPLETED"
    # Backward-compatible version-one event.
    ROUND_TRIP_COMPLETED = "ROUND_TRIP_COMPLETED"
    RECONCILIATION = "RECONCILIATION"
    MANUAL_CORRECTION = "MANUAL_CORRECTION"


BOOK_EVENT_TYPES = {
    TradeEventType.BOOK_USE_CONFIRMED.value,
    TradeEventType.PURCHASE_BOOK_CONFIRMED.value,
}
CYCLE_COMPLETION_TYPES = {
    TradeEventType.CYCLE_COMPLETED.value,
    TradeEventType.ROUND_TRIP_COMPLETED.value,
}
CORRECTION_TYPES = {
    TradeEventType.RECONCILIATION.value,
    TradeEventType.MANUAL_CORRECTION.value,
}


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
    operation_sequence: int = 0
    purchase_book_delta: int = 0
    fatigue_delta: int = 0
    profit_delta: int = 0
    metadata_version: int = 1
    metadata: dict[str, Any] | None = None


@dataclass(frozen=True)
class TradeCycleState:
    cycle_id: str
    route_id: str
    server_week_id: str
    phase: str
    completed_leg_ids: tuple[str, ...]
    current_leg_id: str
    current_leg_phase: str
    purchase_books_used: int
    events: tuple[dict[str, Any], ...]

    @property
    def ready_to_finalize(self) -> bool:
        return len(self.completed_leg_ids) >= 2 and self.phase != "CYCLE_COMPLETED"


@dataclass(frozen=True)
class TradeWeekState:
    schema_version: int
    server_week_id: str
    source: ProgressSource
    confidence: str
    baseline_known: bool
    baseline_round_trips: int | None
    tracking_started_at: datetime | None
    confirmed_delta_since_baseline: int
    full_week_total: int | None
    # Compatibility alias: this is the full-week fact and therefore remains
    # None while the pre-tracking baseline is unknown.
    confirmed_round_trips: int | None
    confirmed_legs: int
    current_partial_cycle: dict[str, Any] | None
    purchase_books_used: int
    fatigue_used_for_trade: int
    confirmed_profit: int
    last_confirmed_transaction_at: datetime | None
    last_reconciled_at: datetime | None
    ledger_sequence: int


def stable_trade_event_id(
    server_week_id: str,
    route_id: str,
    cycle_id: str,
    leg_id: str,
    event_type: TradeEventType | str,
    operation_sequence: int = 0,
) -> str:
    """Return the replay-stable identity for one irreversible operation."""

    event_value = event_type.value if isinstance(event_type, TradeEventType) else str(event_type)
    return ":".join(
        (
            str(server_week_id),
            str(route_id),
            str(cycle_id),
            str(leg_id),
            event_value,
            str(max(0, int(operation_sequence))),
        )
    )


def _empty_document() -> dict[str, Any]:
    return {"schema_version": SCHEMA_VERSION, "events": []}


def _read(path: Path, *, for_write: bool = False) -> dict[str, Any]:
    if not path.exists():
        return _empty_document()
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError) as error:
        raise LedgerCorruptionError(f"trade ledger cannot be read: {path}") from error
    if not isinstance(document, dict) or not isinstance(document.get("events"), list):
        raise LedgerCorruptionError(f"trade ledger has an invalid document shape: {path}")
    version = document.get("schema_version")
    if version not in SUPPORTED_SCHEMA_VERSIONS:
        raise LedgerMigrationRequired(
            f"trade ledger schema {version!r} is not supported; original file preserved"
        )
    if for_write and version != SCHEMA_VERSION:
        raise LedgerMigrationRequired(
            f"trade ledger schema {version} is read-only; run explicit migration first"
        )
    return document


def _write(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(document, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def migrate_trade_ledger(path: Path = LEDGER_PATH) -> bool:
    """Explicitly migrate the only recognized legacy schema without data loss."""

    with _LOCK:
        document = _read(path)
        version = document.get("schema_version")
        if version == SCHEMA_VERSION:
            return False
        if version != 1:
            raise LedgerMigrationRequired(
                f"no explicit migration is available for schema {version!r}"
            )
        migrated = dict(document)
        migrated["schema_version"] = SCHEMA_VERSION
        migrated["migrated_at"] = SERVER_CLOCK.server_now().isoformat(timespec="seconds")
        _write(path, migrated)
    return True


def _serialize(event: TradeEvent) -> dict[str, Any]:
    if event.observed_at.tzinfo is None or event.observed_at.utcoffset() is None:
        raise ValueError("trade event timestamps must be timezone-aware")
    payload = asdict(event)
    payload["event_type"] = event.event_type.value
    payload["observed_at"] = event.observed_at.isoformat(timespec="seconds")
    payload["operation_sequence"] = max(0, int(event.operation_sequence))
    return payload


def append_trade_events(path: Path, events: Iterable[TradeEvent]) -> int:
    """Append a batch atomically, ignoring already committed stable IDs."""

    payloads = [_serialize(event) for event in events]
    if not payloads:
        return 0
    with _LOCK:
        document = _read(path, for_write=True)
        existing = {str(item.get("event_id", "")) for item in document["events"]}
        pending = []
        for payload in payloads:
            event_id = str(payload.get("event_id", ""))
            if event_id and event_id not in existing:
                pending.append(payload)
                existing.add(event_id)
        if pending:
            document["events"].extend(pending)
            _write(path, document)
    return len(pending)


def append_trade_event(path: Path, event: TradeEvent) -> bool:
    return append_trade_events(path, (event,)) == 1


def reconcile_trade_baseline(
    path: Path,
    confirmed_round_trips: int,
    *,
    observed_at: datetime,
    source: ProgressSource = ProgressSource.USER_CORRECTED,
) -> bool:
    week_id = SERVER_CLOCK.server_week_id(observed_at)
    sequence = int(observed_at.timestamp())
    event = TradeEvent(
        event_id=stable_trade_event_id(
            week_id,
            "manual-baseline",
            "manual-baseline",
            "",
            TradeEventType.MANUAL_CORRECTION,
            sequence,
        ),
        server_week_id=week_id,
        route_id="manual-baseline",
        cycle_id="manual-baseline",
        leg_id="",
        event_type=TradeEventType.MANUAL_CORRECTION,
        origin="",
        destination="",
        observed_at=observed_at,
        confirmed_by=source.value,
        operation_sequence=sequence,
        metadata={
            "baseline_known": True,
            "baseline_round_trips": max(0, int(confirmed_round_trips)),
            "confirmed_round_trips": max(0, int(confirmed_round_trips)),
            "cutoff_observed_at": observed_at.isoformat(timespec="seconds"),
        },
    )
    return append_trade_event(path, event)


def _event_time(item: dict[str, Any]) -> datetime:
    return datetime.fromisoformat(str(item["observed_at"]))


def _cycle_phase(events: list[dict[str, Any]]) -> tuple[str, str, str]:
    if not events:
        return "NOT_STARTED", "", "NOT_STARTED"
    event_types = {str(item.get("event_type", "")) for item in events}
    if event_types & CYCLE_COMPLETION_TYPES:
        return "CYCLE_COMPLETED", "", "CYCLE_COMPLETED"
    completed = {
        str(item.get("leg_id", ""))
        for item in events
        if item.get("event_type") == TradeEventType.LEG_COMPLETED.value
        and item.get("leg_id")
    }
    if len(completed) >= 2:
        return "RETURN_COMPLETED", "", "LEG_COMPLETED"
    if len(completed) == 1:
        cycle_phase = "OUTBOUND_COMPLETED"
    else:
        cycle_phase = "CYCLE_STARTED" if TradeEventType.CYCLE_STARTED.value in event_types else "NOT_STARTED"
    active = [item for item in events if str(item.get("leg_id", "")) not in completed]
    if not active:
        return cycle_phase, "", "LEG_COMPLETED" if completed else "NOT_STARTED"
    latest = max(active, key=_event_time)
    leg_id = str(latest.get("leg_id", ""))
    order = (
        TradeEventType.LEG_PLANNED,
        TradeEventType.LEG_STARTED,
        TradeEventType.BOOK_USE_CONFIRMED,
        TradeEventType.PURCHASE_BOOK_CONFIRMED,
        TradeEventType.PURCHASE_CONFIRMED,
        TradeEventType.DEPARTURE_REQUESTED,
        TradeEventType.DEPARTURE_CONFIRMED,
        TradeEventType.ARRIVAL_CONFIRMED,
        TradeEventType.SALE_CONFIRMED,
        TradeEventType.LEG_COMPLETED,
    )
    leg_events = {str(item.get("event_type", "")) for item in active if item.get("leg_id") == leg_id}
    current_phase = next(
        (phase.value for phase in reversed(order) if phase.value in leg_events),
        "NOT_STARTED",
    )
    return cycle_phase, leg_id, current_phase


def load_trade_cycle_state(path: Path, cycle_id: str) -> TradeCycleState:
    with _LOCK:
        events = [
            item
            for item in _read(path)["events"]
            if str(item.get("cycle_id", "")) == str(cycle_id)
        ]
    events.sort(key=_event_time)
    phase, current_leg, current_leg_phase = _cycle_phase(events)
    completed = tuple(
        sorted(
            {
                str(item.get("leg_id", ""))
                for item in events
                if item.get("event_type") == TradeEventType.LEG_COMPLETED.value
                and item.get("leg_id")
            }
        )
    )
    first = events[0] if events else {}
    return TradeCycleState(
        cycle_id=str(cycle_id),
        route_id=str(first.get("route_id", "")),
        server_week_id=str(first.get("server_week_id", "")),
        phase=phase,
        completed_leg_ids=completed,
        current_leg_id=current_leg,
        current_leg_phase=current_leg_phase,
        purchase_books_used=sum(
            max(0, int(item.get("purchase_book_delta", 0)))
            for item in events
            if item.get("event_type") in BOOK_EVENT_TYPES
        ),
        events=tuple(events),
    )


def finalize_trade_cycle(
    path: Path,
    cycle_id: str,
    *,
    observed_at: datetime | None = None,
) -> bool:
    """Idempotently derive cycle completion from its two confirmed legs."""

    state = load_trade_cycle_state(path, cycle_id)
    if state.phase == "CYCLE_COMPLETED":
        return False
    if not state.ready_to_finalize:
        return False
    now = observed_at or SERVER_CLOCK.server_now()
    route = state.route_id.split("|") if state.route_id else []
    event = TradeEvent(
        event_id=stable_trade_event_id(
            state.server_week_id,
            state.route_id,
            state.cycle_id,
            "|".join(state.completed_leg_ids),
            TradeEventType.CYCLE_COMPLETED,
            0,
        ),
        server_week_id=state.server_week_id,
        route_id=state.route_id,
        cycle_id=state.cycle_id,
        leg_id="|".join(state.completed_leg_ids),
        event_type=TradeEventType.CYCLE_COMPLETED,
        origin=route[0] if route else "",
        destination=route[0] if route else "",
        observed_at=now,
        confirmed_by=ProgressSource.LEDGER_CONFIRMED.value,
    )
    return append_trade_event(path, event)


def _latest_correction(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    corrections = [item for item in events if item.get("event_type") in CORRECTION_TYPES]
    return max(corrections, key=_event_time) if corrections else None


def find_recoverable_cycle(path: Path, route_id: str) -> TradeCycleState | None:
    """Find the newest non-completed cycle, including one crossing a week reset."""

    with _LOCK:
        all_events = list(_read(path)["events"])
    corrections_by_week: dict[str, datetime] = {}
    for item in all_events:
        if item.get("event_type") in CORRECTION_TYPES:
            week_id = str(item.get("server_week_id", ""))
            corrections_by_week[week_id] = max(
                corrections_by_week.get(week_id, datetime.min.replace(tzinfo=_event_time(item).tzinfo)),
                _event_time(item),
            )
    cycle_ids: dict[str, datetime] = {}
    for item in all_events:
        if item.get("route_id") != route_id or not item.get("cycle_id"):
            continue
        observed = _event_time(item)
        cutoff = corrections_by_week.get(str(item.get("server_week_id", "")))
        if cutoff is not None and observed <= cutoff:
            continue
        cycle_ids[str(item["cycle_id"])] = max(
            cycle_ids.get(str(item["cycle_id"]), observed), observed
        )
    for cycle_id, _ in sorted(cycle_ids.items(), key=lambda item: item[1], reverse=True):
        state = load_trade_cycle_state(path, cycle_id)
        if state.phase != "CYCLE_COMPLETED":
            return state
    return None


def load_trade_week_state(
    path: Path = LEDGER_PATH,
    *,
    now: datetime | None = None,
) -> TradeWeekState:
    current = now or SERVER_CLOCK.server_now()
    week_id = SERVER_CLOCK.server_week_id(current)
    with _LOCK:
        document = _read(path)
        all_events = list(document["events"])
    events = [item for item in all_events if item.get("server_week_id") == week_id]
    correction = _latest_correction(events)
    cutoff = _event_time(correction) if correction else None
    active_events = [
        item
        for item in events
        if item.get("event_type") not in CORRECTION_TYPES
        and (cutoff is None or _event_time(item) > cutoff)
    ]
    baseline_known = correction is not None
    baseline = None
    source = ProgressSource.UNKNOWN
    last_reconciled_at = None
    if correction is not None:
        metadata = correction.get("metadata") or {}
        baseline = max(
            0,
            int(
                metadata.get(
                    "baseline_round_trips",
                    metadata.get("confirmed_round_trips", 0),
                )
            ),
        )
        try:
            source = ProgressSource(
                correction.get("confirmed_by", ProgressSource.RECONCILED.value)
            )
        except ValueError:
            source = ProgressSource.RECONCILED
        last_reconciled_at = cutoff
    elif active_events:
        source = ProgressSource.LEDGER_CONFIRMED

    completed_cycle_ids = {
        str(item.get("cycle_id", ""))
        for item in active_events
        if item.get("event_type") in CYCLE_COMPLETION_TYPES and item.get("cycle_id")
    }
    confirmed_delta = len(completed_cycle_ids)
    full_week_total = (baseline or 0) + confirmed_delta if baseline_known else None
    leg_events = [
        item
        for item in active_events
        if item.get("event_type") == TradeEventType.LEG_COMPLETED.value
    ]
    partial_by_cycle: dict[str, list[dict[str, Any]]] = {}
    for item in leg_events:
        cycle_id = str(item.get("cycle_id", ""))
        if cycle_id and cycle_id not in completed_cycle_ids:
            partial_by_cycle.setdefault(cycle_id, []).append(item)
    current_partial = None
    if partial_by_cycle:
        cycle_id, legs = max(
            partial_by_cycle.items(),
            key=lambda item: max(_event_time(leg) for leg in item[1]),
        )
        unique_legs = {str(leg.get("leg_id", leg.get("event_id", ""))) for leg in legs}
        latest_leg = max(legs, key=_event_time)
        current_partial = {
            "cycle_id": cycle_id,
            "route_id": latest_leg.get("route_id", ""),
            "server_week_id": latest_leg.get("server_week_id", week_id),
            "confirmed_legs": len(unique_legs),
            "last_destination": latest_leg.get("destination", ""),
        }
    transaction_types = {
        TradeEventType.PURCHASE_CONFIRMED.value,
        *BOOK_EVENT_TYPES,
        TradeEventType.SALE_CONFIRMED.value,
        TradeEventType.LEG_COMPLETED.value,
        *CYCLE_COMPLETION_TYPES,
    }
    transaction_events = [
        item for item in active_events if item.get("event_type") in transaction_types
    ]
    last_transaction = (
        max((_event_time(item) for item in transaction_events), default=None)
        if transaction_events
        else None
    )
    tracking_started_at = (
        cutoff
        if cutoff is not None
        else min((_event_time(item) for item in active_events), default=None)
    )
    return TradeWeekState(
        schema_version=int(document.get("schema_version", SCHEMA_VERSION)),
        server_week_id=week_id,
        source=source,
        confidence="UNKNOWN" if source is ProgressSource.UNKNOWN else "HIGH",
        baseline_known=baseline_known,
        baseline_round_trips=baseline,
        tracking_started_at=tracking_started_at,
        confirmed_delta_since_baseline=confirmed_delta,
        full_week_total=full_week_total,
        confirmed_round_trips=full_week_total,
        confirmed_legs=len(leg_events),
        current_partial_cycle=current_partial,
        purchase_books_used=sum(
            max(0, int(item.get("purchase_book_delta", 0)))
            for item in active_events
            if item.get("event_type") in BOOK_EVENT_TYPES
        ),
        fatigue_used_for_trade=sum(
            max(0, int(item.get("fatigue_delta", 0))) for item in active_events
        ),
        confirmed_profit=sum(int(item.get("profit_delta", 0)) for item in active_events),
        last_confirmed_transaction_at=last_transaction,
        last_reconciled_at=last_reconciled_at,
        ledger_sequence=len(events),
    )
