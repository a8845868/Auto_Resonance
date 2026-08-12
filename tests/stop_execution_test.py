from unittest import TestCase
from unittest.mock import patch

import auto.run_business.main as business
import auto.inventory as inventory
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


def test_stop_execution_construction_does_not_log_error():
    with patch("core.exception.exceptions.logger") as logger:
        error = StopExecution()

    assert str(error) == "停止执行程序"
    logger.error.assert_not_called()


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
    ) as weekly_run, patch.object(business, "go_business", return_value=True), patch.object(
        business, "read_strength", return_value=(0, 100)
    ), patch.object(business, "get_station", return_value="A"), patch(
        "core.services.weekly_plan_state.save_current_resource_evidence"
    ), patch("core.services.weekly_plan_state.save_current_city_evidence"):
        with TestCase().assertRaises(StopExecution):
            business.adaptive_weekly_run()
    weekly_run.assert_not_called()


def test_inventory_book_read_does_not_swallow_stop_request():
    with patch.object(inventory, "connect", return_value=True), patch.object(
        inventory, "go_home", side_effect=StopExecution()
    ) as go_home, patch.object(inventory, "screenshot") as screenshot:
        screenshot.return_value.ocr.return_value = []
        with TestCase().assertRaises(StopExecution):
            inventory.read_restock_book_count()

    go_home.assert_called_once()
