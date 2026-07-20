from __future__ import annotations

from auto.resident_activity import ResidentActivityAutomation, ScreenDriver
from core.services.screen_state import ResidentHomeState, resident_home_state


def _items(*texts: str) -> list[dict]:
    return [{"text": text, "position": [[0, 0], [10, 0], [10, 10], [0, 10]]}
            for text in texts]


def test_resident_activity_import_chain():
    automation = ResidentActivityAutomation(ScreenDriver(sleep=lambda _seconds: None))
    assert isinstance(automation.driver, ScreenDriver)


def test_resident_home_ready_without_overlay():
    assert resident_home_state(_items("访问城市", "作战终端")) is ResidentHomeState.HOME_READY


def test_announcement_overlay_is_observed_without_click():
    assert resident_home_state(
        _items("公告", "触碰空白区域退出")
    ) is ResidentHomeState.ANNOUNCEMENT_OVERLAY


def test_checkin_overlay_is_observed_without_click():
    assert resident_home_state(
        _items("每日签到奖励", "触碰空白区域退出")
    ) is ResidentHomeState.CHECKIN_OVERLAY


def test_unknown_overlay_blocks_action():
    taps: list[tuple[int, int]] = []
    driver = ScreenDriver(sleep=lambda _seconds: None)
    driver.texts = lambda: _items("限时活动", "触碰空白区域退出")
    driver.tap = lambda pos, **_kwargs: taps.append(pos)
    assert driver.go_home() is False
    assert taps == []
    assert resident_home_state(driver.texts()) is ResidentHomeState.UNKNOWN_OVERLAY
