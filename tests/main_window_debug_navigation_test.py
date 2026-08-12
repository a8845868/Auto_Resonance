from types import SimpleNamespace

from app.view.main_window import MainWindow


class _Navigation:
    def __init__(self):
        self.items = []

    def addItem(self, **kwargs):
        self.items.append(kwargs)
        return object()


def _window_stub():
    pages = {
        name: object()
        for name in (
            "homeInterface",
            "debugInterface",
            "residentActivityInterface",
            "rewardCollectionInterface",
            "fatiguePlannerInterface",
            "bookPlannerInterface",
            "inventoryInterface",
            "gachaPlannerInterface",
            "passengerPlannerInterface",
            "shopPlannerInterface",
            "two_run_business_interface",
            "adb_data_interface",
            "settingInterface",
        )
    }
    entries = []
    window = SimpleNamespace(
        **pages,
        navigationInterface=_Navigation(),
        _update=lambda: None,
    )
    window.addSubInterface = lambda interface, icon, text, **kwargs: entries.append(
        (interface, icon, text, kwargs)
    )
    return window, entries


def test_debug_page_is_a_left_navigation_entry_next_to_home():
    window, entries = _window_stub()

    MainWindow.initNavigation(window)

    assert entries[0][0] is window.homeInterface
    assert entries[0][2] == "主页"
    assert entries[1][0] is window.debugInterface
    assert entries[1][2] == "调试"


def test_close_stops_debug_refresh_even_while_queue_is_finishing():
    calls = []

    class _Event:
        def __init__(self):
            self.ignored = False

        def ignore(self):
            self.ignored = True

    event = _Event()
    window = SimpleNamespace(
        homeInterface=SimpleNamespace(shutdown=lambda: False),
        adb_data_interface=SimpleNamespace(shutdown=lambda: True),
        debugInterface=SimpleNamespace(shutdown=lambda: calls.append("debug")),
        _closeRetryScheduled=True,
    )

    MainWindow.closeEvent(window, event)

    assert calls == ["debug"]
    assert event.ignored is True
