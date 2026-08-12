from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from core.services.daily_capabilities import CurrentResourceEvidence
from core.services.trade_planning import (
    StalePriceSnapshot,
    validate_executable_trade_budget,
    validate_price_execution_evidence,
)
from core.services.weekly_plan_state import (
    WeeklyStateCorrupt,
    progress_summary,
    save_current_city_evidence,
    save_current_resource_evidence,
    update_weekly_state,
)


NOW = datetime(2026, 7, 18, 12, 0, tzinfo=timezone(timedelta(hours=8)))


def _state() -> dict:
    return {
        "schema_version": 3,
        "cycle": ["A", "B"],
        "total_runs": 1,
        "completed_runs": 0,
        "runs": [{"A": 0, "B": 0}],
        "books_total": 0,
        "cycle_fatigue": 100,
        "leg_fatigue_schedule": [50, 50],
        "expected_profit": 1000,
        "price_time": NOW.isoformat(),
        "price_source": "game_observed",
        "price_revision": "r1",
        "current_price_evidence": {
            "source": "game_observed",
            "observed_at": NOW.isoformat(),
            "valid_until": (NOW + timedelta(minutes=15)).isoformat(),
            "revision": "r1",
            "station_pair": ["A", "B"],
            "server_day_id": "2026-07-18",
        },
        "current_resources": {
            "fatigue_used": 0, "fatigue_cap": 200, "available_fatigue": 200,
            "recoverable_fatigue_today": 0, "purchase_books_available": 0,
            "source": "game_observed", "observed_at": NOW.isoformat(),
            "valid_until": (NOW + timedelta(minutes=10)).isoformat(),
            "server_day_id": "2026-07-18", "revision": "resources-r1",
        },
        "current_city_evidence": {
            "city": "A", "source": "game_observed", "observed_at": NOW.isoformat(),
            "valid_until": (NOW + timedelta(minutes=10)).isoformat(),
            "server_day_id": "2026-07-18", "revision": "city-r1",
        },
    }


def _resource() -> CurrentResourceEvidence:
    return CurrentResourceEvidence(
        fatigue_used=0, fatigue_cap=200, available_fatigue=200,
        recoverable_fatigue_today=0, purchase_books_available=0,
        source="game_observed", observed_at=NOW,
        valid_until=NOW + timedelta(minutes=10), server_day_id="2026-07-18",
        revision="resources-r1",
    )


def test_resource_and_city_concurrent_updates_do_not_clobber(tmp_path: Path):
    path = tmp_path / "weekly.json"
    path.write_text(json.dumps(_state()), encoding="utf-8")
    threads = [
        threading.Thread(target=save_current_resource_evidence, kwargs={"evidence": _resource(), "path": path}),
        threading.Thread(target=save_current_city_evidence, kwargs={"city": "A", "source": "game_observed", "observed_at": NOW, "valid_until": NOW + timedelta(minutes=10), "revision": "city-r1", "path": path}),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["current_resources"]["revision"] == "resources-r1"
    assert saved["current_city_evidence"]["revision"] == "city-r1"


def test_state_update_preserves_route_runs_and_price(tmp_path: Path):
    path = tmp_path / "weekly.json"
    original = _state()
    path.write_text(json.dumps(original), encoding="utf-8")
    save_current_resource_evidence(_resource(), path=path)
    saved = json.loads(path.read_text(encoding="utf-8"))
    for key in ("cycle", "runs", "price_revision", "current_price_evidence"):
        assert saved[key] == original[key]


def test_corrupt_weekly_state_is_not_overwritten_with_partial_evidence(tmp_path: Path):
    path = tmp_path / "weekly.json"
    path.write_text("{broken", encoding="utf-8")
    with pytest.raises(WeeklyStateCorrupt):
        save_current_resource_evidence(_resource(), path=path)
    assert path.read_text(encoding="utf-8") == "{broken"


def test_corrupt_weekly_state_is_backed_up(tmp_path: Path):
    path = tmp_path / "weekly.json"
    path.write_text("{broken", encoding="utf-8")
    with pytest.raises(WeeklyStateCorrupt):
        update_weekly_state(lambda state: state.update(x=1), path=path)
    assert list(tmp_path.glob("weekly.json.corrupt.*"))


def test_weekly_state_read_modify_write_uses_one_lock_scope(tmp_path: Path):
    path = tmp_path / "weekly.json"
    path.write_text(json.dumps({"count": 0}), encoding="utf-8")
    threads = [threading.Thread(target=update_weekly_state, args=(lambda state: state.update(count=state["count"] + 1),), kwargs={"path": path}) for _ in range(20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert json.loads(path.read_text(encoding="utf-8"))["count"] == 20


def test_ui_recommendation_rejects_unapproved_price_source(tmp_path: Path):
    state = _state()
    state["current_price_evidence"]["source"] = "spreadsheet"
    summary = progress_summary(state, ledger_path=tmp_path / "ledger.json", now=NOW)
    assert summary["price_snapshot_fresh"] is False
    assert summary["today_recommendation_reason"] == "price_snapshot_not_fresh"


def test_ui_recommendation_detects_revision_mismatch(tmp_path: Path):
    state = _state()
    state["current_price_evidence"]["revision"] = "r2"
    summary = progress_summary(state, ledger_path=tmp_path / "ledger.json", now=NOW)
    assert summary["price_snapshot_fresh"] is False


def test_current_revision_is_not_copied_from_plan(tmp_path: Path):
    state = _state()
    state.pop("current_price_evidence")
    summary = progress_summary(state, ledger_path=tmp_path / "ledger.json", now=NOW)
    assert summary["current_price_revision"] == ""
    assert summary["price_snapshot_fresh"] is False


def test_ui_and_execution_share_same_price_gate(tmp_path: Path):
    state = _state()
    state["current_price_evidence"]["revision"] = "r2"
    with pytest.raises(StalePriceSnapshot, match="revision"):
        validate_executable_trade_budget(state, now=NOW, fatigue_budget=100, purchase_books=0)
    assert progress_summary(state, ledger_path=tmp_path / "ledger.json", now=NOW)["price_snapshot_fresh"] is False


def test_price_evidence_requires_station_pair_and_server_day():
    state = _state()
    state["current_price_evidence"]["station_pair"] = ["A", "C"]
    with pytest.raises(StalePriceSnapshot, match="station pair"):
        validate_price_execution_evidence(state, now=NOW)
