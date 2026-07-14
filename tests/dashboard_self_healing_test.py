import json
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


def test_self_healing_status_format_keeps_running_and_candidate_visible():
    running = dashboard_module._self_healing_status_lines(
        {
            "status": "starting",
            "mode": "repair",
            "branch_name": "codex/incident-123",
            "worktree_path": r"C:\project\_worktrees\incident-123",
            "reason": "diagnosing_failure",
            "codex_output_path": r"C:\project\logs\codex-output.jsonl",
            "run_path": r"C:\project\logs\status.json",
        },
        enabled=True,
        allow_repair=True,
    )
    running_text = "\n".join(running)

    assert "运行中" in running_text
    assert "starting" in running_text
    assert "repair" in running_text
    assert "codex/incident-123" in running_text
    assert r"C:\project\_worktrees\incident-123" in running_text
    assert "diagnosing_failure" in running_text
    assert "codex-output.jsonl" in running_text

    candidate_text = "\n".join(
        dashboard_module._self_healing_status_lines(
            {
                "status": "candidate_unvalidated",
                "mode": "repair",
                "worktree_path": r"C:\project\candidate",
                "reason": "automatic_candidate_execution_prohibited",
                "legacy_worktree_paths": [r"C:\legacy\candidate"],
            },
            enabled=True,
            allow_repair=True,
        )
    )
    assert "候选补丁未验证" in candidate_text
    assert "candidate_unvalidated" in candidate_text
    assert "branch_name: detached" in candidate_text
    assert "automatic_candidate_execution_prohibited" in candidate_text
    assert "历史外置工作树（只读保留）" in candidate_text
    assert r"C:\legacy\candidate" in candidate_text


def test_current_self_healing_status_reads_runs_and_pending_queue_without_mutation(
    tmp_path,
):
    status_path = tmp_path / "runs" / "incident-old" / "status.json"
    status_path.parent.mkdir(parents=True)
    status_path.write_text(
        json.dumps(
            {
                "incident_id": "incident-old",
                "status": "candidate_unvalidated",
                "mode": "repair",
            }
        ),
        encoding="utf-8",
    )

    current = dashboard_module._current_self_healing_status(tmp_path)
    assert current["status"] == "candidate_unvalidated"

    pending_path = tmp_path / "dispatch" / "pending" / "incident-new.json"
    pending_path.parent.mkdir(parents=True)
    pending_path.write_text(
        json.dumps(
            {
                "incident_id": "incident-new",
                "incident_path": "incidents/incident-new.json",
                "mode": "diagnose",
            }
        ),
        encoding="utf-8",
    )
    queued = dashboard_module._current_self_healing_status(tmp_path)
    assert queued == {
        "status": "queued",
        "mode": "diagnose",
        "incident_id": "incident-new",
        "incident_path": "incidents/incident-new.json",
    }
    assert pending_path.is_file()

    status_path.write_text(
        json.dumps(
            {
                "incident_id": "incident-new",
                "status": "starting",
                "mode": "diagnose",
            }
        ),
        encoding="utf-8",
    )
    active = dashboard_module._current_self_healing_status(tmp_path)
    assert active["status"] == "starting"
    assert pending_path.is_file()


def test_default_dashboard_marks_orphaned_active_run_as_interrupted(monkeypatch):
    monkeypatch.setattr(
        dashboard_module,
        "list_run_statuses",
        lambda *_args, **_kwargs: [
            {
                "incident_id": "incident-stale",
                "status": "codex_running",
                "mode": "repair",
            }
        ],
    )
    monkeypatch.setattr(
        dashboard_module, "_latest_pending_dispatch", lambda _root: None
    )
    monkeypatch.setattr(
        dashboard_module, "global_runner_active", lambda _root: False
    )
    monkeypatch.setattr(
        dashboard_module, "list_legacy_worktrees", lambda: []
    )

    current = dashboard_module._current_self_healing_status()

    assert current["status"] == "interrupted_stale"
    assert current["reason"] == "runner_process_not_active"


def test_dashboard_refreshes_self_healing_state_and_survives_read_failure(
    monkeypatch,
):
    monkeypatch.setattr(cfg.enableCodexSelfHealing, "value", True)
    monkeypatch.setattr(cfg.allowCodexIsolatedRepair, "value", False)
    monkeypatch.setattr(
        dashboard_module,
        "_current_self_healing_status",
        lambda: {"status": "codex_running", "mode": "diagnose"},
    )
    dashboard = SimpleNamespace(selfHealingPanel=_Panel())

    DashboardInterface.refreshSelfHealingStatus(dashboard)

    text = "\n".join(dashboard.selfHealingPanel.tasks)
    assert "运行中" in text
    assert "codex_running" in text
    assert "diagnose" in text

    def fail_read():
        raise OSError("status file changed during read")

    monkeypatch.setattr(
        dashboard_module,
        "_current_self_healing_status",
        fail_read,
    )
    DashboardInterface.refreshSelfHealingStatus(dashboard)
    assert "状态读取失败" in dashboard.selfHealingPanel.tasks[0]


def test_self_healing_status_timer_refreshes_periodically(monkeypatch):
    timers = []

    class _Timer:
        def __init__(self, parent):
            self.parent = parent
            self.interval = None
            self.timeout = _Signal()
            self.started = False
            timers.append(self)

        def setInterval(self, interval):
            self.interval = interval

        def start(self):
            self.started = True

    monkeypatch.setattr(dashboard_module, "QTimer", _Timer)
    refresh = lambda: None
    dashboard = SimpleNamespace(refreshSelfHealingStatus=refresh)

    DashboardInterface._startSelfHealingStatusTimer(dashboard)

    assert dashboard.selfHealingTimer is timers[0]
    assert timers[0].interval == dashboard_module.SELF_HEALING_REFRESH_INTERVAL_MS
    assert timers[0].started is True
    assert timers[0].timeout.slots == [refresh]
