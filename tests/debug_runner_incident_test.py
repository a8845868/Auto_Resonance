from types import SimpleNamespace

from app.common.config import cfg
import core.services.debug_tasks as debug_tasks
import core.services.emulator_lifecycle as emulator_lifecycle
from debug_runner import _run_debug_task


def _lease():
    return SimpleNamespace(pid=1234, stop_requested=lambda: False)


def test_debug_unexpected_result_reports_incident(monkeypatch):
    task = SimpleNamespace(key="probe", name="探测", run=lambda: False)
    incidents = []
    monkeypatch.setattr(debug_tasks, "resolve_task", lambda _name: task)
    monkeypatch.setattr(cfg.enableAutoGameLifecycle, "value", False)

    response = _run_debug_task(
        _lease(),
        {"id": "cmd-1", "task": "probe"},
        incident_reporter=incidents.append,
    )

    assert response["success"] is False
    assert incidents[0]["failure_kind"] == "unexpected_result"
    assert incidents[0]["context"]["dispatch_allowed"] is True


def test_debug_incident_is_reported_after_cleanup(monkeypatch):
    events = []

    class Lifecycle:
        def __init__(self, *_args, **_kwargs):
            pass

        def prepare(self, _cancelled):
            events.append("prepare")

        def cleanup(self):
            events.append("cleanup")

    def run():
        events.append("task")
        raise RuntimeError("debug exploded")

    task = SimpleNamespace(key="probe", name="探测", run=run)
    monkeypatch.setattr(debug_tasks, "resolve_task", lambda _name: task)
    monkeypatch.setattr(emulator_lifecycle, "EmulatorQueueLifecycle", Lifecycle)
    monkeypatch.setattr(cfg.enableAutoGameLifecycle, "value", True)

    response = _run_debug_task(
        _lease(),
        {"id": "cmd-2", "task": "probe"},
        incident_reporter=lambda incident: events.append(
            ("report", incident["failure_kind"])
        ),
    )

    assert response["success"] is False
    assert events == ["prepare", "task", "cleanup", ("report", "exception")]


def test_cleanup_only_failure_is_diagnostic_only(monkeypatch):
    class Lifecycle:
        def __init__(self, *_args, **_kwargs):
            pass

        def prepare(self, _cancelled):
            pass

        def cleanup(self):
            raise RuntimeError("cleanup exploded")

    task = SimpleNamespace(key="probe", name="探测", run=lambda: True)
    incidents = []
    monkeypatch.setattr(debug_tasks, "resolve_task", lambda _name: task)
    monkeypatch.setattr(emulator_lifecycle, "EmulatorQueueLifecycle", Lifecycle)
    monkeypatch.setattr(cfg.enableAutoGameLifecycle, "value", True)

    response = _run_debug_task(
        _lease(),
        {"id": "cmd-3", "task": "probe"},
        incident_reporter=incidents.append,
    )

    assert response["success"] is True
    assert incidents[0]["failure_kind"] == "resource_cleanup_error"
    assert incidents[0]["context"]["dispatch_allowed"] is False


def test_task_and_cleanup_failure_does_not_dispatch_repair(monkeypatch):
    class Lifecycle:
        def __init__(self, *_args, **_kwargs):
            pass

        def prepare(self, _cancelled):
            pass

        def cleanup(self):
            raise RuntimeError("cleanup also failed")

    def fail():
        raise ValueError("task failed")

    task = SimpleNamespace(key="probe", name="探测", run=fail)
    incidents = []
    monkeypatch.setattr(debug_tasks, "resolve_task", lambda _name: task)
    monkeypatch.setattr(emulator_lifecycle, "EmulatorQueueLifecycle", Lifecycle)
    monkeypatch.setattr(cfg.enableAutoGameLifecycle, "value", True)

    _run_debug_task(
        _lease(),
        {"id": "cmd-4", "task": "probe"},
        incident_reporter=incidents.append,
    )

    assert incidents[0]["failure_kind"] == "exception"
    assert "cleanup also failed" in incidents[0]["context"]["cleanup_error"]
    assert incidents[0]["context"]["dispatch_allowed"] is False
