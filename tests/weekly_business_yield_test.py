from datetime import datetime, timedelta
from unittest.mock import patch

import auto.run_business.main as business
from app.view.two_city_run_business_interface import TwoRunBusinessInterface


def test_weekly_run_yields_after_one_complete_round_trip():
    batches = [{"runs": 3, "books": {"岚心城": 0, "武林源": 0}}]
    city_haggle = {"岚心城": 2, "武林源": 2}
    goods = {"岚心城": [], "武林源": []}

    with patch.object(business.app, "CityHaggle", city_haggle), patch.object(
        business, "city_sell_data", goods
    ), patch.object(business, "RouteModel", side_effect=lambda **kwargs: kwargs), patch.object(
        business, "RoutesModel", side_effect=lambda **kwargs: kwargs
    ), patch.object(business, "run_with_recovery", return_value=True) as run, patch.object(
        business, "is_stopped", return_value=False
    ), patch("core.services.record_completed_run") as record:
        result = business.two_city_weekly_run(
            "岚心城", "武林源", batches, max_runs=1
        )

    assert result is True
    assert run.call_count == 1
    record.assert_called_once_with({"岚心城": 0, "武林源": 0})


def test_unfinished_weekly_plan_is_scheduled_again_after_five_seconds():
    now = datetime(2026, 7, 13, 11, 30)
    state = {"cycle": ["岚心城", "武林源"]}

    with patch("core.services.load_weekly_plan", return_value=state), patch(
        "core.services.progress_summary", return_value={"finished": False}
    ):
        next_run = TwoRunBusinessInterface._nextBusinessRun(
            "岚心城", "武林源", now
        )

    assert next_run == now + timedelta(seconds=5)


def test_finished_weekly_plan_waits_until_next_daily_reset():
    now = datetime(2026, 7, 13, 11, 30)
    state = {"cycle": ["岚心城", "武林源"]}

    with patch("core.services.load_weekly_plan", return_value=state), patch(
        "core.services.progress_summary", return_value={"finished": True}
    ):
        next_run = TwoRunBusinessInterface._nextBusinessRun(
            "岚心城", "武林源", now
        )

    assert next_run == datetime(2026, 7, 14, 5, 0)
