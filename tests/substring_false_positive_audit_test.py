from __future__ import annotations

from unittest.mock import patch

import auto.run_business.sell as sell
from core.services.city_navigation import detect_current_station
from core.services.screen_state import (
    ResidentHomeState,
    is_train_in_transit,
    resident_home_state,
    startup_screen_action,
)


def _items(*texts: str) -> list[dict]:
    return [{"text": text} for text in texts]


class _Frame:
    def __init__(self, *texts: str):
        self._items = _items(*texts)

    def ocr(self) -> list[dict]:
        return list(self._items)


class _OverlayFrame(_Frame):
    def get_bgr(self, *_args, **_kwargs):
        return [0, 0, 0]


def test_shop_info_text_is_not_resident_announcement_overlay():
    assert resident_home_state(
        _items("黑月商店", "资讯", "触碰空白区域退出")
    ) is ResidentHomeState.UNKNOWN_OVERLAY


def test_bare_checkin_word_is_not_checkin_reward_overlay():
    assert resident_home_state(
        _items("活动签到", "触碰空白区域退出")
    ) is ResidentHomeState.UNKNOWN_OVERLAY


def test_gameplay_menu_loading_text_is_not_startup_loading_state():
    assert startup_screen_action(_items("黑月商店", "商品加载中")) is None
    assert startup_screen_action(_items("城市发展度", "正在加载数据")) is None


def test_single_trip_substring_does_not_classify_train_in_transit():
    assert not is_train_in_transit(_items("剩余行程排行榜"))
    assert is_train_in_transit(_items("目的地：岚心城", "剩余行程：80km"))


def test_station_name_inside_mission_text_is_not_current_station():
    result = detect_current_station(
        _Frame("当前城市", "于汇流塔完成10个作战计划"),
        ("岚心城", "汇流塔"),
    )

    assert result.result == "NO_MATCH"
    assert result.station_id is None


def test_generic_dismiss_overlay_is_not_sale_settlement(monkeypatch):
    clock = [0.0]
    taps: list[tuple[int, int]] = []
    monkeypatch.setattr(sell, "screenshot", lambda: _OverlayFrame("点击空白处退出"))
    monkeypatch.setattr(
        sell,
        "input_tap",
        lambda point, **_kwargs: taps.append(point) or True,
    )
    monkeypatch.setattr(sell, "is_sell_page", lambda: False)
    monkeypatch.setattr(sell.time, "time", lambda: clock[0])
    monkeypatch.setattr(
        sell.time,
        "sleep",
        lambda seconds: clock.__setitem__(0, clock[0] + seconds),
    )

    assert sell.click_sell_button(timeout=1.1) is False
    assert taps == [(1056, 647)]


def test_explicit_settlement_title_still_confirms_sale():
    with patch.object(
        sell, "screenshot", return_value=_OverlayFrame("结算报告", "点击空白处退出")
    ), patch.object(sell, "input_tap") as tap, patch.object(sell.time, "sleep"):
        assert sell.click_sell_button(timeout=1.0) is True

    tap.assert_called_once()
