from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from core.utils.utils import ROOT_PATH


STATE_PATH = Path(ROOT_PATH).resolve() / "config" / "weekly_plan.json"


def current_week_start(today: date | None = None) -> str:
    today = today or date.today()
    return (today - timedelta(days=today.weekday())).isoformat()


def _flatten_batches(batches: list[dict]) -> list[dict[str, int]]:
    runs: list[dict[str, int]] = []
    for batch in batches:
        books = {str(city): int(count) for city, count in batch.get("books", {}).items()}
        runs.extend(dict(books) for _ in range(int(batch.get("runs", 0))))
    return runs


def _compress_runs(runs: list[dict[str, int]]) -> list[dict]:
    batches: list[dict] = []
    for books in runs:
        if batches and batches[-1]["books"] == books:
            batches[-1]["runs"] += 1
        else:
            batches.append({"runs": 1, "books": dict(books)})
    return batches


def load_weekly_plan(include_expired: bool = False) -> dict[str, Any] | None:
    if not STATE_PATH.exists():
        return None
    try:
        state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    if not include_expired and state.get("week_start") != current_week_start():
        return None
    return state


def save_weekly_plan(result: dict) -> dict[str, Any]:
    runs = _flatten_batches(result["execution_batches"])
    existing = load_weekly_plan()
    completed_runs = 0
    completed_books = 0
    if existing and existing.get("cycle") == result["cycle"] and existing.get("runs") == runs:
        completed_runs = min(int(existing.get("completed_runs", 0)), len(runs))
        completed_books = int(existing.get("completed_books", 0))
    state: dict[str, Any] = {
        "version": 1,
        "week_start": current_week_start(),
        "cycle": result["cycle"],
        "total_runs": len(runs),
        "runs": runs,
        "completed_runs": completed_runs,
        "completed_books": completed_books,
        "books_total": int(result.get("books_used", 0)),
        "cycle_fatigue": float(result.get("cycle_fatigue", 0)),
        "expected_profit": int(result.get("combined_profit", result.get("profit", 0))),
        "cargo_profit": int(result.get("cargo_profit", result.get("profit", 0))),
        "passenger_profit": int(result.get("passenger_profit", 0)),
        "passenger_plan": result.get("passenger_plan", {}),
        "price_time": result.get("price_time", ""),
        "optimizer_config": result.get("optimizer_config", {}),
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }
    _write_state(state)
    return state


def _write_state(state: dict[str, Any]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    temp_path = STATE_PATH.with_suffix(".tmp")
    temp_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    temp_path.replace(STATE_PATH)


def record_completed_run(books: dict[str, int]) -> dict[str, Any] | None:
    state = load_weekly_plan()
    if not state:
        return None
    completed = min(int(state.get("completed_runs", 0)) + 1, int(state.get("total_runs", 0)))
    state["completed_runs"] = completed
    state["completed_books"] = int(state.get("completed_books", 0)) + sum(int(v) for v in books.values())
    state["updated_at"] = datetime.now().isoformat(timespec="seconds")
    _write_state(state)
    return state


def remaining_batches(state: dict[str, Any] | None = None) -> list[dict]:
    state = state or load_weekly_plan()
    if not state:
        return []
    completed = int(state.get("completed_runs", 0))
    return _compress_runs(state.get("runs", [])[completed:])


def progress_summary(state: dict[str, Any] | None = None) -> dict[str, Any] | None:
    state = state or load_weekly_plan()
    if not state:
        return None
    completed = int(state.get("completed_runs", 0))
    total = int(state.get("total_runs", 0))
    books_used = int(state.get("completed_books", 0))
    books_total = int(state.get("books_total", 0))
    remaining = max(0, total - completed)
    batches = remaining_batches(state)
    current = batches[0] if batches else None
    return {
        **state,
        "remaining_runs": remaining,
        "remaining_books": max(0, books_total - books_used),
        "remaining_fatigue": round(remaining * float(state.get("cycle_fatigue", 0)), 2),
        "current_batch": current,
        "remaining_batches": batches,
        "finished": remaining == 0,
    }
