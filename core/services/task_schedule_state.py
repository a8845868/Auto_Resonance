"""Persistent ALAS-style last/next-run state for scheduler tasks."""

import json
import os
import shutil
import threading
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any


STATE_PATH = Path("config/task_schedule.json")
TASK_KEY_RUN_BUSINESS = "run_business"
_STATE_LOCK = threading.RLock()
SCHEMA_VERSION = 1
DATETIME_FIELDS = frozenset({
    "next_run", "last_run", "last_attempt", "completed_at", "progress_at",
})
TASK_STATUSES = frozenset({"completed", "deferred", "failed_or_stopped"})


class TaskScheduleStateCorrupt(RuntimeError):
    """The persisted task schedule cannot be trusted or safely overwritten."""


def _preserve_corrupt_schedule(path: Path) -> Path | None:
    if not path.is_file():
        return None
    stamp = datetime.now().astimezone().strftime("%Y%m%dT%H%M%S%f")
    backup = path.with_name(
        f"{path.name}.corrupt.{stamp}.{uuid.uuid4().hex[:8]}"
    )
    shutil.copy2(path, backup)
    return backup


def _system_local_timezone():
    return datetime.now().astimezone().tzinfo


def _is_json_value(value: object) -> bool:
    if value is None or isinstance(value, (str, int, float, bool)):
        return True
    if isinstance(value, list):
        return all(_is_json_value(item) for item in value)
    if isinstance(value, dict):
        return all(isinstance(key, str) and _is_json_value(item) for key, item in value.items())
    return False


def _validate_task_timing(task_key: str, entry: object) -> None:
    if not isinstance(entry, dict):
        raise ValueError(f"tasks.{task_key} must be an object")
    string_fields = {
        "key", "name", "next_run", "last_run", "last_attempt",
        "completed_at", "progress_at", "status",
    }
    for field in string_fields:
        if field in entry and not isinstance(entry[field], str):
            raise ValueError(f"tasks.{task_key}.{field} must be a string")
    for field in DATETIME_FIELDS:
        value = entry.get(field, "")
        if value:
            try:
                datetime.fromisoformat(value)
            except ValueError as error:
                raise ValueError(
                    f"tasks.{task_key}.{field} must be an ISO datetime"
                ) from error
    status = entry.get("status", "")
    if status and status not in TASK_STATUSES:
        raise ValueError(f"tasks.{task_key}.status is unsupported")
    if "force_verify" in entry and not isinstance(entry["force_verify"], bool):
        raise ValueError(f"tasks.{task_key}.force_verify must be a boolean")
    if "result" in entry and not _is_json_value(entry["result"]):
        raise ValueError(f"tasks.{task_key}.result must be JSON-safe")


def _validate_completed_entry(index: int, entry: object) -> None:
    if not isinstance(entry, dict):
        raise ValueError(f"completed.{index} must be an object")
    for field in (
        "key", "name", "next_run", "last_run", "last_attempt",
        "completed_at", "progress_at", "status",
    ):
        if field in entry and not isinstance(entry[field], str):
            raise ValueError(f"completed.{index}.{field} must be a string")
    for field in DATETIME_FIELDS:
        value = entry.get(field, "")
        if value:
            try:
                datetime.fromisoformat(value)
            except ValueError as error:
                raise ValueError(
                    f"completed.{index}.{field} must be an ISO datetime"
                ) from error
    status = entry.get("status", "")
    if status and status not in TASK_STATUSES:
        raise ValueError(f"completed.{index}.status is unsupported")
    if "force_verify" in entry and not isinstance(entry["force_verify"], bool):
        raise ValueError(f"completed.{index}.force_verify must be a boolean")
    if "result" in entry and not _is_json_value(entry["result"]):
        raise ValueError(f"completed.{index}.result must be JSON-safe")


def _validate_schedule_schema(data: object) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ValueError("schedule root must be an object")
    version = data.get("schema_version", SCHEMA_VERSION)
    if type(version) is not int or version != SCHEMA_VERSION:
        raise ValueError("task schedule schema version is unsupported")
    tasks = data.get("tasks", {})
    completed = data.get("completed", [])
    if not isinstance(tasks, dict) or not isinstance(completed, list):
        raise ValueError("task schedule root collections are invalid")
    for key, entry in tasks.items():
        if not isinstance(key, str) or not key:
            raise ValueError("task schedule key must be a non-empty string")
        _validate_task_timing(key, entry)
    for index, entry in enumerate(completed):
        _validate_completed_entry(index, entry)
    data["schema_version"] = SCHEMA_VERSION
    data.setdefault("tasks", {})
    data.setdefault("completed", [])
    return data


def _load_unlocked(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"schema_version": SCHEMA_VERSION, "tasks": {}, "completed": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError) as error:
        backup = _preserve_corrupt_schedule(path)
        raise TaskScheduleStateCorrupt(
            f"task schedule unreadable; preserved={backup}: {type(error).__name__}"
        ) from error
    try:
        return _validate_schedule_schema(data)
    except (TypeError, ValueError) as error:
        backup = _preserve_corrupt_schedule(path)
        raise TaskScheduleStateCorrupt(
            f"task schedule invalid schema; preserved={backup}: {error}"
        ) from error


def _save_unlocked(state: dict[str, Any], path: Path) -> None:
    _validate_schedule_schema(state)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(
        f"{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    )
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(state, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def task_result_succeeded(result: object) -> bool:
    """Honor an explicit result status before falling back to truthiness.

    Once a task returns a ``success`` field it is part of the task contract, so
    malformed values fail closed instead of making strings such as ``"false"``
    look successful merely because they are non-empty.
    """
    if isinstance(result, dict) and "success" in result:
        return result.get("success") is True
    return bool(result)


def task_result_deferred(result: object) -> bool:
    """Return whether a successful task intentionally requested a retry.

    A deferral is not a runtime failure and must not trigger self-healing, but
    it also must not advance the task's completion timestamp or normal
    schedule.  Requiring both fields to be the boolean ``True`` keeps malformed
    result documents fail-closed.
    """
    return (
        isinstance(result, dict)
        and result.get("success") is True
        and result.get("deferred") is True
    )


def load_task_schedule(path: Path = STATE_PATH) -> dict[str, Any]:
    with _STATE_LOCK:
        return _load_unlocked(path)


def _save(state: dict[str, Any], path: Path = STATE_PATH) -> None:
    with _STATE_LOCK:
        _save_unlocked(state, path)


def task_timing(task_key: str, path: Path = STATE_PATH) -> dict[str, Any]:
    return load_task_schedule(path)["tasks"].get(task_key, {})


def is_task_due(task_key: str, now: datetime | None = None, path: Path = STATE_PATH) -> bool:
    next_run = task_timing(task_key, path).get("next_run")
    if not next_run:
        return True
    target = datetime.fromisoformat(next_run)
    current = now or datetime.now(target.tzinfo)
    if target.tzinfo is not None and current.tzinfo is None:
        current = current.replace(
            tzinfo=_system_local_timezone()
        ).astimezone(target.tzinfo)
    elif target.tzinfo is None and current.tzinfo is not None:
        current = current.astimezone(_system_local_timezone()).replace(tzinfo=None)
    return target <= current


def next_daily_reset(now: datetime | None = None) -> datetime:
    from core.services.server_calendar import SERVER_CLOCK

    current = now or datetime.now()
    was_naive = current.tzinfo is None or current.utcoffset() is None
    aware = (
        current.replace(tzinfo=_system_local_timezone()).astimezone(
            SERVER_CLOCK.timezone
        )
        if was_naive
        else current.astimezone(SERVER_CLOCK.timezone)
    )
    target = SERVER_CLOCK.next_daily_reset(aware)
    return target.replace(tzinfo=None) if was_naive else target


def task_result_next_run(result: object) -> datetime | None:
    if not isinstance(result, dict):
        return None
    value = result.get("next_run_at")
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return None


def set_next_run(task_key: str, value: datetime | str | None, path: Path = STATE_PATH) -> None:
    with _STATE_LOCK:
        state = _load_unlocked(path)
        task = state["tasks"].setdefault(task_key, {})
        if isinstance(value, datetime):
            value = value.isoformat(timespec="seconds")
        task["next_run"] = (value or "").strip()
        task.pop("force_verify", None)
        _save_unlocked(state, path)


def request_immediate_run(task_key: str, path: Path = STATE_PATH) -> None:
    """Schedule a task now and preserve that the run was explicitly requested."""
    with _STATE_LOCK:
        state = _load_unlocked(path)
        task = state["tasks"].setdefault(task_key, {})
        task["next_run"] = ""
        task["force_verify"] = True
        _save_unlocked(state, path)


def is_force_verify_requested(task_key: str, path: Path = STATE_PATH) -> bool:
    return task_timing(task_key, path).get("force_verify") is True


def record_task_execution(
    task_key: str,
    name: str,
    succeeded: bool,
    next_run: datetime | None,
    result: object = None,
    now: datetime | None = None,
    path: Path = STATE_PATH,
    *,
    deferred: bool = False,
) -> dict[str, Any]:
    now = now or datetime.now()
    with _STATE_LOCK:
        state = _load_unlocked(path)
        previous = state["tasks"].get(task_key, {})
        attempt_time = now.isoformat(timespec="seconds")
        previous_completed_at = previous.get("completed_at", "")
        previous_progress_at = previous.get("progress_at", "")
        if not previous_completed_at and previous.get("status") == "completed":
            previous_completed_at = previous.get("last_run", "")
        completed = bool(succeeded and not deferred)
        entry = {
            "key": task_key,
            "name": name,
            "last_run": attempt_time,
            "last_attempt": attempt_time,
            "completed_at": attempt_time if completed else previous_completed_at,
            "progress_at": (
                attempt_time
                if isinstance(result, dict) and result.get("progress_made") is True
                else previous_progress_at
            ),
            "next_run": next_run.isoformat(timespec="seconds") if next_run else "",
            "status": (
                "deferred"
                if succeeded and deferred
                else "completed"
                if completed
                else "failed_or_stopped"
            ),
            "result": (
                result
                if isinstance(
                    result, (dict, list, str, int, float, bool, type(None))
                )
                else str(result)
            ),
        }
        state["tasks"][task_key] = entry
        history_entry = dict(entry)
        if not completed:
            history_entry["completed_at"] = ""
        state["completed"].insert(0, history_entry)
        state["completed"] = state["completed"][:100]
        _save_unlocked(state, path)
        return entry


def completed_history(path: Path = STATE_PATH) -> list[dict[str, Any]]:
    return load_task_schedule(path)["completed"]
