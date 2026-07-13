from unittest.mock import Mock

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

    assert dispatch.collect_dispatch_rewards() is False
    screen.assert_not_called()
