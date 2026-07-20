from __future__ import annotations

import inspect
from datetime import datetime, timedelta, timezone

import numpy as np

import core.control.control as control
from core.services.city_navigation import (
    CityNavigationAdapter,
    CityNavigationState,
    ExchangeEntryAdapter,
    observe_city_frame,
)
from core.services.read_only_policy import DEFAULT_POLICY_SPECS
from tools import sixth_read_only_probe as live_probe
from tools import eighteenth_city_read_only_probe as city_probe


NOW = datetime(2026, 7, 20, 13, 10, tzinfo=timezone(timedelta(hours=8)))


def _item(text: str, x: int, y: int) -> dict:
    return {
        "text": text,
        "position": [
            [x - 30, y - 12], [x + 30, y - 12],
            [x + 30, y + 12], [x - 30, y + 12],
        ],
    }


class _Frame:
    def __init__(self, *texts: str, pixel: int = 0, capture_id: str = "capture"):
        self.image = np.full((720, 1280, 3), pixel, dtype=np.uint8)
        self.raw_frame_hash = f"{pixel + 1:064x}"
        self.source_capture_id = capture_id
        self.captured_at = NOW
        self._items = [
            _item(text, 1100 if "访问城市" in text else 300 + index * 100, 480 if "访问城市" in text else 220)
            for index, text in enumerate(texts)
        ]

    def ocr(self):
        return list(self._items)


class _Clock:
    def __init__(self):
        self.value = 100.0

    def monotonic(self):
        return self.value

    def sleep(self, seconds: float):
        self.value += float(seconds)


def _adapter(frames, *, tap=lambda *_args, **_kwargs: True, timeout=10.0, stall_frames=5):
    iterator = iter(frames)
    clock = _Clock()
    return CityNavigationAdapter(
        frame_provider=lambda: next(iterator),
        tap=tap,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
        now=lambda: NOW,
        timeout=timeout,
        max_attempts=len(frames),
        stall_frames=stall_frames,
        correlation_id="CITYNAV-20260720-TEST",
    )


def _exchange_adapter(frames, *, tap=lambda *_args, **_kwargs: True):
    iterator = iter(frames)
    clock = _Clock()
    return ExchangeEntryAdapter(
        frame_provider=lambda: next(iterator),
        tap=tap,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
        now=lambda: NOW,
        timeout=10.0,
        max_attempts=len(frames),
        stall_frames=5,
        correlation_id="CITYNAV-20260720-EXCHANGE-TEST",
    )


def _home(*, pixel=1):
    return _Frame("访问城市", "作战终端", "启程", pixel=pixel, capture_id=f"home-{pixel}")


def _city_detail(*, pixel=3):
    return _Frame("当前城市", "城市设施", "城市手册", pixel=pixel, capture_id=f"city-{pixel}")


def test_home_ready_to_city_detail_uses_observed_anchor_and_waits_through_transition():
    taps = []
    result = _adapter(
        [_home(), _Frame(pixel=2, capture_id="transition"), _city_detail()],
        tap=lambda *args, **kwargs: taps.append((args, kwargs)) or True,
    ).enter_city()
    assert result.status == "PASS"
    assert result.state is CityNavigationState.CITY_DETAIL
    assert result.reason == "city_postcondition_verified"
    assert taps[0][0][0] == (1100, 480)
    assert taps[0][1]["intent"].action_key == "city_entry_navigation"
    assert CityNavigationState.CITY_TRANSITION in [event.state for event in result.trace]


def test_unknown_page_blocks_action():
    taps = []
    result = _adapter(
        [_Frame("神秘弹窗", pixel=9)],
        tap=lambda *args, **kwargs: taps.append((args, kwargs)),
    ).enter_city()
    assert result.status == "BLOCKED"
    assert result.state is CityNavigationState.UNKNOWN
    assert result.reason == "unknown_page_blocks_action"
    assert taps == []


def test_missing_city_entry_anchor_blocks_action():
    result = _adapter([_Frame("作战终端", "启程", pixel=1)]).enter_city()
    assert result.status == "BLOCKED"
    assert result.reason == "city_entry_anchor_missing"


def test_navigation_timeout_is_bounded():
    frames = [_home()] + [
        _Frame(pixel=index, capture_id=f"transition-{index}")
        for index in range(2, 9)
    ]
    result = _adapter(frames, timeout=1.5, stall_frames=99).enter_city()
    assert result.status == "BLOCKED"
    assert result.state is CityNavigationState.TIMEOUT
    assert result.reason == "navigation_deadline_or_attempt_limit"


def test_stall_detection_uses_repeated_frame_or_page_fingerprint():
    stalled = _Frame(pixel=2, capture_id="stalled")
    result = _adapter(
        [_home(), stalled, stalled, stalled], stall_frames=3,
    ).enter_city()
    assert result.status == "FAILED"
    assert result.state is CityNavigationState.FAILED
    assert result.reason == "NAVIGATION_STALLED"


def test_buy_page_requires_title_list_button_and_price_evidence():
    observed = observe_city_frame(
        _Frame("买入", "商品列表", "买入按钮", "购买价格", "预计买入")
    )
    assert observed.state is CityNavigationState.EXCHANGE_BUY


def test_sell_page_requires_title_goods_button_and_price_evidence():
    observed = observe_city_frame(
        _Frame("卖出", "商品出售区域", "卖出按钮", "出售价格", "预计卖出")
    )
    assert observed.state is CityNavigationState.EXCHANGE_SELL


def test_city_map_and_city_detail_are_distinct_states():
    city_map = observe_city_frame(_Frame("当前城市", "城市地图", "地图"))
    city_detail = observe_city_frame(_Frame("当前城市", "城市设施", "城市手册"))
    assert city_map.state is CityNavigationState.CITY_MAP
    assert city_detail.state is CityNavigationState.CITY_DETAIL


def test_exchange_menu_is_not_buy_page():
    observed = observe_city_frame(
        _Frame("交易所", "你想要什么", "我要买", "我要卖", "交易品投资")
    )
    assert observed.state is CityNavigationState.EXCHANGE_MENU
    assert not ExchangeEntryAdapter.page_matches(observed, "BUY")


def test_buy_like_page_without_distinct_button_remains_unknown():
    observed = observe_city_frame(
        _Frame("预计买入", "商品列表", "购买价格", "买入总价")
    )
    assert observed.state is CityNavigationState.UNKNOWN


def test_buy_page_is_not_sell_page():
    observed = observe_city_frame(
        _Frame("买入", "商品列表", "买入按钮", "购买价格", "预计买入")
    )
    assert ExchangeEntryAdapter.page_matches(observed, "BUY")
    assert not ExchangeEntryAdapter.page_matches(observed, "SELL")


def test_guard_denial_propagates_without_retry():
    calls = []
    result = _adapter(
        [_home()],
        tap=lambda *args, **kwargs: calls.append((args, kwargs)) or False,
    ).enter_city()
    assert result.status == "BLOCKED"
    assert result.state is CityNavigationState.FAILED
    assert result.reason == "guard_denied_city_entry"
    assert len(calls) == 1


def test_navigation_result_never_counts_irreversible_action():
    result = _adapter([_home(), _city_detail()], tap=lambda *_args, **_kwargs: True).enter_city()
    assert result.status == "PASS"
    assert result.irreversible_actions == 0
    assert all(event.action not in {"BUY", "SELL", "PURCHASE", "CLAIM"} for event in result.trace)


def test_city_transition_is_post_only_and_cannot_authorize_an_input():
    for action_key in (
        "city_entry_navigation", "navigation_anchor",
        "exchange_buy_navigation", "exchange_sell_navigation", "page_back",
    ):
        spec = DEFAULT_POLICY_SPECS[action_key]
        assert "city_transition" in spec.allowed_post_page_types
        assert "city_transition" not in spec.allowed_page_types


def test_city_detail_to_exchange_menu_uses_exchange_ocr_anchor():
    taps = []
    city = _Frame("当前城市", "城市设施", "交易所", pixel=3)
    transition = _Frame(pixel=4)
    menu = _Frame("交易所", "你想要什么", "我要买", "我要卖", pixel=5)
    result = _exchange_adapter(
        [city, transition, menu],
        tap=lambda *args, **kwargs: taps.append((args, kwargs)) or True,
    ).open_menu()
    assert result.status == "PASS"
    assert result.state is CityNavigationState.EXCHANGE_MENU
    assert taps[0][1]["intent"].action_key == "navigation_anchor"
    assert taps[0][1]["intent"].requested_target == "交易所"
    assert CityNavigationState.CITY_TRANSITION in [event.state for event in result.trace]


def test_exchange_menu_to_buy_waits_for_strong_buy_page_evidence():
    taps = []
    menu = _Frame("交易所", "你想要什么", "我要买", "我要卖", pixel=5)
    transition = _Frame(pixel=6)
    buy = _Frame("预计买入", "交易品", "全部买入", "买入总价", pixel=7)
    result = _exchange_adapter(
        [menu, transition, buy],
        tap=lambda *args, **kwargs: taps.append((args, kwargs)) or True,
    ).open_action("BUY")
    assert result.status == "PASS"
    assert result.state is CityNavigationState.EXCHANGE_BUY
    assert taps[0][1]["intent"].action_key == "exchange_buy_navigation"
    assert result.irreversible_actions == 0


def test_exchange_menu_to_sell_never_uses_transaction_action():
    taps = []
    menu = _Frame("交易所", "你想要什么", "我要买", "我要卖", pixel=5)
    sell = _Frame("预计卖出", "交易品", "全部卖出", "卖出总价", pixel=8)
    result = _exchange_adapter(
        [menu, sell],
        tap=lambda *args, **kwargs: taps.append((args, kwargs)) or True,
    ).open_action("SELL")
    assert result.status == "PASS"
    assert result.state is CityNavigationState.EXCHANGE_SELL
    assert taps[0][1]["intent"].action_key == "exchange_sell_navigation"
    assert all(event.action != "SELL" for event in result.trace)


def test_live_observer_classifies_transition_as_post_only_state():
    assert live_probe._classify_observed_items([]) == (
        "city_transition", ["city_transition"],
    )


def test_live_observer_maps_city_and_exchange_pages_to_guard_page_types():
    city = [_item("当前城市", 200, 200), _item("城市设施", 300, 200), _item("交易所", 900, 300)]
    menu = [_item("交易所", 900, 200), _item("你想要什么", 800, 250), _item("我要买", 800, 320), _item("我要卖", 800, 400)]
    buy = [_item("预计买入", 900, 500), _item("交易品", 200, 100), _item("全部买入", 900, 600), _item("买入总价", 900, 550)]
    assert live_probe._classify_observed_items(city)[0] == "city_map"
    assert live_probe._classify_observed_items(menu)[0] == "exchange"
    assert live_probe._classify_observed_items(buy)[0] == "exchange_buy"


def test_live_city_probe_has_no_direct_input_or_irreversible_action_key():
    source = inspect.getsource(city_probe)
    assert "input_tap" not in source
    assert "transaction_buy" not in source
    assert "transaction_sell" not in source
    assert "reward_claim" not in source
    assert "departure_confirm" not in source


def test_screenshot_preserves_backend_capture_identity(monkeypatch):
    envelope = type("Envelope", (), {
        "frame": np.zeros((720, 1280, 3), dtype=np.uint8),
        "raw_frame_hash": "a" * 64,
        "backend_capture_id": "capture-city-1",
        "captured_at": NOW,
        "backend_generation": 7,
        "instance_id": "test-instance-0",
    })()
    monkeypatch.setattr(control, "capture_envelope", lambda: envelope)
    frame = control.screenshot()
    observed = observe_city_frame(frame)
    assert observed.screenshot_hash == "a" * 64
    assert observed.source_capture_id == "capture-city-1"
    assert observed.timestamp == NOW.isoformat(timespec="milliseconds")
