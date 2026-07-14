from unittest.mock import Mock

import pytest

import auto.module.dispatch as dispatch


def test_dispatch_check_returns_home_before_reading_reminder(monkeypatch):
    calls = []
    frame = Mock()
    frame.ocr.return_value = []
    monkeypatch.setattr(dispatch, "go_home", lambda: calls.append("home") or True)
    monkeypatch.setattr(dispatch, "screenshot", lambda: calls.append("screen") or frame)

    assert dispatch.collect_dispatch_rewards() is False
    assert calls == ["home", "screen"]


def test_dispatch_check_stops_when_home_cannot_be_confirmed(monkeypatch):
    screen = Mock()
    monkeypatch.setattr(dispatch, "go_home", lambda: False)
    monkeypatch.setattr(dispatch, "screenshot", screen)

    with pytest.raises(RuntimeError, match="无法返回主界面"):
        dispatch.collect_dispatch_rewards()
    screen.assert_not_called()


def test_dispatch_check_raises_when_reminder_opens_but_claim_button_is_missing(
    monkeypatch,
):
    frame = Mock()
    frame.ocr.return_value = [{"text": "委派奖励可收取"}]
    home_calls = []
    monkeypatch.setattr(
        dispatch,
        "go_home",
        lambda: home_calls.append("home") or True,
    )
    monkeypatch.setattr(dispatch, "screenshot", lambda: frame)
    monkeypatch.setattr(
        dispatch,
        "blurry_ocr_click",
        Mock(side_effect=[True, False]),
    )
    monkeypatch.setattr(dispatch.time, "sleep", lambda _seconds: None)

    with pytest.raises(RuntimeError, match="没有识别到领取奖励按钮"):
        dispatch.collect_dispatch_rewards()

    assert home_calls == ["home", "home"]
