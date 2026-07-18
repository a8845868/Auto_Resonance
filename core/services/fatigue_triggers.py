"""Persistent, deduplicated triggers for deferred fatigue-plan actions."""

from __future__ import annotations

import json
import hashlib
import os
import shutil
import threading
import uuid
from dataclasses import asdict, is_dataclass
from pathlib import Path
from datetime import datetime
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


class CheckpointProcessingOutcome(str, Enum):
    ACKNOWLEDGE = "ACKNOWLEDGE"
    RETRY_AT = "RETRY_AT"
    RETRY_ON_EVENT = "RETRY_ON_EVENT"
    MANUAL_BLOCKED = "MANUAL_BLOCKED"
    CANCELLED_BY_USER = "CANCELLED_BY_USER"


class CheckpointStateCorrupt(RuntimeError):
    """The checkpoint journal cannot be trusted and must block departure."""


_TERMINAL_STATES = {
    FatigueActionState.ACKNOWLEDGED.value,
    FatigueActionState.CANCELLED.value,
    FatigueActionState.SUPERSEDED.value,
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
    if data["server_day_id"] != SERVER_CLOCK.server_day_id():
        data = {"server_day_id": SERVER_CLOCK.server_day_id(), "actions": []}
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
    os.replace(temporary, path)


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
        FatigueActionState.CLAIMED.value,
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
    path: Path = STATE_PATH,
) -> dict[str, Any]:
    """Atomically claim a scheduled checkpoint after validating its identity."""

    with _LOCK:
        data = _read(path)
        if expected_server_day and data.get("server_day_id") != expected_server_day:
            raise RuntimeError("fatigue checkpoint server day mismatch")
        item = _matching_checkpoint(
            data["actions"],
            action_id=action_id,
            expected_waypoint=expected_waypoint,
            plan_revision=plan_revision,
        )
        if item is None:
            raise RuntimeError("matching fatigue checkpoint is not scheduled")
        _set_state(item, FatigueActionState.CLAIMED)
        item["claimed_at"] = SERVER_CLOCK.server_now().isoformat(timespec="seconds")
        item["claim_attempt"] = int(item.get("claim_attempt", 0)) + 1
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
    if status in {"UNKNOWN", "DEFER_UNTIL_RELEASE"}:
        return CheckpointProcessingOutcome.RETRY_AT
    return CheckpointProcessingOutcome.MANUAL_BLOCKED


def complete_fatigue_checkpoint_processing(
    action_id: str,
    result: dict[str, Any],
    *,
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
        if item is None or _state(item) != FatigueActionState.CLAIMED.value:
            raise RuntimeError("only a claimed fatigue checkpoint can be completed")
        if outcome is CheckpointProcessingOutcome.ACKNOWLEDGE:
            _set_state(item, FatigueActionState.ACKNOWLEDGED)
            item["acknowledged_at"] = SERVER_CLOCK.server_now().isoformat(timespec="seconds")
            item["processing_outcome"] = outcome.value
            item["source_result"] = dict(result)
            _write(path, data)
            return {"outcome": outcome.value, "acknowledged": True, "checkpoint": dict(item)}

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
