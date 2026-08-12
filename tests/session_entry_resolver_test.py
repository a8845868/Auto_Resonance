from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

from core.services.read_only_policy import ActionIntent
from core.services import read_only_policy
from core.services.session_entry_resolver import (
    SessionEntryResolver,
    SessionEntryState,
    classify_session_entry_frame,
)
from core.services.startup_overlay_resolver import StartupResolver, StartupState
from tools import sixth_read_only_probe as probe


NOW = datetime(2026, 7, 20, 15, 30, tzinfo=timezone(timedelta(hours=8)))


def _item(text: str, x: int = 640, y: int = 560) -> dict:
    return {
        "text": text,
        "position": [[x - 180, y - 20], [x + 180, y - 20],
                     [x + 180, y + 20], [x - 180, y + 20]],
    }


class _Frame:
    def __init__(self, *texts: str, pixel: int = 0):
        self.image = np.full((720, 1280, 3), pixel, dtype=np.uint8)
        self._items = [_item(text) for text in texts]

    def ocr(self):
        return list(self._items)


class _Clock:
    def __init__(self):
        self.value = 100.0

    def __call__(self):
        return self.value

    def sleep(self, seconds: float):
        self.value += float(seconds)


def _home(pixel: int) -> _Frame:
    return _Frame("访问城市", "作战终端", "启程", "整备列车", pixel=pixel)


def _resolver(frames, *, tap=lambda *_args, **_kwargs: True):
    values = iter(frames)
    clock = _Clock()
    return SessionEntryResolver(
        frame_provider=lambda: next(values),
        tap=tap,
        sleep=clock.sleep,
        monotonic=clock,
        now=lambda: NOW,
        timeout=10.0,
        max_attempts=len(frames),
        poll_interval=1.0,
        correlation_id="SESSION-20260720-TEST",
    )


def test_existing_session_entry_reaches_home_ready():
    taps = []
    result = _resolver(
        [
            _Frame("下载已经完成，点击任意位置进入游戏"),
            _home(1),
            _home(2),
        ],
        tap=lambda point, *, intent: taps.append((point, intent)) or True,
    ).resolve()

    assert result.state is SessionEntryState.HOME_READY
    assert result.status == "PASS"
    assert result.action_attempted is True
    assert result.action_executed is True
    assert len(taps) == 1
    assert taps[0][1] == ActionIntent(
        "enter_session",
        "session_entry",
        "SESSION-20260720-TEST:ENTER_SESSION",
    )


def test_password_page_is_blocked_without_tap():
    taps = []
    result = _resolver(
        [_Frame("请输入密码", "账号登录")],
        tap=lambda *args, **kwargs: taps.append((args, kwargs)),
    ).resolve()

    assert result.state is SessionEntryState.BLOCKED
    assert result.status == "BLOCKED"
    assert result.reason == "AUTHENTICATION_INPUT_PRESENT"
    assert taps == []


def test_verification_code_page_is_blocked_without_tap():
    result = _resolver([_Frame("请输入验证码", "获取验证码")]).resolve()
    assert result.state is SessionEntryState.BLOCKED
    assert result.status == "BLOCKED"
    assert result.action_attempted is False


def test_continue_game_entry_is_allowed_once():
    calls = []
    result = _resolver(
        [_Frame("继续游戏"), _home(3), _home(4)],
        tap=lambda point, *, intent: calls.append((point, intent)) or True,
    ).resolve()

    assert result.state is SessionEntryState.HOME_READY
    assert result.path[0:3] == (
        "ENTRY_GATE_REQUIRED",
        "ENTRY_CONFIRMING",
        "HOME_READY",
    )
    assert len(calls) == 1


def test_click_without_two_home_frames_fails_without_retrying_entry():
    calls = []
    result = _resolver(
        [
            _Frame("点击任意位置进入游戏"),
            _Frame("正在连接服务器"),
            _Frame("未知过渡画面"),
        ],
        tap=lambda point, *, intent: calls.append((point, intent)) or True,
    ).resolve()

    assert result.state is SessionEntryState.ENTRY_FAILED
    assert result.status == "FAILED"
    assert result.reason == "ENTRY_FAILED"
    assert len(calls) == 1


def test_home_ready_requires_two_consecutive_semantic_frames():
    one_home = _resolver([
        _Frame("开始游戏"),
        _home(5),
        _Frame("正在加载"),
    ]).resolve()
    assert one_home.state is SessionEntryState.ENTRY_FAILED

    two_home = _resolver([
        _Frame("开始游戏"),
        _home(6),
        _home(7),
    ]).resolve()
    assert two_home.state is SessionEntryState.HOME_READY


def test_unknown_page_remains_unknown_without_tap():
    result = _resolver([_Frame("无法识别的页面")]).resolve()
    assert result.state is SessionEntryState.UNKNOWN
    assert result.status == "UNKNOWN"
    assert result.action_attempted is False


def test_entry_text_with_purchase_or_claim_control_is_blocked():
    purchase = classify_session_entry_frame(
        _Frame("点击任意位置进入游戏", "确认购买")
    )
    claim = classify_session_entry_frame(
        _Frame("继续游戏", "领取奖励")
    )

    assert purchase.state is SessionEntryState.BLOCKED
    assert claim.state is SessionEntryState.BLOCKED


def test_live_probe_has_no_credential_input_capability():
    source = Path("tools/twentieth_session_entry_read_only_probe.py").read_text(
        encoding="utf-8"
    )
    assert "input_text" not in source
    assert "input_keyevent" not in source
    assert "password" not in source.casefold()
    assert "验证码" not in source
    assert "BLOCKED_ACTIONS" in source


def test_enter_session_policy_is_not_login_and_requires_session_anchor():
    spec = read_only_policy.DEFAULT_POLICY_SPECS["enter_session"]
    assert spec.allowed_page_types == frozenset({"session_ready"})
    assert spec.anchor_id == "session_entry"
    assert "login" not in spec.allowed_page_types

    page_type, markers = probe._classify_observed_items(
        [_item("下载已经完成，点击任意位置进入游戏")]
    )
    assert page_type == "session_ready"
    assert "session_ready" in markers


def test_startup_lifecycle_enters_existing_session_once_then_reaches_home():
    values = iter([
        _Frame("点击任意位置进入游戏"),
        _home(8),
        _home(9),
    ])
    calls = []
    clock = _Clock()
    result = StartupResolver(
        frame_provider=lambda: next(values),
        tap=lambda point, *, intent: calls.append((point, intent)) or True,
        sleep=clock.sleep,
        monotonic=clock,
        now=lambda: NOW,
        timeout=10.0,
        max_attempts=3,
        poll_interval=1.0,
        correlation_id="STARTUP-20260720-TEST",
    ).resolve()

    assert result.state is StartupState.HOME_READY
    assert result.status == "PASS"
    assert result.path[0:5] == (
        "STARTING",
        "LOGIN_STATE",
        "SESSION_READY",
        "ENTRY_GATE_REQUIRED",
        "ENTRY_CONFIRMING",
    )
    assert result.path[-1] == "HOME_READY"
    assert len(calls) == 1
    assert calls[0][1].action_key == "enter_session"
