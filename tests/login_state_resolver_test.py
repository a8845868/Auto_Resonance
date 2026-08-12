from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

from core.services.login_state_resolver import (
    LoginState,
    LoginStateResolver,
    classify_login_frame,
)
from core.services.startup_overlay_resolver import StartupResolver, StartupState


NOW = datetime(2026, 7, 20, 15, 10, tzinfo=timezone(timedelta(hours=8)))


def _item(text: str) -> dict:
    return {
        "text": text,
        "position": [[500, 330], [780, 330], [780, 370], [500, 370]],
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


def _resolver(
    frames,
    *,
    timeout: float = 60.0,
    initial_observation=None,
):
    values = iter(frames)
    clock = _Clock()
    return LoginStateResolver(
        frame_provider=lambda: next(values),
        sleep=clock.sleep,
        monotonic=clock,
        now=lambda: NOW,
        timeout=timeout,
        max_attempts=len(frames) + (1 if initial_observation is not None else 0),
        poll_interval=1.0,
        correlation_id="LOGIN-20260720-TEST",
        initial_observation=initial_observation,
    )


def _home(pixel: int):
    return _Frame("访问城市", "作战终端", "启程", "整备列车", pixel=pixel)


def test_existing_session_recovers_from_validation_to_home_ready():
    result = _resolver([
        _Frame("正在验证登录状态"),
        _home(1),
        _home(2),
    ]).resolve()

    assert result.state is LoginState.HOME_READY
    assert result.status == "PASS"
    assert result.state_before is LoginState.SESSION_VALIDATING
    assert result.state_after is LoginState.HOME_READY
    assert result.irreversible_actions == 0


def test_account_password_page_blocks_for_manual_authentication():
    observation = classify_login_frame(_Frame("账号登录", "输入账号", "输入密码"))
    assert observation.state is LoginState.LOGIN_REQUIRED

    result = _resolver([_Frame("账号登录", "输入账号", "输入密码")]).resolve()
    assert result.state is LoginState.LOGIN_REQUIRED
    assert result.status == "BLOCKED"
    assert result.reason == "BLOCKED_MANUAL_AUTH_REQUIRED"


def test_verification_code_page_is_blocked_without_requesting_code():
    result = _resolver([_Frame("请输入验证码", "获取验证码")]).resolve()
    assert result.state is LoginState.LOGIN_REQUIRED
    assert result.status == "BLOCKED"
    assert result.reason == "BLOCKED_MANUAL_AUTH_REQUIRED"
    assert all(event.action == "OBSERVE_ONLY" for event in result.trace)


def test_slow_server_connection_waits_within_deadline():
    result = _resolver([
        _Frame("正在连接服务器"),
        _Frame("正在连接服务器"),
        _home(3),
        _home(4),
    ]).resolve()

    assert result.state is LoginState.HOME_READY
    assert result.status == "PASS"
    assert result.attempt_count == 4
    assert LoginState.SERVER_CONNECTING in result.path


def test_login_wait_has_deadline_and_times_out():
    result = _resolver([
        _Frame("正在验证登录状态"),
        _Frame("正在验证登录状态"),
    ], timeout=2.0).resolve()

    assert result.state is LoginState.TIMEOUT
    assert result.status == "BLOCKED"
    assert result.reason == "login_deadline_or_attempt_limit"


def test_unknown_login_page_remains_unknown():
    result = _resolver([_Frame("无法识别的启动画面")]).resolve()
    assert result.state is LoginState.UNKNOWN
    assert result.status == "UNKNOWN"
    assert result.reason == "login_state_unknown"


def test_home_ready_requires_two_consistent_semantic_frames():
    one_frame = _resolver([_home(5)]).resolve()
    assert one_frame.state is LoginState.TIMEOUT

    two_frames = _resolver([_home(6), _home(7)]).resolve()
    assert two_frames.state is LoginState.HOME_READY
    assert two_frames.status == "PASS"
    assert two_frames.reason == "home_ready_confirmed"


def test_session_ready_is_observed_but_never_clicked():
    result = _resolver([
        _Frame("下载已经完成，点击任意位置进入游戏"),
        _Frame("下载已经完成，点击任意位置进入游戏"),
    ], timeout=2.0).resolve()

    assert result.state is LoginState.TIMEOUT
    assert result.state_before is LoginState.SESSION_READY
    assert result.status == "BLOCKED"
    assert all(event.action == "OBSERVE_ONLY" for event in result.trace)


def test_explicit_login_and_server_errors_block_immediately():
    login_failed = _resolver([_Frame("登录失败", "请稍后重试")]).resolve()
    server_error = _resolver([_Frame("服务器异常", "网络错误")]).resolve()

    assert login_failed.state is LoginState.LOGIN_FAILED
    assert login_failed.status == "BLOCKED"
    assert server_error.state is LoginState.SERVER_ERROR
    assert server_error.status == "BLOCKED"


def test_result_schema_contains_live_validation_fields():
    payload = _resolver([_Frame("请输入验证码")]).resolve().to_dict()
    assert {
        "scenario",
        "state_before",
        "state_after",
        "status",
        "reason",
        "irreversible_actions",
    } <= payload.keys()
    assert payload["irreversible_actions"] == 0


def test_transient_unknown_after_known_session_waits_instead_of_downgrading():
    initial = classify_login_frame(
        _Frame("下载已经完成，点击任意位置进入游戏")
    )
    result = _resolver(
        [_Frame("过渡动画，无稳定文字"), _home(10), _home(11)],
        initial_observation=initial,
    ).resolve()

    assert result.state_before is LoginState.SESSION_READY
    assert result.state is LoginState.HOME_READY
    assert result.status == "PASS"


def test_startup_lifecycle_runs_login_then_overlay_before_home_ready():
    values = iter([
        _Frame("正在验证登录状态"),
        _home(8),
        _home(9),
    ])
    clock = _Clock()
    result = StartupResolver(
        frame_provider=lambda: next(values),
        tap=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("login recovery must not tap")
        ),
        sleep=clock.sleep,
        monotonic=clock,
        now=lambda: NOW,
        timeout=10.0,
        max_attempts=3,
    ).resolve()

    assert result.state is StartupState.HOME_READY
    assert result.status == "PASS"
    assert result.path[0:2] == ("STARTING", "LOGIN_STATE")
    assert "OVERLAY_RESOLUTION" in result.path


def test_startup_lifecycle_blocks_manual_auth_without_tapping():
    taps = []
    result = StartupResolver(
        frame_provider=lambda: _Frame("请输入账号", "请输入密码"),
        tap=lambda *args, **kwargs: taps.append((args, kwargs)),
        timeout=10.0,
        max_attempts=2,
    ).resolve()

    assert result.state is StartupState.BLOCKED
    assert result.reason == "BLOCKED_MANUAL_AUTH_REQUIRED"
    assert result.irreversible_actions == 0
    assert taps == []


def test_live_login_probe_has_no_input_or_credential_capability():
    source = Path("tools/nineteenth_login_read_only_probe.py").read_text(
        encoding="utf-8"
    )
    legacy_probe = Path("tools/sixth_read_only_probe.py").read_text(
        encoding="utf-8"
    )

    assert "input_tap" not in source
    assert "input_swipe" not in source
    assert "authorize_coordinate" not in source
    assert "password" not in source.casefold()
    assert 'if result["initial_page"] == "login":' not in legacy_probe
