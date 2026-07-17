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


def test_run_from_off_route_station_repositions_before_cleanup_and_route_actions():
    routes = RoutesModel(
        city_data=[
            RouteModel(
                buy_city_name="A",
                sell_city_name="B",
                goods_data={"good-a": {}},
            ),
            RouteModel(
                buy_city_name="B",
                sell_city_name="A",
                goods_data={"good-b": {}},
            ),
        ]
    )
    image = MagicMock()
    image.ocr.return_value = []
    travel = MagicMock()
    travel.wait.return_value = True
    events = []

    def navigate(name, cur_station=None):
        events.append(("navigate", name, cur_station))
        return travel

    def enter_business(mode):
        events.append(("business", mode))
        return True

    def clear_residual(goods):
        events.append(("clear", tuple(goods)))
        return True

    def buy(*args, **kwargs):
        events.append(("buy",))
        return True

    with patch.object(business, "connect", return_value=True), patch.object(
        business, "is_game_running", return_value=True
    ), patch.object(business, "is_sell_page", return_value=False), patch.object(
        business, "_normalize_trade_startup_screen", return_value=True
    ), patch.object(business, "get_station", return_value="C"), patch.object(
        business, "screenshot", return_value=image
    ), patch.object(
        business, "is_train_in_transit", return_value=False
    ), patch.object(
        business, "_clear_residual_cargo", side_effect=clear_residual
    ) as clear, patch.object(
        business, "go_business", side_effect=enter_business
    ) as go_business, patch.object(
        business, "click_station", side_effect=navigate
    ) as click_station, patch.object(
        business, "prepare_negotiation", return_value=2
    ), patch.object(
        business, "buy_business", side_effect=buy
    ), patch.object(
        business, "_prepare_max_sell_haggle", return_value=2
    ), patch.object(
        business, "sell_business", return_value=True
    ), patch.object(
        business, "read_strength", return_value=(100, 816)
    ):
        result = business.run(routes)

    assert result is True
    assert click_station.call_args_list[0].args == ("A",)
    assert click_station.call_args_list[0].kwargs == {"cur_station": "C"}
    clear.assert_called_once_with(["good-b"])
    assert go_business.call_args_list[0].args == ("sell",)
    assert click_station.call_args_list[1].args == ("B",)
    assert click_station.call_args_list[1].kwargs == {"cur_station": "A"}
    assert click_station.call_args_list[2].args == ("A",)
    assert click_station.call_args_list[2].kwargs == {"cur_station": "B"}
    assert [
        (call.args[0], call.kwargs["cur_station"])
        for call in click_station.call_args_list
    ] == [("A", "C"), ("B", "A"), ("A", "B")]
    assert events[:5] == [
        ("navigate", "A", "C"),
        ("business", "sell"),
        ("clear", ("good-b",)),
        ("business", "buy"),
        ("buy",),
    ]


def test_run_from_off_route_station_stops_when_repositioning_fails():
    routes = RoutesModel(
        city_data=[
            RouteModel(buy_city_name="A", sell_city_name="B"),
            RouteModel(buy_city_name="B", sell_city_name="A"),
        ]
    )
    image = MagicMock()
    image.ocr.return_value = []
    travel = MagicMock()
    travel.wait.return_value = False

    with patch.object(business, "connect", return_value=True), patch.object(
        business, "is_game_running", return_value=True
    ), patch.object(business, "is_sell_page", return_value=False), patch.object(
        business, "_normalize_trade_startup_screen", return_value=True
    ), patch.object(business, "get_station", return_value="C"), patch.object(
        business, "screenshot", return_value=image
    ), patch.object(
        business, "is_train_in_transit", return_value=False
    ), patch.object(
        business, "click_station", return_value=travel
    ), patch.object(
        business, "go_business"
    ) as go_business, patch.object(
        business, "_clear_residual_cargo"
    ) as clear:
        result = business.run(routes)

    assert result is False
    go_business.assert_not_called()
    clear.assert_not_called()


def test_run_stops_when_buy_step_explicitly_fails():
    routes = RoutesModel(
        city_data=[
            RouteModel(
                buy_city_name="A",
                sell_city_name="B",
                goods_data={"good-a": {}},
            ),
            RouteModel(
                buy_city_name="B",
                sell_city_name="A",
                goods_data={"good-b": {}},
            ),
        ]
    )
    image = MagicMock()
    image.ocr.return_value = []
    travel = MagicMock()
    travel.wait.return_value = True

    with patch.object(business, "connect", return_value=True), patch.object(
        business, "is_game_running", return_value=True
    ), patch.object(business, "is_sell_page", return_value=False), patch.object(
        business, "_normalize_trade_startup_screen", return_value=True
    ), patch.object(business, "get_station", return_value="A"), patch.object(
        business, "screenshot", return_value=image
    ), patch.object(
        business, "is_train_in_transit", return_value=False
    ), patch.object(
        business, "_clear_residual_cargo", return_value=True
    ), patch.object(
        business, "go_business", return_value=True
    ), patch.object(
        business, "click_station", return_value=travel
    ) as click_station, patch.object(
        business, "prepare_negotiation", return_value=2
    ), patch.object(
        business, "buy_business", return_value=False
    ), patch.object(
        business, "read_strength", return_value=(100, 816)
    ):
        result = business.run(routes)

    assert result is False
    click_station.assert_not_called()


def test_run_from_off_route_station_refuses_to_reposition_while_in_transit():
    routes = RoutesModel(
        city_data=[
            RouteModel(buy_city_name="A", sell_city_name="B"),
            RouteModel(buy_city_name="B", sell_city_name="A"),
        ]
    )
    image = MagicMock()
    image.ocr.return_value = [{"text": "自动巡航中"}]

    with patch.object(business, "connect", return_value=True), patch.object(
        business, "is_game_running", return_value=True
    ), patch.object(business, "is_sell_page", return_value=False), patch.object(
        business, "_normalize_trade_startup_screen", return_value=True
    ), patch.object(business, "get_station", return_value="C"), patch.object(
        business, "screenshot", return_value=image
    ), patch.object(
        business, "is_train_in_transit", return_value=True
    ), patch.object(
        business, "click_station"
    ) as click_station, patch.object(
        business, "go_business"
    ) as go_business:
        result = business.run(routes)

    assert result is False
    click_station.assert_not_called()
    go_business.assert_not_called()


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
        business, "_route_availability_deferral", return_value=None
    ), patch.object(
        business, "is_stopped", return_value=False
    ), patch("core.services.record_completed_run") as record:
        result = business.two_city_weekly_run("岚心城", "武林源", batches)

    assert result == deferred
    record.assert_not_called()
