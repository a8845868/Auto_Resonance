from unittest.mock import PropertyMock, patch

import auto.run_business.main as business
import core.services as services
from app.common.config import cfg


STALE_STATE = {
    "cycle": ["岚心城", "武林源"],
    "optimizer_config": {},
}
SUMMARY = {
    "finished": False,
    "remaining_books": 0,
    "remaining_fatigue": 100,
    "remaining_runs": 1,
}


def test_direct_two_city_route_defers_before_reading_closed_station_config():
    result = business.two_city_run("岚心城", "武林源")

    assert result == {
        "success": True,
        "deferred": True,
        "reason": "route_station_unavailable",
        "stations": ["武林源"],
    }


def test_adaptive_weekly_run_replaces_stale_closed_station_plan():
    replacement = {
        "cycle": ["A", "B"],
        "books_used": 0,
        "price_time": business.SERVER_CLOCK.server_now().isoformat(),
    }
    saved = dict(replacement)

    with patch.object(
        type(cfg.InventoryBooks), "value", new_callable=PropertyMock, return_value=0
    ), patch.object(services, "load_weekly_plan", return_value=STALE_STATE), patch.object(
        services, "progress_summary", return_value=SUMMARY
    ), patch.object(
        services, "optimize_live_routes", return_value=replacement
    ) as optimize, patch.object(
        services, "save_weekly_plan", return_value=saved
    ) as save, patch.object(
        services, "remaining_batches", return_value=[{"runs": 1, "books": {}}]
    ), patch.object(
        business, "unavailable_stations", return_value=["武林源"]
    ), patch.object(
        business, "is_sell_page", return_value=False
    ), patch.object(
        business, "two_city_weekly_run", return_value=True
    ) as execute:
        result = business.adaptive_weekly_run()

    assert result is True
    optimize.assert_called_once()
    save.assert_called_once_with(replacement)
    execute.assert_called_once_with("A", "B", [{"runs": 1, "books": {}}], max_runs=1)


def test_adaptive_weekly_run_never_falls_back_to_closed_route():
    with patch.object(
        type(cfg.InventoryBooks), "value", new_callable=PropertyMock, return_value=0
    ), patch.object(services, "load_weekly_plan", return_value=STALE_STATE), patch.object(
        services, "progress_summary", return_value=SUMMARY
    ), patch.object(
        services, "optimize_live_routes", side_effect=RuntimeError("offline")
    ), patch.object(
        business, "unavailable_stations", return_value=["武林源"]
    ), patch.object(
        business, "is_sell_page", return_value=False
    ), patch.object(
        business, "two_city_weekly_run"
    ) as execute:
        result = business.adaptive_weekly_run()

    assert result["success"] is True
    assert result["deferred"] is True
    assert result["reason"] == "route_station_unavailable"
    execute.assert_not_called()
