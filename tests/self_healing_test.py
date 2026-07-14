from __future__ import annotations

import json
import os
from pathlib import Path
import time

from core.services import self_healing


def _incident(**context):
    return {
        "source": "test_queue",
        "task_key": "daily",
        "task_name": "Daily task",
        "failure_kind": "unexpected_result",
        "message": "expected result did not happen",
        "expected": True,
        "observed": False,
        "context": context,
    }


def _stable_enrichment(monkeypatch):
    monkeypatch.delenv(self_healing.REPAIR_ENV_VAR, raising=False)
    monkeypatch.delenv(self_healing.RUNNER_ENV_VAR, raising=False)
    monkeypatch.setattr(self_healing, "_read_git_revision", lambda: "abc123")
    monkeypatch.setattr(self_healing, "_runtime_snapshot", lambda: None)
    monkeypatch.setattr(self_healing, "_tail", lambda *_args, **_kwargs: "")


def test_disabled_dispatch_still_records_incident(tmp_path, monkeypatch):
    _stable_enrichment(monkeypatch)

    result = self_healing.submit_incident(
        _incident(),
        dispatch=False,
        storage_root=tmp_path,
        allow_during_tests=True,
    )

    assert result.reason == "recorded_only"
    assert result.dispatched is False
    assert result.incident_path is not None
    document = json.loads(result.incident_path.read_text(encoding="utf-8"))
    assert document["context"]["git_revision"] == "abc123"


def test_dispatch_is_detached_and_cooldown_is_single_flight(tmp_path, monkeypatch):
    _stable_enrichment(monkeypatch)
    calls = []

    def fake_popen(argv, **kwargs):
        calls.append((argv, kwargs))
        return object()

    monkeypatch.setattr(self_healing.subprocess, "Popen", fake_popen)

    first = self_healing.submit_incident(
        _incident(),
        dispatch=True,
        allow_repair=False,
        storage_root=tmp_path,
        allow_during_tests=True,
    )
    second = self_healing.submit_incident(
        _incident(),
        dispatch=True,
        allow_repair=False,
        storage_root=tmp_path,
        allow_during_tests=True,
    )

    assert first.dispatched is True
    assert second.dispatched is False
    assert second.reason == "cooldown"
    assert len(calls) == 1
    argv, kwargs = calls[0]
    if self_healing.os.name == "nt":
        assert argv[-3:] == ["drain", "--enabled", "--visible"]
        assert Path(argv[0]).name.casefold() == "python.exe"
        assert kwargs["creationflags"] == getattr(
            self_healing.subprocess, "CREATE_NEW_CONSOLE", 0
        )
        assert not (
            kwargs["creationflags"]
            & getattr(self_healing.subprocess, "DETACHED_PROCESS", 0)
        )
        assert not (
            kwargs["creationflags"]
            & getattr(self_healing.subprocess, "CREATE_NO_WINDOW", 0)
        )
        assert "stdout" not in kwargs
        assert "stderr" not in kwargs
    else:
        assert argv[-2:] == ["drain", "--enabled"]
        assert kwargs["start_new_session"] is True
        assert kwargs["stdout"] is self_healing.subprocess.DEVNULL
    assert kwargs["shell"] is False
    assert kwargs["env"][self_healing.RUNNER_ENV_VAR] == "1"
    assert self_healing.REPAIR_ENV_VAR not in {
        key for key, value in kwargs["env"].items() if value == "1"
    }


def test_windows_spawn_uses_visible_python_console(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(self_healing.os, "name", "nt")
    monkeypatch.setattr(
        self_healing, "_console_python_executable", lambda: r"C:\Python\python.exe"
    )
    monkeypatch.setattr(
        self_healing.subprocess,
        "CREATE_NEW_CONSOLE",
        0x10,
        raising=False,
    )
    monkeypatch.setattr(
        self_healing.subprocess,
        "Popen",
        lambda argv, **kwargs: calls.append((argv, kwargs)),
    )

    self_healing._spawn_runner(
        storage_root=tmp_path,
        global_claim=tmp_path / "claim.json",
        global_token="token",
    )

    argv, kwargs = calls[0]
    assert argv == [
        r"C:\Python\python.exe",
        str(self_healing.RUNNER_PATH),
        "drain",
        "--enabled",
        "--visible",
    ]
    assert kwargs["creationflags"] == 0x10
    assert "start_new_session" not in kwargs
    assert "stdin" not in kwargs


def test_second_switch_selects_repair_mode(tmp_path, monkeypatch):
    _stable_enrichment(monkeypatch)
    calls = []
    monkeypatch.setattr(
        self_healing.subprocess,
        "Popen",
        lambda argv, **kwargs: calls.append((argv, kwargs)),
    )

    result = self_healing.submit_incident(
        _incident(),
        dispatch=True,
        allow_repair=True,
        storage_root=tmp_path,
        allow_during_tests=True,
    )

    assert result.dispatched is True
    queued = self_healing.pending_dispatches(tmp_path)
    assert len(queued) == 1
    assert queued[0][1]["mode"] == "repair"


def test_different_fingerprints_share_one_global_runner(tmp_path, monkeypatch):
    _stable_enrichment(monkeypatch)
    calls = []
    monkeypatch.setattr(
        self_healing.subprocess,
        "Popen",
        lambda argv, **kwargs: calls.append((argv, kwargs)),
    )

    first_incident = _incident()
    first_incident["observed"] = "first"
    second_incident = _incident()
    second_incident["observed"] = "second"
    first = self_healing.submit_incident(
        first_incident,
        dispatch=True,
        storage_root=tmp_path,
        allow_during_tests=True,
    )
    second = self_healing.submit_incident(
        second_incident,
        dispatch=True,
        storage_root=tmp_path,
        allow_during_tests=True,
    )

    assert first.dispatched is True
    assert second.reason == "queued"
    assert len(calls) == 1
    assert len(self_healing.pending_dispatches(tmp_path)) == 2


def test_dispatch_disallowed_context_never_spawns(tmp_path, monkeypatch):
    _stable_enrichment(monkeypatch)
    monkeypatch.setattr(
        self_healing.subprocess,
        "Popen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("spawned")),
    )

    result = self_healing.submit_incident(
        _incident(dispatch_allowed=False),
        dispatch=True,
        allow_repair=True,
        storage_root=tmp_path,
        allow_during_tests=True,
    )

    assert result.reason == "dispatch_disallowed"
    assert result.incident_path is not None


def test_repair_and_runner_processes_do_not_report_recursively(tmp_path, monkeypatch):
    monkeypatch.setenv(self_healing.REPAIR_ENV_VAR, "true")

    result = self_healing.submit_incident(
        _incident(),
        dispatch=True,
        storage_root=tmp_path,
        allow_during_tests=True,
    )

    assert result.incident_path is None
    assert result.reason.startswith("recursive:")
    assert list(tmp_path.iterdir()) == []


def test_log_monitor_starts_at_eof_then_discovers_appended_failure(
    tmp_path, monkeypatch
):
    root = tmp_path / "repository"
    logs = root / "logs"
    logs.mkdir(parents=True)
    debug_log = logs / "debug.log"
    debug_log.write_text("ERROR | historical failure\n", encoding="utf-8")
    storage = tmp_path / "incidents"
    monkeypatch.setattr(self_healing, "ROOT", root)
    monkeypatch.setattr(self_healing, "_read_git_revision", lambda: "abc123")
    monkeypatch.setattr(self_healing, "_runtime_snapshot", lambda: None)

    first = self_healing.discover_log_incidents(
        dispatch=False,
        storage_root=storage,
        allow_during_tests=True,
    )
    with debug_log.open("a", encoding="utf-8") as stream:
        stream.write("ERROR | newly appended failure\n")
    second = self_healing.discover_log_incidents(
        dispatch=False,
        storage_root=storage,
        allow_during_tests=True,
    )

    assert first == []
    assert len(second) == 1
    assert second[0].reason == "recorded_only"
    assert second[0].incident_path is not None
    document = json.loads(second[0].incident_path.read_text(encoding="utf-8"))
    assert document["context"]["git_revision"] == "abc123"


def test_spawn_failure_releases_claim_for_retry(tmp_path, monkeypatch):
    _stable_enrichment(monkeypatch)
    attempts = 0

    def fake_popen(*_args, **_kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("cannot start")
        return object()

    monkeypatch.setattr(self_healing.subprocess, "Popen", fake_popen)

    first = self_healing.submit_incident(
        _incident(),
        dispatch=True,
        storage_root=tmp_path,
        allow_during_tests=True,
    )
    retry = self_healing.submit_incident(
        _incident(),
        dispatch=True,
        storage_root=tmp_path,
        allow_during_tests=True,
    )

    assert first.reason == "spawn_failed"
    assert retry.reason == "cooldown"
    assert attempts == 2


def test_pending_dispatch_is_published_only_after_complete_json(tmp_path, monkeypatch):
    store = self_healing.IncidentLearningStore(tmp_path)
    incident = store.record_incident(store.new_incident(**_incident()))
    observations = []
    atomic_write = self_healing._write_json_atomic

    def observe_before_publish(path, document):
        observations.append(self_healing.pending_dispatches(tmp_path))
        assert not path.exists()
        atomic_write(path, document)

    monkeypatch.setattr(self_healing, "_write_json_atomic", observe_before_publish)

    path = self_healing._enqueue_dispatch(
        incident, allow_repair=False, storage_root=tmp_path
    )

    assert observations == [[]]
    assert json.loads(path.read_text(encoding="utf-8"))["incident_id"] == incident.id
    assert not list(path.parent.glob("*.tmp"))


def test_fresh_incomplete_global_claim_is_busy_until_launch_ttl_expires(tmp_path):
    claim_path = tmp_path / "dispatch" / "global-runner.json"
    claim_path.parent.mkdir(parents=True)
    claim_path.write_bytes(b"")

    assert self_healing.claim_global_dispatch(tmp_path) is None
    assert claim_path.exists()

    stale = time.time() - 61
    os.utime(claim_path, (stale, stale))
    claimed = self_healing.claim_global_dispatch(tmp_path)

    assert claimed is not None
    assert json.loads(claim_path.read_text(encoding="utf-8"))["token"] == claimed[1]


def test_global_runner_active_is_read_only_liveness_check(tmp_path):
    assert self_healing.global_runner_active(tmp_path) is False
    claimed = self_healing.claim_global_dispatch(tmp_path)
    assert claimed is not None
    claim_path, token = claimed
    assert self_healing.adopt_global_dispatch(claim_path, token) is True

    assert self_healing.global_runner_active(tmp_path) is True

    self_healing.release_global_dispatch(claim_path, token)
    assert self_healing.global_runner_active(tmp_path) is False
