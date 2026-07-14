from datetime import datetime
from unittest.mock import patch

import auto.fatigue_recovery as fatigue_recovery
from core.services.fatigue_planner import fatigue_cycle, next_fatigue_refresh


def test_fatigue_resources_follow_the_five_am_game_day():
    assert fatigue_cycle(datetime(2026, 7, 13, 4, 59)) == "2026-07-12"
    assert fatigue_cycle(datetime(2026, 7, 13, 5, 0)) == "2026-07-13"
    assert next_fatigue_refresh(datetime(2026, 7, 13, 4, 59)) == datetime(
        2026, 7, 13, 5, 0
    )
    assert next_fatigue_refresh(datetime(2026, 7, 13, 5, 0)) == datetime(
        2026, 7, 13, 12, 0
    )
    assert next_fatigue_refresh(datetime(2026, 7, 13, 12, 0)) == datetime(
        2026, 7, 13, 18, 0
    )
    assert next_fatigue_refresh(datetime(2026, 7, 13, 18, 0)) == datetime(
        2026, 7, 14, 5, 0
    )


def test_daily_fatigue_task_is_independent_and_returns_explicit_result():
    with patch.object(fatigue_recovery, "connect", return_value=True), patch.object(
        fatigue_recovery, "get_station", return_value="岚心城"
    ), patch.object(
        fatigue_recovery, "_open_exchange_buy_page", return_value=True
    ), patch.object(
        fatigue_recovery, "_wait_strength", side_effect=[(612, 816), (12, 816)]
    ), patch.object(
        fatigue_recovery, "recover_strength", return_value=True
    ) as recover, patch.object(
        fatigue_recovery, "go_home", return_value=True
    ) as go_home:
        result = fatigue_recovery.run_daily_fatigue_recovery()

    recover.assert_called_once_with("buy", min_available=80, station_name="岚心城")
    assert result["success"] is True
    assert result["before"] == 612
    assert result["after"] == 12
    assert result["restored"] == 600


def test_daily_fatigue_task_uses_safe_lunches_at_station_without_rest_area():
    with patch.object(fatigue_recovery, "connect", return_value=True), patch.object(
        fatigue_recovery, "get_station", return_value="武林源"
    ), patch.object(
        fatigue_recovery, "_open_exchange_buy_page", return_value=True
    ), patch.object(
        fatigue_recovery, "_wait_strength", side_effect=[(791, 816), (600, 816)]
    ), patch.object(
        fatigue_recovery, "recover_strength", return_value=True
    ) as recover, patch.object(
        fatigue_recovery, "go_home", return_value=True
    ) as go_home:
        result = fatigue_recovery.run_daily_fatigue_recovery()

    assert result["success"] is True
    assert result["success"] is True
    assert result["restored"] == 191
    recover.assert_called_once_with("buy", min_available=80, station_name="武林源")
    go_home.assert_called_once()


def test_daily_fatigue_task_can_complete_at_no_rest_station_without_lunch_waste():
    with patch.object(fatigue_recovery, "connect", return_value=True), patch.object(
        fatigue_recovery, "get_station", return_value="武林源"
    ), patch.object(
        fatigue_recovery, "_open_exchange_buy_page", return_value=True
    ), patch.object(
        fatigue_recovery, "_wait_strength", return_value=(12, 816)
    ), patch.object(
        fatigue_recovery, "recover_strength", return_value=True
    ) as recover, patch.object(
        fatigue_recovery, "go_home", return_value=True
    ) as go_home:
        result = fatigue_recovery.run_daily_fatigue_recovery()

    assert result["success"] is True
    assert result["success"] is True
    assert result["restored"] == 0
    recover.assert_called_once_with("buy", min_available=80, station_name="武林源")
    go_home.assert_called_once()


def test_daily_fatigue_task_defers_with_explicit_success_when_recovery_is_unneeded():
    with patch.object(fatigue_recovery, "connect", return_value=True), patch.object(
        fatigue_recovery, "get_station", return_value="test-station"
    ), patch.object(
        fatigue_recovery, "_open_exchange_buy_page", return_value=True
    ), patch.object(
        fatigue_recovery, "_wait_strength", return_value=(12, 816)
    ), patch.object(
        fatigue_recovery, "recover_strength", return_value=False
    ), patch.object(
        fatigue_recovery, "go_home", return_value=True
    ) as go_home:
        result = fatigue_recovery.run_daily_fatigue_recovery()

    assert result["success"] is True
    assert result["deferred"] is True
    assert result["reason"] == "recovery_conditions_not_met"
    assert result["station"] == "test-station"
    assert result["before"] == 12
    assert result["maximum"] == 816
    go_home.assert_called_once()
