from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

from core.services.city_entry_resolver import (
    CityEntryExecution,
    CityEntryResolver,
    CityEntryState,
    observe_city_entry_frame,
)


NOW = datetime(2026, 7, 20, 17, 0, tzinfo=timezone(timedelta(hours=8)))


def _item(text: str, x: int = 600, y: int = 350) -> dict:
    return {
        "text": text,
        "position": [
            [x - 60, y - 15],
            [x + 60, y - 15],
            [x + 60, y + 15],
            [x - 60, y + 15],
        ],
    }


class _Frame:
    def __init__(self, *items: dict, pixel: int = 0, capture_id: str = "capture"):
        self.image = np.full((720, 1280, 3), pixel, dtype=np.uint8)
        self._items = list(items)
        self.source_capture_id = capture_id

    def ocr(self):
        return list(self._items)


class _Clock:
    def __init__(self):
        self.value = 100.0

    def __call__(self):
        return self.value

    def sleep(self, seconds: float):
        self.value += float(seconds)


def _home(pixel: int = 1, *, anchor: bool = True) -> _Frame:
    items = [_item("作战终端", 1180, 405), _item("启程", 1190, 660)]
    if anchor:
        items.append(_item("访问城市", 1170, 485))
    return _Frame(*items, pixel=pixel, capture_id=f"home-{pixel}")


def _city(pixel: int = 2, *, back: bool = False) -> _Frame:
    items = [
        _item("城市详情", 180, 90),
        _item("城市设施", 500, 240),
        _item("交流", 880, 410),
    ]
    if back:
        items.append(_item("返回", 70, 40))
    return _Frame(*items, pixel=pixel, capture_id=f"city-{pixel}")


def _resolver(frames, *, tap=lambda *_args, **_kwargs: True, **kwargs):
    values = iter(frames)
    clock = _Clock()
    return CityEntryResolver(
        frame_provider=lambda: next(values),
        tap=tap,
        sleep=clock.sleep,
        monotonic=clock,
        now=lambda: NOW,
        timeout=kwargs.pop("timeout", 10.0),
        max_attempts=kwargs.pop("max_attempts", len(frames)),
        stall_frames=kwargs.pop("stall_frames", 5),
        poll_interval=1.0,
        correlation_id="CITYENTRY-20260720-TEST",
        **kwargs,
    )


def test_home_ready_recognizes_unique_city_entry_from_capture():
    observation = observe_city_entry_frame(_home())
    assert observation.state is CityEntryState.CITY_ENTRY_VISIBLE
    assert observation.visible is True
    assert observation.anchor_bbox == (1110, 470, 1230, 500)
    assert observation.confidence == "HIGH"
    assert observation.capture_id == "home-1"
    assert observation.screenshot_hash


def test_city_entry_missing_blocks_without_action():
    calls = []
    result = _resolver(
        [_home(anchor=False)],
        tap=lambda *args, **kwargs: calls.append((args, kwargs)),
    ).enter_city()
    assert result.status == "BLOCKED"
    assert result.reason == "city_entry_anchor_missing"
    assert calls == []


def test_multiple_city_entry_anchors_are_unknown_and_never_clicked():
    frame = _Frame(
        _item("作战终端"),
        _item("启程"),
        _item("访问城市", 1100, 450),
        _item("访问城市", 900, 450),
    )
    observation = observe_city_entry_frame(frame)
    assert observation.state is CityEntryState.UNKNOWN
    assert observation.confidence == "UNKNOWN"
    assert observation.reason == "city_entry_anchor_untrusted"


def test_guarded_city_entry_reaches_city_detail_with_two_evidence_categories():
    calls = []
    result = _resolver(
        [_home(), _city()],
        tap=lambda point, *, intent: calls.append((point, intent)) or True,
    ).enter_city()
    assert result.state is CityEntryState.CITY_DETAIL
    assert result.status == "PASS"
    assert result.reason == "city_detail_verified"
    assert len(calls) == 1
    assert calls[0][1].action_key == "city_entry_navigation"
    assert calls[0][1].requested_target == "city_entry"


def test_unchanged_home_after_click_fails_stall_detector():
    calls = []
    repeated = _home()
    result = _resolver(
        [repeated, repeated, repeated],
        tap=lambda point, *, intent: calls.append((point, intent)) or True,
        stall_frames=2,
    ).enter_city()
    assert result.state is CityEntryState.FAILED
    assert result.status == "FAILED"
    assert result.reason == "NAVIGATION_STALLED"
    assert len(calls) == 1


def test_unknown_non_transition_page_fails_postcondition():
    result = _resolver([
        _home(),
        _Frame(_item("无法确认的页面"), pixel=4),
    ]).enter_city()
    assert result.state is CityEntryState.FAILED
    assert result.reason == "NAVIGATION_POSTCONDITION_FAILED"


def test_deadline_timeout_does_not_click():
    calls = []
    result = _resolver(
        [_home()],
        tap=lambda *args, **kwargs: calls.append((args, kwargs)),
        timeout=0.0,
    ).enter_city()
    assert result.state is CityEntryState.TIMEOUT
    assert result.status == "BLOCKED"
    assert calls == []


def test_stall_detection_uses_page_fingerprint_even_when_frames_animate():
    result = _resolver(
        [_home(1), _home(2), _home(3)],
        stall_frames=2,
    ).enter_city()
    assert result.reason == "NAVIGATION_STALLED"


def test_guard_denial_propagates_without_retry():
    calls = []
    result = _resolver(
        [_home()],
        tap=lambda point, *, intent: calls.append((point, intent)) or False,
    ).enter_city()
    assert result.state is CityEntryState.FAILED
    assert result.status == "BLOCKED"
    assert result.reason == "guard_denied_city_entry"
    assert len(calls) == 1


def test_executed_input_can_use_resolver_postcondition_after_guard_window():
    result = _resolver(
        [_home(), _city()],
        tap=lambda _point, *, intent: CityEntryExecution(
            allowed=True,
            executed=True,
            guard_result="postcondition_failed",
        ),
    ).enter_city()
    assert result.status == "PASS"
    assert result.action_allowed is True
    assert result.action_executed is True
    assert result.trace[1].guard_result == "postcondition_failed"


def test_login_reward_and_purchase_pages_block_immediately():
    for text in ("请输入密码", "领取奖励", "确认购买"):
        result = _resolver([_Frame(_item(text))]).enter_city()
        assert result.status == "BLOCKED"
        assert result.action_count == 0


def test_only_reversible_navigation_action_keys_can_be_requested():
    intents = []
    enter = _resolver(
        [_home(), _city()],
        tap=lambda _point, *, intent: intents.append(intent) or True,
    ).enter_city()
    assert enter.irreversible_actions == 0
    assert {intent.action_key for intent in intents} == {"city_entry_navigation"}
    assert not ({"buy", "sell", "claim", "consume", "depart"} & {
        intent.action_key for intent in intents
    })


def test_city_detail_can_safely_return_to_home_with_observed_back_anchor():
    intents = []
    result = _resolver(
        [_city(back=True), _home(8)],
        tap=lambda _point, *, intent: intents.append(intent) or True,
    ).safe_back_to_home()
    assert result.state is CityEntryState.HOME_READY
    assert result.status == "PASS"
    assert [intent.action_key for intent in intents] == ["page_back"]
    assert result.irreversible_actions == 0


def test_live_probe_cannot_open_exchange_or_execute_irreversible_actions():
    source = Path("tools/twentysecond_city_entry_read_only_probe.py").read_text(
        encoding="utf-8"
    )
    assert "ExchangeEntryAdapter" not in source
    assert "open_exchange" not in source
    assert "buy" not in source.casefold()
    assert "sell" not in source.casefold()
    assert "max_attempts=10" in source
    assert "stall_frames=5" in source
