from types import SimpleNamespace

from app.view.main_window import MainWindow


class _Tray:
    def __init__(self):
        self.shown = False

    def show(self):
        self.shown = True


def test_minimize_to_tray_hides_window_and_keeps_tray_visible():
    calls = []
    tray = _Tray()
    window = SimpleNamespace(
        trayIcon=tray,
        _systemTrayAvailable=lambda: True,
        hide=lambda: calls.append("hide"),
        showMinimized=lambda: calls.append("minimize"),
    )

    MainWindow.minimizeToTray(window)

    assert tray.shown is True
    assert calls == ["hide"]


def test_minimize_uses_normal_window_when_system_tray_is_unavailable():
    calls = []
    window = SimpleNamespace(
        _systemTrayAvailable=lambda: False,
        showMinimized=lambda: calls.append("minimize"),
    )

    MainWindow.minimizeToTray(window)

    assert calls == ["minimize"]
