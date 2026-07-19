from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import Mock

from app.utils.task_queue import QueuedTask, TaskQueueWorker
from auto.exchange_navigation import (
    ExchangeAction,
    ExchangeNavigator,
    exchange_page_matches,
    open_exchange_action,
)
from core.services.fatigue_triggers import (
    FatigueActionState,
    acknowledge_fatigue_checkpoint,
    cancel_deferred_fatigue_actions,
    claim_fatigue_checkpoint,
    fail_fatigue_checkpoint,
    fatigue_checkpoint_deferral,
    list_fatigue_actions,
    notify_fatigue_event,
    register_deferred_fatigue_actions,
)


def _ocr(text: str, x: int, y: int) -> dict:
    return {
        "text": text,
        "position": [[x - 10, y - 10], [x + 10, y - 10], [x + 10, y + 10], [x - 10, y + 10]],
    }


def _lobby() -> list[dict]:
    return [
        _ocr("交易所", 960, 176),
        _ocr("我要买", 805, 324),
        _ocr("我要卖", 804, 407),
        _ocr("交易品投资", 820, 489),
        _ocr("私人仓库", 814, 572),
    ]


def _buy() -> list[dict]:
    return [_ocr("交易品", 200, 100), _ocr("预计买入", 900, 500), _ocr("买入总价(含税)", 900, 550), _ocr("载货量", 600, 650)]


def _sell() -> list[dict]:
    return [_ocr("交易品", 200, 100), _ocr("预计卖出", 900, 500), _ocr("卖出总价(含税)", 900, 550), _ocr("载货量", 600, 650)]


def _checkpoint(tmp_path: Path, *, waypoint: str = "B") -> Path:
    path = tmp_path / "fatigue.json"
    register_deferred_fatigue_actions(
        [{"kind": "REOBSERVE_RECOVERY_AT_WAYPOINT", "waypoint_id": waypoint}],
        plan_revision="rev-6",
        path=path,
    )
    return path


def test_default_waypoint_schedule_pauses_before_next_leg(tmp_path, monkeypatch):
    path = _checkpoint(tmp_path)
    scheduled = Mock()
    monkeypatch.setattr("core.services.fatigue_triggers.set_next_run", scheduled)
    assert notify_fatigue_event("arrival", "B", path=path)
    outcome = fatigue_checkpoint_deferral("B", path=path)
    assert outcome["deferred"] is True
    assert outcome["reason"] == "fatigue_checkpoint_pending"
    scheduled.assert_called_once()


def test_waypoint_action_is_not_acknowledged_by_set_next_run_only(tmp_path):
    path = _checkpoint(tmp_path)
    notify_fatigue_event("arrival", "B", path=path, schedule=Mock())
    assert list_fatigue_actions(path=path)[0]["state"] == FatigueActionState.SCHEDULED.value


def test_fatigue_checkpoint_ack_allows_next_leg_resume(tmp_path):
    path = _checkpoint(tmp_path)
    notify_fatigue_event("arrival", "B", path=path, schedule=Mock())
    action = claim_fatigue_checkpoint(expected_waypoint="B", path=path)
    acknowledge_fatigue_checkpoint(
        action["id"], owner_id=action["owner_id"],
        lease_token=action["lease_token"], path=path,
    )
    assert fatigue_checkpoint_deferral("B", path=path) is None


def test_failed_checkpoint_remains_retryable_and_does_not_depart(tmp_path):
    path = _checkpoint(tmp_path)
    notify_fatigue_event("arrival", "B", path=path, schedule=Mock())
    action = claim_fatigue_checkpoint(expected_waypoint="B", path=path)
    fail_fatigue_checkpoint(
        action["id"], "ocr_unstable", owner_id=action["owner_id"],
        lease_token=action["lease_token"], path=path,
    )
    assert list_fatigue_actions(path=path)[0]["state"] == FatigueActionState.FAILED_RETRYABLE.value
    assert fatigue_checkpoint_deferral("B", path=path)["deferred"] is True


def test_checkpoint_restart_recovery_preserves_waypoint_and_revision(tmp_path):
    path = _checkpoint(tmp_path, waypoint="岚心城")
    notify_fatigue_event("arrival", "岚心城", path=path, schedule=Mock())
    restored = list_fatigue_actions(path=path)[0]
    assert restored["waypoint_id"] == "岚心城"
    assert restored["plan_revision"] == "rev-6"


def test_business_and_fatigue_never_control_screen_concurrently():
    sequence: list[str] = []
    worker = TaskQueueWorker([
        QueuedTask("跑商", lambda: sequence.extend(["business:start", "business:end"]) or True),
        QueuedTask("疲劳", lambda: sequence.extend(["fatigue:start", "fatigue:end"]) or True),
    ])
    worker.run()
    assert sequence == ["business:start", "business:end", "fatigue:start", "fatigue:end"]


def test_stop_all_cancels_checkpoint_without_marking_success(tmp_path):
    path = _checkpoint(tmp_path)
    notify_fatigue_event("arrival", "B", path=path, schedule=Mock())
    cancel_deferred_fatigue_actions(path=path)
    action = list_fatigue_actions(path=path)[0]
    assert action["state"] == FatigueActionState.CANCELLED.value
    assert action.get("acknowledged_at") is None


def test_exchange_buy_clicks_current_ocr_anchor_not_legacy_coordinate():
    frames = iter([_lobby(), _buy(), _buy()])
    taps: list[tuple[int, int]] = []
    result = ExchangeNavigator(lambda: next(frames), taps.append, lambda _s: None).open(ExchangeAction.BUY)
    assert result.success
    assert taps == [(805, 324)]
    assert taps[0] != (927, 321)


def test_exchange_sell_clicks_current_ocr_anchor():
    frames = iter([_lobby(), _sell(), _sell()])
    taps: list[tuple[int, int]] = []
    result = ExchangeNavigator(lambda: next(frames), taps.append, lambda _s: None).open(ExchangeAction.SELL)
    assert result.success
    assert taps == [(804, 407)]


def test_exchange_menu_is_not_misclassified_as_buy_page():
    assert exchange_page_matches(_lobby(), ExchangeAction.BUY) is False


def test_buy_page_can_retain_exchange_bottom_tabs():
    assert exchange_page_matches(_buy() + _lobby(), ExchangeAction.BUY) is True


def test_buy_navigation_requires_multiframe_postcondition():
    frames = iter([_lobby(), _buy(), *([_lobby()] * 8)])
    result = ExchangeNavigator(lambda: next(frames), Mock(), lambda _s: None).open(ExchangeAction.BUY)
    assert result.success is False
    assert result.reason == "postcondition_unstable"


def test_fatigue_and_business_share_exchange_navigation():
    import auto.fatigue_recovery as fatigue
    import auto.run_business.main as business

    assert fatigue.exchange_navigation is business.exchange_navigation


def test_navigation_failure_performs_no_irreversible_click():
    taps: list[tuple[int, int]] = []
    result = ExchangeNavigator(lambda: [_ocr("交谈", 800, 650)], taps.append, lambda _s: None).open(ExchangeAction.BUY)
    assert result.success is False
    assert taps == []


def test_exchange_wrapper_waits_for_late_dialogue_menu(monkeypatch):
    import auto.exchange_navigation as navigation

    frames = iter([
        [_ocr("访问城市", 1100, 480)],
        [_ocr("交谈", 800, 650)],
        _lobby(),
        [_ocr("载入中", 640, 360)],
        _buy(),
        _buy(),
    ])
    taps: list[tuple[int, int]] = []
    monkeypatch.setattr(navigation, "screenshot", lambda: next(frames))
    monkeypatch.setattr(navigation, "input_tap", lambda pos, **_semantic: taps.append(pos))
    monkeypatch.setattr(navigation.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr("core.preset.control.go_home", lambda: True)
    monkeypatch.setattr("core.preset.go_outlets", lambda _name: True)

    result = open_exchange_action(ExchangeAction.BUY, read_only=True)

    assert result.success is True
    assert taps == [(805, 324)]


def test_current_fifth_pass_exchange_evidence_replays_safely():
    path = Path("dist/audit_output/AutoResonance-Pro-Fifth-Audit-20260718/evidence/exchange-navigation-failure/exchange-navigation-failure-manifest.json")
    frames = json.loads(path.read_text(encoding="utf-8"))
    taps: list[tuple[int, int]] = []
    result = ExchangeNavigator(lambda: frames[0]["ocr"], taps.append, lambda _s: None).open(ExchangeAction.BUY, verify_frames=0)
    assert result.success is False
    assert taps == [(806, 324)]
