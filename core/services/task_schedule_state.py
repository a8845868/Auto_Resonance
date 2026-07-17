"""Persistent ALAS-style last/next-run state for scheduler tasks."""

import json
import os
import threading
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any


STATE_PATH = Path("config/task_schedule.json")
_STATE_LOCK = threading.RLock()


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
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError):
            data = {}
    data.setdefault("tasks", {})
    data.setdefault("completed", [])
    return data


def _save(state: dict[str, Any], path: Path = STATE_PATH) -> None:
    with _STATE_LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(
            f"{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
        )
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(state, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)


def task_timing(task_key: str, path: Path = STATE_PATH) -> dict[str, Any]:
    return load_task_schedule(path)["tasks"].get(task_key, {})


def is_task_due(task_key: str, now: datetime | None = None, path: Path = STATE_PATH) -> bool:
    next_run = task_timing(task_key, path).get("next_run")
    if not next_run:
        return True
    try:
        target = datetime.fromisoformat(next_run)
        current = now or datetime.now(target.tzinfo)
        if target.tzinfo is not None and current.tzinfo is None:
            current = current.replace(tzinfo=target.tzinfo)
        elif target.tzinfo is None and current.tzinfo is not None:
            current = current.replace(tzinfo=None)
        return target <= current
    except (TypeError, ValueError):
        return True


def next_daily_reset(now: datetime | None = None) -> datetime:
    from core.services.server_calendar import SERVER_CLOCK

    current = now or datetime.now()
    was_naive = current.tzinfo is None or current.utcoffset() is None
    aware = current.replace(tzinfo=SERVER_CLOCK.timezone) if was_naive else current
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
    state = load_task_schedule(path)
    task = state["tasks"].setdefault(task_key, {})
    if isinstance(value, datetime):
        value = value.isoformat(timespec="seconds")
    task["next_run"] = (value or "").strip()
    task.pop("force_verify", None)
    _save(state, path)


def request_immediate_run(task_key: str, path: Path = STATE_PATH) -> None:
    """Schedule a task now and preserve that the run was explicitly requested."""
    state = load_task_schedule(path)
    task = state["tasks"].setdefault(task_key, {})
    task["next_run"] = ""
    task["force_verify"] = True
    _save(state, path)


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
    state = load_task_schedule(path)
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
        "result": result if isinstance(result, (dict, list, str, int, float, bool, type(None))) else str(result),
    }
    state["tasks"][task_key] = entry
    history_entry = dict(entry)
    if not completed:
        history_entry["completed_at"] = ""
    state["completed"].insert(0, history_entry)
    state["completed"] = state["completed"][:100]
    _save(state, path)
    return entry


def completed_history(path: Path = STATE_PATH) -> list[dict[str, Any]]:
    return load_task_schedule(path)["completed"]
