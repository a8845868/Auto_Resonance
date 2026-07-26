from __future__ import annotations

from auto.resident_activity import ResidentActivityAutomation, ScreenDriver
from core.services.proven_capability_navigation import CapabilityNavigationResult
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


def _capability_result(success=True):
    return CapabilityNavigationResult(
        success=success,
        terminal=True,
        target_capability="ACTION_SUMMARY_VISIBLE",
        initial_state="HOME_READY",
        final_state="ACTION_SUMMARY_VISIBLE" if success else "HOME_READY",
        planned_edge_ids=("home_to_activity_overview",),
        completed_edge_ids=("home_to_activity_overview",) if success else (),
        failed_edge_id=None if success else "home_to_activity_overview",
        physical_dispatches=1 if success else 0,
        unknown_state_actions=0,
        irreversible_actions=0,
        reason="target_capability_reached" if success else "edge_dispatch_failed",
    )


def test_open_action_summary_defaults_to_proven_edge_planner(monkeypatch):
    observed = {}

    def fake_ensure(target, **kwargs):
        observed["target"] = target
        observed["max_steps"] = kwargs["max_steps"]
        return _capability_result()

    monkeypatch.setattr("auto.resident_activity.ensure_capability", fake_ensure)
    driver = ScreenDriver(sleep=lambda _seconds: None)
    automation = ResidentActivityAutomation(driver)

    assert automation.open_action_summary() is True
    assert observed == {"target": "ACTION_SUMMARY_VISIBLE", "max_steps": 4}
    assert automation.last_capability_navigation_result == _capability_result()


def test_legacy_action_summary_fallback_is_only_used_when_switch_is_off(monkeypatch):
    calls = []

    class FakeNavigator:
        def __init__(self, **_kwargs):
            calls.append("constructed")

        def navigate(self):
            return type("Result", (), {"success": True, "reason": "legacy"})()

    monkeypatch.setattr("auto.resident_activity.ActionSummaryNavigator", FakeNavigator)
    automation = ResidentActivityAutomation(
        ScreenDriver(sleep=lambda _seconds: None),
        use_proven_edge_planner=False,
    )

    assert automation.open_action_summary() is True
    assert calls == ["constructed"]
    assert automation.last_capability_navigation_result is None
