from datetime import datetime
from unittest.mock import patch

import auto.fatigue_recovery as fatigue_recovery
from core.services.fatigue_planner import (
    fatigue_cycle,
    fatigue_plan_lines,
    load_fatigue_usage,
    next_fatigue_refresh,
    record_fatigue_usage,
)


EMPTY_USAGE = {
    "cycle": "2026-07-14",
    "bubble_water_uses": 0,
    "lunch_batches": 0,
    "lunch_fatigue_restored": 0,
    "lunches_remaining": None,
    "lunch_schedule": {
        "05:00": "released",
        "12:00": "released",
        "18:00": "pending",
    },
}


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


def test_daily_usage_accumulates_and_resets_at_five_am(tmp_path):
    path = tmp_path / "fatigue-usage.json"
    before_reset = datetime(2026, 7, 14, 4, 59)
    usage = record_fatigue_usage(
        bubble_water_uses=2,
        lunch_batches=1,
        lunch_fatigue_restored=191,
        now=before_reset,
        path=path,
    )
    usage = record_fatigue_usage(
        bubble_water_uses=3,
        now=before_reset,
        path=path,
    )

    assert usage == {
        "cycle": "2026-07-13",
        "bubble_water_uses": 5,
        "lunch_batches": 1,
        "lunch_fatigue_restored": 191,
        "lunches_remaining": None,
        "lunch_schedule": {
            "05:00": "released",
            "12:00": "released",
            "18:00": "released",
        },
    }
    assert "气泡水 5/6 次" in fatigue_plan_lines(usage=usage)[0]
    assert load_fatigue_usage(datetime(2026, 7, 14, 5, 0), path) == {
        "cycle": "2026-07-14",
        "bubble_water_uses": 0,
        "lunch_batches": 0,
        "lunch_fatigue_restored": 0,
        "lunches_remaining": None,
        "lunch_schedule": {
            "05:00": "released",
            "12:00": "pending",
            "18:00": "pending",
        },
    }


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
    ) as go_home, patch.object(
        fatigue_recovery, "rest_area_availability", return_value=True
    ), patch.object(
        fatigue_recovery, "record_fatigue_usage", return_value=EMPTY_USAGE
    ):
        result = fatigue_recovery.run_daily_fatigue_recovery()

    recover.assert_called_once_with(
        "buy", min_available=80, station_name="岚心城", usage={}
    )
    assert result["success"] is True
    assert result["before"] == 612
    assert result["after"] == 12
    assert result["restored"] == 600
    assert "deferred" not in result


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
    ) as go_home, patch.object(
        fatigue_recovery, "rest_area_availability", return_value=False
    ), patch.object(
        fatigue_recovery, "record_fatigue_usage", return_value=EMPTY_USAGE
    ):
        result = fatigue_recovery.run_daily_fatigue_recovery()

    assert result["success"] is True
    assert result["deferred"] is True
    assert result["reason"] == "lunch_only_waiting_for_rest_area"
    assert result["restored"] == 191
    recover.assert_called_once_with(
        "buy", min_available=80, station_name="武林源", usage={}
    )
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
    ) as go_home, patch.object(
        fatigue_recovery, "rest_area_availability", return_value=False
    ), patch.object(
        fatigue_recovery, "record_fatigue_usage", return_value=EMPTY_USAGE
    ):
        result = fatigue_recovery.run_daily_fatigue_recovery()

    assert result["success"] is True
    assert "deferred" not in result
    assert result["restored"] == 0
    recover.assert_called_once_with(
        "buy", min_available=80, station_name="武林源", usage={}
    )
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
    ) as go_home, patch.object(
        fatigue_recovery, "record_fatigue_usage", return_value=EMPTY_USAGE
    ):
        result = fatigue_recovery.run_daily_fatigue_recovery()

    assert result["success"] is True
    assert result["deferred"] is True
    assert result["reason"] == "recovery_conditions_not_met"
    assert result["station"] == "test-station"
    assert result["before"] == 12
    assert result["maximum"] == 816
    assert result["usage"] == EMPTY_USAGE
    go_home.assert_called_once()
