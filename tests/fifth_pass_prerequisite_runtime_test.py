from __future__ import annotations

import inspect
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import pytest

from app.view import dashboard_interface
from core.services import station_availability, weekly_plan_state
from core.services.daily_capabilities import resolve_daily_capability_prerequisites
from core.services.fatigue_triggers import recover_pending_fatigue_schedules
from core.services.server_calendar import SERVER_CLOCK


NOW = datetime(2026, 7, 18, 12, 0, tzinfo=timezone(timedelta(hours=8)))


def _trade_state(**updates):
    state = {
        "schema_version": 2,
        "cycle": ["A", "B"],
        "total_runs": 1,
        "completed_runs": 0,
        "runs": [{"A": 1, "B": 0}],
        "books_total": 1,
        "cycle_fatigue": 100,
        "expected_profit": 1000,
        "price_time": NOW.isoformat(),
        "price_source": "game_observed",
        "price_revision": "r1",
    }
    state.update(updates)
    return state


def _safe_passenger_state(observed_at=NOW, **updates):
    state = {
        "status": "pending",
        "completed_carriages": 2,
        "target_carriages": 8,
        "premium_currency_required": False,
        "automation_safe": True,
        "automation_safety_source": "game_observed",
        "automation_safety_reason": "build_button_and_currency_observed",
        "server_day_id": "2026-07-18",
        "observed_at": observed_at.isoformat(),
        "config_revision": "cfg-1",
        "evidence_config_revision": "cfg-1",
        "evidence_target_carriages": 8,
        "evidence_completed_carriages": 2,
        "evidence_status": "pending",
    }
    state.update(updates)
    return state


def _registry_evidence(*, available=("A", "B"), closed=(), unknown=(), source="station_registry"):
    return {
        "available": list(available),
        "closed": list(closed),
        "unknown": list(unknown),
        "source": source,
        "observed_at": NOW.isoformat(),
        "valid_until": (NOW + timedelta(minutes=5)).isoformat(),
    }


def test_stale_passenger_plan_does_not_satisfy_prerequisite():
    result = resolve_daily_capability_prerequisites(
        trade_state={},
        passenger_state=_safe_passenger_state(NOW - timedelta(hours=1)),
        now=NOW,
    )
    assert "safe_build_available" not in result.satisfied
    assert "stale" in result.evidence["safe_build_available"].block_reason


def test_previous_server_day_passenger_plan_is_unknown():
    result = resolve_daily_capability_prerequisites(
        trade_state={},
        passenger_state=_safe_passenger_state(
            NOW - timedelta(days=1), server_day_id="2026-07-17"
        ),
        now=NOW,
    )
    assert result.evidence["safe_build_available"].status == "UNKNOWN"
    assert "server_day" in result.evidence["safe_build_available"].block_reason


def test_passenger_safety_requires_evidence_source():
    state = _safe_passenger_state()
    state.pop("automation_safety_source")
    result = resolve_daily_capability_prerequisites(
        trade_state={}, passenger_state=state, now=NOW
    )
    assert "safe_build_available" not in result.satisfied
    assert "safety_evidence" in result.evidence["safe_build_available"].block_reason


def test_saved_route_does_not_claim_station_availability(tmp_path, monkeypatch):
    monkeypatch.setattr(weekly_plan_state, "STATE_PATH", tmp_path / "weekly.json")
    state = weekly_plan_state.save_weekly_plan(
        {
            "cycle": ["A", "B"],
            "execution_batches": [{"runs": 1, "books": {"A": 1, "B": 0}}],
            "books_used": 1,
            "cycle_fatigue": 100,
            "profit": 1000,
            "price_time": NOW.isoformat(),
            "price_source": "game_observed",
            "price_revision": "r1",
        }
    )
    assert "stations_available" not in state
    assert "station_availability_evidence" not in state


def test_closed_station_registry_blocks_reward_trade_dependency(monkeypatch):
    monkeypatch.setattr(
        station_availability,
        "station_availability_evidence",
        lambda stations, at=None: _registry_evidence(
            available=("A",), closed=("B",)
        ),
    )
    result = resolve_daily_capability_prerequisites(
        trade_state=_trade_state(), passenger_state={}, now=NOW,
        fatigue_budget=100, purchase_books=1,
    )
    assert "fresh_trade_plan" not in result.satisfied
    assert "B" in result.evidence["fresh_trade_plan"].block_reason


def test_station_availability_evidence_expires(monkeypatch):
    stale = _registry_evidence()
    stale["valid_until"] = (NOW - timedelta(seconds=1)).isoformat()
    refreshed = Mock(return_value=_registry_evidence(available=("A",), unknown=("B",)))
    monkeypatch.setattr(station_availability, "station_availability_evidence", refreshed)
    result = resolve_daily_capability_prerequisites(
        trade_state=_trade_state(station_availability_evidence=stale),
        passenger_state={}, now=NOW, fatigue_budget=100, purchase_books=1,
    )
    refreshed.assert_called_once()
    assert "fresh_trade_plan" not in result.satisfied


def test_prerequisite_log_reports_actual_registry_source(monkeypatch):
    monkeypatch.setattr(
        station_availability,
        "station_availability_evidence",
        lambda stations, at=None: _registry_evidence(),
    )
    result = resolve_daily_capability_prerequisites(
        trade_state=_trade_state(), passenger_state={}, now=NOW,
        fatigue_budget=100, purchase_books=1,
    )
    assert result.evidence["fresh_trade_plan"].source == "weekly_plan+station_registry"


def _pending_payload(server_day_id=None):
    server_day_id = server_day_id or SERVER_CLOCK.server_day_id()
    return {
        "server_day_id": server_day_id,
        "actions": [{
            "id": "pending-1",
            "plan_revision": "r1",
            "trigger_type": "WAYPOINT",
            "kind": "REOBSERVE_RECOVERY_AT_WAYPOINT",
            "waypoint_id": "B",
            "schedule_status": "PENDING_SCHEDULE",
            "fired": False,
            "cancelled": False,
            "superseded": False,
        }],
    }


def test_scheduler_startup_recovers_pending_fatigue_schedule(monkeypatch):
    callback = Mock(return_value=True)
    monkeypatch.setattr(
        dashboard_interface, "recover_pending_fatigue_schedules", callback
    )
    assert dashboard_interface.recover_startup_fatigue_schedules() is True
    callback.assert_called_once_with()


def test_startup_recovery_is_idempotent(tmp_path):
    path = tmp_path / "fatigue.json"
    path.write_text(json.dumps(_pending_payload()), encoding="utf-8")
    schedule = Mock()
    assert recover_pending_fatigue_schedules(path=path, schedule=schedule) is True
    assert recover_pending_fatigue_schedules(path=path, schedule=schedule) is False
    schedule.assert_called_once()


def test_startup_recovery_failure_remains_retryable(tmp_path):
    path = tmp_path / "fatigue.json"
    path.write_text(json.dumps(_pending_payload()), encoding="utf-8")
    with pytest.raises(OSError):
        recover_pending_fatigue_schedules(
            path=path, schedule=Mock(side_effect=OSError("state locked"))
        )
    persisted = json.loads(path.read_text(encoding="utf-8"))
    assert persisted["actions"][0]["schedule_status"] == "PENDING_SCHEDULE"


def test_previous_server_day_pending_trigger_is_discarded(tmp_path):
    path = tmp_path / "fatigue.json"
    path.write_text(json.dumps(_pending_payload("2026-07-17")), encoding="utf-8")
    schedule = Mock()
    assert recover_pending_fatigue_schedules(path=path, schedule=schedule) is False
    schedule.assert_not_called()


def test_recovery_symbol_has_real_production_caller():
    source = inspect.getsource(dashboard_interface.DashboardInterface.__init__)
    assert "recover_startup_fatigue_schedules" in source
