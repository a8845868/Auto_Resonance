from unittest.mock import MagicMock, patch

import auto.run_business.main as business
from core.model.city_goods import RouteModel, RoutesModel


def test_residual_cargo_cleanup_defers_without_selling_when_fatigue_is_exhausted():
    goods = ["岚心锦服"]

    with patch.object(business, "is_all_cargo_selected", return_value=False), patch.object(
        business, "has_sellable_cargo", return_value=True
    ), patch.object(business, "_prepare_max_sell_haggle", return_value=0), patch.object(
        business, "sell_existing_cargo", return_value=True
    ) as sell, patch.object(
        business, "read_strength", return_value=(791, 816)
    ):
        result = business._clear_residual_cargo(goods)

    assert result["success"] is True
    assert result["deferred"] is True
    assert result["reason"] == "insufficient_fatigue_for_residual_sale"
    assert result["available"] == 25
    sell.assert_not_called()


def test_run_propagates_residual_deferral_before_any_new_route_action():
    deferred = {
        "success": True,
        "deferred": True,
        "reason": "insufficient_fatigue_for_residual_sale",
    }
    routes = RoutesModel(
        city_data=[
            RouteModel(
                buy_city_name="武林源",
                sell_city_name="岚心城",
                goods_data={},
            ),
            RouteModel(
                buy_city_name="岚心城",
                sell_city_name="武林源",
                goods_data={},
            ),
        ]
    )
    image = MagicMock()
    image.ocr.return_value = []
    with patch.object(business, "connect", return_value=True), patch.object(
        business, "is_game_running", return_value=True
    ), patch.object(business, "is_sell_page", return_value=True), patch.object(
        business, "_read_route_city_from_current_screen", return_value="武林源"
    ), patch.object(business, "screenshot", return_value=image), patch.object(
        business, "is_train_in_transit", return_value=False
    ), patch.object(
        business, "_clear_residual_cargo", return_value=deferred
    ), patch.object(business, "click_station") as click_station:
        result = business.run(routes)

    assert result == deferred
    click_station.assert_not_called()


def test_weekly_run_propagates_deferral_without_recording_completion():
    deferred = {
        "success": True,
        "deferred": True,
        "reason": "insufficient_fatigue_for_residual_sale",
    }
    batches = [{"runs": 1, "books": {"岚心城": 0, "武林源": 0}}]
    with patch.object(
        business.app, "CityHaggle", {"岚心城": 2, "武林源": 2}
    ), patch.object(
        business, "city_sell_data", {"岚心城": {}, "武林源": {}}
    ), patch.object(
        business, "run_with_recovery", return_value=deferred
    ), patch.object(
        business, "is_stopped", return_value=False
    ), patch("core.services.record_completed_run") as record:
        result = business.two_city_weekly_run("岚心城", "武林源", batches)

    assert result == deferred
    record.assert_not_called()
