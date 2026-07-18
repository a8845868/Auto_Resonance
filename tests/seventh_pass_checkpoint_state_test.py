from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import Mock

import pytest

import auto.fatigue_recovery as recovery
from core.services.fatigue_triggers import (
    FatigueActionState,
    acknowledge_fatigue_checkpoint,
    claim_fatigue_checkpoint,
    fail_fatigue_checkpoint,
    fatigue_checkpoint_deferral,
    list_fatigue_actions,
    notify_fatigue_event,
    register_deferred_fatigue_actions,
    skip_fatigue_checkpoint,
)


def _checkpoint(tmp_path: Path) -> Path:
    path = tmp_path / "fatigue.json"
    register_deferred_fatigue_actions(
        [{"kind": "REOBSERVE", "waypoint_id": "B"}],
        plan_revision="rev-7",
        path=path,
    )
    notify_fatigue_event("arrival", "B", path=path, schedule=Mock())
    return path


def _run_result(monkeypatch, path: Path, result: dict):
    monkeypatch.setattr(recovery, "_run_daily_fatigue_recovery_impl", lambda **_kw: dict(result))
    return recovery.run_daily_fatigue_recovery(expected_waypoint="B", checkpoint_path=path)


def test_unknown_waypoint_result_is_not_acknowledged(tmp_path, monkeypatch):
    path = _checkpoint(tmp_path)
    result = _run_result(monkeypatch, path, {"success": True, "deferred": True, "status": "UNKNOWN", "next_run_at": "2099-01-01T00:00:00+08:00"})
    assert result["checkpoint_acknowledged"] is False
    assert all(item["state"] != FatigueActionState.ACKNOWLEDGED.value for item in list_fatigue_actions(path=path))


def test_defer_until_fatigue_is_not_acknowledged_as_completed(tmp_path, monkeypatch):
    path = _checkpoint(tmp_path)
    result = _run_result(monkeypatch, path, {"success": True, "deferred": True, "status": "DEFER_UNTIL_FATIGUE"})
    assert result["checkpoint_outcome"] == "RETRY_ON_EVENT"
    assert result["checkpoint_acknowledged"] is False


def test_defer_until_release_replaces_checkpoint_before_ack(tmp_path, monkeypatch):
    path = _checkpoint(tmp_path)
    result = _run_result(monkeypatch, path, {"success": True, "deferred": True, "status": "DEFER_UNTIL_RELEASE", "next_run_at": "2099-01-01T00:00:00+08:00"})
    states = [item["state"] for item in list_fatigue_actions(path=path)]
    assert FatigueActionState.SUPERSEDED.value in states
    assert FatigueActionState.SCHEDULED.value in states
    assert result["replacement_checkpoint_id"]


def test_unknown_checkpoint_keeps_next_leg_blocked(tmp_path, monkeypatch):
    path = _checkpoint(tmp_path)
    _run_result(monkeypatch, path, {"success": True, "deferred": True, "status": "UNKNOWN"})
    assert fatigue_checkpoint_deferral("B", path=path)["deferred"] is True


def test_retry_exhaustion_enters_manual_blocked(tmp_path):
    path = _checkpoint(tmp_path)
    for _ in range(3):
        action = claim_fatigue_checkpoint(expected_waypoint="B", path=path)
        failed = fail_fatigue_checkpoint(action["id"], "ocr_unknown", path=path)
    assert failed["state"] == FatigueActionState.MANUAL_BLOCKED.value


def test_manual_blocked_checkpoint_still_defers_business(tmp_path):
    path = _checkpoint(tmp_path)
    for _ in range(3):
        action = claim_fatigue_checkpoint(expected_waypoint="B", path=path)
        fail_fatigue_checkpoint(action["id"], "ocr_unknown", path=path)
    assert fatigue_checkpoint_deferral("B", path=path)["checkpoint_state"] == "MANUAL_BLOCKED"


def test_user_explicit_skip_is_audited_before_unblock(tmp_path):
    path = _checkpoint(tmp_path)
    action = list_fatigue_actions(path=path)[0]
    skipped = skip_fatigue_checkpoint(action["id"], reason="operator_confirmed", actor="user", path=path)
    assert skipped["state"] == FatigueActionState.CANCELLED.value
    assert skipped["skip_audit"]["actor"] == "user"
    assert fatigue_checkpoint_deferral("B", path=path) is None


def test_ack_requests_business_resume_after_worker_yield(tmp_path, monkeypatch):
    path = _checkpoint(tmp_path)
    requested = Mock()
    monkeypatch.setattr("core.services.task_schedule_state.request_immediate_run", requested)
    result = _run_result(monkeypatch, path, {"success": True, "status": "COMPLETE_FOR_DAY"})
    assert result["checkpoint_acknowledged"] is True
    requested.assert_called_once_with("business")


def test_checkpoint_result_and_persisted_state_are_atomic(tmp_path, monkeypatch):
    path = _checkpoint(tmp_path)
    result = _run_result(monkeypatch, path, {"success": True, "deferred": True, "status": "UNKNOWN"})
    actions = list_fatigue_actions(path=path)
    replacement = next(item for item in actions if item["id"] == result["replacement_checkpoint_id"])
    assert replacement["processing_outcome"] == result["checkpoint_outcome"]
    assert replacement["source_result"]["status"] == "UNKNOWN"


def test_corrupt_checkpoint_state_fails_closed(tmp_path):
    path = tmp_path / "fatigue.json"
    path.write_text("{broken", encoding="utf-8")
    result = fatigue_checkpoint_deferral("B", path=path)
    assert result["deferred"] is True
    assert result["reason"] == "fatigue_checkpoint_state_corrupt"


def test_corrupt_checkpoint_file_is_preserved(tmp_path):
    path = tmp_path / "fatigue.json"
    original = b"{broken"
    path.write_bytes(original)
    fatigue_checkpoint_deferral("B", path=path)
    backups = list(tmp_path.glob("fatigue.json.corrupt.*"))
    assert path.read_bytes() == original
    assert backups and backups[0].read_bytes() == original


def test_business_does_not_depart_when_checkpoint_state_unreadable(tmp_path):
    path = tmp_path / "fatigue.json"
    path.write_text("[]", encoding="utf-8")
    assert fatigue_checkpoint_deferral("B", path=path)["deferred"] is True


def test_valid_next_server_day_rotation_is_not_treated_as_corruption(tmp_path, monkeypatch):
    path = tmp_path / "fatigue.json"
    path.write_text(json.dumps({"server_day_id": "2000-01-01", "actions": []}), encoding="utf-8")
    assert list_fatigue_actions(path=path) == []
    assert not list(tmp_path.glob("*.corrupt.*"))
