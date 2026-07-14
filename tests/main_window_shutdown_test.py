from types import SimpleNamespace

from app.view.main_window import MainWindow


class _ThemeListener:
    def __init__(self):
        self.calls = []

    def requestInterruption(self):
        self.calls.append("interrupt")

    def terminate(self):
        self.calls.append("terminate")

    def wait(self, timeout):
        self.calls.append(("wait", timeout))
        return True

    def deleteLater(self):
        self.calls.append("delete")


def test_theme_listener_is_joined_before_qobject_deletion():
    listener = _ThemeListener()
    window = SimpleNamespace(themeListener=listener)

    MainWindow._stopThemeListener(window)

    assert listener.calls == [
        "interrupt",
        "terminate",
        ("wait", 1000),
        "delete",
    ]
