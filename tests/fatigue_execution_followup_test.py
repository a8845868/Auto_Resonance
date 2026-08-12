from datetime import datetime
from unittest.mock import Mock, patch

from auto.fatigue_recovery import run_daily_fatigue_recovery
from auto.module.strength import _lunchbox_recovery_values, _lunchbox_total_recovery
from core.services.fatigue_planner import (
    FatigueSnapshot,
    SodaPriceTier,
    plan_fatigue_recovery,
)
from core.services.fatigue_triggers import (
    notify_fatigue_event,
    register_deferred_fatigue_actions,
)
from core.services.server_calendar import GameServerClock


CLOCK = GameServerClock()


def _snapshot(fatigue=44, tiers=()):
    return FatigueSnapshot(
        server_day_id="2026-07-17",
        observed_at=datetime(2026, 7, 17, 12, 0, tzinfo=CLOCK.timezone),
        fatigue_used=fatigue,
        fatigue_cap=816,
        current_city_id="岚心城",
        current_station_id="岚心城",
        current_amenities=frozenset({"REST_AREA"}),
        soda_uses_used=0,
        soda_uses_remaining=len(tiers),
        soda_reduction_per_use=50,
        soda_price_tiers=tiers,
        bento_batches_available=0,
        bento_total_reduction_available=0,
        next_bento_release_at=None,
        natural_recovery_at=None,
        source_confidence="HIGH",
    )


def test_44_fatigue_first_observes_bento_inventory():
    observation = {
        "lunches_remaining": 2,
        "lunch_total_recovery": 48,
        "soda_price_tiers": (),
    }
    with patch("auto.fatigue_recovery.connect", return_value=True), patch(
        "auto.fatigue_recovery.get_station", return_value="岚心城"
    ), patch("auto.fatigue_recovery._open_exchange_buy_page", return_value=True), patch(
        "auto.fatigue_recovery._wait_strength", return_value=(44, 816)
    ), patch(
        "auto.fatigue_recovery.observe_recovery_resources", return_value=observation
    ) as observe, patch("auto.fatigue_recovery.go_home", return_value=True), patch(
        "auto.fatigue_recovery.register_deferred_fatigue_actions"
    ):
        result = run_daily_fatigue_recovery()
    observe.assert_called_once_with("岚心城")
    assert result["plan"]["snapshot"]["bento_batches_available"] == 2


def test_actual_bento_total_is_read_from_confirmation():
    items = [{"text": "使用全部便当，以消除 73 疲劳值"}]
    assert _lunchbox_total_recovery(items) == 73


def test_each_bento_recovery_value_is_read_from_cards():
    items = [
        {"text": "恢复 24 疲劳值"},
        {"text": "恢复 31 疲劳"},
    ]
    assert _lunchbox_recovery_values(items) == (24, 31)


def test_mixed_free_and_premium_soda_stops_before_premium():
    plan = plan_fatigue_recovery(
        _snapshot(
            fatigue=120,
            tiers=(
                SodaPriceTier(1, "FREE", 0, True),
                SodaPriceTier(2, "SILVER", 1, False),
            ),
        ),
        allow_premium_soda=False,
    )
    assert plan.immediate_actions[0].kind == "DRINK_SODA"
    assert plan.immediate_actions[0].count == 1


def test_deferred_waypoint_action_is_executed_once_on_arrival(tmp_path):
    path = tmp_path / "fatigue-waypoints.json"
    register_deferred_fatigue_actions(
        [{"kind": "DRINK_SODA", "waypoint_id": "武林源"}],
        path=path,
    )
    schedule = Mock()
    assert notify_fatigue_event("arrival", "武林源", path=path, schedule=schedule) is True
    assert notify_fatigue_event("arrival", "武林源", path=path, schedule=schedule) is False
    schedule.assert_called_once()
