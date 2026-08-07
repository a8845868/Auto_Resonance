from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import numpy as np

from core.services import read_only_policy as policy
from core.services.city_entry_resolver import (
    CityEntryResolver,
    CityEntryState,
    observe_city_entry_frame,
)
from tools.twentysecond_city_entry_read_only_probe import _sanitized_guard_journal


NOW = datetime(2026, 7, 20, 18, 0, tzinfo=timezone(timedelta(hours=8)))


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
    def __init__(self, *items: dict, pixel: int = 0):
        self.image = np.full((720, 1280, 3), pixel, dtype=np.uint8)
        self._items = list(items)
        self.source_capture_id = f"fault-{pixel}"

    def ocr(self):
        return list(self._items)


class _Clock:
    def __init__(self):
        self.value = 100.0

    def __call__(self):
        return self.value

    def sleep(self, seconds: float):
        self.value += float(seconds)


def _home(pixel: int = 1, *extra: str) -> _Frame:
    return _Frame(
        _item("作战终端", 1180, 405),
        _item("访问城市", 1170, 485),
        _item("启程", 1190, 660),
        *(_item(text, 600, 300) for text in extra),
        pixel=pixel,
    )


def _city(pixel: int = 9, *extra: str) -> _Frame:
    return _Frame(
        _item("城市详情", 180, 90),
        _item("城市设施", 500, 240),
        *(_item(text, 800, 400) for text in extra),
        pixel=pixel,
    )


def _resolver(
    frames,
    *,
    tap=lambda *_args, **_kwargs: True,
    initial=None,
    timeout=30.0,
    max_attempts=None,
    stall_frames=5,
):
    values = iter(frames)
    clock = _Clock()
    return CityEntryResolver(
        frame_provider=lambda: next(values),
        tap=tap,
        sleep=clock.sleep,
        monotonic=clock,
        now=lambda: NOW,
        timeout=timeout,
        max_attempts=max_attempts or max(1, len(frames) + (1 if initial else 0)),
        stall_frames=stall_frames,
        poll_interval=1.0,
        correlation_id="CITYENTRY-FAULT-TEST",
        initial_observation=initial,
    )


def test_delayed_postcondition_executes_physical_input_once():
    taps = []
    result = _resolver(
        [_home(), _Frame(pixel=2), _Frame(pixel=3), _city()],
        tap=lambda point, *, intent: taps.append((point, intent)) or True,
        stall_frames=5,
    ).enter_city()

    assert result.status == "PASS"
    assert result.action_count == 1
    assert len(taps) == 1
    assert result.action_journal[0].requested is True
    assert result.action_journal[0].approved is True
    assert result.action_journal[0].executed is True
    assert result.action_journal[0].observed is True
    assert result.action_journal[0].verified is True


def test_executed_action_is_not_repeated_by_same_correlation():
    taps = []
    initial = observe_city_entry_frame(_home())
    resolver = _resolver(
        [_city(2), _city(3)],
        initial=initial,
        tap=lambda point, *, intent: taps.append((point, intent)) or True,
    )

    first = resolver.enter_city()
    second = resolver.enter_city()

    assert first.status == "PASS"
    assert second.status == "BLOCKED"
    assert second.reason == "duplicate_action_prevented"
    assert second.action_count == 0
    assert len(taps) == 1


def test_activity_popup_contamination_never_becomes_city_detail():
    observation = observe_city_entry_frame(_home(1, "活动详情", "关闭"))
    assert observation.state is CityEntryState.UNKNOWN
    assert observation.reason == "OVERLAY_BLOCKED"


def test_npc_dialog_contamination_blocks_city_detail():
    observation = observe_city_entry_frame(_city(1, "你想要什么"))
    assert observation.state is CityEntryState.UNKNOWN
    assert observation.reason == "NPC_DIALOG_BLOCKED"


def test_single_city_ocr_keyword_is_not_city_detail():
    observation = observe_city_entry_frame(_Frame(_item("城市详情")))
    assert observation.state is CityEntryState.UNKNOWN
    assert observation.reason == "city_page_evidence_unknown"


def test_partial_city_evidence_is_unknown():
    observation = observe_city_entry_frame(
        _Frame(_item("城市详情"), _item("无关页面按钮"))
    )
    assert observation.state is CityEntryState.UNKNOWN


def test_navigation_deadline_returns_timeout_without_second_input():
    taps = []
    result = _resolver(
        [_home(), _Frame(pixel=2), _home(3), _Frame(pixel=4), _home(5)],
        tap=lambda point, *, intent: taps.append((point, intent)) or True,
        timeout=2.5,
        max_attempts=10,
        stall_frames=5,
    ).enter_city()
    assert result.state is CityEntryState.TIMEOUT
    assert result.reason == "navigation_timeout"
    assert len(taps) == 1


def test_initial_adb_capture_failure_returns_failed_without_input():
    taps = []

    def disconnected():
        raise ConnectionError("device disconnected")

    resolver = CityEntryResolver(
        frame_provider=disconnected,
        tap=lambda *args, **kwargs: taps.append((args, kwargs)),
        now=lambda: NOW,
        correlation_id="CITYENTRY-ADB-FAIL",
    )
    result = resolver.enter_city()
    assert result.state is CityEntryState.FAILED
    assert result.reason == "CAPTURE_FAILED:ConnectionError"
    assert result.action_count == 0
    assert taps == []


def test_disconnect_after_execution_preserves_action_journal_and_stops():
    taps = []

    def disconnected():
        raise ConnectionError("device disconnected")

    resolver = CityEntryResolver(
        frame_provider=disconnected,
        tap=lambda point, *, intent: taps.append((point, intent)) or True,
        now=lambda: NOW,
        correlation_id="CITYENTRY-POST-ACTION-DISCONNECT",
        initial_observation=observe_city_entry_frame(_home()),
    )
    result = resolver.enter_city()
    assert result.state is CityEntryState.FAILED
    assert result.reason == "CAPTURE_FAILED:ConnectionError"
    assert result.action_count == 1
    assert len(taps) == 1
    assert result.action_journal[0].executed is True
    assert result.action_journal[0].observed is False
    assert result.action_journal[0].verified is False


def test_missing_game_window_returns_failed_without_input():
    resolver = _resolver([None])
    result = resolver.enter_city()
    assert result.state is CityEntryState.FAILED
    assert result.reason == "GAME_WINDOW_UNAVAILABLE"
    assert result.action_count == 0


def test_invalid_direct_city_detail_source_cannot_issue_action():
    taps = []
    result = _resolver(
        [_city()],
        tap=lambda *args, **kwargs: taps.append((args, kwargs)),
    ).enter_city()
    assert result.status == "BLOCKED"
    assert result.reason == "invalid_source_state:CITY_DETAIL_VISIBLE"
    assert taps == []


def test_unknown_source_cannot_transition_to_action():
    taps = []
    result = _resolver(
        [_Frame(_item("未知页面"))],
        tap=lambda *args, **kwargs: taps.append((args, kwargs)),
    ).enter_city()
    assert result.status == "BLOCKED"
    assert result.action_count == 0
    assert taps == []


class _Executor:
    def __init__(self):
        self.taps = []

    def tap(self, point):
        self.taps.append(tuple(point))

    def swipe(self, _trajectory, _duration_ms):
        raise AssertionError("not used")


def _guard_observation(sequence: int, page_type: str):
    anchors = (
        (policy.ObservedAnchor("city_entry", "访问城市", (1080, 450, 1260, 535)),)
        if page_type == "home"
        else ()
    )
    markers = ("top_level_hud",) if page_type == "home" else (page_type,)
    return policy.PageObservation(
        observation_id=f"fault-guard-{sequence}",
        screenshot_hash=f"{sequence:064x}",
        page_type=page_type,
        markers=markers,
        anchors=anchors,
        captured_at=NOW - timedelta(seconds=1) + timedelta(microseconds=sequence),
        capture_sequence=sequence,
        source_capture_id=f"fault-guard-capture-{sequence}",
        source_monotonic_sequence=sequence,
        backend_generation=1,
        instance_id="test-instance-0",
        adb_serial="test-adb-0",
    )


def test_guard_journal_records_full_action_lifecycle_in_order():
    captures = iter(
        [
            _guard_observation(1, "home"),
            _guard_observation(2, "home"),
            _guard_observation(3, "city_map"),
        ]
    )
    issuer = policy.ReadOnlyPermitIssuer(
        policy.PageObserver(lambda: next(captures)),
        policy.AnchorResolver(),
        now=lambda: NOW,
        postcondition_sleep=lambda _seconds: None,
    )
    executor = _Executor()
    guard = policy.ReadOnlySafetySession(
        executor,
        permit_issuer=issuer,
        now=lambda: NOW,
    )
    correlation_id = "CITYENTRY-LIFECYCLE-001"
    allowed = guard.authorize_coordinate(
        (1170, 485),
        intent=policy.ActionIntent(
            "city_entry_navigation",
            "city_entry",
            correlation_id,
        ),
    )

    assert allowed is True
    stages = [
        entry.stage
        for entry in guard.journal
        if entry.correlation_id == correlation_id
    ]
    assert stages == [
        "PRECONDITION_OBSERVED",
        "AUTHORIZED",
        "CONSUME_OBSERVED",
        "EXECUTION_STARTED",
        "EXECUTED",
        "POSTCONDITION_VERIFIED",
    ]
    assert executor.taps == [(1170, 485)]


def test_disconnect_evidence_serializer_preserves_sanitized_guard_journal():
    guard = SimpleNamespace(
        journal=(
            SimpleNamespace(
                correlation_id="CITYENTRY-DISCONNECT-001",
                action_key="city_entry_navigation",
                stage="EXECUTED",
                allowed=True,
                reason="hardware_execution_completed",
                side_effect_occurred=True,
                screenshot_hash="a" * 64,
                page_type="home",
            ),
        )
    )
    payload = _sanitized_guard_journal(guard)
    assert payload == [
        {
            "correlation_id": "CITYENTRY-DISCONNECT-001",
            "action_key": "city_entry_navigation",
            "stage": "EXECUTED",
            "allowed": True,
            "reason": "hardware_execution_completed",
            "side_effect_occurred": True,
            "screenshot_hash": "a" * 64,
            "page_type": "home",
        }
    ]
