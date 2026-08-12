from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

from core.services.session_entry_chain import EntryChainResolver
from core.services.session_entry_resolver import SessionEntryState
from core.services.startup_overlay_resolver import StartupResolver, StartupState


NOW = datetime(2026, 7, 20, 16, 20, tzinfo=timezone(timedelta(hours=8)))


def _item(text: str, x: int = 640, y: int = 560) -> dict:
    return {
        "text": text,
        "position": [[x - 170, y - 18], [x + 170, y - 18],
                     [x + 170, y + 18], [x - 170, y + 18]],
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


def _gate(text: str, pixel: int) -> _Frame:
    return _Frame(text, pixel=pixel)


def _home(pixel: int) -> _Frame:
    return _Frame("访问城市", "作战终端", "启程", "整备列车", pixel=pixel)


def _resolver(frames, *, tap=lambda *_args, **_kwargs: True, max_steps=3):
    values = iter(frames)
    clock = _Clock()
    return EntryChainResolver(
        frame_provider=lambda: next(values),
        tap=tap,
        sleep=clock.sleep,
        monotonic=clock,
        now=lambda: NOW,
        timeout=20.0,
        max_attempts=len(frames),
        max_entry_steps=max_steps,
        poll_interval=1.0,
        correlation_id="CHAIN-20260720-TEST",
    )


def test_single_entry_gate_reaches_home():
    calls = []
    result = _resolver([
        _gate("点击任意位置进入游戏", 1),
        _home(2),
        _home(3),
    ], tap=lambda point, *, intent: calls.append((point, intent)) or True).resolve()

    assert result.state is SessionEntryState.HOME_READY
    assert result.status == "PASS"
    assert result.entry_steps == 1
    assert len(calls) == 1


def test_two_distinct_entry_gates_are_allowed_then_home():
    calls = []
    result = _resolver([
        _gate("下载已经完成，点击任意位置进入游戏", 1),
        _gate("点击屏幕进入游戏", 2),
        _home(3),
        _home(4),
    ], tap=lambda point, *, intent: calls.append((point, intent)) or True).resolve()

    assert result.state is SessionEntryState.HOME_READY
    assert result.entry_steps == 2
    assert len(calls) == 2


def test_three_distinct_entry_gates_are_allowed():
    calls = []
    result = _resolver([
        _gate("点击任意位置进入游戏", 1),
        _gate("继续游戏", 2),
        _gate("开始游戏", 3),
        _home(4),
        _home(5),
    ], tap=lambda point, *, intent: calls.append((point, intent)) or True).resolve()

    assert result.state is SessionEntryState.HOME_READY
    assert result.entry_steps == 3
    assert len(calls) == 3


def test_fourth_entry_gate_reaches_chain_limit_without_fourth_click():
    calls = []
    result = _resolver([
        _gate("下载已经完成，点击任意位置进入游戏", 1),
        _gate("点击屏幕进入游戏", 2),
        _gate("继续游戏", 3),
        _gate("开始游戏", 4),
    ], tap=lambda point, *, intent: calls.append((point, intent)) or True).resolve()

    assert result.state is SessionEntryState.ENTRY_FAILED
    assert result.status == "FAILED"
    assert result.reason == "ENTRY_CHAIN_LIMIT_REACHED"
    assert result.entry_steps == 3
    assert len(calls) == 3


def test_login_page_is_blocked_without_click():
    calls = []
    result = _resolver(
        [_Frame("账号登录", "请输入密码")],
        tap=lambda *args, **kwargs: calls.append((args, kwargs)),
    ).resolve()
    assert result.state is SessionEntryState.BLOCKED
    assert result.status == "BLOCKED"
    assert calls == []


def test_claim_page_is_blocked_without_click():
    result = _resolver([
        _Frame("点击屏幕进入游戏", "领取奖励")
    ]).resolve()
    assert result.state is SessionEntryState.BLOCKED
    assert result.reason == "IRREVERSIBLE_CONTROL_PRESENT"


def test_unknown_page_stops_chain_without_click():
    result = _resolver([_Frame("无法识别的启动页")]).resolve()
    assert result.state is SessionEntryState.UNKNOWN
    assert result.status == "UNKNOWN"
    assert result.entry_steps == 0


def test_failed_guarded_click_stops_chain():
    calls = []
    result = _resolver([
        _gate("点击屏幕进入游戏", 1)
    ], tap=lambda point, *, intent: calls.append((point, intent)) or False).resolve()
    assert result.state is SessionEntryState.ENTRY_FAILED
    assert result.status == "FAILED"
    assert result.reason == "ENTRY_CLICK_FAILED"
    assert len(calls) == 1


def test_same_captured_entry_gate_is_never_clicked_twice():
    calls = []
    repeated = _gate("点击屏幕进入游戏", 1)
    result = _resolver([
        repeated,
        repeated,
        repeated,
    ], tap=lambda point, *, intent: calls.append((point, intent)) or True).resolve()
    assert result.state is SessionEntryState.ENTRY_FAILED
    assert result.reason == "ENTRY_GATE_REPEATED"
    assert len(calls) == 1


def test_same_entry_text_on_changed_capture_is_next_bounded_step():
    calls = []
    result = _resolver([
        _gate("点击屏幕进入游戏", 1),
        _gate("点击屏幕进入游戏", 2),
        _gate("点击屏幕进入游戏", 2),
        _home(3),
        _home(4),
    ], tap=lambda point, *, intent: calls.append((point, intent)) or True).resolve()

    assert result.state is SessionEntryState.HOME_READY
    assert result.entry_steps == 2
    assert len(calls) == 2


def test_transient_same_entry_frame_waits_for_home_without_second_click():
    calls = []
    result = _resolver([
        _gate("点击屏幕进入游戏", 1),
        _gate("点击屏幕进入游戏", 2),
        _home(3),
        _home(4),
    ], tap=lambda point, *, intent: calls.append((point, intent)) or True).resolve()

    assert result.state is SessionEntryState.HOME_READY
    assert SessionEntryState.ENTRY_TRANSITION.value in result.path
    assert result.entry_steps == 1
    assert len(calls) == 1


def test_startup_lifecycle_follows_two_distinct_entry_gates_then_home():
    frames = iter([
        _gate("\u4e0b\u8f7d\u5df2\u7ecf\u5b8c\u6210\uff0c\u70b9\u51fb\u4efb\u610f\u4f4d\u7f6e\u8fdb\u5165\u6e38\u620f", 1),
        _gate("\u70b9\u51fb\u5c4f\u5e55\u8fdb\u5165\u6e38\u620f", 2),
        _home(3),
        _home(4),
    ])
    calls = []
    clock = _Clock()
    result = StartupResolver(
        frame_provider=lambda: next(frames),
        tap=lambda point, *, intent: calls.append((point, intent)) or True,
        sleep=clock.sleep,
        monotonic=clock,
        now=lambda: NOW,
        timeout=20.0,
        max_attempts=4,
        poll_interval=1.0,
        correlation_id="STARTUP-20260720-CHAIN-TEST",
    ).resolve()

    assert result.state is StartupState.HOME_READY
    assert result.status == "PASS"
    assert result.path.count(SessionEntryState.ENTRY_GATE_REQUIRED.value) == 2
    assert result.path[-1] == StartupState.HOME_READY.value
    assert [intent.action_key for _, intent in calls] == [
        "enter_session",
        "enter_session",
    ]


def test_live_chain_probe_has_no_credential_input_capability():
    source = Path("tools/twentyfirst_session_entry_chain_probe.py").read_text(
        encoding="utf-8"
    )
    assert "input_text" not in source
    assert "input_keyevent" not in source
    assert "password" not in source.casefold()
    assert "验证码" not in source
    assert "max_entry_steps: int = 3" in source
    assert '"--max-entry-steps", type=int, default=3' in source
