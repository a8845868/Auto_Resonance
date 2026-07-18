"""Persistent, deduplicated triggers for deferred fatigue-plan actions."""

from __future__ import annotations

import json
import hashlib
import os
import shutil
import threading
import time
import uuid
from dataclasses import asdict, dataclass, is_dataclass
from pathlib import Path
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Callable, Iterable

from core.services.runtime_control import RUNTIME_DIR
from core.services.server_calendar import SERVER_CLOCK
from core.services.task_schedule_state import set_next_run


STATE_PATH = RUNTIME_DIR / "fatigue-waypoints.json"
_LOCK = threading.RLock()


class FatigueActionState(str, Enum):
    ACTIVE = "ACTIVE"
    SCHEDULED = "SCHEDULED"
    CLAIMED = "CLAIMED"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    FAILED_RETRYABLE = "FAILED_RETRYABLE"
    MANUAL_BLOCKED = "MANUAL_BLOCKED"
    CANCELLED = "CANCELLED"
    SUPERSEDED = "SUPERSEDED"
    EXPIRED = "EXPIRED"


class CheckpointProcessingOutcome(str, Enum):
    ACKNOWLEDGE = "ACKNOWLEDGE"
    RETRY_AT = "RETRY_AT"
    RETRY_ON_EVENT = "RETRY_ON_EVENT"
    MANUAL_BLOCKED = "MANUAL_BLOCKED"
    CANCELLED_BY_USER = "CANCELLED_BY_USER"
    TRANSFER_TO_NEW_CHECKPOINT = "TRANSFER_TO_NEW_CHECKPOINT"


@dataclass(frozen=True)
class CheckpointTransferIntent:
    target_waypoint: str
    trigger_type: str
    action_payload: dict[str, Any]
    source_plan_revision: str
    cycle_id: str
    cycle_server_day: str
    reason: str

    @classmethod
    def from_result(cls, result: dict[str, Any]) -> "CheckpointTransferIntent":
        raw = result.get("transfer_intent")
        if not isinstance(raw, dict):
            raise ValueError("DEFER_UNTIL_WAYPOINT requires transfer_intent")
        intent = cls(
            target_waypoint=str(raw.get("target_waypoint", "")).strip(),
            trigger_type=str(raw.get("trigger_type", "")).upper().strip(),
            action_payload=dict(raw.get("action_payload") or {}),
            source_plan_revision=str(raw.get("source_plan_revision", "")).strip(),
            cycle_id=str(raw.get("cycle_id", "")).strip(),
            cycle_server_day=str(raw.get("cycle_server_day", "")).strip(),
            reason=str(raw.get("reason", "")).strip(),
        )
        if (
            not intent.target_waypoint
            or intent.trigger_type != "WAYPOINT"
            or not intent.source_plan_revision
            or not intent.cycle_id
            or not intent.cycle_server_day
            or str(intent.action_payload.get("waypoint_id", "")) != intent.target_waypoint
        ):
            raise ValueError("fatigue checkpoint transfer contract is incomplete")
        return intent


class CheckpointStateCorrupt(RuntimeError):
    """The checkpoint journal cannot be trusted and must block departure."""


_TERMINAL_STATES = {
    FatigueActionState.ACKNOWLEDGED.value,
    FatigueActionState.CANCELLED.value,
    FatigueActionState.SUPERSEDED.value,
    FatigueActionState.EXPIRED.value,
}


def _state(item: dict[str, Any]) -> str:
    value = str(item.get("state") or item.get("schedule_status") or "ACTIVE")
    if value == "FIRED":
        return FatigueActionState.ACKNOWLEDGED.value
    if value == "PENDING_SCHEDULE":
        return FatigueActionState.ACTIVE.value
    return value


def _set_state(item: dict[str, Any], state: FatigueActionState) -> None:
    item["state"] = state.value
    item["schedule_status"] = state.value
    item["fired"] = state is FatigueActionState.ACKNOWLEDGED
    item["cancelled"] = state is FatigueActionState.CANCELLED
    item["superseded"] = state is FatigueActionState.SUPERSEDED


def _read(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"server_day_id": SERVER_CLOCK.server_day_id(), "actions": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError) as error:
        _preserve_corrupt_state(path)
        raise CheckpointStateCorrupt(f"checkpoint state unreadable: {type(error).__name__}") from error
    if not isinstance(data, dict) or not isinstance(data.get("actions", []), list):
        _preserve_corrupt_state(path)
        raise CheckpointStateCorrupt("checkpoint state has invalid schema")
    data.setdefault("server_day_id", SERVER_CLOCK.server_day_id())
    data.setdefault("actions", [])
    current_day = SERVER_CLOCK.server_day_id()
    if data["server_day_id"] != current_day:
        previous_day = str(data["server_day_id"])
        expired_ids: list[str] = []
        preserved_ids: list[str] = []
        for item in data["actions"]:
            state = _state(item)
            active_cycle = bool(item.get("cycle_id") or item.get("cycle_server_day"))
            if state == FatigueActionState.ACTIVE.value and not active_cycle:
                _set_state(item, FatigueActionState.EXPIRED)
                item["expired_at"] = SERVER_CLOCK.server_now().isoformat(timespec="seconds")
                item["expiry_audit"] = {
                    "reason": "server_day_rollover_untriggered",
                    "from_server_day": previous_day,
                    "to_server_day": current_day,
                }
                expired_ids.append(str(item.get("id", "")))
            else:
                preserved_ids.append(str(item.get("id", "")))
        data["server_day_id"] = current_day
        data.setdefault("rollover_audit", []).append({
            "at": SERVER_CLOCK.server_now().isoformat(timespec="seconds"),
            "from_server_day": previous_day, "to_server_day": current_day,
            "expired_ids": expired_ids, "preserved_ids": preserved_ids,
        })
        _write(path, data)
    return data


def _preserve_corrupt_state(path: Path) -> Path | None:
    if not path.is_file():
        return None
    stamp = SERVER_CLOCK.server_now().strftime("%Y%m%dT%H%M%S%f")
    backup = path.with_name(f"{path.name}.corrupt.{stamp}.{uuid.uuid4().hex[:8]}")
    try:
        shutil.copy2(path, backup)
    except OSError:
        return None
    return backup


def _write(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    for attempt in range(5):
        try:
            os.replace(temporary, path)
            break
        except PermissionError:
            if attempt == 4:
                temporary.unlink(missing_ok=True)
                raise
            time.sleep(0.02 * (attempt + 1))


def replace_deferred_fatigue_plan(
    actions: Iterable[object],
    *,
    plan_revision: str | None = None,
    path: Path = STATE_PATH,
) -> int:
    payloads = []
    for action in actions:
        if is_dataclass(action):
            payload = asdict(action)
        elif isinstance(action, dict):
            payload = dict(action)
        else:
            continue
        payloads.append(payload)
    if not plan_revision:
        canonical = json.dumps(payloads, ensure_ascii=False, sort_keys=True, default=str)
        plan_revision = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
    normalized = []
    for index, payload in enumerate(payloads):
        trigger_type = str(payload.get("trigger_type", "")).upper()
        if not trigger_type:
            if payload.get("waypoint_id"):
                trigger_type = "WAYPOINT"
            elif payload.get("fatigue_threshold") is not None:
                trigger_type = "FATIGUE_THRESHOLD"
            elif payload.get("run_at"):
                trigger_type = "REOBSERVE_AT"
            else:
                continue
        identity = ":".join(
            (
                SERVER_CLOCK.server_day_id(),
                str(plan_revision),
                trigger_type,
                str(payload.get("kind", "")),
                str(payload.get("waypoint_id", "")),
                str(index),
            )
        )
        normalized.append(
            {
                "id": identity,
                "plan_revision": str(plan_revision),
                "trigger_type": trigger_type,
                "fired": False,
                "state": FatigueActionState.ACTIVE.value,
                "schedule_status": "ACTIVE",
                "cancelled": False,
                "superseded": False,
                **payload,
            }
        )
    with _LOCK:
        data = _read(path)
        for item in data["actions"]:
            if (
                item.get("plan_revision") != plan_revision
                and _state(item) not in _TERMINAL_STATES
                and _state(item) != FatigueActionState.CLAIMED.value
            ):
                _set_state(item, FatigueActionState.SUPERSEDED)
                item["superseded_by"] = plan_revision
        existing = {str(item.get("id", "")): item for item in data["actions"]}
        for item in normalized:
            existing.setdefault(item["id"], item)
        data["actions"] = list(existing.values())
        data["active_revision"] = str(plan_revision)
        _write(path, data)
    return len(normalized)


def register_deferred_fatigue_actions(
    actions: Iterable[object],
    *,
    plan_revision: str | None = None,
    path: Path = STATE_PATH,
) -> int:
    """Compatibility alias for replacement semantics."""

    return replace_deferred_fatigue_plan(
        actions, plan_revision=plan_revision, path=path
    )


def notify_fatigue_event(
    event: str,
    waypoint_id: str = "",
    *,
    fatigue_used: int | None = None,
    now: datetime | None = None,
    path: Path = STATE_PATH,
    schedule: Callable[[], None] | None = None,
) -> bool:
    """Schedule one replan when a matching deferred action becomes reachable."""

    event = str(event)
    waypoint_id = str(waypoint_id)
    pending_ids: list[str] = []
    with _LOCK:
        data = _read(path)
        current = now or SERVER_CLOCK.server_now()
        for item in data["actions"]:
            if _state(item) in _TERMINAL_STATES:
                continue
            if item.get("schedule_status") == "PENDING_SCHEDULE":
                pending_ids.append(str(item.get("id", "")))
                continue
            if _state(item) in {
                FatigueActionState.SCHEDULED.value,
                FatigueActionState.CLAIMED.value,
                FatigueActionState.FAILED_RETRYABLE.value,
            }:
                continue
            trigger_type = str(item.get("trigger_type", "")).upper()
            is_match = False
            if trigger_type == "WAYPOINT":
                is_match = event == "arrival" and str(item.get("waypoint_id", "")) == waypoint_id
            elif trigger_type == "FATIGUE_THRESHOLD":
                is_match = (
                    event == "fatigue_threshold"
                    and fatigue_used is not None
                    and int(fatigue_used) >= int(item.get("fatigue_threshold", 0))
                )
            elif trigger_type in {"BENTO_RELEASE_AT", "REOBSERVE_AT"}:
                expected_event = "bento_release" if trigger_type == "BENTO_RELEASE_AT" else "reobserve"
                try:
                    run_at = datetime.fromisoformat(str(item.get("run_at", "")))
                    is_match = event == expected_event and current >= run_at
                except (TypeError, ValueError):
                    is_match = False
            if not is_match:
                continue
            item["schedule_status"] = "PENDING_SCHEDULE"
            item["pending_by"] = event
            item["pending_at"] = SERVER_CLOCK.server_now().isoformat(timespec="seconds")
            pending_ids.append(str(item.get("id", "")))
        if pending_ids:
            _write(path, data)
    if not pending_ids:
        return False
    callback = schedule or (
        lambda: set_next_run("fatigue_recovery", SERVER_CLOCK.server_now())
    )
    try:
        callback()
    except Exception as error:
        with _LOCK:
            data = _read(path)
            for item in data["actions"]:
                if str(item.get("id", "")) in pending_ids:
                    if event == "recover_pending_schedule":
                        item["schedule_status"] = "PENDING_SCHEDULE"
                    else:
                        _set_state(item, FatigueActionState.ACTIVE)
                    item["schedule_error"] = repr(error)
                    item["schedule_failed_at"] = SERVER_CLOCK.server_now().isoformat(
                        timespec="seconds"
                    )
            _write(path, data)
        raise
    with _LOCK:
        data = _read(path)
        for item in data["actions"]:
            if str(item.get("id", "")) not in pending_ids:
                continue
            _set_state(item, FatigueActionState.SCHEDULED)
            item["scheduled_by"] = item.pop("pending_by", event)
            item["scheduled_at"] = SERVER_CLOCK.server_now().isoformat(timespec="seconds")
        _write(path, data)
    return True


def recover_pending_fatigue_schedules(
    *,
    path: Path = STATE_PATH,
    schedule: Callable[[], None] | None = None,
) -> bool:
    """Finish a scheduling transaction left pending by a process crash."""

    with _LOCK:
        data = _read(path)
        if not any(
            item.get("schedule_status") == "PENDING_SCHEDULE"
            and _state(item) not in _TERMINAL_STATES
            for item in data["actions"]
        ):
            return False
    return notify_fatigue_event("recover_pending_schedule", path=path, schedule=schedule)


def cancel_deferred_fatigue_actions(*, path: Path = STATE_PATH) -> None:
    with _LOCK:
        data = _read(path)
        cancelled_at = SERVER_CLOCK.server_now().isoformat(timespec="seconds")
        for item in data["actions"]:
            if _state(item) not in {
                FatigueActionState.ACKNOWLEDGED.value,
                FatigueActionState.SUPERSEDED.value,
            }:
                _set_state(item, FatigueActionState.CANCELLED)
                item["cancelled_at"] = cancelled_at
        data["cancelled_at"] = cancelled_at
        _write(path, data)


def list_fatigue_actions(*, path: Path = STATE_PATH) -> list[dict[str, Any]]:
    """Return a detached, normalized checkpoint snapshot for diagnostics."""

    with _LOCK:
        actions = json.loads(json.dumps(_read(path).get("actions", [])))
    for item in actions:
        item["state"] = _state(item)
    return actions


def _matching_checkpoint(
    actions: list[dict[str, Any]],
    *,
    action_id: str | None = None,
    expected_waypoint: str | None = None,
    plan_revision: str | None = None,
) -> dict[str, Any] | None:
    allowed = {
        FatigueActionState.SCHEDULED.value,
        FatigueActionState.FAILED_RETRYABLE.value,
    }
    return next(
        (
            item
            for item in actions
            if _state(item) in allowed
            and (not action_id or str(item.get("id")) == str(action_id))
            and (
                expected_waypoint is None
                or str(item.get("waypoint_id", "")) == str(expected_waypoint)
            )
            and (
                plan_revision is None
                or str(item.get("plan_revision", "")) == str(plan_revision)
            )
        ),
        None,
    )


def claim_fatigue_checkpoint(
    action_id: str | None = None,
    *,
    expected_waypoint: str | None = None,
    plan_revision: str | None = None,
    expected_server_day: str | None = None,
    owner_id: str | None = None,
    lease_token: str | None = None,
    lease_duration: timedelta = timedelta(minutes=5),
    path: Path = STATE_PATH,
) -> dict[str, Any]:
    """Atomically claim a scheduled checkpoint after validating its identity."""

    with _LOCK:
        data = _read(path)
        if expected_server_day and data.get("server_day_id") != expected_server_day:
            raise RuntimeError("fatigue checkpoint server day mismatch")
        owner = str(owner_id or f"legacy-process:{os.getpid()}")
        token = str(lease_token or uuid.uuid4().hex)
        claimed = next((
            entry for entry in data["actions"]
            if _state(entry) == FatigueActionState.CLAIMED.value
            and (not action_id or str(entry.get("id")) == str(action_id))
            and (expected_waypoint is None or str(entry.get("waypoint_id", "")) == str(expected_waypoint))
            and (plan_revision is None or str(entry.get("plan_revision", "")) == str(plan_revision))
        ), None)
        if claimed is not None:
            if claimed.get("owner_id") == owner and claimed.get("lease_token") == token:
                return dict(claimed)
            raise RuntimeError(
                "fatigue checkpoint is already claimed; expired leases require recover_stale_claim"
            )
        item = _matching_checkpoint(
            data["actions"],
            action_id=action_id,
            expected_waypoint=expected_waypoint,
            plan_revision=plan_revision,
        )
        if item is None:
            raise RuntimeError("matching fatigue checkpoint is not scheduled")
        _set_state(item, FatigueActionState.CLAIMED)
        claimed_at = SERVER_CLOCK.server_now()
        item["owner_id"] = owner
        item["lease_token"] = token
        item["claimed_at"] = claimed_at.isoformat(timespec="seconds")
        item["lease_expires_at"] = (
            claimed_at + max(timedelta(seconds=1), lease_duration)
        ).isoformat(timespec="seconds")
        item["claim_attempt"] = int(item.get("claim_attempt", 0)) + 1
        _write(path, data)
        return dict(item)


def recover_stale_claim(
    action_id: str,
    *,
    actor: str,
    reason: str = "lease_expired",
    now: datetime | None = None,
    path: Path = STATE_PATH,
) -> dict[str, Any]:
    """Explicitly recover an expired lease and preserve an audit trail."""

    if not actor.strip():
        raise ValueError("stale claim recovery requires an actor")
    with _LOCK:
        data = _read(path)
        item = next((entry for entry in data["actions"] if str(entry.get("id")) == str(action_id)), None)
        if item is None or _state(item) != FatigueActionState.CLAIMED.value:
            raise RuntimeError("only a claimed fatigue checkpoint can be recovered")
        current = now or SERVER_CLOCK.server_now()
        try:
            expires = datetime.fromisoformat(str(item.get("lease_expires_at", "")))
        except ValueError as error:
            raise RuntimeError("claimed checkpoint lease expiry is invalid") from error
        if current <= expires:
            raise RuntimeError("fatigue checkpoint lease has not expired")
        audit = {
            "actor": actor.strip(), "reason": str(reason),
            "recovered_at": current.isoformat(timespec="seconds"),
            "previous_owner_id": item.get("owner_id"),
            "previous_lease_token": item.get("lease_token"),
        }
        item.setdefault("lease_recovery_history", []).append(audit)
        item["lease_recovery_audit"] = audit
        for key in ("owner_id", "lease_token", "claimed_at", "lease_expires_at"):
            item.pop(key, None)
        _set_state(item, FatigueActionState.FAILED_RETRYABLE)
        _write(path, data)
        return dict(item)


def acknowledge_fatigue_checkpoint(action_id: str, *, path: Path = STATE_PATH) -> dict[str, Any]:
    with _LOCK:
        data = _read(path)
        item = next((item for item in data["actions"] if str(item.get("id")) == str(action_id)), None)
        if item is None or _state(item) != FatigueActionState.CLAIMED.value:
            raise RuntimeError("only a claimed fatigue checkpoint can be acknowledged")
        _set_state(item, FatigueActionState.ACKNOWLEDGED)
        item["acknowledged_at"] = SERVER_CLOCK.server_now().isoformat(timespec="seconds")
        _write(path, data)
        return dict(item)


def fail_fatigue_checkpoint(
    action_id: str,
    reason: str,
    *,
    max_attempts: int = 3,
    path: Path = STATE_PATH,
) -> dict[str, Any]:
    with _LOCK:
        data = _read(path)
        item = next((item for item in data["actions"] if str(item.get("id")) == str(action_id)), None)
        if item is None:
            raise RuntimeError("fatigue checkpoint not found")
        attempts = int(item.get("claim_attempt", 0))
        if attempts >= max(1, int(max_attempts)):
            _set_state(item, FatigueActionState.MANUAL_BLOCKED)
            item["manual_blocked_reason"] = "retry_limit_exhausted"
            item["processing_outcome"] = CheckpointProcessingOutcome.MANUAL_BLOCKED.value
        else:
            _set_state(item, FatigueActionState.FAILED_RETRYABLE)
        item["failure_reason"] = str(reason)
        item["failed_at"] = SERVER_CLOCK.server_now().isoformat(timespec="seconds")
        _write(path, data)
        return dict(item)


def checkpoint_processing_outcome(result: dict[str, Any]) -> CheckpointProcessingOutcome:
    if result.get("success") is not True:
        return CheckpointProcessingOutcome.MANUAL_BLOCKED
    if result.get("deferred") is not True:
        return CheckpointProcessingOutcome.ACKNOWLEDGE
    status = str(result.get("status", "")).upper()
    if status == "DEFER_UNTIL_FATIGUE":
        return CheckpointProcessingOutcome.RETRY_ON_EVENT
    if status == "DEFER_UNTIL_WAYPOINT":
        return CheckpointProcessingOutcome.TRANSFER_TO_NEW_CHECKPOINT
    if status in {"UNKNOWN", "DEFER_UNTIL_RELEASE"}:
        return CheckpointProcessingOutcome.RETRY_AT
    return CheckpointProcessingOutcome.MANUAL_BLOCKED


def complete_fatigue_checkpoint_processing(
    action_id: str,
    result: dict[str, Any],
    *,
    owner_id: str | None = None,
    lease_token: str | None = None,
    path: Path = STATE_PATH,
) -> dict[str, Any]:
    """Atomically persist the processing outcome before returning it."""

    outcome = checkpoint_processing_outcome(result)
    with _LOCK:
        data = _read(path)
        item = next(
            (entry for entry in data["actions"] if str(entry.get("id")) == str(action_id)),
            None,
        )
        if item is None:
            raise RuntimeError("fatigue checkpoint not found")
        if not owner_id or not lease_token:
            raise RuntimeError("fatigue checkpoint completion requires owner and lease token")
        if str(item.get("owner_id", "")) != str(owner_id):
            raise RuntimeError("fatigue checkpoint owner mismatch")
        if str(item.get("lease_token", "")) != str(lease_token):
            raise RuntimeError("fatigue checkpoint lease token mismatch")
        if (
            outcome is CheckpointProcessingOutcome.TRANSFER_TO_NEW_CHECKPOINT
            and _state(item) == FatigueActionState.SUPERSEDED.value
            and item.get("superseded_by")
        ):
            intent = CheckpointTransferIntent.from_result(result)
            replay_identity = "|".join((
                str(item["id"]), intent.target_waypoint,
                intent.source_plan_revision, intent.cycle_id,
                intent.cycle_server_day,
            ))
            expected_replacement_id = "transfer:" + hashlib.sha256(
                replay_identity.encode("utf-8")
            ).hexdigest()[:24]
            if str(item["superseded_by"]) != expected_replacement_id:
                raise RuntimeError("fatigue checkpoint transfer replay intent mismatch")
            replacement = next(
                (entry for entry in data["actions"] if str(entry.get("id")) == str(item["superseded_by"])),
                None,
            )
            if replacement is not None:
                return {
                    "outcome": outcome.value,
                    "acknowledged": False,
                    "checkpoint": dict(item),
                    "replacement": dict(replacement),
                    "idempotent_replay": True,
                }
        try:
            lease_expires = datetime.fromisoformat(str(item.get("lease_expires_at", "")))
        except ValueError as error:
            raise RuntimeError("fatigue checkpoint lease expiry is invalid") from error
        if SERVER_CLOCK.server_now() > lease_expires:
            raise RuntimeError("fatigue checkpoint lease expired; explicit recovery required")
        if _state(item) != FatigueActionState.CLAIMED.value:
            raise RuntimeError("only a claimed fatigue checkpoint can be completed")
        if outcome is CheckpointProcessingOutcome.ACKNOWLEDGE:
            _set_state(item, FatigueActionState.ACKNOWLEDGED)
            item["acknowledged_at"] = SERVER_CLOCK.server_now().isoformat(timespec="seconds")
            item["processing_outcome"] = outcome.value
            item["source_result"] = dict(result)
            _write(path, data)
            return {"outcome": outcome.value, "acknowledged": True, "checkpoint": dict(item)}

        if outcome is CheckpointProcessingOutcome.TRANSFER_TO_NEW_CHECKPOINT:
            try:
                intent = CheckpointTransferIntent.from_result(result)
                old_cycle = str(item.get("cycle_id", ""))
                old_day = str(item.get("cycle_server_day") or data.get("server_day_id") or "")
                if old_cycle and old_cycle != intent.cycle_id:
                    raise ValueError("fatigue checkpoint transfer cycle mismatch")
                if old_day != intent.cycle_server_day:
                    raise ValueError("fatigue checkpoint transfer server day mismatch")
                if intent.cycle_server_day != str(data.get("server_day_id", "")):
                    raise ValueError("fatigue checkpoint transfer is not in the active server day")
            except ValueError as error:
                _set_state(item, FatigueActionState.FAILED_RETRYABLE)
                item["processing_outcome"] = CheckpointProcessingOutcome.RETRY_ON_EVENT.value
                item["checkpoint_diagnostic"] = str(error)
                item["source_result"] = dict(result)
                _write(path, data)
                return {
                    "outcome": CheckpointProcessingOutcome.RETRY_ON_EVENT.value,
                    "acknowledged": False,
                    "checkpoint": dict(item),
                    "diagnostic": str(error),
                }

            identity_source = "|".join((
                str(item["id"]), intent.target_waypoint,
                intent.source_plan_revision, intent.cycle_id,
                intent.cycle_server_day,
            ))
            replacement_id = "transfer:" + hashlib.sha256(
                identity_source.encode("utf-8")
            ).hexdigest()[:24]
            replacement = next(
                (entry for entry in data["actions"] if str(entry.get("id")) == replacement_id),
                None,
            )
            if replacement is None:
                replacement = {
                    **intent.action_payload,
                    "id": replacement_id,
                    "trigger_type": "WAYPOINT",
                    "waypoint_id": intent.target_waypoint,
                    "plan_revision": intent.source_plan_revision,
                    "source_plan_revision": intent.source_plan_revision,
                    "parent_checkpoint_id": str(item["id"]),
                    "replaces_checkpoint_id": str(item["id"]),
                    "cycle_id": intent.cycle_id,
                    "cycle_server_day": intent.cycle_server_day,
                    "reason": intent.reason,
                    "claim_attempt": 0,
                }
                _set_state(replacement, FatigueActionState.ACTIVE)
                data["actions"].append(replacement)
            _set_state(item, FatigueActionState.SUPERSEDED)
            item["superseded_by"] = replacement_id
            item["processing_outcome"] = CheckpointProcessingOutcome.TRANSFER_TO_NEW_CHECKPOINT.value
            item["source_result"] = dict(result)
            replacement["transfer_validated_at"] = SERVER_CLOCK.server_now().isoformat(timespec="seconds")
            _write(path, data)
            return {
                "outcome": CheckpointProcessingOutcome.TRANSFER_TO_NEW_CHECKPOINT.value,
                "acknowledged": False,
                "checkpoint": dict(item),
                "replacement": dict(replacement),
            }

        if outcome is CheckpointProcessingOutcome.MANUAL_BLOCKED:
            _set_state(item, FatigueActionState.MANUAL_BLOCKED)
            item["processing_outcome"] = outcome.value
            item["source_result"] = dict(result)
            item["manual_blocked_at"] = SERVER_CLOCK.server_now().isoformat(timespec="seconds")
            _write(path, data)
            return {"outcome": outcome.value, "acknowledged": False, "checkpoint": dict(item)}

        # _impl may already have installed the next plan. Prefer that action;
        # otherwise create a replacement in this same state transaction.
        replacement = next(
            (
                entry
                for entry in data["actions"]
                if entry is not item
                and _state(entry) not in _TERMINAL_STATES
                and str(entry.get("waypoint_id", "")) == str(item.get("waypoint_id", ""))
            ),
            None,
        )
        if replacement is None:
            replacement = dict(item)
            replacement["id"] = f"{item.get('id')}:retry:{uuid.uuid4().hex[:12]}"
            replacement.pop("claimed_at", None)
            replacement["claim_attempt"] = int(item.get("claim_attempt", 0))
            if outcome is CheckpointProcessingOutcome.RETRY_ON_EVENT:
                _set_state(replacement, FatigueActionState.ACTIVE)
            else:
                _set_state(replacement, FatigueActionState.SCHEDULED)
            data["actions"].append(replacement)
        elif outcome is CheckpointProcessingOutcome.RETRY_AT:
            _set_state(replacement, FatigueActionState.SCHEDULED)
        replacement["processing_outcome"] = outcome.value
        replacement["source_result"] = dict(result)
        replacement["replaces_checkpoint_id"] = str(item.get("id"))
        if result.get("next_run_at"):
            replacement["run_at"] = str(result["next_run_at"])
        _set_state(item, FatigueActionState.SUPERSEDED)
        item["superseded_by"] = str(replacement.get("id"))
        item["processing_outcome"] = outcome.value
        _write(path, data)
        return {
            "outcome": outcome.value,
            "acknowledged": False,
            "checkpoint": dict(item),
            "replacement": dict(replacement),
        }


def skip_fatigue_checkpoint(
    action_id: str,
    *,
    reason: str,
    actor: str,
    path: Path = STATE_PATH,
) -> dict[str, Any]:
    if not reason.strip() or not actor.strip():
        raise ValueError("checkpoint skip requires actor and reason")
    with _LOCK:
        data = _read(path)
        item = next((entry for entry in data["actions"] if str(entry.get("id")) == str(action_id)), None)
        if item is None or _state(item) in _TERMINAL_STATES:
            raise RuntimeError("active fatigue checkpoint not found")
        _set_state(item, FatigueActionState.CANCELLED)
        item["processing_outcome"] = CheckpointProcessingOutcome.CANCELLED_BY_USER.value
        item["skip_audit"] = {
            "actor": actor.strip(),
            "reason": reason.strip(),
            "at": SERVER_CLOCK.server_now().isoformat(timespec="seconds"),
        }
        _write(path, data)
        return dict(item)


def fatigue_checkpoint_deferral(
    waypoint_id: str,
    *,
    path: Path = STATE_PATH,
) -> dict[str, Any] | None:
    try:
        actions = list_fatigue_actions(path=path)
    except CheckpointStateCorrupt:
        return {
            "success": True,
            "deferred": True,
            "progress_made": False,
            "reason": "fatigue_checkpoint_state_corrupt",
            "checkpoint_state": "CORRUPT",
            "expected_waypoint": str(waypoint_id),
            "server_day_id": SERVER_CLOCK.server_day_id(),
        }
    blocking_states = {
        FatigueActionState.ACTIVE.value,
        FatigueActionState.SCHEDULED.value,
        FatigueActionState.CLAIMED.value,
        FatigueActionState.FAILED_RETRYABLE.value,
        FatigueActionState.MANUAL_BLOCKED.value,
    }
    action = next(
        (
            item for item in actions
            if _state(item) in blocking_states
            and str(item.get("waypoint_id", "")) == str(waypoint_id)
        ),
        None,
    )
    if action is None:
        return None
    return {
        "success": True,
        "deferred": True,
        "progress_made": False,
        "reason": "fatigue_checkpoint_pending",
        "checkpoint_id": action.get("id"),
        "checkpoint_state": _state(action),
        "plan_revision": action.get("plan_revision"),
        "expected_waypoint": action.get("waypoint_id"),
        "server_day_id": SERVER_CLOCK.server_day_id(),
    }
