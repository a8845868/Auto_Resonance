from datetime import datetime, timedelta
from unittest.mock import MagicMock, Mock, PropertyMock, patch

import numpy as np
import pytest

import auto.reward_collection as rewards
import auto.run_business.main as business
import core.services as services
from app.common.config import cfg
from app.utils.task_queue import QueuedTask
from app.view.dashboard_interface import _last_reward_snapshot
from core.services.daily_capabilities import DailyCapability, select_daily_capabilities
from core.services.daily_rewards import DailyProgressSnapshot, RewardStrategy
from core.services.fatigue_planner import FatigueSnapshot, SodaPriceTier, plan_fatigue_recovery
from core.services.fatigue_triggers import notify_fatigue_event, register_deferred_fatigue_actions
from core.services.server_calendar import GameServerClock
from core.services.trade_ledger import (
    TradeEvent, TradeEventType, append_trade_event, load_trade_cycle_state,
    stable_trade_event_id,
)
import core.services.trade_planning as trade_planning


CLOCK = GameServerClock()
NOW = datetime(2026, 7, 17, 12, 0, tzinfo=CLOCK.timezone)


def _box(x, y, text):
    return {"text": text, "position": ((x-20,y-10),(x+20,y-10),(x+20,y+10),(x-20,y+10))}


def _event(kind, minute=0):
    leg = "A|B"
    return TradeEvent(
        event_id=stable_trade_event_id("2026-07-13", "A|B", "cycle", leg, kind),
        server_week_id="2026-07-13", route_id="A|B", cycle_id="cycle", leg_id=leg,
        event_type=kind, origin="A", destination="B",
        observed_at=NOW + timedelta(minutes=minute), confirmed_by="GAME_OBSERVED",
    )


def _fatigue(fatigue=100, tiers=(), bentos=0, bento_reduction=0):
    return FatigueSnapshot(
        server_day_id="2026-07-17", observed_at=NOW, fatigue_used=fatigue,
        fatigue_cap=816, current_city_id="A", current_station_id="A",
        current_amenities=frozenset({"REST_AREA"}), soda_uses_used=0,
        soda_uses_remaining=len(tiers), soda_reduction_per_use=50,
        soda_price_tiers=tiers, bento_batches_available=bentos,
        bento_total_reduction_available=bento_reduction,
        next_bento_release_at=NOW + timedelta(hours=6),
        natural_recovery_at=NOW + timedelta(minutes=30), source_confidence="HIGH",
    )


def _progress(current=590, observed_at=NOW, server_day="2026-07-17"):
    return DailyProgressSnapshot(
        server_day_id=server_day, daily_activity_current=current,
        daily_activity_max=600, daily_activity_source="OCR",
        daily_activity_confidence="HIGH", daily_activity_claimable_tiers=0,
        daily_activity_unclaimed_tiers=1, handbook_daily_tasks_total=5,
        handbook_daily_tasks_completed=4, handbook_rewards_claimable=0,
        handbook_rewards_unclaimed=1, observed_at=observed_at,
    )


def _cap(key, activity, handbook=0, fatigue=0, seconds=0):
    task = QueuedTask(key, lambda: True, key=key)
    return DailyCapability(
        task_key=key, enabled=True, automation_available=True,
        activity_contribution_min=activity, activity_contribution_max=activity,
        handbook_contribution=handbook, completion_known=True,
        fatigue_cost=fatigue, time_cost_seconds=seconds, purchase_book_cost=0,
        premium_currency_risk=False, prerequisites=(), repeatable=True,
        next_safe_run_at=NOW, run_factory=lambda: task,
    )


def test_departure_event_is_not_confirmed_until_travel_state_observed(tmp_path):
    path = tmp_path / "ledger.json"
    context = {
        "ledger_path": path, "server_week_id": "2026-07-13",
        "route_id": "A|B", "cycle_id": "cycle",
    }
    requested_only = MagicMock()
    requested_only.__bool__.return_value = False
    with patch.object(
        business,
        "click_station",
        side_effect=lambda _destination, cur_station, on_departure_requested: (
            on_departure_requested() or requested_only
        ),
    ):
        business._begin_departure(context, origin="A", destination="B", leg_id="A|B")
    assert load_trade_cycle_state(path, "cycle").current_leg_phase == "DEPARTURE_REQUESTED"
    confirmed = MagicMock()
    confirmed.__bool__.return_value = True
    with patch.object(
        business,
        "click_station",
        side_effect=lambda _destination, cur_station, on_departure_requested: (
            on_departure_requested() or confirmed
        ),
    ):
        business._begin_departure(context, origin="A", destination="B", leg_id="A|B")
    assert load_trade_cycle_state(path, "cycle").current_leg_phase == "DEPARTURE_CONFIRMED"


def test_failed_departure_can_be_retried_from_origin(tmp_path):
    path = tmp_path / "ledger.json"
    append_trade_event(path, _event(TradeEventType.DEPARTURE_REQUESTED))
    context = {"ledger_path": path, "cycle_id": "cycle"}
    action = business.resume_action_for_leg(context, "A|B")
    assert action == "DEPART"
    assert business.should_issue_departure(action, "A", "B") is True


def test_stale_price_reoptimization_generic_failure_never_runs_old_route():
    state = {"cycle": ["A", "B"], "price_time": "2020-01-01T00:00:00+08:00", "optimizer_config": {}, "books_total": 0}
    summary = {"finished": False, "remaining_books": 0, "remaining_fatigue": 100, "remaining_runs": 1}
    with patch.object(type(cfg.InventoryBooks), "value", new_callable=PropertyMock, return_value=0), patch.object(
        services, "load_weekly_plan", return_value=state
    ), patch.object(services, "progress_summary", return_value=summary), patch.object(
        services, "optimize_live_routes", side_effect=RuntimeError("offline")
    ), patch.object(business, "unavailable_stations", return_value=[]), patch.object(
        business, "is_sell_page", return_value=False
    ), patch.object(business, "two_city_weekly_run") as execute, patch.object(
        business, "go_business", return_value=True
    ), patch.object(business, "read_strength", return_value=(0, 100)), patch.object(
        business, "get_station", return_value="A"
    ), patch("core.services.weekly_plan_state.save_current_resource_evidence"), patch(
        "core.services.weekly_plan_state.save_current_city_evidence"
    ):
        result = business.adaptive_weekly_run()
    assert result["deferred"] is True
    assert result["reason"] == "stale_price_reoptimization_failed"
    assert "next_run_at" in result
    execute.assert_not_called()


def test_deferred_fatigue_action_can_rearm_after_replan(tmp_path):
    path = tmp_path / "triggers.json"
    action = {"trigger_type": "WAYPOINT", "kind": "DRINK_SODA", "waypoint_id": "B"}
    register_deferred_fatigue_actions([action], plan_revision="plan-1", path=path)
    assert notify_fatigue_event("arrival", "B", path=path, schedule=Mock())
    register_deferred_fatigue_actions([action], plan_revision="plan-2", path=path)
    assert notify_fatigue_event("arrival", "B", path=path, schedule=Mock())


def test_fatigue_threshold_trigger_ignores_unrelated_arrival(tmp_path):
    path = tmp_path / "triggers.json"
    register_deferred_fatigue_actions(
        [{"trigger_type":"FATIGUE_THRESHOLD","kind":"REPLAN","fatigue_threshold":50}],
        plan_revision="p1", path=path,
    )
    assert not notify_fatigue_event("arrival", "B", fatigue_used=100, path=path, schedule=Mock())


def test_bento_release_trigger_does_not_fire_on_sale_event(tmp_path):
    path = tmp_path / "triggers.json"
    register_deferred_fatigue_actions(
        [{"trigger_type":"BENTO_RELEASE_AT","kind":"REPLAN","run_at":NOW.isoformat()}],
        plan_revision="p1", path=path,
    )
    assert not notify_fatigue_event("sale_confirmed", "B", now=NOW, path=path, schedule=Mock())


def test_soda_first_policy_wins_zero_waste_tie():
    plan = plan_fatigue_recovery(
        _fatigue(100, (SodaPriceTier(1,"FREE",0,True),), bentos=1, bento_reduction=50)
    )
    assert [item.kind for item in plan.immediate_actions] == ["DRINK_SODA", "USE_ALL_BENTOS"]


def test_iron_price_above_configured_limit_is_blocked():
    plan = plan_fatigue_recovery(
        _fatigue(100, (SodaPriceTier(1,"IRON",501,True),)), max_iron_soda_cost=500
    )
    assert not plan.immediate_actions


def test_590_of_600_selects_minimum_safe_capability_set():
    selected = select_daily_capabilities(
        [_cap("slow",100,fatigue=40,seconds=300), _cap("small",10,seconds=30)],
        _progress(), RewardStrategy.MAXIMIZE_PROGRESS,
    )
    assert [item.task_key for item in selected] == ["small"]


def test_dependency_progress_is_reobserved_before_next_capability():
    capabilities = [_cap("first",10), _cap("second",10)]
    first = select_daily_capabilities(capabilities, _progress(590), RewardStrategy.MAXIMIZE_PROGRESS)
    second = select_daily_capabilities(capabilities, _progress(600), RewardStrategy.MAXIMIZE_PROGRESS)
    assert len(first) == 1
    assert second == []


def test_previous_server_day_snapshot_is_ignored():
    payload = _progress(server_day="2026-07-16", observed_at=NOW-timedelta(hours=12))
    raw = {**payload.__dict__, "observed_at": payload.observed_at.isoformat()}
    with patch(
        "app.view.dashboard_interface.task_timing",
        return_value={"result": {"snapshot_after": raw}},
    ):
        assert _last_reward_snapshot() is None


def test_daily_completion_parser_does_not_turn_300_600_into_300600():
    assert rewards._daily_activity_value([_box(225,200,"300/600")]) == 300


def test_daily_task_claim_button_counts_as_unclaimed_reward():
    frame = Mock(image=np.zeros((720,1280,3), dtype=np.uint8))
    frame.ocr.return_value = [_box(450,60,"每日活跃"), _box(225,200,"590/600"), _box(500,610,"可领取")]
    driver = Mock()
    driver.frame.return_value = frame
    collector = rewards.RewardCollector(driver)
    collector._open_from_home = Mock(return_value=True)
    result = collector.observe_daily_activity()
    assert result["unclaimed_tiers"] >= 2


def test_handbook_parser_ignores_unrelated_ratio_with_task_context_present():
    assert rewards._manual_daily_progress([_box(200,150,"每日任务"), _box(900,500,"背包 3/20")]) is None


def test_reward_name_error_is_not_transient_success():
    with patch("auto.reward_collection.RewardCollector.run", side_effect=NameError("bug")):
        with pytest.raises(NameError):
            rewards.collect_scheduled_rewards()


def test_fatigue_shortage_uses_threshold_next_run_not_five_seconds():
    with patch.object(business, "read_strength", return_value=(800,816)):
        result = business._fatigue_deferral("insufficient_fatigue_for_route", 50)
    assert datetime.fromisoformat(result["next_run_at"]) >= CLOCK.server_now() + timedelta(minutes=5)


def test_station_unavailable_does_not_poll_every_five_seconds():
    with patch.object(business, "unavailable_stations", return_value=["B"]):
        result = business._route_availability_deferral("A", "B")
    assert datetime.fromisoformat(result["next_run_at"]) >= CLOCK.server_now() + timedelta(minutes=15)


def test_departure_wait_polling_is_bounded():
    result = business._departure_wait_deferral("cycle")
    next_run = datetime.fromisoformat(result["next_run_at"])
    assert CLOCK.server_now()+timedelta(seconds=10) <= next_run <= CLOCK.server_now()+timedelta(minutes=2)


def test_price_invalid_fallback_requires_fresh_validation():
    stale = {"cycle":["A","B"], "price_time":"2020-01-01T00:00:00+08:00", "expected_profit":10, "cycle_fatigue":1}
    with pytest.raises(services.StalePriceSnapshot):
        trade_planning.validate_executable_trade_budget(
            stale, now=NOW, fatigue_budget=10, purchase_books=0
        )


def test_production_optimizer_role_matches_its_name_and_inputs():
    validator = trade_planning.validate_executable_trade_budget
    assert validator is not trade_planning.choose_trade_plan
    assert "validate" in validator.__name__
    assert "candidates" in trade_planning.choose_trade_plan.__annotations__
