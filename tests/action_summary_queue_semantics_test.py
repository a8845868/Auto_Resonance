from __future__ import annotations

from datetime import datetime, timedelta
import os
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from app.utils.task_queue import QueuedTask, TaskQueueWorker
import app.view.dashboard_interface as dashboard_module
from app.view.dashboard_interface import (
    ACTION_SUMMARY_LEGACY_TASK_NAME,
    ACTION_SUMMARY_READ_ONLY_TASK_NAME,
    DashboardInterface,
    action_summary_execution_status,
    build_resident_activity_task,
)
from core.services.action_summary_execution_interlock import (
    ActionSummaryExecutionMode,
)
from core.services.debug_tasks import task_registry
from core.services.task_schedule_state import (
    TaskOutcome,
    record_task_execution,
    task_result_business_progress_made,
    task_result_deferred,
    task_result_halt_eligible,
    task_result_incident_eligible,
    task_result_outcome,
    task_result_progress_made,
    task_result_succeeded,
    task_timing,
)


def policy_result() -> dict[str, object]:
    return {
        "success": False,
        "terminal": True,
        "execution_status": "BLOCKED",
        "reason": "business_policy_required",
        "decision": {"decision": "TASK_AVAILABLE_NEEDS_POLICY"},
        "task_outcome": "DEFERRED_EXPECTED",
        "task_terminal": True,
        "task_deferred": True,
        "progress_made": False,
        "business_progress_made": False,
        "next_run_reason": "business_policy_required",
        "incident_eligible": False,
        "halt_eligible": False,
    }


def no_action_result() -> dict[str, object]:
    return {
        "success": True,
        "terminal": True,
        "execution_status": "READ_ONLY_COMPLETE",
        "reason": "read_only_complete",
        "decision": {"decision": "NO_ACTION_REQUIRED"},
        "task_outcome": "COMPLETED_NO_PROGRESS",
        "task_terminal": True,
        "task_deferred": False,
        "progress_made": False,
        "business_progress_made": False,
        "next_run_reason": "",
        "incident_eligible": False,
        "halt_eligible": False,
    }


def test_policy_required_is_expected_deferral_not_runtime_failure():
    result = policy_result()

    assert task_result_outcome(result) is TaskOutcome.DEFERRED_EXPECTED
    assert task_result_succeeded(result) is True
    assert task_result_deferred(result) is True
    assert task_result_incident_eligible(result) is False
    assert task_result_halt_eligible(result) is False
    assert task_result_progress_made(result) is False


def test_malformed_runtime_result_cannot_suppress_incident_or_halt():
    malformed = {
        "success": False,
        "task_outcome": "NOT_A_REAL_OUTCOME",
        "incident_eligible": False,
        "halt_eligible": False,
    }

    assert task_result_outcome(malformed) is TaskOutcome.FAILED_RUNTIME
    assert task_result_incident_eligible(malformed) is True
    assert task_result_halt_eligible(malformed) is True


def test_policy_required_continues_queue_without_incident_or_halt():
    ran = []
    incidents = []
    completions = []
    worker = TaskQueueWorker(
        [
            QueuedTask(ACTION_SUMMARY_READ_ONLY_TASK_NAME, policy_result),
            QueuedTask("after", lambda: ran.append("after") or True),
        ],
        incident_reporter=incidents.append,
        halt_on_failure=True,
    )
    worker.taskCompleted.connect(
        lambda task, succeeded, result: completions.append(
            (task.name, succeeded, result)
        )
    )

    worker.run()

    assert ran == ["after"]
    assert incidents == []
    assert completions[0][1] is True
    assert worker.halted_for_repair is False
    assert worker.queue_state == "COMPLETED"


def test_safety_block_is_distinct_and_does_not_self_heal_or_halt():
    safety = {
        "success": False,
        "terminal": True,
        "task_outcome": "BLOCKED_SAFETY",
        "incident_eligible": False,
        "halt_eligible": False,
    }
    ran = []
    incidents = []
    results = []
    worker = TaskQueueWorker(
        [
            QueuedTask("safe stop", lambda: safety),
            QueuedTask("after", lambda: ran.append("after") or True),
        ],
        incident_reporter=incidents.append,
        halt_on_failure=True,
    )
    worker.taskResult.connect(lambda _name, result: results.append(result))

    worker.run()

    assert ran == ["after"]
    assert incidents == []
    assert results[0] == safety
    assert worker.halted_for_repair is False
    assert worker.queue_state == "FAILED"


def test_history_records_policy_wait_as_deferred_with_reason(tmp_path):
    path = tmp_path / "schedule.json"
    now = datetime(2026, 7, 26, 12, 0, 0)
    retry = now + timedelta(minutes=10)

    entry = record_task_execution(
        "resident_activity",
        ACTION_SUMMARY_READ_ONLY_TASK_NAME,
        True,
        retry,
        policy_result(),
        now,
        path,
        deferred=True,
    )

    assert entry["status"] == "deferred"
    assert entry["name"] == ACTION_SUMMARY_READ_ONLY_TASK_NAME
    assert entry["next_run_reason"] == "business_policy_required"
    assert entry["completed_at"] == ""
    assert entry["progress_at"] == ""


def test_no_action_required_completes_without_business_progress(tmp_path):
    path = tmp_path / "schedule.json"
    now = datetime(2026, 7, 26, 12, 0, 0)
    result = no_action_result()

    entry = record_task_execution(
        "resident_activity",
        ACTION_SUMMARY_READ_ONLY_TASK_NAME,
        True,
        now + timedelta(days=1),
        result,
        now,
        path,
    )

    assert task_result_outcome(result) is TaskOutcome.COMPLETED_NO_PROGRESS
    assert task_result_progress_made(result) is False
    assert entry["status"] == "completed"
    assert entry["progress_at"] == ""
    assert task_timing("resident_activity", path)["result"] == result


def test_only_explicit_completed_progress_can_trigger_reward_recheck():
    assert task_result_progress_made(True) is False
    assert task_result_progress_made({"success": True}) is False
    assert task_result_progress_made({"success": True, "arbitrary": 9}) is False
    assert task_result_progress_made({"success": True, "success_count": 3}) is False
    assert task_result_progress_made({
        "success": True,
        "progress_made": True,
    }) is True
    assert task_result_progress_made({
        "success": False,
        "task_outcome": "DEFERRED_EXPECTED",
        "progress_made": True,
    }) is True
    assert task_result_business_progress_made({
        "success": False,
        "task_outcome": "DEFERRED_EXPECTED",
        "progress_made": True,
    }) is False


def test_default_and_explicit_legacy_queue_names_are_truthful():
    with patch(
        "app.view.dashboard_interface.run_resident_activity",
        return_value=policy_result(),
    ) as public_entry:
        default = build_resident_activity_task("特殊订单", "学会装备箱")
        legacy = build_resident_activity_task(
            "特殊订单",
            "学会装备箱",
            execution_mode=ActionSummaryExecutionMode.LEGACY_COMPATIBILITY,
        )
        default.run()
        legacy.run()

    assert default.name == ACTION_SUMMARY_READ_ONLY_TASK_NAME
    assert legacy.name == ACTION_SUMMARY_LEGACY_TASK_NAME
    assert public_entry.call_args_list[0].kwargs["execution_mode"] is (
        ActionSummaryExecutionMode.READ_ONLY
    )
    assert public_entry.call_args_list[1].kwargs["execution_mode"] is (
        ActionSummaryExecutionMode.LEGACY_COMPATIBILITY
    )


def test_headless_registry_uses_read_only_product_name():
    assert task_registry()["resident_activity"].name == (
        ACTION_SUMMARY_READ_ONLY_TASK_NAME
    )


class _Signal:
    def __init__(self):
        self.values = []

    def emit(self, *values):
        self.values.append(values)


def test_dashboard_policy_result_has_one_truthful_terminal_message():
    signal = _Signal()
    dashboard = SimpleNamespace(
        activityStateChanged=signal,
        queueWorker=SimpleNamespace(stop_requested=False),
    )

    DashboardInterface._taskResult(
        dashboard,
        ACTION_SUMMARY_READ_ONLY_TASK_NAME,
        policy_result(),
    )
    DashboardInterface._taskFinished(
        dashboard,
        ACTION_SUMMARY_READ_ONLY_TASK_NAME,
        True,
    )

    assert signal.values == [
        ("■  行动汇总评估完成，等待业务策略", "#f0a44b")
    ]
    assert all("扫荡" not in message for message, _color in signal.values)


def test_dashboard_no_action_and_safety_messages_are_distinct():
    assert action_summary_execution_status(no_action_result()) == (
        "✓  行动汇总评估完成，当前无需执行",
        "#65c466",
    )
    safety = {
        "success": False,
        "execution_status": "BLOCKED",
        "decision": {"decision": "AMBIGUOUS_PAGE"},
        "task_outcome": "BLOCKED_SAFETY",
    }
    assert action_summary_execution_status(safety) == (
        "■  行动汇总因页面安全门禁停止（AMBIGUOUS_PAGE）",
        "#f0a44b",
    )


def test_policy_and_no_action_do_not_schedule_reward_recheck(monkeypatch):
    recorded = []
    reward_rechecks = []
    monkeypatch.setattr(
        dashboard_module,
        "record_task_execution",
        lambda *args, **kwargs: recorded.append((args, kwargs)),
    )
    import core.services.daily_rewards as daily_rewards

    monkeypatch.setattr(
        daily_rewards,
        "schedule_debounced_reward_recheck",
        lambda: reward_rechecks.append("scheduled"),
    )
    task = SimpleNamespace(
        key="resident_activity",
        name=ACTION_SUMMARY_READ_ONLY_TASK_NAME,
        next_run_after=lambda succeeded: "normal" if succeeded else "retry",
    )
    dashboard = SimpleNamespace(refreshScheduleOverview=lambda: None)

    DashboardInterface._taskCompleted(dashboard, task, True, policy_result())
    DashboardInterface._taskCompleted(dashboard, task, True, no_action_result())

    assert reward_rechecks == []
    assert recorded[0][1] == {"deferred": True}
    assert recorded[1][1] == {"deferred": False}


def test_explicit_progress_still_schedules_reward_recheck(monkeypatch):
    reward_rechecks = []
    monkeypatch.setattr(dashboard_module, "record_task_execution", lambda *_a, **_k: None)
    import core.services.daily_rewards as daily_rewards

    monkeypatch.setattr(
        daily_rewards,
        "schedule_debounced_reward_recheck",
        lambda: reward_rechecks.append("scheduled"),
    )
    task = SimpleNamespace(
        key="run_business",
        name="端点跑商",
        next_run_after=lambda _succeeded: "next",
    )
    dashboard = SimpleNamespace(refreshScheduleOverview=lambda: None)

    DashboardInterface._taskCompleted(
        dashboard,
        task,
        True,
        {"success": True, "progress_made": True},
    )

    assert reward_rechecks == ["scheduled"]
