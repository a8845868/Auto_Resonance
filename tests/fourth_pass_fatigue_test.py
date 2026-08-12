from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from core.services.fatigue_planner import (
    FatiguePlanStatus,
    FatigueSnapshot,
    SodaPriceTier,
    plan_fatigue_recovery,
)
from core.services.fatigue_triggers import (
    notify_fatigue_event,
    recover_pending_fatigue_schedules,
    replace_deferred_fatigue_plan,
)


NOW = datetime(2026, 7, 18, 12, 0, tzinfo=timezone(timedelta(hours=8)))
ACTION = {"kind": "REPLAN", "trigger_type": "WAYPOINT", "waypoint_id": "B"}


def _state(path):
    return json.loads(path.read_text(encoding="utf-8"))


def _snapshot(**updates):
    values = dict(
        server_day_id="2026-07-18",
        observed_at=NOW,
        fatigue_used=100,
        fatigue_cap=200,
        current_city_id="B",
        current_station_id="B",
        current_amenities=frozenset({"REST_AREA"}),
        soda_uses_used=0,
        soda_uses_remaining=1,
        soda_reduction_per_use=50,
        soda_price_tiers=(SodaPriceTier(1, "FREE", 0, True),),
        bento_batches_available=0,
        bento_total_reduction_available=0,
        next_bento_release_at=None,
        natural_recovery_at=None,
        source_confidence="HIGH",
        fatigue_confidence="HIGH",
        station_confidence="HIGH",
        amenity_confidence="HIGH",
        soda_tier_confidence="HIGH",
        soda_remaining_confidence="HIGH",
        bento_inventory_confidence="HIGH",
        bento_value_confidence="HIGH",
    )
    values.update(updates)
    return FatigueSnapshot(**values)


def test_complete_plan_supersedes_old_fatigue_trigger(tmp_path):
    path = tmp_path / "fatigue.json"
    replace_deferred_fatigue_plan([ACTION], plan_revision="active", path=path)
    replace_deferred_fatigue_plan([], plan_revision="complete", path=path)
    old = _state(path)["actions"][0]
    assert old["superseded"] is True
    assert old["superseded_by"] == "complete"


def test_empty_revision_cancels_previous_active_action(tmp_path):
    path = tmp_path / "fatigue.json"
    replace_deferred_fatigue_plan([ACTION], plan_revision="one", path=path)
    assert replace_deferred_fatigue_plan([], plan_revision="empty", path=path) == 0
    assert not notify_fatigue_event("arrival", "B", path=path, schedule=lambda: None)


def test_schedule_failure_does_not_consume_trigger(tmp_path):
    path = tmp_path / "fatigue.json"
    replace_deferred_fatigue_plan([ACTION], plan_revision="one", path=path)
    with pytest.raises(RuntimeError, match="scheduler down"):
        notify_fatigue_event(
            "arrival",
            "B",
            path=path,
            schedule=lambda: (_ for _ in ()).throw(RuntimeError("scheduler down")),
        )
    item = _state(path)["actions"][0]
    assert item["schedule_status"] == "ACTIVE"
    assert item["fired"] is False


def test_pending_schedule_is_recovered_after_restart(tmp_path):
    path = tmp_path / "fatigue.json"
    replace_deferred_fatigue_plan([ACTION], plan_revision="one", path=path)
    with pytest.raises(KeyboardInterrupt):
        notify_fatigue_event(
            "arrival",
            "B",
            path=path,
            schedule=lambda: (_ for _ in ()).throw(KeyboardInterrupt()),
        )
    calls = []
    assert recover_pending_fatigue_schedules(path=path, schedule=lambda: calls.append(True))
    assert calls == [True]
    # Scheduling is durable but is not an acknowledgement. The recovery task
    # must still claim and observe this checkpoint after restart.
    assert _state(path)["actions"][0]["schedule_status"] == "SCHEDULED"


def test_same_event_after_schedule_failure_can_retry(tmp_path):
    path = tmp_path / "fatigue.json"
    replace_deferred_fatigue_plan([ACTION], plan_revision="one", path=path)
    with pytest.raises(RuntimeError):
        notify_fatigue_event(
            "arrival", "B", path=path, schedule=lambda: (_ for _ in ()).throw(RuntimeError())
        )
    calls = []
    assert notify_fatigue_event("arrival", "B", path=path, schedule=lambda: calls.append(True))
    assert calls == [True]


def test_known_bento_unknown_soda_keeps_recovery_plan_unknown():
    plan = plan_fatigue_recovery(
        _snapshot(
            bento_batches_available=1,
            bento_total_reduction_available=50,
            soda_tier_confidence="UNKNOWN",
            soda_remaining_confidence="UNKNOWN",
        )
    )
    assert plan.status is FatiguePlanStatus.UNKNOWN
    assert plan.immediate_actions == ()


def test_known_bento_unknown_soda_keeps_plan_unknown():
    test_known_bento_unknown_soda_keeps_recovery_plan_unknown()


def test_unknown_rest_area_does_not_become_confident_no_soda():
    plan = plan_fatigue_recovery(
        _snapshot(
            current_amenities=frozenset(),
            amenity_confidence="UNKNOWN",
            soda_uses_remaining=0,
            soda_price_tiers=(),
        )
    )
    assert plan.status is FatiguePlanStatus.UNKNOWN


def test_action_executes_only_when_required_resource_evidence_is_high():
    high = plan_fatigue_recovery(_snapshot())
    unknown = plan_fatigue_recovery(_snapshot(soda_tier_confidence="UNKNOWN"))
    assert high.status is FatiguePlanStatus.ACTION_NOW
    assert high.immediate_actions[0].kind == "DRINK_SODA"
    assert unknown.status is FatiguePlanStatus.UNKNOWN
    assert unknown.immediate_actions == ()
