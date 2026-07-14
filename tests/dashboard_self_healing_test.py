from types import SimpleNamespace

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

    def setEnabled(self, enabled):
        self.enabled = enabled


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


def test_queue_failure_disarms_scheduler_before_timer_can_restart_tasks():
    worker = _Worker()
    dashboard = SimpleNamespace(
        queueWorker=worker,
        schedulerArmed=True,
        runningPanel=_Panel(),
        pendingPanel=_Panel(),
        startButton=_Button(),
        stopButton=_Button(),
        refreshScheduleOverview=lambda: None,
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


def test_scheduled_rewards_defer_without_self_healing_when_nothing_is_claimable(monkeypatch):
    monkeypatch.setattr(
        dashboard_module,
        "collect_rewards",
        lambda daily, manual: {"daily": 0, "manual": 0},
    )
    monkeypatch.setattr(
        dashboard_module,
        "collect_dispatch_rewards",
        lambda: False,
    )

    result = _collect_scheduled_rewards(True, True)

    assert result == {
        "success": True,
        "deferred": True,
        "reason": "nothing_claimed",
        "task_rewards": {"daily": 0, "manual": 0},
        "dispatch_collected": False,
    }
    assert task_result_succeeded(result)
    assert task_result_deferred(result)


def test_scheduled_rewards_complete_normally_after_a_confirmed_claim(monkeypatch):
    monkeypatch.setattr(
        dashboard_module,
        "collect_rewards",
        lambda daily, manual: {"daily": 1, "manual": 0},
    )
    monkeypatch.setattr(
        dashboard_module,
        "collect_dispatch_rewards",
        lambda: False,
    )

    result = _collect_scheduled_rewards(True, True)

    assert result["success"] is True
    assert not task_result_deferred(result)


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
        startButton=_Button(),
        stopButton=_Button(),
    )

    DashboardInterface.startTaskQueue(dashboard)
    worker = dashboard.queueWorker
    assert worker.halt_on_failure is True
    assert worker.started is True

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
