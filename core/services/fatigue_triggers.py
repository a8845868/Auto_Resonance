"""Persistent, deduplicated triggers for deferred fatigue-plan actions."""

from __future__ import annotations

import json
import os
import threading
import uuid
from dataclasses import asdict, is_dataclass
from pathlib import Path
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


def register_deferred_fatigue_actions(
    actions: Iterable[object],
    *,
    path: Path = STATE_PATH,
) -> int:
    normalized = []
    for index, action in enumerate(actions):
        if is_dataclass(action):
            payload = asdict(action)
        elif isinstance(action, dict):
            payload = dict(action)
        else:
            continue
        identity = ":".join(
            (
                SERVER_CLOCK.server_day_id(),
                str(payload.get("kind", "")),
                str(payload.get("waypoint_id", "")),
                str(index),
            )
        )
        normalized.append({"id": identity, "fired": False, **payload})
    with _LOCK:
        data = _read(path)
        existing = {str(item.get("id", "")): item for item in data["actions"]}
        for item in normalized:
            existing.setdefault(item["id"], item)
        data["actions"] = list(existing.values())
        _write(path, data)
    return len(normalized)


def notify_fatigue_event(
    event: str,
    waypoint_id: str = "",
    *,
    path: Path = STATE_PATH,
    schedule: Callable[[], None] | None = None,
) -> bool:
    """Schedule one replan when a matching deferred action becomes reachable."""

    event = str(event)
    waypoint_id = str(waypoint_id)
    matched = False
    with _LOCK:
        data = _read(path)
        for item in data["actions"]:
            if item.get("fired") is True:
                continue
            expected_waypoint = str(item.get("waypoint_id", ""))
            if expected_waypoint and expected_waypoint != waypoint_id:
                continue
            if event not in {
                "arrival",
                "sale_confirmed",
                "leg_completed",
                "bento_release",
                "fatigue_threshold",
            }:
                continue
            item["fired"] = True
            item["fired_by"] = event
            item["fired_at"] = SERVER_CLOCK.server_now().isoformat(timespec="seconds")
            matched = True
        if matched:
            _write(path, data)
    if matched:
        callback = schedule or (
            lambda: set_next_run("fatigue_recovery", SERVER_CLOCK.server_now())
        )
        callback()
    return matched


def cancel_deferred_fatigue_actions(*, path: Path = STATE_PATH) -> None:
    with _LOCK:
        _write(
            path,
            {
                "server_day_id": SERVER_CLOCK.server_day_id(),
                "actions": [],
                "cancelled_at": SERVER_CLOCK.server_now().isoformat(timespec="seconds"),
            },
        )
