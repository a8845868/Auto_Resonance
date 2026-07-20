from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import numpy as np

from auto import exchange_navigation
from auto.module.strength import observe_fatigue_frame
from auto.reward_collection import build_daily_visual_snapshot
from core.preset import presets
from core.preset import control as preset_control
from core.services.live_readonly_validation import (
    ReadOnlyValidationResult,
    ReadOnlyValidationStatus,
)
from core.services.read_only_policy import (
    ActionIntent,
    ObservedAnchor,
    PageObservation,
    PageObserver,
    create_test_read_only_session,
)
from tools.sixth_read_only_probe import _classify_page


NOW = datetime(2026, 7, 20, 3, 0, tzinfo=timezone(timedelta(hours=8)))


def _ocr(text: str, x: int = 600, y: int = 300) -> dict:
    return {
        "text": text,
        "position": [[x - 10, y - 10], [x + 10, y - 10],
                     [x + 10, y + 10], [x - 10, y + 10]],
    }


class _Frame:
    def __init__(self, *texts: str):
        self.image = np.zeros((720, 1280, 3), dtype=np.uint8)
        self._items = [_ocr(text) for text in texts]

    def ocr(self):
        return list(self._items)


class _Clock:
    def __init__(self):
        self.value = 10.0

    def __call__(self):
        return self.value

    def sleep(self, seconds):
        self.value += float(seconds)


def test_readonly_matrix_schema():
    result = ReadOnlyValidationResult(
        scenario="home",
        page_before="UNKNOWN",
        action_attempted="observe",
        action_allowed=True,
        action_executed=False,
        page_after="HOME",
        postcondition="home_confirmed",
        screenshot_hash_before="a" * 64,
        screenshot_hash_after="b" * 64,
        status=ReadOnlyValidationStatus.PASS,
        reason="home_ready",
    )
    assert set(result.to_dict()) == {
        "scenario", "page_before", "action_attempted", "action_allowed",
        "action_executed", "page_after", "postcondition",
        "screenshot_hash_before", "screenshot_hash_after", "status", "reason",
    }
    assert result.to_dict()["status"] == "PASS"


def test_navigation_state_machine(monkeypatch):
    assert {"HOME", "CITY_MAP", "CITY_DETAIL", "NPC_DIALOG", "EXCHANGE_MENU",
            "EXCHANGE_BUY", "EXCHANGE_SELL", "UNKNOWN", "TIMEOUT"} <= {
        state.name for state in presets.CityNavigationState
    }
    monkeypatch.setattr(
        presets,
        "match_template",
        lambda *_args, **_kwargs: SimpleNamespace(status=False, score=0.2),
    )
    frame = _Frame("当前城市", "城市设施", "交易所")
    assert presets._city_frame_observation(frame).state is presets.CityNavigationState.CITY_MAP
    assert _classify_page(["每日签到奖励", "触碰空白区域退出"])[0] == "checkin_overlay"
    assert _classify_page(["公告", "触碰空白区域退出"])[0] == "announcement_overlay"


def test_go_city_timeout():
    clock = _Clock()
    frames = iter(
        SimpleNamespace(
            state="HOME",
            page_fingerprint=f"frame-{index}",
            last_template_score=0.5,
            city_entry_anchor=(1080, 450, 1260, 535),
            diagnostics=(),
        )
        for index in range(3)
    )
    result = presets.go_city(
        frame_provider=lambda: next(frames),
        frame_analyzer=lambda frame: frame,
        tap=lambda *_args, **_kwargs: True,
        sleep=clock.sleep,
        monotonic=clock,
        max_attempts=3,
        timeout=20,
        stall_frames=99,
    )
    assert result.state is presets.CityNavigationState.TIMEOUT
    assert result.actions_executed == ("city_entry_navigation",)


def test_fatigue_observation_unknown():
    blocked = observe_fatigue_frame(_Frame("访问城市"), captured_at=NOW)
    assert blocked.status == "BLOCKED"
    assert blocked.current is None and blocked.maximum is None

    unknown = observe_fatigue_frame(_Frame("恢复疲劳值方式"), captured_at=NOW)
    assert unknown.status == "UNKNOWN"
    assert unknown.confidence == "UNKNOWN"


def test_daily_unknown_not_completed():
    snapshot = build_daily_visual_snapshot([[]], captured_at=NOW)
    assert snapshot.status == "UNKNOWN"
    assert snapshot.activity_current is None
    assert snapshot.activity_maximum is None
    assert snapshot.completed is not True


def test_exchange_buy_sell_classification():
    buy = [_ocr(text) for text in ("交易品", "载货量", "预计买入", "全部买入", "买入总价")]
    sell = [_ocr(text) for text in ("交易品", "载货量", "预计卖出", "全部卖出", "卖出总价")]
    assert exchange_navigation.exchange_page_matches(buy, "BUY")
    assert not exchange_navigation.exchange_page_matches(buy, "SELL")
    assert exchange_navigation.exchange_page_matches(sell, "SELL")
    assert not exchange_navigation.exchange_page_matches(sell, "BUY")


def test_city_outlet_click_remains_inside_observed_anchor(monkeypatch):
    class CroppedFrame:
        def crop_image(self, *_args):
            return self

        def ocr(self):
            # Image.ocr() restores crop offsets before returning coordinates.
            return [{
                "text": "交易所",
                "position": [[850, 260], [950, 260], [950, 290], [850, 290]],
            }]

    taps = []
    monkeypatch.setattr(preset_control, "screenshot", lambda: CroppedFrame())
    monkeypatch.setattr(
        preset_control,
        "input_tap",
        lambda point, **_kwargs: taps.append(tuple(map(int, point))) or True,
    )
    assert preset_control.blurry_ocr_click(
        "交易所",
        cropped_pos1=(160, 40),
        cropped_pos2=(1000, 500),
        excursion_pos=(0, 0),
        trynum=1,
        log=False,
        action_key="navigation_anchor",
        page_id="city_outlets",
    )
    assert taps == [(900, 275)]


def _observation(*, captured_at: datetime) -> PageObservation:
    return PageObservation(
        observation_id="obs-1",
        screenshot_hash="1" * 64,
        page_type="daily_activity",
        markers=("daily_activity",),
        anchors=(ObservedAnchor("top_left_back", "返回", (20, 10, 130, 85)),),
        captured_at=captured_at,
        capture_sequence=1,
        source_capture_id="capture-1",
        source_monotonic_sequence=1,
        backend_generation=1,
        instance_id="test-instance-0",
        adb_serial="test-adb-0",
    )


def test_stale_page_observation_rejected():
    state = {"value": _observation(captured_at=NOW - timedelta(seconds=1))}
    guard, issuer = create_test_read_only_session(
        PageObserver(lambda: state["value"]), lambda _point: None, now=lambda: NOW,
    )
    permit = issuer.issue(
        ActionIntent("reward_back", "top_left_back", "stale-regression"),
        ((50, 40),),
    )
    state["value"] = _observation(captured_at=NOW - timedelta(seconds=30))
    assert guard.authorize_coordinate((50, 40), permit=permit) is False


def test_no_irreversible_action_in_live_mode():
    guard, _issuer = create_test_read_only_session(
        PageObserver(lambda: _observation(captured_at=NOW)),
        lambda _point: None,
        now=lambda: NOW,
    )
    for action in sorted(guard.BLOCKED_ACTIONS):
        assert guard.authorize_coordinate(
            (640, 500), intent=ActionIntent(action, "blocked", "live-readonly"),
        ) is False
    assert not any(entry.side_effect_occurred for entry in guard.journal)
