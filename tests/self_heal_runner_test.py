from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import self_heal_runner
from core.services import self_healing


def _incident(message):
    return {
        "source": "runner-test",
        "task_key": "probe",
        "failure_kind": "exception",
        "message": message,
        "context": {"git_revision": "abc123"},
    }


def test_drain_processes_pending_incidents_serially_and_releases_global_claim(
    tmp_path, monkeypatch
):
    monkeypatch.delenv(self_healing.REPAIR_ENV_VAR, raising=False)
    monkeypatch.delenv(self_healing.RUNNER_ENV_VAR, raising=False)
    monkeypatch.setattr(self_healing, "_read_git_revision", lambda: "abc123")
    monkeypatch.setattr(self_healing, "_runtime_snapshot", lambda: None)
    monkeypatch.setattr(self_healing, "_tail", lambda *_args, **_kwargs: "")
    spawned = []
    monkeypatch.setattr(
        self_healing.subprocess,
        "Popen",
        lambda argv, **kwargs: spawned.append((argv, kwargs)),
    )

    first = self_healing.submit_incident(
        _incident("first failure"),
        dispatch=True,
        storage_root=tmp_path,
        allow_during_tests=True,
    )
    second = self_healing.submit_incident(
        _incident("second failure"),
        dispatch=True,
        allow_repair=True,
        storage_root=tmp_path,
        allow_during_tests=True,
    )
    assert first.dispatched is True
    assert second.reason == "queued"
    assert len(spawned) == 1

    environment = spawned[0][1]["env"]
    monkeypatch.setenv(
        self_healing.GLOBAL_CLAIM_ENV_VAR,
        environment[self_healing.GLOBAL_CLAIM_ENV_VAR],
    )
    monkeypatch.setenv(
        self_healing.GLOBAL_TOKEN_ENV_VAR,
        environment[self_healing.GLOBAL_TOKEN_ENV_VAR],
    )
    processed = []

    def fake_execute(incident, **kwargs):
        processed.append((incident, kwargs["mode"]))
        return SimpleNamespace(
            succeeded=True,
            to_dict=lambda: {"status": "diagnosed", "incident": incident},
        )

    monkeypatch.setattr(self_heal_runner, "_execute_incident", fake_execute)

    exit_code = self_heal_runner._drain_pending(
        storage_root=tmp_path,
        enabled=True,
        codex_bin=None,
        timeout=5,
    )

    assert exit_code == 0
    assert sorted(mode for _path, mode in processed) == ["diagnose", "repair"]
    assert self_healing.pending_dispatches(tmp_path) == []
    claim_path = tmp_path / "dispatch" / "global-runner.json"
    assert not claim_path.exists()
    for incident_path, _mode in processed:
        assert json.loads(Path(incident_path).read_text(encoding="utf-8"))["id"]
