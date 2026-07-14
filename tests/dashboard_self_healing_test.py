from types import SimpleNamespace

from app.common.config import cfg
import app.view.dashboard_interface as dashboard_module
from app.view.dashboard_interface import DashboardInterface


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
