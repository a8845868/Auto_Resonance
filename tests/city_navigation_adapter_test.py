from __future__ import annotations

import inspect
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import numpy as np
import pytest

import core.control.control as control
import core.services.city_navigation as city_navigation_module
from core.preset import presets
from core.services.city_navigation import (
    CityNavigationAdapter,
    CityNavigationState,
    ExchangeEntryAdapter,
    StationDetectionResult,
    detect_current_station,
    observe_city_frame,
)
from core.services.read_only_policy import DEFAULT_POLICY_SPECS
from tools import sixth_read_only_probe as live_probe
from tools import city_entry_single_action_probe as single_city_probe
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
    def __init__(
        self, *texts: str, pixel: int = 0, capture_id: str = "capture",
        size: tuple[int, int] = (1280, 720),
    ):
        width, height = size
        self.image = np.full((height, width, 3), pixel, dtype=np.uint8)
        self.raw_frame_hash = f"{pixel + 1:064x}"
        self.source_capture_id = capture_id
        self.captured_at = NOW
        self._items = [
            _item(
                text,
                round(width * 1100 / 1280) if "访问城市" in text else round(width * (300 + index * 100) / 1280),
                round(height * 480 / 720) if "访问城市" in text else round(height * 220 / 720),
            )
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


def _adapter(
    frames,
    *,
    tap=lambda *_args, **_kwargs: True,
    timeout=10.0,
    stall_frames=5,
    **adapter_kwargs,
):
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
        evidence_recorder=lambda _evidence: True,
        **adapter_kwargs,
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


def _city_detail(*, pixel=3, station: str | None = None):
    texts = ["当前城市", "城市设施", "城市手册"]
    if station:
        texts.append(station)
    return _Frame(*texts, pixel=pixel, capture_id=f"city-{pixel}")


def test_home_ready_to_city_detail_uses_observed_anchor_and_waits_through_transition():
    taps = []
    result = _adapter(
        [_home(), _home(pixel=2), _Frame(pixel=3, capture_id="transition"), _city_detail(pixel=4)],
        tap=lambda *args, **kwargs: taps.append((args, kwargs)) or True,
    ).enter_city()
    assert result.status == "PASS"
    assert result.state is CityNavigationState.CITY_DETAIL
    assert result.reason == "city_postcondition_verified"
    assert taps[0][0][0] == (1100, 480)
    assert taps[0][1]["intent"].action_key == "city_entry_navigation"
    assert CityNavigationState.CITY_TRANSITION in [event.state for event in result.trace]


def test_post_click_home_frame_with_lost_anchor_waits_without_redispatch():
    taps = []
    result = _adapter(
        [
            _home(),
            _home(pixel=2),
            _Frame("市问城市", "启程", "整备列车", pixel=3, capture_id="home-ocr-glitch"),
            _Frame(pixel=4, capture_id="transition"),
            _city_detail(pixel=5),
        ],
        tap=lambda *args, **kwargs: taps.append((args, kwargs)) or True,
    ).enter_city()

    assert result.status == "PASS"
    assert result.state is CityNavigationState.CITY_DETAIL
    assert len(taps) == 1
    assert any(
        event.reason == "home_page_still_visible_after_dispatch"
        and event.state is CityNavigationState.CITY_TRANSITION
        for event in result.trace
    )


def test_get_station_uses_guarded_navigation_once_and_observation_consensus():
    clock = _Clock()
    navigation_calls = []
    frames = iter([_Frame("城市详情", pixel=index) for index in range(10, 17)])
    def navigator(**kwargs):
        navigation_calls.append(kwargs)
        return SimpleNamespace(
            success=True, station_confirmed=True, station_id="岚心城"
        )

    station = presets.get_station(
        is_go_home=False,
        frame_provider=lambda: next(frames),
        recognizer=lambda *_args, **_kwargs: pytest.fail("legacy fixed-ROI OCR used"),
        home_resolver=lambda **_kwargs: True,
        city_navigator=navigator,
        tap=lambda *_args, **_kwargs: True,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
        observation_sample_budget=20,
        state_deadline_seconds=30,
        physical_dispatch_budget=1,
    )

    assert station == "岚心城"
    assert len(navigation_calls) == 1
    assert navigation_calls[0]["max_attempts"] == 20


def test_get_station_never_retries_failed_city_navigation():
    navigation_calls = []

    def navigator(**kwargs):
        navigation_calls.append(kwargs)
        return SimpleNamespace(success=False)

    result = presets.get_station(
        is_go_home=False,
        frame_provider=lambda: pytest.fail("no frame after failed navigation"),
        recognizer=lambda *_args, **_kwargs: pytest.fail("no OCR after failed navigation"),
        home_resolver=lambda **_kwargs: True,
        city_navigator=navigator,
        observation_sample_budget=20,
        physical_dispatch_budget=1,
    )

    assert result is None
    assert len(navigation_calls) == 1


def test_city_entry_fresh_confirmation_records_exact_device_point_without_random_offset():
    taps = []
    recorded = []
    result = _adapter(
        [_home(), _home(pixel=2), _city_detail(pixel=3)],
        tap=lambda *args, **kwargs: taps.append((args, kwargs)) or True,
    )
    result.evidence_recorder = recorded.append
    outcome = result.enter_city()

    assert outcome.status == "PASS"
    assert len(taps) == 1
    assert taps[0][1]["random_offset"] is False
    assert len(recorded) == 1
    evidence = recorded[0]
    assert evidence.random_offset_enabled is False
    assert evidence.random_offset_requested is False
    assert evidence.actual_dispatched_point == evidence.coordinate_chain.device_point
    assert evidence.coordinate_chain.complete is True
    assert evidence.dispatch_acknowledged is True


def test_city_entry_coordinate_mapping_is_consistent_for_supported_capture_widths():
    mapped = []
    for index, size in enumerate(((1280, 720), (851, 480), (853, 480)), start=1):
        first = _Frame("访问城市", "作战终端", "启程", pixel=index, capture_id=f"plan-{index}", size=size)
        fresh = _Frame("访问城市", "作战终端", "启程", pixel=index + 10, capture_id=f"fresh-{index}", size=size)
        city = _Frame("当前城市", "城市设施", "城市手册", pixel=index + 20, capture_id=f"city-{index}", size=size)
        adapter = _adapter([first, fresh, city])
        adapter.geometry_provider = lambda: SimpleNamespace(physical_width=1280, physical_height=720)
        result = adapter.enter_city()
        assert result.status == "PASS"
        mapped.append(result.evidence.coordinate_chain.device_point)
    assert max(point[0] for point in mapped) - min(point[0] for point in mapped) <= 1
    assert max(point[1] for point in mapped) - min(point[1] for point in mapped) <= 1


def test_multiple_city_entry_candidates_block_without_dispatch():
    taps = []
    result = _adapter(
        [_Frame("访问城市", "访问城市", "启程", pixel=1)],
        tap=lambda *args, **kwargs: taps.append((args, kwargs)) or True,
    ).enter_city()
    assert result.reason == "city_entry_candidate_not_unique"
    assert taps == []


def test_stale_fresh_confirmation_blocks_without_dispatch():
    frame = _home()
    taps = []
    result = _adapter(
        [frame, frame],
        tap=lambda *args, **kwargs: taps.append((args, kwargs)) or True,
    ).enter_city()
    assert result.reason == "stale_frame_action"
    assert taps == []


def test_unknown_transition_then_station_confirmation_passes_with_one_dispatch():
    taps = []
    adapter = _adapter(
        [_home(), _home(pixel=2), _Frame(pixel=3, capture_id="blank"), _city_detail(pixel=4, station="七号自由港")],
        tap=lambda *args, **kwargs: taps.append((args, kwargs)) or True,
    )
    adapter.require_station_confirmation = True
    adapter.station_ids = ("岚心城", "七号自由港")
    result = adapter.enter_city()
    assert result.status == "PASS"
    assert result.entry_opened is True
    assert result.station_confirmed is True
    assert result.station_id == "七号自由港"
    assert len(taps) == 1


def test_nonempty_unknown_transition_replays_real_defect_then_reaches_station():
    taps = []
    adapter = _adapter(
        [
            _home(),
            _home(pixel=2),
            _Frame("载入中的装饰文字", pixel=3, capture_id="unknown-text-1"),
            _Frame("过渡动画字幕", pixel=4, capture_id="unknown-text-2"),
            _city_detail(pixel=5, station="岚心城"),
        ],
        tap=lambda *args, **kwargs: taps.append((args, kwargs)) or True,
        city_entry_minimum_grace_seconds=2.0,
    )
    adapter.require_station_confirmation = True
    adapter.station_ids = ("岚心城", "七号自由港")

    result = adapter.enter_city()

    assert result.status == "PASS"
    assert result.reason == "station_confirmed"
    assert result.dispatch_count == 1
    assert result.post_observation_count == 3
    assert result.transition_result == "PASS"
    assert result.last_observed_state == "CITY_DETAIL"
    assert result.attempt_count != result.dispatch_count
    assert len(taps) == 1
    pending = [event for event in result.trace if event.transition_classification == "PENDING"]
    assert any(event.reason == "city_transition_with_uncommitted_page_evidence" for event in pending)
    assert all(event.elapsed_since_dispatch_seconds is not None for event in pending)


def test_nonempty_unknown_after_minimum_grace_remains_pending_until_success():
    adapter = _adapter(
        [
            _home(),
            _home(pixel=2),
            _Frame("普通过渡文字", pixel=3),
            _Frame("普通过渡文字", pixel=4),
            _Frame("普通过渡文字", pixel=5),
            _city_detail(pixel=6),
        ],
        city_entry_minimum_grace_seconds=0.5,
    )

    result = adapter.enter_city()

    assert result.status == "PASS"
    assert result.dispatch_count == 1
    assert result.post_observation_count == 4
    assert any(
        event.transition_classification == "PENDING"
        and event.elapsed_since_dispatch_seconds >= 0.5
        for event in result.trace
    )


@pytest.mark.parametrize(
    "foreign_texts",
    [
        ("装备", "载货", "素材"),
        ("行动汇总", "任务结果"),
        ("https://example.invalid", "浏览器"),
    ],
)
def test_committed_foreign_pages_fail_early_without_redispatch(foreign_texts):
    taps = []
    result = _adapter(
        [_home(), _home(pixel=2), _Frame(*foreign_texts, pixel=3)],
        tap=lambda *args, **kwargs: taps.append((args, kwargs)) or True,
    ).enter_city()

    assert result.reason == "city_entry_unexpected_page"
    assert result.transition_result == "EXPLICIT_FAILURE"
    assert result.post_observation_count == 1
    assert result.dispatch_count == 1
    assert len(taps) == 1


@pytest.mark.parametrize(
    ("post_frames", "expected_state"),
    [
        (
            [
                _Frame("作战终端", "启程", pixel=3),
                _Frame("作战终端", "启程", pixel=4),
            ],
            "HOME_READY",
        ),
        ([_home(pixel=3), _home(pixel=4)], "CITY_ENTRY_VISIBLE"),
    ],
)
def test_home_states_after_dispatch_remain_pending_until_timeout(
    post_frames, expected_state,
):
    result = _adapter(
        [_home(), _home(pixel=2), *post_frames],
        timeout=1.5,
        stall_frames=1,
    ).enter_city()

    assert result.reason == "city_entry_postcondition_timeout"
    assert result.transition_result == "TIMEOUT"
    assert result.dispatch_count == 1
    assert result.post_observation_count == 2
    assert result.last_observed_state == expected_state
    assert all(
        event.transition_classification == "PENDING"
        for event in result.trace
        if event.action == "WAIT_CITY_TRANSITION"
    )


def test_post_dispatch_capture_failure_has_precise_reason():
    result = _adapter([_home(), _home(pixel=2)]).enter_city()

    assert result.reason == "city_entry_transition_capture_failed"
    assert result.transition_result == "FAIL"
    assert result.dispatch_count == 1
    assert result.post_observation_count == 0


def test_post_dispatch_detection_failure_has_precise_reason(monkeypatch):
    original = city_navigation_module.observe_city_frame
    calls = 0

    def fail_third_observation(frame, *, now=lambda: NOW):
        nonlocal calls
        calls += 1
        if calls >= 3:
            raise RuntimeError("detector failed")
        return original(frame, now=now)

    monkeypatch.setattr(city_navigation_module, "observe_city_frame", fail_third_observation)
    result = _adapter([_home(), _home(pixel=2), _Frame("过渡", pixel=3)]).enter_city()

    assert result.reason == "city_entry_transition_detection_failed"
    assert result.transition_result == "FAIL"
    assert result.dispatch_count == 1
    assert result.post_observation_count == 0


def test_station_detector_no_match_is_not_defaulted_to_lanxin():
    result = detect_current_station(_city_detail(), ("岚心城", "七号自由港"))
    assert result.result == "NO_MATCH"
    assert result.station_id is None
    assert result.reason == "station_detector_no_match"


def test_station_detector_rejects_multiple_station_names():
    frame = _Frame("当前城市", "城市设施", "岚心城", "七号自由港")
    result = detect_current_station(frame, ("岚心城", "七号自由港"))
    assert result.result == "AMBIGUOUS"
    assert result.station_id is None
    assert result.reason == "station_detector_ambiguous"


def test_station_detector_exception_has_precise_terminal_reason():
    adapter = _adapter([_home(), _home(pixel=2), _city_detail(pixel=3)])
    adapter.require_station_confirmation = True
    adapter.station_ids = ("岚心城",)
    adapter.station_detector = lambda *_args: (_ for _ in ()).throw(ValueError("boom"))
    result = adapter.enter_city()
    assert result.status == "FAILED"
    assert result.reason == "station_detector_error"


def test_trusted_city_page_without_station_cue_fails_as_station_no_match():
    adapter = _adapter([_home(), _home(pixel=2), _city_detail(pixel=3)])
    adapter.require_station_confirmation = True
    adapter.station_ids = ("岚心城",)
    result = adapter.enter_city()
    assert result.entry_opened is True
    assert result.station_confirmed is False
    assert result.reason == "station_detector_no_match"
    assert result.transition_result == "PASS"


def test_explicit_inventory_page_after_dispatch_fails_without_second_action():
    taps = []
    result = _adapter(
        [_home(), _home(pixel=2), _Frame("装备", "载货", "素材", pixel=3)],
        tap=lambda *args, **kwargs: taps.append((args, kwargs)) or True,
    ).enter_city()
    assert result.reason == "city_entry_unexpected_page"
    assert result.transition_result == "EXPLICIT_FAILURE"
    assert result.dispatch_count == 1
    assert len(taps) == 1


def test_cancellation_after_dispatch_sends_no_additional_input():
    calls = 0
    taps = []

    def cancelled():
        nonlocal calls
        calls += 1
        return calls >= 3

    iterator = iter([_home(), _home(pixel=2)])
    clock = _Clock()
    result = CityNavigationAdapter(
        frame_provider=lambda: next(iterator),
        tap=lambda *args, **kwargs: taps.append((args, kwargs)) or True,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
        timeout=10,
        cancellation=cancelled,
    ).enter_city()
    assert result.reason == "city_entry_cancelled"
    assert result.transition_result == "CANCELLED"
    assert result.dispatch_count == 1
    assert len(taps) == 1


def test_unknown_page_blocks_action():
    taps = []
    result = _adapter(
        [_Frame("神秘弹窗", pixel=9)],
        tap=lambda *args, **kwargs: taps.append((args, kwargs)),
    ).enter_city()
    assert result.status == "BLOCKED"
    assert result.state is CityNavigationState.UNKNOWN
    assert result.reason == "city_entry_candidate_not_unique"
    assert taps == []


def test_missing_city_entry_anchor_blocks_action():
    result = _adapter([_Frame("作战终端", "启程", pixel=1)]).enter_city()
    assert result.status == "BLOCKED"
    assert result.reason == "city_entry_candidate_not_unique"


def test_navigation_timeout_is_bounded():
    frames = [_home(), _home(pixel=2)] + [
        _Frame(pixel=index, capture_id=f"transition-{index}")
        for index in range(3, 9)
    ]
    result = _adapter(frames, timeout=1.5, stall_frames=99).enter_city()
    assert result.status == "BLOCKED"
    assert result.state is CityNavigationState.TIMEOUT
    assert result.reason == "city_entry_postcondition_timeout"


def test_repeated_unknown_frames_wait_for_transition_timeout():
    stalled = _Frame(pixel=2, capture_id="stalled")
    result = _adapter(
        [_home(), _home(pixel=3), stalled, stalled, stalled],
        timeout=1.5,
        stall_frames=1,
    ).enter_city()
    assert result.status == "BLOCKED"
    assert result.state is CityNavigationState.TIMEOUT
    assert result.reason == "city_entry_postcondition_timeout"
    assert result.transition_result == "TIMEOUT"
    assert result.dispatch_count == 1


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
        [_home(), _home(pixel=2)],
        tap=lambda *args, **kwargs: calls.append((args, kwargs)) or False,
    ).enter_city()
    assert result.status == "BLOCKED"
    assert result.state is CityNavigationState.FAILED
    assert result.reason == "city_entry_dispatch_not_acknowledged"
    assert len(calls) == 1


def test_navigation_result_never_counts_irreversible_action():
    result = _adapter([_home(), _home(pixel=2), _city_detail()], tap=lambda *_args, **_kwargs: True).enter_city()
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


def test_single_action_probe_stops_before_exchange_or_other_product_features():
    source = inspect.getsource(single_city_probe)
    for forbidden in (
        "open_menu(", "open_action(", "run_business", "fatigue_recovery",
        "reward_claim", "departure_confirm", "sweep",
    ):
        assert forbidden not in source
    blocked = single_city_probe._blocked("test")
    assert blocked["real_ui_actions"] == 0
    assert blocked["city_entry_dispatches"] == 0
    assert blocked["transition_observation_count"] == 0
    assert blocked["transition_result"] == "NOT_RUN"


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
