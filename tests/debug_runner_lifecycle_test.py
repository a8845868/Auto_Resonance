from types import SimpleNamespace

import core.services.debug_tasks as debug_tasks
from debug_runner import _run_debug_task


def test_stopping_debug_runtime_never_starts_a_new_task(monkeypatch):
    calls = []
    task = SimpleNamespace(
        key="probe",
        name="探测",
        run=lambda: calls.append("run") or True,
    )
    lease = SimpleNamespace(stop_requested=lambda: True)
    monkeypatch.setattr(debug_tasks, "resolve_task", lambda _name: task)

    response = _run_debug_task(lease, {"id": "cmd-1", "task": "probe"})

    assert response["success"] is False
    assert "未启动" in response["error"]
    assert calls == []
