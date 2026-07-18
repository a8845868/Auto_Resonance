from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock

import pytest

import auto.fatigue_recovery as recovery
import core.services.fatigue_triggers as triggers
from core.services.fatigue_planner import (
    FatigueAction,
    FatiguePlan,
    FatiguePlanStatus,
    FatigueSnapshot,
    RouteLeg,
    TradeRouteContext,
)
from core.services.server_calendar import SERVER_CLOCK


def _snapshot() -> FatigueSnapshot:
    return FatigueSnapshot(
        server_day_id=SERVER_CLOCK.server_day_id(),
        observed_at=SERVER_CLOCK.server_now(),
        fatigue_used=100,
        fatigue_cap=800,
        current_city_id="B",
        current_station_id="B",
        current_amenities=frozenset(),
        soda_uses_used=0,
        soda_uses_remaining=0,
        soda_reduction_per_use=0,
        soda_price_tiers=(),
        bento_batches_available=0,
        bento_total_reduction_available=0,
        next_bento_release_at=None,
        natural_recovery_at=None,
        source_confidence="HIGH",
    )


def _defer_plan() -> FatiguePlan:
    return FatiguePlan(
        snapshot=_snapshot(),
        immediate_actions=(),
        deferred_actions=(FatigueAction("REOBSERVE_RECOVERY_AT_WAYPOINT", waypoint_id="C"),),
        expected_fatigue_by_waypoint={"C": 150},
        expected_resource_usage={},
        expected_waste=0,
        next_trigger={"waypoint_id": "C"},
        status=FatiguePlanStatus.DEFER_UNTIL_WAYPOINT,
        reason="recover_at_future_waypoint",
    )


def _production_result(monkeypatch) -> dict:
    plan = _defer_plan()
    route = TradeRouteContext("B|C", (RouteLeg("B", "C", 50, frozenset()),), 0)
    monkeypatch.setattr(recovery, "connect", lambda: True)
    monkeypatch.setattr(recovery, "get_station", lambda: "B")
    monkeypatch.setattr(recovery, "_open_exchange_buy_page", lambda: True)
    monkeypatch.setattr(recovery, "_wait_strength", lambda *args, **kwargs: (100, 800))
    monkeypatch.setattr(recovery, "load_fatigue_usage", lambda: {})
    monkeypatch.setattr(recovery, "observe_recovery_resources", lambda _station: {})
    monkeypatch.setattr(recovery, "_snapshot_from_observation", lambda *args, **kwargs: plan.snapshot)
    monkeypatch.setattr(recovery, "_route_context", lambda _station=None: route)
    monkeypatch.setattr(recovery, "plan_fatigue_recovery", lambda *args, **kwargs: plan)
    monkeypatch.setattr(recovery, "go_home", lambda: None)
    register = Mock()
    monkeypatch.setattr(recovery, "register_deferred_fatigue_actions", register)
    result = recovery._run_daily_fatigue_recovery_impl(expected_waypoint="B")
    assert register.call_count == 0
    return result


def _claimed(path: Path) -> dict:
    day = SERVER_CLOCK.server_day_id()
    triggers.register_deferred_fatigue_actions(
        [{
            "kind": "REOBSERVE_RECOVERY_AT_WAYPOINT",
            "trigger_type": "WAYPOINT",
            "waypoint_id": "B",
            "cycle_id": "B|C",
            "cycle_server_day": day,
            "source_plan_revision": "rev-old",
        }],
        plan_revision="rev-old",
        path=path,
    )
    triggers.notify_fatigue_event("arrival", "B", path=path, schedule=Mock())
    return triggers.claim_fatigue_checkpoint(
        expected_waypoint="B",
        owner_id="worker-1",
        lease_token="lease-1",
        lease_duration=timedelta(minutes=5),
        path=path,
    )


def _intent() -> dict:
    return {
        "target_waypoint": "C",
        "trigger_type": "WAYPOINT",
        "action_payload": {"kind": "REOBSERVE_RECOVERY_AT_WAYPOINT", "waypoint_id": "C"},
        "source_plan_revision": "rev-new",
        "cycle_id": "B|C",
        "cycle_server_day": SERVER_CLOCK.server_day_id(),
        "reason": "recover_at_future_waypoint",
    }


def _result() -> dict:
    return {
        "success": True,
        "deferred": True,
        "status": "DEFER_UNTIL_WAYPOINT",
        "transfer_intent": _intent(),
    }


def test_real_fatigue_impl_transfers_b_to_c_without_manual_block(tmp_path: Path, monkeypatch):
    path = tmp_path / "fatigue.json"
    old = _claimed(path)
    result = _production_result(monkeypatch)
    transaction = triggers.complete_fatigue_checkpoint_processing(
        old["id"], result, owner_id="worker-1", lease_token="lease-1", path=path
    )
    assert transaction["outcome"] == "TRANSFER_TO_NEW_CHECKPOINT"
    assert transaction["checkpoint"]["state"] == "SUPERSEDED"
    assert transaction["replacement"]["waypoint_id"] == "C"


def test_production_shaped_defer_until_waypoint_contains_transfer_contract(monkeypatch):
    result = _production_result(monkeypatch)
    contract = result["transfer_intent"]
    assert contract["target_waypoint"] == "C"
    assert contract["cycle_id"] == "B|C"
    assert contract["cycle_server_day"]
    assert contract["source_plan_revision"]


def test_transfer_creates_parent_and_cycle_metadata_atomically(tmp_path: Path):
    path = tmp_path / "fatigue.json"
    old = _claimed(path)
    transaction = triggers.complete_fatigue_checkpoint_processing(
        old["id"], _result(), owner_id="worker-1", lease_token="lease-1", path=path
    )
    replacement = transaction["replacement"]
    assert replacement["parent_checkpoint_id"] == old["id"]
    assert replacement["replaces_checkpoint_id"] == old["id"]
    assert replacement["cycle_id"] == "B|C"
    assert replacement["cycle_server_day"] == SERVER_CLOCK.server_day_id()


def test_transfer_replay_is_idempotent(tmp_path: Path):
    path = tmp_path / "fatigue.json"
    old = _claimed(path)
    first = triggers.complete_fatigue_checkpoint_processing(
        old["id"], _result(), owner_id="worker-1", lease_token="lease-1", path=path
    )
    second = triggers.complete_fatigue_checkpoint_processing(
        old["id"], _result(), owner_id="worker-1", lease_token="lease-1", path=path
    )
    assert second["replacement"]["id"] == first["replacement"]["id"]
    data = json.loads(path.read_text(encoding="utf-8"))
    assert sum(item.get("waypoint_id") == "C" for item in data["actions"]) == 1


def test_transfer_write_failure_keeps_old_checkpoint_blocking(tmp_path: Path, monkeypatch):
    path = tmp_path / "fatigue.json"
    old = _claimed(path)
    monkeypatch.setattr(triggers, "_write", Mock(side_effect=PermissionError("locked")))
    with pytest.raises(PermissionError):
        triggers.complete_fatigue_checkpoint_processing(
            old["id"], _result(), owner_id="worker-1", lease_token="lease-1", path=path
        )
    persisted = json.loads(path.read_text(encoding="utf-8"))
    current = next(item for item in persisted["actions"] if item["id"] == old["id"])
    assert current["state"] == "CLAIMED"


def test_task_queue_b_to_c_sequence_resumes_route_after_c_ack(tmp_path: Path):
    path = tmp_path / "fatigue.json"
    old = _claimed(path)
    transferred = triggers.complete_fatigue_checkpoint_processing(
        old["id"], _result(), owner_id="worker-1", lease_token="lease-1", path=path
    )
    replacement = transferred["replacement"]
    triggers.notify_fatigue_event("arrival", "C", path=path, schedule=Mock())
    claimed_c = triggers.claim_fatigue_checkpoint(
        replacement["id"], owner_id="worker-2", lease_token="lease-2", path=path
    )
    acknowledged = triggers.complete_fatigue_checkpoint_processing(
        claimed_c["id"], {"success": True, "deferred": False},
        owner_id="worker-2", lease_token="lease-2", path=path,
    )
    assert acknowledged["acknowledged"] is True
    assert triggers.fatigue_checkpoint_deferral("C", path=path) is None


def test_no_handcrafted_replacement_metadata_required_by_caller(tmp_path: Path, monkeypatch):
    path = tmp_path / "fatigue.json"
    old = _claimed(path)
    production = _production_result(monkeypatch)
    assert "replacement" not in production
    result = triggers.complete_fatigue_checkpoint_processing(
        old["id"], production, owner_id="worker-1", lease_token="lease-1", path=path
    )
    assert result["replacement"]["parent_checkpoint_id"] == old["id"]
