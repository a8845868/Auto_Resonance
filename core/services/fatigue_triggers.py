"""Persistent, deduplicated triggers for deferred fatigue-plan actions."""

from __future__ import annotations

import json
import hashlib
import os
import threading
import uuid
from dataclasses import asdict, is_dataclass
from pathlib import Path
from datetime import datetime
from typing import Any, Callable, Iterable

from core.services.runtime_control import RUNTIME_DIR
from core.services.server_calendar import SERVER_CLOCK
from core.services.task_schedule_state import set_next_run


STATE_PATH = RUNTIME_DIR / "fatigue-waypoints.json"
_LOCK = threading.RLock()


def _read(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    data.setdefault("server_day_id", SERVER_CLOCK.server_day_id())
    data.setdefault("actions", [])
    if data["server_day_id"] != SERVER_CLOCK.server_day_id():
        data = {"server_day_id": SERVER_CLOCK.server_day_id(), "actions": []}
    return data


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
                and item.get("fired") is not True
                and item.get("cancelled") is not True
            ):
                item["superseded"] = True
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
            if any(item.get(flag) is True for flag in ("fired", "cancelled", "superseded")):
                continue
            if item.get("schedule_status") == "PENDING_SCHEDULE":
                pending_ids.append(str(item.get("id", "")))
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
                    item["schedule_status"] = (
                        "PENDING_SCHEDULE"
                        if event == "recover_pending_schedule"
                        else "ACTIVE"
                    )
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
            item["schedule_status"] = "FIRED"
            item["fired"] = True
            item["fired_by"] = item.pop("pending_by", event)
            item["fired_at"] = SERVER_CLOCK.server_now().isoformat(timespec="seconds")
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
            and not any(item.get(flag) is True for flag in ("fired", "cancelled", "superseded"))
            for item in data["actions"]
        ):
            return False
    return notify_fatigue_event("recover_pending_schedule", path=path, schedule=schedule)


def cancel_deferred_fatigue_actions(*, path: Path = STATE_PATH) -> None:
    with _LOCK:
        data = _read(path)
        cancelled_at = SERVER_CLOCK.server_now().isoformat(timespec="seconds")
        for item in data["actions"]:
            if item.get("fired") is not True and item.get("superseded") is not True:
                item["cancelled"] = True
                item["cancelled_at"] = cancelled_at
        data["cancelled_at"] = cancelled_at
        _write(path, data)
