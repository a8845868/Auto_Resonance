"""Persistent ALAS-style last/next-run state for scheduler tasks."""

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any


STATE_PATH = Path("config/task_schedule.json")


def load_task_schedule(path: Path = STATE_PATH) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        data = {}
    data.setdefault("tasks", {})
    data.setdefault("completed", [])
    return data


def _save(state: dict[str, Any], path: Path = STATE_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def task_timing(task_key: str, path: Path = STATE_PATH) -> dict[str, Any]:
    return load_task_schedule(path)["tasks"].get(task_key, {})


def is_task_due(task_key: str, now: datetime | None = None, path: Path = STATE_PATH) -> bool:
    next_run = task_timing(task_key, path).get("next_run")
    if not next_run:
        return True
    try:
        return datetime.fromisoformat(next_run) <= (now or datetime.now())
    except ValueError:
        return True


def next_daily_reset(now: datetime | None = None) -> datetime:
    now = now or datetime.now()
    target = now.replace(hour=5, minute=0, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return target


def set_next_run(task_key: str, value: datetime | str | None, path: Path = STATE_PATH) -> None:
    state = load_task_schedule(path)
    task = state["tasks"].setdefault(task_key, {})
    if isinstance(value, datetime):
        value = value.isoformat(timespec="seconds")
    task["next_run"] = (value or "").strip()
    _save(state, path)


def record_task_execution(
    task_key: str,
    name: str,
    succeeded: bool,
    next_run: datetime | None,
    result: object = None,
    now: datetime | None = None,
    path: Path = STATE_PATH,
) -> dict[str, Any]:
    now = now or datetime.now()
    state = load_task_schedule(path)
    entry = {
        "key": task_key,
        "name": name,
        "last_run": now.isoformat(timespec="seconds"),
        "next_run": next_run.isoformat(timespec="seconds") if next_run else "",
        "status": "completed" if succeeded else "failed_or_stopped",
        "result": result if isinstance(result, (dict, list, str, int, float, bool, type(None))) else str(result),
    }
    state["tasks"][task_key] = entry
    state["completed"].insert(0, dict(entry))
    state["completed"] = state["completed"][:100]
    _save(state, path)
    return entry


def completed_history(path: Path = STATE_PATH) -> list[dict[str, Any]]:
    return load_task_schedule(path)["completed"]
