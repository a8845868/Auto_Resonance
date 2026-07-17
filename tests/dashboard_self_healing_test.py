import os
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from app.common.config import cfg
import app.view.dashboard_interface as dashboard_module
from app.view.dashboard_interface import (
    DashboardInterface,
    _collect_scheduled_rewards,
    _history_status_label,
)
from core.services.task_schedule_state import task_result_deferred, task_result_succeeded


class _Panel:
    def __init__(self):
        self.tasks = None

    def setTasks(self, tasks):
        self.tasks = tasks


class _Button:
    def __init__(self):
        self.enabled = None
        self.text = None
        self.icon = None

    def setEnabled(self, enabled):
        self.enabled = enabled

    def setText(self, text):
        self.text = text

    def setIcon(self, icon):
        self.icon = icon


class _Signal:
    def __init__(self):
        self.slots = []

    def connect(self, slot):
        self.slots.append(slot)


class _Worker:
    halted_for_repair = True

    def __init__(self):
        self.deleted = False

    def deleteLater(self):
        self.deleted = True


def test_scheduler_starts_due_tasks_without_a_manual_button_click(monkeypatch):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(cfg.enableCodexSelfHealing, "value", False)
    monkeypatch.setattr(dashboard_module, "is_task_due", lambda _key: True)
    dashboard = DashboardInterface()
    dashboard.scheduleTimer.stop()
    starts = []
    dashboard._allEnabledTasks = lambda: [SimpleNamespace(key="scheduled_task")]
    dashboard.startTaskQueue = lambda: starts.append("started")

    DashboardInterface._runDueTasks(dashboard)

    assert dashboard.schedulerArmed is True
    assert dashboard.controlButton.text() == "停止全部任务"
    assert starts == ["started"]
    assert dashboard.shutdown() is True
    dashboard.deleteLater()
    app.processEvents()


def test_queue_failure_disarms_scheduler_before_timer_can_restart_tasks():
    worker = _Worker()
    dashboard = SimpleNamespace(
        queueWorker=worker,
        schedulerArmed=True,
        runningPanel=_Panel(),
        pendingPanel=_Panel(),
        controlButton=_Button(),
        refreshScheduleOverview=lambda: None,
    )
    dashboard._setControlRunning = lambda running: DashboardInterface._setControlRunning(
        dashboard, running
    )

    DashboardInterface._queueFinished(dashboard, worker)

    assert dashboard.schedulerArmed is False
    assert dashboard.queueWorker is None
    assert dashboard.pendingPanel.tasks
    assert "暂停" in dashboard.pendingPanel.tasks[0]
    assert "调试" in dashboard.pendingPanel.tasks[0]
    assert worker.deleted is True


def test_log_monitor_does_not_dispatch_while_queue_is_running(monkeypatch):
    calls = []
    monkeypatch.setattr(cfg.enableCodexSelfHealing, "value", True)
    monkeypatch.setattr(
        dashboard_module,
        "discover_log_incidents",
        lambda **kwargs: calls.append(kwargs),
    )
    dashboard = SimpleNamespace(queueWorker=object(), schedulerArmed=True)

    DashboardInterface._runDueTasks(dashboard)

    assert calls == []


def test_single_control_button_toggles_scheduler_state():
    calls = []
    dashboard = SimpleNamespace(
        schedulerArmed=False,
        startTaskQueue=lambda: calls.append("start"),
        stopTaskQueue=lambda: calls.append("stop"),
    )

    DashboardInterface._toggleTaskQueue(dashboard)
    dashboard.schedulerArmed = True
    DashboardInterface._toggleTaskQueue(dashboard)

    assert calls == ["start", "stop"]


def test_scheduled_rewards_defer_without_self_healing_when_nothing_is_claimable(monkeypatch):
    monkeypatch.setattr(
        dashboard_module,
        "collect_scheduled_rewards",
        lambda daily, manual, strategy: {
            "success": True,
            "deferred": True,
            "status": "INCOMPLETE",
            "task_rewards": {"daily": 0, "manual": 0},
            "progress_made": False,
            "completion_predicate": False,
            "next_run_at": "2026-07-17T13:08:34+08:00",
            "next_run_reason": "daily_objectives_incomplete",
        },
    )
    monkeypatch.setattr(
        dashboard_module,
        "collect_dispatch_rewards",
        lambda: False,
    )

    result = _collect_scheduled_rewards(True, True)

    assert result["status"] == "INCOMPLETE"
    assert result["completion_predicate"] is False
    assert result["dispatch_collected"] is False
    assert task_result_succeeded(result)
    assert task_result_deferred(result)


def test_scheduled_rewards_do_not_complete_just_because_something_was_claimed(monkeypatch):
    monkeypatch.setattr(
        dashboard_module,
        "collect_scheduled_rewards",
        lambda daily, manual, strategy: {
            "success": True,
            "deferred": True,
            "status": "INCOMPLETE",
            "task_rewards": {"daily": 1, "manual": 0},
            "progress_made": True,
            "completion_predicate": False,
            "next_run_at": "2026-07-17T13:08:34+08:00",
            "next_run_reason": "daily_objectives_incomplete",
        },
    )
    monkeypatch.setattr(
        dashboard_module,
        "collect_dispatch_rewards",
        lambda: False,
    )

    result = _collect_scheduled_rewards(True, True)

    assert result["success"] is True
    assert task_result_deferred(result)
    assert result["completion_predicate"] is False


def test_deferred_reward_uses_failure_interval_without_recording_completion(monkeypatch):
    recorded = []
    next_run_arguments = []
    monkeypatch.setattr(
        dashboard_module,
        "record_task_execution",
        lambda *args, **kwargs: recorded.append((args, kwargs)),
    )
    task = SimpleNamespace(
        key="reward_collection",
        name="领取任务奖励",
        next_run_after=lambda succeeded: next_run_arguments.append(succeeded) or "retry-at",
    )
    dashboard = SimpleNamespace(refreshScheduleOverview=lambda: None)
    result = {"success": True, "deferred": True, "reason": "nothing_claimed"}

    DashboardInterface._taskCompleted(dashboard, task, True, result)

    assert next_run_arguments == [False]
    assert recorded[0][0][4] == result
    assert recorded[0][1] == {"deferred": True}


def test_deferred_history_is_not_labelled_as_failure():
    assert _history_status_label("completed") == "完成"
    assert _history_status_label("deferred") == "等待复核"
    assert _history_status_label("failed") == "失败/停止"


def test_queue_captures_one_self_healing_policy_snapshot(monkeypatch):
    submissions = []

    class CapturingWorker:
        def __init__(self, _tasks, _parent, **kwargs):
            self.incident_reporter = kwargs["incident_reporter"]
            self.halt_on_failure = kwargs["halt_on_failure"]
            self.started = False
            self.taskStarted = _Signal()
            self.taskFinished = _Signal()
            self.taskResult = _Signal()
            self.taskCompleted = _Signal()
            self.queueChanged = _Signal()
            self.error = _Signal()
            self.finished = _Signal()

        def start(self):
            self.started = True

    monkeypatch.setattr(dashboard_module, "TaskQueueWorker", CapturingWorker)
    monkeypatch.setattr(
        dashboard_module,
        "submit_incident",
        lambda incident, **kwargs: submissions.append((incident, kwargs)),
    )
    monkeypatch.setattr(cfg.enableCodexSelfHealing, "value", True)
    monkeypatch.setattr(cfg.allowCodexIsolatedRepair, "value", False)
    monkeypatch.setattr(cfg.enableAutoGameLifecycle, "value", False)
    dashboard = SimpleNamespace(
        queueWorker=None,
        schedulerArmed=False,
        _enabledTasks=lambda: [SimpleNamespace(name="task")],
        _taskStarted=lambda *_args: None,
        _taskFinished=lambda *_args: None,
        _taskResult=lambda *_args: None,
        _taskCompleted=lambda *_args: None,
        pendingPanel=_Panel(),
        controlButton=_Button(),
    )
    dashboard._setControlRunning = lambda running: DashboardInterface._setControlRunning(
        dashboard, running
    )

    DashboardInterface.startTaskQueue(dashboard)
    worker = dashboard.queueWorker
    assert worker.halt_on_failure is True
    assert worker.started is True
    assert dashboard.controlButton.text == "停止全部任务"

    # Changing settings mid-batch must not split halt and dispatch policies.
    monkeypatch.setattr(cfg.enableCodexSelfHealing, "value", False)
    monkeypatch.setattr(cfg.allowCodexIsolatedRepair, "value", True)
    worker.incident_reporter({"message": "failed"})

    assert submissions == [
        (
            {"message": "failed"},
            {"dispatch": True, "allow_repair": False},
        )
    ]


@pytest.mark.parametrize("close_game_when_idle", [False, True])
def test_queue_captures_close_game_preference(monkeypatch, close_game_when_idle):
    captured = {}

    class CapturingLifecycle:
        def __init__(self, device, *, options):
            captured["device"] = device
            captured["options"] = options

    class CapturingWorker:
        def __init__(self, _tasks, _parent, **kwargs):
            captured["lifecycle"] = kwargs["lifecycle"]
            self.taskStarted = _Signal()
            self.taskFinished = _Signal()
            self.taskResult = _Signal()
            self.taskCompleted = _Signal()
            self.queueChanged = _Signal()
            self.error = _Signal()
            self.finished = _Signal()

        def start(self):
            pass

    monkeypatch.setattr(
        dashboard_module, "EmulatorQueueLifecycle", CapturingLifecycle
    )
    monkeypatch.setattr(dashboard_module, "TaskQueueWorker", CapturingWorker)
    monkeypatch.setattr(cfg.enableCodexSelfHealing, "value", False)
    monkeypatch.setattr(cfg.allowCodexIsolatedRepair, "value", False)
    monkeypatch.setattr(cfg.enableAutoGameLifecycle, "value", True)
    monkeypatch.setattr(cfg.autoStartEmulator, "value", True)
    monkeypatch.setattr(
        cfg.closeGameWhenIdle, "value", close_game_when_idle
    )
    monkeypatch.setattr(cfg.closeEmulatorWhenIdle, "value", False)
    monkeypatch.setattr(cfg.device, "value", "127.0.0.1:16544")
    dashboard = SimpleNamespace(
        queueWorker=None,
        schedulerArmed=False,
        _enabledTasks=lambda: [SimpleNamespace(name="task")],
        _taskStarted=lambda *_args: None,
        _taskFinished=lambda *_args: None,
        _taskResult=lambda *_args: None,
        _taskCompleted=lambda *_args: None,
        pendingPanel=_Panel(),
        controlButton=_Button(),
    )
    dashboard._setControlRunning = lambda running: DashboardInterface._setControlRunning(
        dashboard, running
    )

    DashboardInterface.startTaskQueue(dashboard)

    assert captured["device"] == "127.0.0.1:16544"
    assert captured["lifecycle"] is not None
    assert (
        captured["options"].close_game_when_idle
        is close_game_when_idle
    )
