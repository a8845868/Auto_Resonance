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

    recover.assert_called_once_with("buy", min_available=0, station_name="岚心城")
    assert result["success"] is True
    assert result["before"] == 612
    assert result["after"] == 12
    assert result["restored"] == 600


def test_daily_fatigue_task_defers_at_station_without_rest_area():
    with patch.object(fatigue_recovery, "connect", return_value=True), patch.object(
        fatigue_recovery, "get_station", return_value="武林源"
    ), patch.object(
        fatigue_recovery, "_open_exchange_buy_page"
    ) as open_exchange, patch.object(
        fatigue_recovery, "_wait_strength"
    ) as wait_strength, patch.object(
        fatigue_recovery, "recover_strength"
    ) as recover, patch.object(
        fatigue_recovery, "go_home", return_value=True
    ) as go_home:
        result = fatigue_recovery.run_daily_fatigue_recovery()

    assert result["success"] is True
    assert result["deferred"] is True
    assert result["reason"] == "station_without_rest_area"
    open_exchange.assert_not_called()
    wait_strength.assert_not_called()
    recover.assert_not_called()
    go_home.assert_not_called()


def test_daily_fatigue_task_does_not_open_exchange_at_low_fatigue_without_rest_area():
    with patch.object(fatigue_recovery, "connect", return_value=True), patch.object(
        fatigue_recovery, "get_station", return_value="武林源"
    ), patch.object(
        fatigue_recovery, "_open_exchange_buy_page"
    ) as open_exchange, patch.object(
        fatigue_recovery, "_wait_strength", return_value=(12, 816)
    ), patch.object(
        fatigue_recovery, "recover_strength"
    ) as recover, patch.object(
        fatigue_recovery, "go_home", return_value=True
    ) as go_home:
        result = fatigue_recovery.run_daily_fatigue_recovery()

    assert result["success"] is True
    assert result["deferred"] is True
    assert result["reason"] == "station_without_rest_area"
    open_exchange.assert_not_called()
    recover.assert_not_called()
    go_home.assert_not_called()


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
