from unittest.mock import patch

import auto.run_business.main as business
import core.preset.control as preset_control
from core.module.bgr import BGR
from core.services.screen_state import (
    CLARITY_REPLENISH_CANCEL_TAP,
    clarity_replenish_cancel_position,
    is_inventory_item_detail,
    is_inventory_screen,
    is_top_level_hud,
    is_train_in_transit,
    startup_screen_action,
)


def _items(*texts):
    return [{"text": text} for text in texts]


class FakeHomeImage:
    def get_bgr(self, _pos):
        return BGR(0, 0, 0, offset=0)

    def ocr(self):
        return _items("目的地：岚心城", "自动巡航中", "车厢内")


class FakeStartupImage:
    def __init__(self, texts):
        self.texts = texts

    def ocr(self):
        return _items(*self.texts)

    def match_template(self, *_args):
        return False

    def get_bgr(self, _pos):
        return BGR(0, 0, 0, offset=0)


class FakeStationHomeImage(FakeStartupImage):
    def get_bgr(self, _pos):
        return BGR(0, 150, 220, offset=0)


class FakeExchangeLobby:
    def ocr(self):
        return _items("我要买", "我要卖", "交易品投资", "私人仓库")


class FakeSellTradePage:
    def ocr(self):
        return []

    def get_bgr(self, pos):
        assert pos == (1175, 460)
        return BGR(0, 183, 253, offset=0)


def test_screen_state_separates_transit_from_station_home():
    assert is_train_in_transit(_items("剩余行程：435km", "自动巡航中"))
    assert not is_train_in_transit(_items("资产", "车厢内", "副官室"))
    assert is_top_level_hud(_items("资产", "车厢内", "副官室"))


def test_startup_screen_actions_keep_go_home_away_from_resource_repair():
    assert startup_screen_action(_items("修复资源完整性会自动退出游戏，是否继续？")) == "cancel_resource_repair"
    assert startup_screen_action(
        _items("需要下载资源包（共20.9MB）", "确认", "0%")
    ) == "confirm_resource_download"
    assert startup_screen_action(_items("正在下载资源包", "42%")) == "wait_for_game"
    assert startup_screen_action(_items("点击屏幕进入游戏")) == "enter_game"
    assert startup_screen_action(_items("81%")) == "wait_for_game"
    assert startup_screen_action(_items("触碰空白区域退出")) == "dismiss_startup_overlay"


def test_clarity_replenish_cancel_requires_prompt_and_prefers_ocr_position():
    prompt = [
        {
            "text": "您当前的澄明度不足，是否补充澄明度？",
            "position": ((480, 348), (888, 348), (888, 375), (480, 375)),
        },
        {
            "text": "取消",
            "position": ((308, 497), (358, 497), (358, 527), (308, 527)),
        },
        {"text": "确认"},
    ]

    assert clarity_replenish_cancel_position(prompt) == (333, 512)
    assert clarity_replenish_cancel_position(_items("普通页面", "取消")) is None


def test_clarity_replenish_cancel_uses_guarded_normalized_fallback():
    assert clarity_replenish_cancel_position(
        _items("您当前的澄明度不足，是否补充澄明度？", "确认")
    ) == CLARITY_REPLENISH_CANCEL_TAP


def test_core_go_home_cancels_clarity_prompt_without_touching_confirm():
    clarity_prompt = FakeStartupImage(
        ["您当前的澄明度不足，是否补充澄明度？", "确认"]
    )
    station_home = FakeStationHomeImage(["资产", "车厢内", "副官室"])
    with patch.object(
        preset_control, "screenshot", side_effect=[clarity_prompt, station_home]
    ), patch.object(preset_control, "click_image") as click_image, patch.object(
        preset_control, "input_tap"
    ) as tap, patch.object(preset_control.time, "sleep"):
        assert preset_control.go_home()

    assert [call.args[0] for call in tap.call_args_list] == [
        CLARITY_REPLENISH_CANCEL_TAP
    ]
    click_image.assert_not_called()


def test_go_home_confirms_prelogin_resource_pack_then_waits_for_login():
    resource_prompt = FakeStartupImage(
        ["需要下载资源包（共20.9MB）", "确认", "0%"]
    )
    download_progress = FakeStartupImage(["42%"])
    login = FakeStartupImage(["点击屏幕进入游戏"])
    station_home = FakeStationHomeImage(["资产", "车厢内", "副官室"])
    with patch.object(
        preset_control,
        "screenshot",
        # More than the ordinary 45-attempt navigation budget proves that the
        # resource prompt grants its dedicated download wait window.
        side_effect=[resource_prompt, *([download_progress] * 50), login, station_home],
    ), patch.object(preset_control, "click_image") as click_image, patch.object(
        preset_control, "input_tap"
    ) as tap, patch.object(preset_control.time, "sleep"):
        assert preset_control.go_home()

    assert [call.args[0] for call in tap.call_args_list] == [
        (640, 506),
        (640, 560),
    ]
    click_image.assert_not_called()


def test_inventory_detail_is_not_mistaken_for_startup_overlay():
    detail = _items("SR", "进货采买书", "拥有：8", "获取途径", "触碰空白区域退出")
    backpack = _items("道具", "材料", "装备", "载货", "冰箱", "私人仓库")

    assert is_inventory_item_detail(detail)
    assert startup_screen_action(detail) is None
    assert is_inventory_screen(backpack)


def test_go_home_closes_inventory_detail_then_backpack_without_startup_wait():
    detail = FakeStartupImage(
        ["SR", "进货采买书", "拥有：8", "获取途径", "触碰空白区域退出"]
    )
    backpack = FakeStartupImage(["道具", "材料", "装备", "载货", "冰箱", "私人仓库"])
    station_home = FakeStationHomeImage(["资产", "车厢内", "副官室"])
    with patch.object(
        preset_control, "screenshot", side_effect=[detail, backpack, station_home]
    ), patch.object(preset_control, "click_image") as click_image, patch.object(
        preset_control, "input_tap"
    ) as tap, patch.object(preset_control.time, "sleep"):
        assert preset_control.go_home()

    assert [call.args[0] for call in tap.call_args_list] == [(100, 650), (78, 38)]
    click_image.assert_not_called()


def test_trade_price_percentages_are_not_mistaken_for_startup_loading():
    assert startup_screen_action(
        _items("交易品", "全部买入", "112%", "105%", "我要买")
    ) is None


def test_go_home_stops_on_travel_hud_without_clicking_back_to_map():
    with patch.object(preset_control, "screenshot", return_value=FakeHomeImage()), patch.object(
        preset_control, "click_image"
    ) as click_image, patch.object(preset_control, "input_tap") as tap:
        assert preset_control.go_home()

    click_image.assert_not_called()
    tap.assert_not_called()


def test_startup_resumes_existing_travel_before_station_operations():
    travel = FakeStartupImage(["目的地：岚心城", "自动巡航中"])
    station_home = FakeStartupImage(["资产", "车厢内", "副官室"])
    station = type("Station", (), {"wait": lambda self: True})
    with patch.object(business, "screenshot", side_effect=[travel, station_home]), patch.object(
        business, "STATION", return_value=station()
    ), patch.object(business, "input_tap") as tap, patch.object(
        business, "go_home", return_value=True
    ), patch.object(business.time, "sleep"):
        assert business._normalize_trade_startup_screen()

    tap.assert_called_once_with((78, 38))


def test_false_arrival_is_ignored_while_transit_markers_remain():
    travel = FakeStartupImage(["剩余行程：120km", "自动巡航中"])
    station_home = FakeStartupImage(["资产", "车厢内", "副官室"])
    station = type("Station", (), {"wait": lambda self: True})
    with patch.object(
        business, "screenshot", side_effect=[travel, station_home]
    ), patch.object(business, "STATION", return_value=station()), patch.object(
        business, "input_tap"
    ) as tap, patch.object(business, "go_home", return_value=True), patch.object(
        business.time, "sleep"
    ):
        assert business._wait_for_verified_arrival(max_false_arrivals=2)

    assert tap.call_count == 2


def test_exchange_navigation_is_blocked_during_transit():
    travel = FakeStartupImage(["目的地：武林源", "剩余行程：80km"])
    with patch.object(business, "screenshot", return_value=travel), patch.object(
        business, "go_outlets"
    ) as go_outlets:
        assert not business.go_business("sell")

    go_outlets.assert_not_called()


def test_existing_exchange_lobby_skips_city_navigation():
    result = type("Result", (), {"success": True})()
    with patch.object(business, "is_train_in_transit", return_value=False), patch.object(
        business, "is_sell_page", return_value=False
    ), patch.object(business, "screenshot", return_value=FakeExchangeLobby()), patch.object(
        business.exchange_navigation, "open_exchange_action", return_value=result
    ) as navigate:
        assert business.go_business("sell")

    navigate.assert_called_once_with(
        business.exchange_navigation.ExchangeAction.SELL, read_only=False
    )
