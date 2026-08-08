"""F-09 slice 1: BlockedBySafetyError taxonomy and queue semantics.

Frozen contract (AUTO_RESONANCE_BLOCKED_SAFETY_OUTCOME_TAXONOMY_V1):
  KNOWN_SAFETY_BLOCK  -> TASK_TERMINAL_BLOCKED, QUEUE_FATAL=NO,
                         SELF_HEALING_ELIGIBLE=NO, AUTOMATIC_RETRY=NO
  UNKNOWN_PROGRAMMING_EXCEPTION -> FATAL_BEHAVIOR_UNCHANGED
"""

from unittest.mock import patch

import pytest

import auto.fatigue_recovery as fatigue_recovery
import auto.furniture_inventory as furniture_inventory
import auto.gacha_resources as gacha_resources
import auto.inventory as inventory
import auto.module.dispatch as dispatch
import auto.passenger_carriage_build as passenger_carriage_build
import auto.shop_purchase as shop_purchase
from app.utils.task_queue import QueuedTask, TaskQueueWorker
from core.services.runtime_errors import (
    BlockedBySafetyError,
    RecoverableAutomationError,
    classify_runtime_error,
)
from core.services.task_schedule_state import TaskOutcome, task_result_outcome


def test_blocked_safety_error_is_terminal_blocked_not_fatal():
    body_runs = {"blocked": 0}
    executed = []
    incidents = []
    results = []
    completions = []

    def blocked_task():
        body_runs["blocked"] += 1
        raise BlockedBySafetyError("疲劳规划无法连接模拟器")

    worker = TaskQueueWorker(
        [
            QueuedTask(
                "blocked", blocked_task,
                recoverable_retries=2, retry_backoff_seconds=0,
            ),
            QueuedTask("next", lambda: executed.append("next") or {"success": True}),
        ],
        incident_reporter=incidents.append,
        retry_sleep=lambda _seconds: None,
    )
    worker.taskResult.connect(lambda name, result: results.append((name, result)))
    worker.taskCompleted.connect(
        lambda task, succeeded, result: completions.append(
            (task.name, succeeded, result)
        )
    )
    worker.run()

    blocked_results = [result for name, result in results if name == "blocked"]
    assert len(blocked_results) == 1
    result = blocked_results[0]
    # taskResult emitted with the structured BLOCKED_SAFETY outcome.
    assert result["task_outcome"] == "BLOCKED_SAFETY"
    assert result["success"] is False and result["terminal"] is True
    assert result["incident_eligible"] is False
    assert result["halt_eligible"] is False
    assert result["queue_automatic_retry"] is False
    assert result["queue_retry_count"] == 0
    # Dashboard-side classification reuses the existing outcome helper.
    assert task_result_outcome(result) is TaskOutcome.BLOCKED_SAFETY
    # taskCompleted receives the SAME result object with succeeded=False.
    assert completions[0][0] == "blocked"
    assert completions[0][1] is False
    assert completions[0][2] is result
    # Never queue-fatal, never self-healing eligible.
    assert worker.fatal_error is None
    assert worker.halted_for_repair is False
    assert incidents == []
    # The following task still executes; the blocked body ran exactly once.
    assert executed == ["next"]
    assert body_runs["blocked"] == 1


def test_blocked_safety_error_does_not_halt_even_with_self_healing_mode():
    executed = []
    incidents = []

    def blocked_task():
        raise BlockedBySafetyError("页面前置条件不满足")

    worker = TaskQueueWorker(
        [
            QueuedTask("blocked", blocked_task),
            QueuedTask("next", lambda: executed.append("next") or {"success": True}),
        ],
        incident_reporter=incidents.append,
        halt_on_failure=True,
    )
    worker.run()

    assert worker.halted_for_repair is False
    assert worker.fatal_error is None
    assert incidents == []
    assert executed == ["next"]


def test_unknown_runtime_error_stays_fatal_with_incident_and_halt():
    executed = []
    incidents = []

    def boom():
        raise RuntimeError("boom")

    worker = TaskQueueWorker(
        [
            QueuedTask("boom", boom),
            QueuedTask("next", lambda: executed.append("next")),
        ],
        incident_reporter=incidents.append,
    )
    worker.run()

    assert worker.fatal_error is not None
    assert worker.halted_for_repair is True
    assert len(incidents) == 1
    assert incidents[0]["failure_kind"] == "fatal_automation_error"
    assert executed == []


def test_recoverable_retry_loop_is_unchanged():
    attempts = {"count": 0}

    def flaky():
        attempts["count"] += 1
        if attempts["count"] < 2:
            raise RecoverableAutomationError("transient")
        return {"success": True}

    worker = TaskQueueWorker(
        [QueuedTask("flaky", flaky, recoverable_retries=2, retry_backoff_seconds=0)],
        retry_sleep=lambda _seconds: None,
    )
    worker.run()

    assert attempts["count"] == 2
    assert worker.fatal_error is None
    assert worker.halted_for_repair is False


def test_classifier_returns_blocked_safety_error_unchanged():
    error = BlockedBySafetyError("疲劳规划未能确认当前站点")
    assert classify_runtime_error(error) is error


def test_fatigue_connect_failure_raises_blocked_safety_error():
    with patch.object(fatigue_recovery, "connect", return_value=False):
        with pytest.raises(BlockedBySafetyError):
            fatigue_recovery._run_daily_fatigue_recovery_impl()


def test_dispatch_home_failure_raises_blocked_safety_error():
    with patch.object(dispatch, "go_home", return_value=False):
        with pytest.raises(BlockedBySafetyError):
            dispatch.collect_dispatch_rewards()


def test_reward_and_resident_connect_sites_raise_blocked_safety_error():
    import auto.resident_activity as resident_activity
    import auto.reward_collection as reward_collection

    with patch.object(reward_collection, "connect", return_value=False):
        with pytest.raises(BlockedBySafetyError):
            reward_collection.RewardCollector(driver=object()).run(False, False)

    automation = object.__new__(resident_activity.ResidentActivityAutomation)
    with patch.object(resident_activity, "connect_resonance", return_value=False):
        with pytest.raises(BlockedBySafetyError):
            resident_activity.ResidentActivityAutomation._run_interlocked(automation)


def test_shop_timeout_site_raises_blocked_safety_error():
    with pytest.raises(BlockedBySafetyError):
        shop_purchase._wait_for_text(("商店",), timeout=0)


def test_passenger_game_timeout_site_raises_blocked_safety_error():
    with patch.object(passenger_carriage_build, "_wait_for_game", return_value=False):
        with pytest.raises(BlockedBySafetyError):
            passenger_carriage_build.scan_passenger_build_inventory()


def test_gacha_connect_site_raises_blocked_safety_error():
    with patch.object(gacha_resources, "connect", return_value=False):
        with pytest.raises(BlockedBySafetyError):
            gacha_resources.scan_gacha_resources()


def test_furniture_connect_site_raises_blocked_safety_error():
    with patch.object(furniture_inventory, "connect", return_value=False):
        with pytest.raises(BlockedBySafetyError):
            furniture_inventory.scan_furniture_inventory()


def test_inventory_connect_site_raises_blocked_safety_error():
    with patch.object(inventory, "connect", return_value=False):
        with pytest.raises(BlockedBySafetyError):
            inventory.scan_inventory_assets()


def test_debug_task_connect_site_raises_blocked_safety_error():
    with patch("core.control.control.connect", return_value=False):
        from core.services import debug_tasks

        with pytest.raises(BlockedBySafetyError):
            debug_tasks._screen_state()


def test_debug_runner_translates_blocked_safety_error_without_incident(monkeypatch):
    from types import SimpleNamespace

    from app.common.config import cfg
    import debug_runner
    from core.services import debug_tasks

    task = SimpleNamespace(
        key="screen",
        name="只读画面识别",
        run=lambda: (_ for _ in ()).throw(BlockedBySafetyError("无法连接模拟器")),
    )
    lease = SimpleNamespace(pid=1234, stop_requested=lambda: False)
    incidents = []
    monkeypatch.setattr(cfg.enableAutoGameLifecycle, "value", False)
    with (
        patch.object(debug_tasks, "resolve_task", return_value=task),
        patch.object(debug_runner, "write_debug_status", return_value=None),
        patch.object(debug_runner, "read_debug_status", return_value={}),
    ):
        response = debug_runner._run_debug_task(
            lease,
            {"id": "blocked-screen", "task": "screen"},
            incident_reporter=incidents.append,
        )

    assert response["success"] is False
    assert response["result"]["task_outcome"] == "BLOCKED_SAFETY"
    assert response["result"]["queue_automatic_retry"] is False
    assert response["result"]["queue_retry_count"] == 0
    assert response["error"] == "任务按页面安全门禁停止"
    assert incidents == []


def test_slice_2_keep_fatal_sites_remain_plain_runtime_errors():
    from core.services import debug_tasks

    with (
        patch.dict(shop_purchase.ADAPTERS, {}, clear=True),
        patch.object(shop_purchase, "_connected_run", side_effect=lambda run: run()),
    ):
        with pytest.raises(RuntimeError) as shop_error:
            shop_purchase.probe_shop_quantity_dialog(capture_evidence=False)
    assert type(shop_error.value) is RuntimeError

    with patch(
        "core.services.weekly_plan_state.load_weekly_plan", return_value=None
    ):
        with pytest.raises(RuntimeError) as debug_error:
            debug_tasks._run_business()
    assert type(debug_error.value) is RuntimeError
