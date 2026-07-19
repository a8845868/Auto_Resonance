from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from unittest.mock import Mock

import pytest

import tools.sixth_read_only_probe as probe
import core.services.fatigue_triggers as fatigue
from core.services import read_only_policy
from core.services.fatigue_triggers import (
    FatigueActionState,
    claim_fatigue_checkpoint,
    complete_fatigue_checkpoint_processing,
    fatigue_checkpoint_deferral,
    list_fatigue_actions,
    notify_fatigue_event,
    register_deferred_fatigue_actions,
)
from core.services.server_calendar import SERVER_CLOCK


def _unsafe_coordinate_is_denied(page: str) -> None:
    guard = read_only_policy.ReadOnlyActionGuard(Mock())
    assert guard.authorize_coordinate((900, 600), page_context=page) is False
    assert guard.journal[-1].action_key == "unclassified_tap"


def test_unclassified_reward_tap_is_denied():
    _unsafe_coordinate_is_denied("每日活跃 可领取")


def test_unclassified_fatigue_confirm_is_denied():
    _unsafe_coordinate_is_denied("便当 确认")


def test_unclassified_depart_tap_is_denied():
    _unsafe_coordinate_is_denied("启程")


def test_unclassified_account_tap_is_denied():
    _unsafe_coordinate_is_denied("账号设置 注销")


def test_explicit_safe_navigation_permit_is_page_bound_and_single_use():
    guard = read_only_policy.ReadOnlyActionGuard(Mock())
    with pytest.raises(PermissionError, match="trusted issuer"):
        guard.issue_permit(
            action_key="navigation_anchor",
            page_id="home",
            page_fingerprint="home-r1",
            anchor_key="daily_tab",
            coordinate=(800, 300),
        )


def test_probe_canaries_are_not_reported_as_live_blocked_actions():
    guard = read_only_policy.ReadOnlyActionGuard(Mock())
    canaries = probe.run_policy_canaries(guard)
    report = probe.policy_report(guard, policy_canary_results=canaries)
    keys = {item["action_key"] for item in report["policy_canary_results"]}
    assert {"transaction_buy", "reward_claim", "fatigue_confirm"} <= keys
    assert {
        "guard_public_api", "daily_horizontal_scroll", "reward_back"
    } <= keys
    assert report["live_action_journal"] == []
    assert report["actual_blocked_production_actions"] == []


def _checkpoint(path: Path, waypoint: str = "B", revision: str = "rev-old") -> dict:
    register_deferred_fatigue_actions(
        [{"kind": "REOBSERVE", "waypoint_id": waypoint}],
        plan_revision=revision,
        path=path,
    )
    notify_fatigue_event("arrival", waypoint, path=path, schedule=Mock())
    return claim_fatigue_checkpoint(
        expected_waypoint=waypoint,
        owner_id="worker-1",
        lease_token="lease-1",
        path=path,
    )


def _install_future(path: Path, old: dict, *, valid: bool = True) -> dict:
    raw = json.loads(path.read_text(encoding="utf-8"))
    future = {
        "id": "future-C",
        "state": "ACTIVE",
        "schedule_status": "ACTIVE",
        "trigger_type": "WAYPOINT",
        "waypoint_id": "C",
        "plan_revision": "rev-new",
        "source_plan_revision": "rev-new" if valid else "wrong",
        "parent_checkpoint_id": old["id"],
        "server_day_id": raw["server_day_id"],
        "cycle_server_day": raw["server_day_id"],
        "cycle_id": "cycle-1",
    }
    raw["actions"].append(future)
    path.write_text(json.dumps(raw), encoding="utf-8")
    return future


def test_defer_to_future_waypoint_transfers_checkpoint_without_deadlock(tmp_path: Path):
    path = tmp_path / "fatigue.json"
    old = _checkpoint(path)
    server_day = SERVER_CLOCK.server_day_id()
    result = complete_fatigue_checkpoint_processing(
        old["id"],
        {
            "success": True,
            "deferred": True,
            "status": "DEFER_UNTIL_WAYPOINT",
            "waypoint_id": "C",
            "source_plan_revision": "rev-new",
            "cycle_server_day": server_day,
            "cycle_id": "cycle-1",
            "transfer_intent": {
                "target_waypoint": "C",
                "trigger_type": "WAYPOINT",
                "action_payload": {"kind": "REOBSERVE", "waypoint_id": "C"},
                "source_plan_revision": "rev-new",
                "cycle_id": "cycle-1",
                "cycle_server_day": server_day,
                "reason": "continue_at_future_waypoint",
            },
        },
        owner_id="worker-1", lease_token="lease-1", path=path,
    )
    assert result["outcome"] == "TRANSFER_TO_NEW_CHECKPOINT"
    assert result["checkpoint"]["state"] == "SUPERSEDED"
    assert fatigue_checkpoint_deferral("B", path=path) is None
    assert fatigue_checkpoint_deferral("C", path=path)["deferred"] is True


def test_missing_transfer_contract_keeps_current_checkpoint_retryable(tmp_path: Path):
    path = tmp_path / "fatigue.json"
    old = _checkpoint(path)
    result = complete_fatigue_checkpoint_processing(
        old["id"],
        {"success": True, "deferred": True, "status": "DEFER_UNTIL_WAYPOINT", "waypoint_id": "C", "source_plan_revision": "rev-new"},
        owner_id="worker-1", lease_token="lease-1", path=path,
    )
    assert result["outcome"] == "RETRY_ON_EVENT"
    assert fatigue_checkpoint_deferral("B", path=path)["checkpoint_state"] == "FAILED_RETRYABLE"
    assert "transfer_intent" in result["diagnostic"]


def test_claimed_checkpoint_cannot_be_claimed_by_second_owner(tmp_path: Path):
    path = tmp_path / "fatigue.json"
    old = _checkpoint(path)
    with pytest.raises(RuntimeError, match="owned|claimed"):
        claim_fatigue_checkpoint(
            action_id=old["id"], owner_id="worker-2", lease_token="lease-2", path=path
        )
    same = claim_fatigue_checkpoint(
        action_id=old["id"], owner_id="worker-1", lease_token="lease-1", path=path
    )
    assert same["claim_attempt"] == 1


def test_expired_claim_requires_explicit_lease_recovery(tmp_path: Path):
    path = tmp_path / "fatigue.json"
    old = _checkpoint(path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["actions"][0]["lease_expires_at"] = "2000-01-01T00:00:00+08:00"
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(RuntimeError, match="recover_stale_claim"):
        claim_fatigue_checkpoint(
            old["id"], owner_id="worker-2", lease_token="lease-2", path=path
        )
    recover = getattr(fatigue, "recover_stale_claim", None)
    assert recover is not None, "production recover_stale_claim entry point is missing"
    recovered = recover(old["id"], actor="supervisor", path=path)
    assert recovered["state"] == "FAILED_RETRYABLE"
    assert recovered["lease_recovery_audit"]["actor"] == "supervisor"


def test_unresolved_checkpoint_survives_server_day_rollover(tmp_path: Path):
    path = tmp_path / "fatigue.json"
    old = _checkpoint(path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["server_day_id"] = "2000-01-01"
    raw["actions"][0]["cycle_id"] = "cycle-1"
    raw["actions"][0]["cycle_server_day"] = "2000-01-01"
    path.write_text(json.dumps(raw), encoding="utf-8")
    actions = list_fatigue_actions(path=path)
    assert next(item for item in actions if item["id"] == old["id"])["state"] == "CLAIMED"


def test_ordinary_untriggered_daily_action_expires_with_audit_record(tmp_path: Path):
    path = tmp_path / "fatigue.json"
    register_deferred_fatigue_actions(
        [{"kind": "REOBSERVE", "waypoint_id": "B"}], plan_revision="old", path=path
    )
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["server_day_id"] = "2000-01-01"
    path.write_text(json.dumps(raw), encoding="utf-8")
    item = list_fatigue_actions(path=path)[0]
    assert item["state"] == FatigueActionState.EXPIRED.value
    assert item["expiry_audit"]["reason"] == "server_day_rollover_untriggered"
