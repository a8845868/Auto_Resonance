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


def _observation(*, lunches, total, tiers=(), rest_area=False):
    return {
        "lunches_remaining": lunches,
        "lunch_total_recovery": total,
        "soda_price_tiers": tiers,
        "rest_area_available": rest_area,
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
        fatigue_recovery,
        "_wait_strength",
        side_effect=[(612, 816), (662, 816), (662, 816)],
    ), patch.object(
        fatigue_recovery,
        "observe_recovery_resources",
        side_effect=[
            _observation(lunches=0, total=None, tiers=("FREE",), rest_area=True),
            _observation(lunches=0, total=None, rest_area=True),
        ],
    ), patch.object(
        fatigue_recovery,
        "execute_planned_recovery_action",
        return_value={"success": True, "bubble_water_uses": 1},
    ) as execute, patch.object(
        fatigue_recovery, "go_home", return_value=True
    ), patch.object(
        fatigue_recovery, "record_fatigue_usage", return_value=EMPTY_USAGE
    ), patch.object(
        fatigue_recovery, "register_deferred_fatigue_actions"
    ), patch.object(
        fatigue_recovery, "_route_context", return_value=None
    ):
        result = fatigue_recovery.run_daily_fatigue_recovery()

    execute.assert_called_once()
    assert result["success"] is True
    assert result["before"] == 612
    assert result["after"] == 662
    assert result["restored"] == 50
    assert result["progress_made"] is True
    assert result["deferred"] is True


def test_daily_fatigue_task_uses_safe_lunches_at_station_without_rest_area():
    with patch.object(fatigue_recovery, "connect", return_value=True), patch.object(
        fatigue_recovery, "get_station", return_value="武林源"
    ), patch.object(
        fatigue_recovery, "_open_exchange_buy_page", return_value=True
    ), patch.object(
        fatigue_recovery,
        "_wait_strength",
        side_effect=[(600, 816), (791, 816), (791, 816)],
    ), patch.object(
        fatigue_recovery,
        "observe_recovery_resources",
        side_effect=[
            _observation(lunches=2, total=191),
            _observation(lunches=0, total=None),
        ],
    ), patch.object(
        fatigue_recovery,
        "execute_planned_recovery_action",
        return_value={
            "success": True,
            "lunch_batches": 1,
            "lunch_fatigue_restored": 191,
            "lunches_remaining": 0,
        },
    ) as execute, patch.object(
        fatigue_recovery, "go_home", return_value=True
    ) as go_home, patch.object(
        fatigue_recovery, "record_fatigue_usage", return_value=EMPTY_USAGE
    ), patch.object(
        fatigue_recovery, "register_deferred_fatigue_actions"
    ), patch.object(
        fatigue_recovery, "_route_context", return_value=None
    ):
        result = fatigue_recovery.run_daily_fatigue_recovery()

    assert result["success"] is True
    assert result["deferred"] is True
    assert result["restored"] == 191
    execute.assert_called_once()
    go_home.assert_called_once()


def test_daily_fatigue_task_keeps_near_cap_plan_pending_at_no_rest_station():
    with patch.object(fatigue_recovery, "connect", return_value=True), patch.object(
        fatigue_recovery, "get_station", return_value="武林源"
    ), patch.object(
        fatigue_recovery, "_open_exchange_buy_page", return_value=True
    ), patch.object(
        fatigue_recovery, "_wait_strength", return_value=(800, 816)
    ), patch.object(
        fatigue_recovery,
        "observe_recovery_resources",
        return_value=_observation(lunches=1, total=24),
    ), patch.object(
        fatigue_recovery, "execute_planned_recovery_action"
    ) as execute, patch.object(
        fatigue_recovery, "go_home", return_value=True
    ) as go_home, patch.object(
        fatigue_recovery, "record_fatigue_usage", return_value=EMPTY_USAGE
    ), patch.object(
        fatigue_recovery, "register_deferred_fatigue_actions"
    ), patch.object(
        fatigue_recovery, "_route_context", return_value=None
    ):
        result = fatigue_recovery.run_daily_fatigue_recovery()

    assert result["success"] is True
    assert result["deferred"] is True
    assert result["status"] == "DEFER_UNTIL_FATIGUE"
    execute.assert_not_called()
    go_home.assert_called_once()


def test_daily_fatigue_task_defers_with_explicit_success_without_headroom():
    with patch.object(fatigue_recovery, "connect", return_value=True), patch.object(
        fatigue_recovery, "get_station", return_value="test-station"
    ), patch.object(
        fatigue_recovery, "_open_exchange_buy_page", return_value=True
    ), patch.object(
        fatigue_recovery, "_wait_strength", return_value=(800, 816)
    ), patch.object(
        fatigue_recovery,
        "observe_recovery_resources",
        return_value=_observation(lunches=1, total=24),
    ), patch.object(
        fatigue_recovery, "execute_planned_recovery_action"
    ) as execute, patch.object(
        fatigue_recovery, "go_home", return_value=True
    ) as go_home, patch.object(
        fatigue_recovery, "record_fatigue_usage", return_value=EMPTY_USAGE
    ), patch.object(
        fatigue_recovery, "register_deferred_fatigue_actions"
    ), patch.object(
        fatigue_recovery, "_route_context", return_value=None
    ):
        result = fatigue_recovery.run_daily_fatigue_recovery()

    assert result["success"] is True
    assert result["deferred"] is True
    assert result["reason"].startswith("wait_for_zero_waste_threshold")
    assert result["station"] == "test-station"
    assert result["before"] == 800
    assert result["maximum"] == 816
    execute.assert_not_called()
    go_home.assert_called_once()
