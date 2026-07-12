from unittest import TestCase
from unittest.mock import patch

import auto.run_business.main as business
import core.control.control as control
from core.exception.exceptions import StopExecution


class FakeConnection:
    def connect(self, _port=None):
        return True


def test_connect_does_not_clear_existing_stop_request():
    control.stop()
    with patch.object(control, "ADB", return_value=FakeConnection()), patch.object(
        control, "NEMU", return_value=FakeConnection()
    ):
        control.connect()
    assert control.is_stopped()
    control.reset_stop()


def test_adaptive_plan_does_not_treat_stop_as_optimizer_failure():
    state = {"cycle": ["岚心城", "武林源"], "optimizer_config": {}}
    summary = {
        "finished": False,
        "remaining_books": 10,
        "remaining_fatigue": 100,
        "remaining_runs": 2,
    }
    with patch("core.services.load_weekly_plan", return_value=state), patch(
        "core.services.progress_summary", return_value=summary
    ), patch("core.services.OptimizationConfig", side_effect=lambda **_: object()), patch(
        "core.services.optimize_live_routes", side_effect=StopExecution()
    ), patch("auto.inventory.read_restock_book_count", return_value=0), patch.object(
        business, "two_city_weekly_run"
    ) as weekly_run:
        with TestCase().assertRaises(StopExecution):
            business.adaptive_weekly_run()
    weekly_run.assert_not_called()
