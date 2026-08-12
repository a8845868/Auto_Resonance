from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import numpy as np

from core.services.startup_overlay_resolver import (
    OverlayType,
    StartupResolver,
    StartupState,
    classify_startup_frame,
)
from core.services.live_readonly_validation import startup_resolution_scenario
from core.services.read_only_policy import DisplayGeometry
from tools import sixth_read_only_probe as probe


NOW = datetime(2026, 7, 20, 11, 30, tzinfo=timezone(timedelta(hours=8)))


def _item(text: str, x: int = 600, y: int = 350) -> dict:
    return {
        "text": text,
        "position": [[x - 20, y - 10], [x + 20, y - 10],
                     [x + 20, y + 10], [x - 20, y + 10]],
    }


class _Frame:
    def __init__(self, *texts: str, pixel: int = 0, x: int = 600):
        self.image = np.full((720, 1280, 3), pixel, dtype=np.uint8)
        self._items = [
            _item(text, x=x, y=680 if "退出" in text else 350)
            for text in texts
        ]

    def ocr(self):
        return list(self._items)


class _Clock:
    def __init__(self):
        self.value = 100.0

    def __call__(self):
        return self.value

    def sleep(self, seconds: float):
        self.value += float(seconds)


def _resolver(frames, *, tap=lambda *_args, **_kwargs: True, timeout=10.0):
    values = iter(frames)
    clock = _Clock()
    return StartupResolver(
        frame_provider=lambda: next(values),
        tap=tap,
        sleep=clock.sleep,
        monotonic=clock,
        now=lambda: NOW,
        timeout=timeout,
        max_attempts=len(frames),
    )


def test_announcement_is_safe_dismissable_with_explicit_close_anchor():
    observation = classify_startup_frame(_Frame("公告", "关闭"))
    assert observation.overlay_type is OverlayType.SAFE_DISMISSABLE
    assert observation.action is not None
    assert observation.action.irreversible is False


def test_checkin_reward_with_claim_button_is_blocked_and_never_clicked():
    taps = []
    frame = _Frame("每日签到奖励", "领取", "触碰空白区域退出")
    observation = classify_startup_frame(frame)
    assert observation.overlay_type is OverlayType.REWARD_RELATED
    assert observation.action is None
    result = _resolver([frame], tap=lambda *args, **kwargs: taps.append((args, kwargs))).resolve()
    assert result.state is StartupState.BLOCKED
    assert result.reason == "CHECKIN_REWARD_REQUIRES_MANUAL"
    assert taps == []


def test_checkin_without_claim_can_use_explicit_exit_instruction():
    observation = classify_startup_frame(
        _Frame("每日签到奖励", "已领取", "触碰空白区域退出")
    )
    assert observation.overlay_type is OverlayType.REWARD_RELATED
    assert observation.action is not None
    assert observation.action.type == "DISMISS_CHECKIN_INFORMATION"


def test_purchase_confirmation_is_blocked():
    observation = classify_startup_frame(_Frame("确认购买", "支付"))
    assert observation.overlay_type is OverlayType.PURCHASE_CONFIRMATION
    assert observation.action is None


def test_unknown_popup_remains_unknown():
    observation = classify_startup_frame(_Frame("神秘弹窗", "稍后再说"))
    assert observation.overlay_type is OverlayType.UNKNOWN
    assert observation.action is None


def test_two_consistent_home_frames_reach_home_ready():
    first = _Frame("访问城市", "作战终端", "启程", pixel=1, x=600)
    second = _Frame("访问城市", "作战终端", "启程", pixel=2, x=604)
    assert classify_startup_frame(first).screenshot_hash != classify_startup_frame(second).screenshot_hash
    assert classify_startup_frame(first).page_fingerprint == classify_startup_frame(second).page_fingerprint
    result = _resolver([first, second]).resolve()
    assert result.state is StartupState.HOME_READY
    assert result.status == "PASS"
    assert result.path[-2:] == ("HOME_CANDIDATE", "HOME_READY")
    scenario = startup_resolution_scenario(result)
    assert scenario["scenario"] == "startup_overlay_resolution"
    assert scenario["status"] == "PASS"
    assert scenario["irreversible_actions"] == 0


def test_overlay_still_present_after_dismiss_is_failed():
    taps = []
    overlay = _Frame("公告", "关闭")
    result = _resolver(
        [overlay, overlay],
        tap=lambda *args, **kwargs: taps.append((args, kwargs)) or True,
    ).resolve()
    assert result.state is StartupState.FAILED
    assert result.reason == "overlay_persisted_after_dismiss"
    assert len(taps) == 1


def test_waiting_unknown_ui_reaches_timeout_without_clicking():
    taps = []
    result = _resolver(
        [_Frame("加载中"), _Frame("加载中"), _Frame("加载中")],
        tap=lambda *args, **kwargs: taps.append((args, kwargs)),
        timeout=1.5,
    ).resolve()
    assert result.state is StartupState.TIMEOUT
    assert result.reason == "startup_deadline_or_attempt_limit"
    assert taps == []
    assert all(event.screenshot_hash for event in result.trace)


def _trusted_probe_observation(monkeypatch, *texts: str):
    items = [_item(text, x=300 + index * 100) for index, text in enumerate(texts)]
    frame = SimpleNamespace(
        image=np.zeros((720, 1280, 3), dtype=np.uint8),
        ocr=lambda: list(items),
    )
    monkeypatch.setattr(probe, "capture_envelope", lambda: SimpleNamespace(
        frame=frame.image,
        raw_frame_hash="f" * 64,
        backend_monotonic_sequence=1,
        backend_capture_id="startup-test-capture",
        captured_at=NOW,
        backend_generation=1,
        instance_id="test-instance-0",
        adb_serial="test-adb-0",
    ))
    monkeypatch.setattr(probe, "Image", lambda _image: frame)
    monkeypatch.setattr(probe, "current_display_geometry", DisplayGeometry)
    return probe._trusted_observation().as_observation()


def test_live_adapter_exposes_only_proven_checkin_exit_to_guard(monkeypatch):
    observed = _trusted_probe_observation(
        monkeypatch, "每日签到奖励", "已领取", "触碰空白区域退出",
    )
    assert observed.page_type == "startup_overlay"
    assert [anchor.anchor_id for anchor in observed.anchors] == ["cancel"]


def test_live_adapter_never_exposes_claiming_checkin_or_unknown_overlay(monkeypatch):
    checkin = _trusted_probe_observation(
        monkeypatch, "每日签到奖励", "领取", "触碰空白区域退出",
    )
    assert checkin.page_type == "startup_overlay"
    assert checkin.anchors == ()
    unknown = _trusted_probe_observation(monkeypatch, "神秘弹窗", "触碰空白区域退出")
    assert unknown.page_type == "unknown_overlay"
    assert unknown.anchors == ()
