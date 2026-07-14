from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import tomllib
import uuid
from pathlib import Path

import psutil
import pytest

from core.services.codex_repair import (
    MAX_CAPTURE_CHARS,
    CodexRepairConfig,
    CodexRepairExecutor,
    ProcessContainmentUnavailable,
    _requires_repository_deny,
    discover_codex_executable,
    list_legacy_worktrees,
    list_run_statuses,
    run_process_tree,
)
from core.services.incident_learning import IncidentLearningStore


REVISION = "a" * 40


def test_repository_ancestor_deny_is_omitted_only_for_nested_windows_worktree(
    tmp_path,
):
    repository = tmp_path / "repository"
    nested = repository / "_worktrees" / "candidate"
    external = tmp_path / "external-candidate"

    assert (
        _requires_repository_deny(repository, nested, platform_name="nt") is False
    )
    assert (
        _requires_repository_deny(repository, nested, platform_name="posix") is True
    )
    assert (
        _requires_repository_deny(repository, external, platform_name="nt") is True
    )


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Codex layout")
def test_discover_codex_prefers_standalone_package_layout(tmp_path, monkeypatch):
    codex_home = tmp_path / "codex-home"
    packaged = (
        codex_home
        / "packages"
        / "standalone"
        / "current"
        / "bin"
        / "codex.exe"
    )
    visible = tmp_path / "visible-bin" / "codex.exe"
    packaged.parent.mkdir(parents=True)
    visible.parent.mkdir(parents=True)
    packaged.write_bytes(b"packaged")
    visible.write_bytes(b"visible-hard-link-placeholder")
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setattr(
        "core.services.codex_repair.shutil.which", lambda _name: str(visible)
    )

    assert discover_codex_executable() == str(packaged.resolve())


def _repository(tmp_path):
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    python = root / ".venv" / "Scripts" / "python.exe"
    python.parent.mkdir(parents=True)
    python.write_bytes(b"")
    return root


def _incident(storage, *, revision=REVISION):
    store = IncidentLearningStore(storage)
    return store.record_incident(
        store.new_incident(
            source="task_queue",
            task_key="daily",
            task_name="Daily task",
            failure_kind="exception",
            message="RuntimeError: failed",
            traceback='  File "C:\\repo\\task.py", line 12, in run\nRuntimeError: failed',
            context={"git_revision": revision, "dispatch_allowed": True},
        )
    )


class _Runner:
    def __init__(
        self,
        *,
        dirty=False,
        revision=REVISION,
        codex_returncode=0,
        changed=True,
        include_test=True,
        timeout_codex=False,
        status_payload=None,
        status_returncode=0,
        mutate_git_marker=False,
        on_codex=None,
    ):
        self.dirty = dirty
        self.revision = revision
        self.codex_returncode = codex_returncode
        self.changed = changed
        self.include_test = include_test
        self.timeout_codex = timeout_codex
        self.status_payload = status_payload
        self.status_returncode = status_returncode
        self.mutate_git_marker = mutate_git_marker
        self.on_codex = on_codex
        self.calls = []
        self.worktrees = set()

    def __call__(self, argv, **kwargs):
        self.calls.append((list(argv), kwargs))
        executable = Path(str(argv[0])).name.casefold()
        if executable == "git":
            if argv[1:4] == ["status", "--porcelain=v1", "-z"]:
                output = self.status_payload
                if output is None:
                    output = " M core/services/example.py\0" if self.changed else ""
                    if self.changed and self.include_test:
                        output += "?? tests/example_regression_test.py\0"
                return subprocess.CompletedProcess(
                    argv, self.status_returncode, output, ""
                )
            if argv[1:3] == ["status", "--porcelain"]:
                cwd = Path(kwargs["cwd"])
                if cwd.resolve() in self.worktrees:
                    output = " M core/services/example.py\n" if self.changed else ""
                    if self.changed and self.include_test:
                        output += "?? tests/example_regression_test.py\n"
                else:
                    output = " M user-change.py\n" if self.dirty else ""
                return subprocess.CompletedProcess(argv, 0, output, "")
            if argv[1:3] == ["rev-parse", "HEAD"]:
                return subprocess.CompletedProcess(argv, 0, self.revision + "\n", "")
            if argv[1:4] == ["worktree", "add", "--detach"]:
                worktree = Path(argv[4])
                worktree.mkdir(parents=True)
                git_dir = (
                    Path(kwargs["cwd"])
                    / ".git"
                    / "worktrees"
                    / worktree.name
                )
                git_dir.mkdir(parents=True)
                (worktree / ".git").write_text(
                    f"gitdir: {git_dir}", encoding="utf-8"
                )
                self.worktrees.add(worktree.resolve())
                return subprocess.CompletedProcess(argv, 0, "prepared", "")
            if argv[1:4] == ["worktree", "add", "-b"]:
                worktree = Path(argv[5])
                worktree.mkdir(parents=True)
                git_dir = (
                    Path(kwargs["cwd"])
                    / ".git"
                    / "worktrees"
                    / worktree.name
                )
                git_dir.mkdir(parents=True)
                (worktree / ".git").write_text(
                    f"gitdir: {git_dir}", encoding="utf-8"
                )
                self.worktrees.add(worktree.resolve())
                return subprocess.CompletedProcess(argv, 0, "prepared", "")
            raise AssertionError(f"unexpected Git command: {argv}")
        if executable == "codex.exe":
            if len(argv) > 1 and argv[1] == "sandbox":
                return subprocess.CompletedProcess(argv, 0, "sandbox-ok", "")
            if self.on_codex is not None:
                self.on_codex(argv, kwargs)
            if self.timeout_codex:
                raise subprocess.TimeoutExpired(argv, 1, output="partial")
            if self.mutate_git_marker:
                (Path(kwargs["cwd"]) / ".git").write_text(
                    "gitdir: malicious", encoding="utf-8"
                )
            return subprocess.CompletedProcess(
                argv,
                self.codex_returncode,
                '{"type":"result","message":"done"}\n',
                "codex error" if self.codex_returncode else "",
            )
        raise AssertionError(f"unexpected executable: {argv}")


def _executor(tmp_path, runner, *, mode="diagnose", enabled=True):
    repository = _repository(tmp_path)
    storage = tmp_path / "storage"
    config = CodexRepairConfig(
        repository_root=repository,
        storage_root=storage,
        worktree_root=repository / "_worktrees" / "self_healing",
        enabled=enabled,
        mode=mode,
        codex_timeout_seconds=30,
        validation_timeout_seconds=30,
    )
    executor = CodexRepairExecutor(
        config,
        command_runner=runner,
        codex_locator=lambda _explicit: "codex.exe",
    )
    return executor, repository, storage


def _config_overrides(argv):
    return [argv[index + 1] for index, item in enumerate(argv[:-1]) if item == "-c"]


def _parsed_config(argv):
    return tomllib.loads("\n".join(_config_overrides(argv)))


def test_disabled_executor_never_discovers_or_runs_codex(tmp_path):
    runner = _Runner()
    executor, _repository_root, storage = _executor(
        tmp_path, runner, enabled=False
    )
    incident = _incident(storage)
    executor._locate_codex = lambda _explicit: (_ for _ in ()).throw(
        AssertionError("Codex discovery should not run")
    )

    result = executor.process(incident)

    assert result.status == "disabled"
    assert result.reason == "explicit_enable_required"
    assert runner.calls == []


def test_diagnosis_uses_read_only_noninteractive_codex_and_runtime_guards(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-leak")
    monkeypatch.setenv("CI_JOB_TOKEN", "must-not-leak")
    monkeypatch.setenv("PATH", "safe-path")
    runner = _Runner()
    executor, repository, storage = _executor(tmp_path, runner)
    incident = _incident(storage)

    result = executor.process(incident.persisted_path)

    assert result.status == "diagnosed"
    codex_argv, kwargs = next(
        call
        for call in runner.calls
        if Path(call[0][0]).name == "codex.exe" and "exec" in call[0]
    )
    assert codex_argv[:4] == ["codex.exe", "-a", "never", "exec"]
    assert "-s" not in codex_argv
    assert "--ignore-user-config" in codex_argv
    assert "--ephemeral" in codex_argv
    assert "--search" not in codex_argv
    assert "--add-dir" not in codex_argv
    assert kwargs["output_reporter"] is not None
    assert kwargs["visible_process"] is False
    assert kwargs["shell"] is False
    assert Path(kwargs["cwd"]) == Path(result.worktree_path)
    assert Path(result.worktree_path).is_dir()
    assert Path(result.worktree_path) != repository
    assert kwargs["env"]["HEIYUE_CODEX_REPAIR"] == "1"
    assert kwargs["env"]["HEIYUE_SELF_HEALING_RUNNER"] == "1"
    assert kwargs["env"]["PATH"] == "safe-path"
    assert "OPENAI_API_KEY" not in kwargs["env"]
    assert "CI_JOB_TOKEN" not in kwargs["env"]
    assert kwargs["env"]["HEIYUE_RUNTIME_DIR"] == str(
        (repository / "logs" / "runtime").resolve()
    )
    assert kwargs["env"]["HEIYUE_TEST_PYTHON"] == str(
        (repository / ".venv" / "Scripts" / "python.exe").resolve()
    )
    assert Path(result.codex_output_path).is_file()
    assert "done" in Path(result.codex_output_path).read_text(encoding="utf-8")
    config = _parsed_config(codex_argv)
    assert config["model_reasoning_effort"] == "minimal"
    assert config["service_tier"] == "fast"
    assert config["features"]["fast_mode"] is True
    if sys.platform == "win32":
        assert config["windows"]["sandbox"] == "elevated"
    profile = config["permissions"]["heiyue_diagnose"]
    filesystem = profile["filesystem"]
    assert config["default_permissions"] == "heiyue_diagnose"
    assert profile["extends"] == ":workspace"
    assert filesystem[":root"] == "deny"
    assert filesystem[":minimal"] == "read"
    assert filesystem[":tmpdir"] == "deny"
    assert filesystem[":slash_tmp"] == "deny"
    assert filesystem[":workspace_roots"]["."] == "read"
    assert filesystem[":workspace_roots"][".git"] == "read"
    assert filesystem[":workspace_roots"][".codex"] == "read"
    assert filesystem[":workspace_roots"]["AGENTS.md"] == "read"
    assert filesystem[":workspace_roots"]["**/*.env"] == "deny"
    assert str(repository.resolve()) not in filesystem
    assert filesystem[str(repository.resolve() / ".venv")] == "read"
    assert filesystem[kwargs["env"]["TEMP"]] == "write"
    assert profile["network"]["enabled"] is False


def test_repair_keeps_detached_candidate_without_executing_model_authored_code(
    tmp_path,
):
    runner = _Runner()
    executor, _repository_root, storage = _executor(
        tmp_path, runner, mode="repair"
    )
    incident = _incident(storage)

    result = executor.process(incident)

    assert result.status == "candidate_unvalidated"
    assert result.reason == "automatic_candidate_execution_prohibited"
    assert result.validation_returncode is None
    assert result.changed_files == [
        "core/services/example.py",
        "tests/example_regression_test.py",
    ]
    assert Path(result.worktree_path).is_dir()
    assert Path(result.worktree_path).is_relative_to(
        _repository_root / "_worktrees"
    )
    assert result.branch_name.startswith(f"codex/self-heal/{incident.id}-")
    assert result.branch_name.endswith(result.attempt_id[:12])
    worktree_call = next(
        argv for argv, _kwargs in runner.calls if argv[1:4] == ["worktree", "add", "-b"]
    )
    assert worktree_call[4] == result.branch_name
    assert worktree_call[-1] == REVISION
    codex_call = next(
        argv
        for argv, _kwargs in runner.calls
        if Path(argv[0]).name == "codex.exe" and "exec" in argv
    )
    assert "-s" not in codex_call
    config = _parsed_config(codex_call)
    profile = config["permissions"]["heiyue_repair"]
    assert config["default_permissions"] == "heiyue_repair"
    assert profile["extends"] == ":workspace"
    assert profile["filesystem"][":root"] == "deny"
    assert profile["filesystem"][":workspace_roots"]["."] == "write"
    assert profile["filesystem"][":workspace_roots"][".git"] == "read"
    assert profile["filesystem"][result.worktree_path] == "write"
    assert profile["network"]["enabled"] is False
    assert not any(
        Path(argv[0]).name.casefold() in {"python.exe", "python"}
        for argv, _kwargs in runner.calls
    )


def test_background_codex_output_is_jsonl_before_runner_returns(tmp_path):
    observed = {}

    def observe_output(_argv, kwargs):
        reporter = kwargs.get("output_reporter")
        assert reporter is not None
        assert kwargs["visible_process"] is False
        reporter("stdout", '{"type":"progress","message":"working"}')
        reporter("stderr", "background warning")
        output_path = next(
            (tmp_path / "storage" / "runs").glob("*/codex-output.jsonl")
        )
        observed["records"] = [
            json.loads(line)
            for line in output_path.read_text(encoding="utf-8").splitlines()
        ]
        observed["raw"] = output_path.read_text(encoding="utf-8")

    executor, _repository_root, storage = _executor(
        tmp_path, _Runner(on_codex=observe_output)
    )

    result = executor.process(_incident(storage))

    assert result.status == "diagnosed"
    assert observed["records"] == [
        {"type": "progress", "message": "working"},
        {
            "type": "stderr",
            "stream": "stderr",
            "text": "background warning",
        },
    ]
    assert observed["raw"].startswith(
        '{"type":"progress","message":"working"}\n'
    )
    final_records = [
        json.loads(line)
        for line in Path(result.codex_output_path).read_text(
            encoding="utf-8"
        ).splitlines()
    ]
    assert final_records == observed["records"]


def test_output_reporter_serializes_concurrent_streams_and_bounds_log(
    tmp_path, monkeypatch
):
    monkeypatch.setattr("core.services.codex_repair.MAX_CAPTURE_CHARS", 2_000)

    def report_concurrently(_argv, kwargs):
        reporter = kwargs["output_reporter"]

        def emit(stream_name):
            for index in range(50):
                reporter(stream_name, f"{stream_name}-{index}-" + "x" * 40)

        import threading

        threads = [
            threading.Thread(target=emit, args=(stream_name,))
            for stream_name in ("stdout", "stderr")
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

    executor, _repository_root, storage = _executor(
        tmp_path, _Runner(on_codex=report_concurrently)
    )

    result = executor.process(_incident(storage))

    payload = Path(result.codex_output_path).read_bytes()
    assert len(payload) <= 2_000
    records = [json.loads(line) for line in payload.decode("utf-8").splitlines()]
    assert records
    assert records[-1].get("truncated") is True
    assert {record["stream"] for record in records[:-1]} <= {"stdout", "stderr"}


def test_dirty_or_revision_mismatched_main_tree_blocks_repair_before_codex(tmp_path):
    for name, runner, incident_revision, reason in (
        ("dirty", _Runner(dirty=True), REVISION, "main_worktree_dirty"),
        (
            "revision",
            _Runner(revision="b" * 40),
            REVISION,
            "incident_revision_mismatch",
        ),
    ):
        case = tmp_path / name
        executor, _repository_root, storage = _executor(
            case, runner, mode="repair"
        )
        result = executor.process(_incident(storage, revision=incident_revision))

        assert result.status == "blocked"
        assert result.reason == reason
        assert not any(Path(call[0][0]).name == "codex.exe" for call in runner.calls)


def test_diagnosis_blocks_when_main_tree_is_dirty(tmp_path):
    runner = _Runner(dirty=True)
    executor, _repository, storage = _executor(tmp_path, runner)

    result = executor.process(_incident(storage))

    assert result.status == "blocked"
    assert result.reason == "main_worktree_dirty"
    assert not any(Path(call[0][0]).name == "codex.exe" for call in runner.calls)


def test_diagnosis_snapshot_does_not_copy_ignored_main_config(tmp_path):
    runner = _Runner()
    executor, repository, storage = _executor(tmp_path, runner)
    (repository / "config").mkdir()
    (repository / "config" / "app.json").write_text(
        '{"token":"must-not-be-visible"}', encoding="utf-8"
    )

    result = executor.process(_incident(storage))

    assert result.status == "diagnosed"
    assert not (Path(result.worktree_path) / "config" / "app.json").exists()


def test_codex_timeout_is_recorded_without_removing_any_artifact(tmp_path):
    runner = _Runner(timeout_codex=True)
    executor, _repository_root, storage = _executor(tmp_path, runner)
    incident = _incident(storage)

    result = executor.process(incident)

    assert result.status == "failed"
    assert result.reason == "codex_timeout"
    assert Path(result.run_path).is_file()
    assert "partial" in Path(result.codex_output_path).read_text(encoding="utf-8")


def test_repair_without_changed_regression_test_is_retained_as_unvalidated(tmp_path):
    runner = _Runner(include_test=False)
    executor, _repository_root, storage = _executor(
        tmp_path, runner, mode="repair"
    )
    incident = _incident(storage)

    result = executor.process(incident)

    assert result.status == "candidate_unvalidated"
    assert result.reason == "automatic_candidate_execution_prohibited"
    assert result.succeeded is False
    assert Path(result.worktree_path).is_dir()


def test_repair_with_no_diff_reports_no_change_and_keeps_worktree(tmp_path):
    runner = _Runner(changed=False)
    executor, _repository_root, storage = _executor(
        tmp_path, runner, mode="repair"
    )

    result = executor.process(_incident(storage))

    assert result.status == "no_change"
    assert Path(result.worktree_path).is_dir()


def test_candidate_touching_safety_policy_is_rejected_before_validation(
    tmp_path, monkeypatch
):
    runner = _Runner()
    executor, _repository_root, storage = _executor(
        tmp_path, runner, mode="repair"
    )
    monkeypatch.setattr(
        executor,
        "_changed_files",
        lambda _worktree: [
            "core/services/repair_safety.py",
            "tests/example_regression_test.py",
        ],
    )

    result = executor.process(_incident(storage))

    assert result.status == "failed"
    assert result.reason == "policy_violation_protected_path"
    assert not any(
        Path(argv[0]).name.casefold() == "python.exe"
        for argv, _kwargs in runner.calls
    )


def test_change_detection_handles_rename_source_and_fails_closed(tmp_path):
    rename_runner = _Runner(
        status_payload="R  moved.md\0AGENTS.md\0",
    )
    executor, _repository_root, _storage = _executor(tmp_path / "rename", rename_runner)
    assert executor._changed_files(tmp_path / "rename-worktree") == [
        "AGENTS.md",
        "moved.md",
    ]

    failing_runner = _Runner(status_returncode=1)
    executor, _repository_root, storage = _executor(
        tmp_path / "failure", failing_runner, mode="repair"
    )
    result = executor.process(_incident(storage))
    assert result.status == "failed"
    assert result.reason == "change_detection_failed"


def test_worktree_git_pointer_is_verified_before_and_after_codex(tmp_path):
    runner = _Runner(mutate_git_marker=True)
    executor, _repository_root, storage = _executor(
        tmp_path, runner, mode="repair"
    )

    result = executor.process(_incident(storage))

    assert result.status == "failed"
    assert result.reason == "policy_violation_worktree_git_metadata"
    assert not any(
        argv[1:4] == ["status", "--porcelain=v1", "-z"]
        for argv, _kwargs in runner.calls
        if Path(argv[0]).name.casefold() == "git"
    )


def test_reused_diagnosis_worktree_marker_is_checked_before_git(tmp_path):
    runner = _Runner()
    executor, _repository_root, storage = _executor(tmp_path, runner)
    incident = _incident(storage)
    worktree = executor.config.worktree_root / (
        f"diagnose-{incident.fingerprint[:24]}"
    )
    worktree.mkdir(parents=True)
    (worktree / ".git").write_text("gitdir: malicious", encoding="utf-8")

    result = executor.process(incident)

    assert result.status == "blocked"
    assert result.reason == "diagnosis_worktree_invalid"
    assert not any(
        Path(kwargs["cwd"]).resolve() == worktree.resolve()
        for argv, kwargs in runner.calls
        if Path(argv[0]).name.casefold() == "git"
    )


def test_installed_codex_accepts_generated_permission_profile(tmp_path):
    codex = discover_codex_executable()
    if codex is None:
        pytest.skip("Codex CLI is not installed")
    runner = _Runner()
    executor, _repository, _storage = _executor(tmp_path, runner)
    worktree = _repository / "_worktrees" / "sandbox-worktree"
    runtime_temp = _repository / "_worktrees" / "self_healing" / "_runtime"
    worktree.mkdir(parents=True)
    (worktree / ".git").write_text("gitdir: parser-smoke", encoding="utf-8")
    (worktree / ".codex").mkdir()
    (worktree / "AGENTS.md").write_text("smoke", encoding="utf-8")
    runtime_temp.mkdir(parents=True)
    arguments = executor._permission_arguments(
        worktree, runtime_temp=runtime_temp, writable=True
    )

    completed = subprocess.run(
        [codex, "debug", "prompt-input", *arguments, "permission-smoke"],
        cwd=worktree,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=30,
        shell=False,
    )

    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    visible_text = "\n".join(
        content.get("text", "")
        for item in payload
        for content in item.get("content", [])
        if isinstance(content, dict)
    )
    assert '<permission_profile type="managed">' in visible_text
    assert "<special>:root</special>" in visible_text
    assert (
        f'<entry access="read"><path>{(worktree / ".git").resolve()}</path></entry>'
        in visible_text
    )
    assert (
        f'<entry access="read"><path>{(worktree / ".codex").resolve()}</path></entry>'
        in visible_text
    )
    assert (
        f'<entry access="write"><path>{worktree.resolve()}</path></entry>'
        in visible_text
    )
    assert (
        f'<entry access="write"><path>{runtime_temp.resolve()}</path></entry>'
        in visible_text
    )
    assert (
        f'<entry access="read"><path>'
        f'{(executor.config.repository_root / ".venv").resolve()}</path></entry>'
        in visible_text
    )
    sibling = executor.config.repository_root / "_worktrees" / "sibling"
    assert f'<entry access="write"><path>{sibling.resolve()}</path></entry>' not in visible_text
    assert (
        f'<entry access="deny" escalatable="false"><path>'
        f'{executor.config.repository_root}</path></entry>'
        not in visible_text
    )


@pytest.mark.skipif(sys.platform != "win32", reason="Windows sandbox enforcement")
def test_installed_codex_enforces_project_local_repair_write_boundaries(tmp_path):
    if os.environ.get("HEIYUE_RUN_CODEX_SANDBOX_IO_TEST") != "1":
        pytest.skip("requires a provisioned elevated Codex Windows sandbox")
    codex = discover_codex_executable()
    if codex is None:
        pytest.skip("Codex CLI is not installed")
    repository = Path(__file__).resolve().parents[1]
    worktree_root = repository / "_worktrees" / "self_healing"
    suffix = uuid.uuid4().hex[:12]
    worktree = worktree_root / f"permission-smoke-{suffix}"
    sibling = worktree_root / f"permission-sibling-{suffix}"
    runtime_temp = worktree_root / "_runtime" / f"permission-smoke-{suffix}"
    main_probe = repository / f"permission-main-{suffix}.txt"
    venv_probe = repository / ".venv" / f"permission-venv-{suffix}.txt"
    worktree.mkdir(parents=True)
    sibling.mkdir(parents=True)
    runtime_temp.mkdir(parents=True)
    (worktree / ".git").write_text("gitdir: parser-smoke", encoding="utf-8")
    (worktree / ".codex").mkdir()
    (worktree / "AGENTS.md").write_text("smoke", encoding="utf-8")
    (sibling / "existing.txt").write_text("sibling", encoding="utf-8")

    executor = CodexRepairExecutor(
        CodexRepairConfig(
            repository_root=repository,
            storage_root=tmp_path / "storage",
            worktree_root=worktree_root,
        ),
        command_runner=_Runner(),
    )
    arguments = executor._permission_arguments(
        worktree, runtime_temp=runtime_temp, writable=True
    )
    paths = {
        "candidate_write": str(worktree / "candidate.txt"),
        "runtime_write": str(runtime_temp / "runtime.txt"),
        "main_write": str(main_probe),
        "main_read": str(repository / "docs" / "SELF_HEALING.md"),
        "sibling_read": str(sibling / "existing.txt"),
        "sibling_write": str(sibling / "candidate.txt"),
        "git_write": str(worktree / ".git"),
        "codex_write": str(worktree / ".codex" / "candidate.txt"),
        "agents_write": str(worktree / "AGENTS.md"),
        "venv_write": str(venv_probe),
    }
    script = """
import json, pathlib, sys
paths = json.loads(sys.argv[1])
outcomes = {}
for name in ("candidate_write", "runtime_write", "main_write", "sibling_write", "git_write", "codex_write", "agents_write", "venv_write"):
    try:
        pathlib.Path(paths[name]).write_text("probe", encoding="utf-8")
        outcomes[name] = "allowed"
    except OSError:
        outcomes[name] = "denied"
for name in ("main_read", "sibling_read"):
    try:
        pathlib.Path(paths[name]).read_text(encoding="utf-8")
        outcomes[name] = "allowed"
    except OSError:
        outcomes[name] = "denied"
print(json.dumps(outcomes, sort_keys=True))
expected = {name: "denied" for name in outcomes}
expected["candidate_write"] = "allowed"
expected["runtime_write"] = "allowed"
# Native Windows does not apply permission-profile deny_read rules to shell
# subprocess reads. Direct Codex file tools still receive the deny policy.
expected["main_read"] = "allowed"
expected["sibling_read"] = "allowed"
raise SystemExit(0 if outcomes == expected else 9)
    """
    try:
        try:
            preflight = subprocess.run(
                [
                    codex,
                    "sandbox",
                    *arguments,
                    "-P",
                    "heiyue_repair",
                    "-C",
                    str(worktree),
                    os.environ.get("COMSPEC", "cmd.exe"),
                    "/d",
                    "/c",
                    "exit",
                    "0",
                ],
                cwd=worktree,
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                timeout=20,
                shell=False,
            )
        except subprocess.TimeoutExpired:
            pytest.skip("Codex elevated Windows sandbox preflight timed out")
        assert preflight.returncode == 0, preflight.stderr or preflight.stdout
        try:
            completed = subprocess.run(
                [
                    codex,
                    "sandbox",
                    *arguments,
                    "-P",
                    "heiyue_repair",
                    "-C",
                    str(worktree),
                    sys.executable,
                    "-I",
                    "-S",
                    "-c",
                    script,
                    json.dumps(paths),
                ],
                cwd=worktree,
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                timeout=30,
                shell=False,
            )
        except subprocess.TimeoutExpired:
            pytest.skip("Codex elevated Windows sandbox setup is incomplete")
        sandbox_error = f"{completed.stdout}\n{completed.stderr}".casefold()
        if completed.returncode != 0 and any(
            marker in sandbox_error
            for marker in (
                "requires the elevated windows sandbox backend",
                "createrestrictedtoken failed",
            )
        ):
            pytest.skip("Codex Windows sandbox backend is not provisioned")
        assert completed.returncode == 0, completed.stderr or completed.stdout
        outcomes = json.loads(completed.stdout.strip().splitlines()[-1])
        assert outcomes["candidate_write"] == "allowed"
        assert outcomes["runtime_write"] == "allowed"
        assert outcomes["main_read"] == "allowed"
        assert outcomes["sibling_read"] == "allowed"
        assert all(
            outcome == "denied"
            for name, outcome in outcomes.items()
            if name
            not in {
                "candidate_write",
                "runtime_write",
                "main_read",
                "sibling_read",
            }
        )
    finally:
        for path in (main_probe, venv_probe):
            path.unlink(missing_ok=True)
        shutil.rmtree(worktree, ignore_errors=True)
        shutil.rmtree(sibling, ignore_errors=True)
        shutil.rmtree(runtime_temp, ignore_errors=True)


def test_status_listing_returns_most_recent_runs(tmp_path):
    storage = tmp_path / "storage"
    older = storage / "runs" / "older" / "status.json"
    newer = storage / "runs" / "newer" / "status.json"
    older.parent.mkdir(parents=True)
    newer.parent.mkdir(parents=True)
    older.write_text(json.dumps({"incident_id": "older"}), encoding="utf-8")
    newer.write_text(json.dumps({"incident_id": "newer"}), encoding="utf-8")
    older.touch()
    newer.touch()

    statuses = list_run_statuses(storage, limit=2)

    assert {item["incident_id"] for item in statuses} == {"older", "newer"}


def test_bounded_command_runner_terminates_timed_out_process():
    with pytest.raises(subprocess.TimeoutExpired):
        run_process_tree(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            text=True,
            capture_output=True,
            timeout=0.2,
            shell=False,
        )


def test_bounded_command_runner_delivers_input_to_child_stdin():
    completed = run_process_tree(
        [
            sys.executable,
            "-c",
            "import sys; print(sys.stdin.read(), end='')",
        ],
        input="prompt-through-stdin",
        text=True,
        capture_output=True,
        timeout=5,
        shell=False,
    )

    assert completed.returncode == 0
    assert completed.stdout == "prompt-through-stdin"


def test_bounded_command_runner_streams_and_retains_child_output():
    observed = []
    completed = run_process_tree(
        [
            sys.executable,
            "-c",
            (
                "import sys; data=sys.stdin.read(); "
                "print('out:' + data, flush=True); "
                "print('err:' + data, file=sys.stderr, flush=True)"
            ),
        ],
        input="visible",
        text=True,
        capture_output=True,
        timeout=5,
        shell=False,
        output_reporter=lambda stream, line: observed.append((stream, line)),
    )

    assert completed.returncode == 0
    assert completed.stdout == "out:visible\n"
    assert completed.stderr == "err:visible\n"
    assert ("stdout", "out:visible") in observed
    assert ("stderr", "err:visible") in observed


def test_stream_capture_is_bounded_while_both_pipes_are_drained():
    completed = run_process_tree(
        [
            sys.executable,
            "-c",
            (
                "import sys; "
                f"sys.stdout.write('x' * {MAX_CAPTURE_CHARS + 8192}); "
                "sys.stdout.flush(); "
                "sys.stderr.write('y' * 200000); sys.stderr.flush()"
            ),
        ],
        text=True,
        capture_output=True,
        timeout=10,
        shell=False,
        output_reporter=lambda *_args: None,
    )

    assert completed.returncode == 0
    assert len(completed.stdout) < MAX_CAPTURE_CHARS + 100
    assert "stream capture truncated" in completed.stdout
    assert completed.stderr == "y" * 200000


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Job Object behavior")
def test_streaming_does_not_wait_for_descendant_inherited_pipes(tmp_path):
    pid_path = tmp_path / "grandchild.pid"
    code = (
        "import pathlib, subprocess, sys; "
        "child=subprocess.Popen([sys.executable, '-c', "
        "'import time; time.sleep(10)']); "
        f"pathlib.Path({str(pid_path)!r}).write_text(str(child.pid)); "
        "print('parent-exit', flush=True)"
    )
    started = time.monotonic()
    completed = run_process_tree(
        [sys.executable, "-c", code],
        text=True,
        capture_output=True,
        timeout=5,
        shell=False,
        output_reporter=lambda *_args: None,
    )
    elapsed = time.monotonic() - started
    grandchild_pid = int(pid_path.read_text())
    grandchild_alive = False
    try:
        grandchild = psutil.Process(grandchild_pid)
        grandchild_alive = grandchild.is_running()
        if grandchild_alive:
            grandchild.terminate()
            grandchild.wait(timeout=2)
    except psutil.Error:
        pass

    assert completed.returncode == 0
    assert "parent-exit" in completed.stdout
    assert elapsed < 3.0
    assert grandchild_alive is False


def test_runner_cleans_up_child_tree_on_keyboard_interrupt(monkeypatch):
    terminated = []

    class InterruptedProcess:
        pid = 4242

        def communicate(self, **_kwargs):
            raise KeyboardInterrupt

    monkeypatch.setattr(
        "core.services.codex_repair.subprocess.Popen",
        lambda *_args, **_kwargs: InterruptedProcess(),
    )
    monkeypatch.setattr(
        "core.services.codex_repair._terminate_process_tree",
        lambda pid: terminated.append(pid),
    )

    with pytest.raises(KeyboardInterrupt):
        run_process_tree(["interrupted-child"], shell=False)

    assert terminated == [4242]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Job Object behavior")
def test_required_windows_process_containment_fails_closed(monkeypatch):
    terminated = []

    class UncontainedProcess:
        pid = 4343

    monkeypatch.setattr(
        "core.services.codex_repair.subprocess.Popen",
        lambda *_args, **_kwargs: UncontainedProcess(),
    )
    monkeypatch.setattr(
        "core.services.codex_repair._create_windows_kill_job",
        lambda _process: None,
    )
    monkeypatch.setattr(
        "core.services.codex_repair._terminate_process_tree",
        lambda pid: terminated.append(pid),
    )

    with pytest.raises(ProcessContainmentUnavailable):
        run_process_tree(
            ["uncontained-child"],
            shell=False,
            require_process_containment=True,
        )

    assert terminated == [4343]


def test_worktree_root_is_restricted_to_project_worktrees_directory(tmp_path):
    repository = _repository(tmp_path)

    with pytest.raises(ValueError, match="inside repository_root"):
        CodexRepairConfig(
            repository_root=repository,
            worktree_root=tmp_path / "outside",
        )
    with pytest.raises(ValueError, match="repository_root/_worktrees"):
        CodexRepairConfig(
            repository_root=repository,
            worktree_root=repository / "core" / "self_healing",
        )


def test_running_status_exposes_project_worktree_and_branch_before_codex(tmp_path):
    observed = {}

    def observe_status(_argv, _kwargs):
        status_path = next(
            (tmp_path / "storage" / "runs").glob("*/status.json")
        )
        observed.update(json.loads(status_path.read_text(encoding="utf-8")))

    runner = _Runner(on_codex=observe_status)
    executor, repository, storage = _executor(
        tmp_path, runner, mode="repair"
    )
    result = executor.process(_incident(storage))

    assert result.status == "candidate_unvalidated"
    assert observed["status"] == "codex_running"
    assert observed["branch_name"] == result.branch_name
    assert Path(observed["worktree_path"]).is_relative_to(
        repository / "_worktrees"
    )


def test_retries_use_independent_attempt_artifacts_and_runtime_temp(tmp_path):
    runner = _Runner(changed=False)
    executor, _repository_root, storage = _executor(
        tmp_path, runner, mode="repair"
    )
    incident = _incident(storage)

    first = executor.process(incident)
    first_status = Path(first.run_path).read_text(encoding="utf-8")
    second = executor.process(incident)

    assert first.status == second.status == "no_change"
    assert first.attempt_id != second.attempt_id
    assert first.worktree_path != second.worktree_path
    assert first.branch_name != second.branch_name
    assert first.run_path != second.run_path
    assert first.codex_output_path != second.codex_output_path
    assert Path(first.run_path).read_text(encoding="utf-8") == first_status
    codex_calls = [
        kwargs
        for argv, kwargs in runner.calls
        if Path(argv[0]).name.casefold() == "codex.exe" and "exec" in argv
    ]
    assert codex_calls[0]["env"]["TEMP"] != codex_calls[1]["env"]["TEMP"]
    worktree_calls = [
        argv for argv, _kwargs in runner.calls if argv[1:4] == ["worktree", "add", "-b"]
    ]
    assert len(worktree_calls) == 2
    assert all("-B" not in argv and "--force" not in argv for argv in worktree_calls)


def test_operator_interrupt_is_recorded_as_terminal_status(tmp_path):
    class InterruptingRunner(_Runner):
        def __call__(self, argv, **kwargs):
            if (
                Path(str(argv[0])).name.casefold() == "codex.exe"
                and "exec" in argv
            ):
                raise KeyboardInterrupt
            return super().__call__(argv, **kwargs)

    executor, _repository_root, storage = _executor(
        tmp_path, InterruptingRunner(), mode="repair"
    )

    result = executor.process(_incident(storage))

    assert result.status == "interrupted"
    assert result.reason == "operator_interrupted"
    assert "Interrupted by operator" in Path(result.codex_output_path).read_text(
        encoding="utf-8"
    )


def test_operator_interrupt_during_worktree_preparation_is_terminal(tmp_path):
    runner = _Runner()

    def interrupt_first_git(_argv, **_kwargs):
        raise KeyboardInterrupt

    executor, _repository_root, storage = _executor(
        tmp_path, runner, mode="repair"
    )
    executor._run = interrupt_first_git

    result = executor.process(_incident(storage))

    assert result.status == "interrupted"
    assert result.reason == "operator_interrupted_during_preparation"
    status = json.loads(Path(result.run_path).read_text(encoding="utf-8"))
    assert status["status"] == "interrupted"


def test_executor_records_unavailable_process_containment(tmp_path):
    class UncontainedRunner(_Runner):
        def __call__(self, argv, **kwargs):
            if Path(str(argv[0])).name.casefold() == "codex.exe":
                raise ProcessContainmentUnavailable("job assignment failed")
            return super().__call__(argv, **kwargs)

    executor, _repository_root, storage = _executor(
        tmp_path, UncontainedRunner(), mode="repair"
    )

    result = executor.process(_incident(storage))

    assert result.status == "blocked"
    assert result.reason == "process_containment_unavailable"


def test_executor_reports_missing_windows_sandbox_backend(tmp_path):
    class MissingSandboxRunner(_Runner):
        def __call__(self, argv, **kwargs):
            if Path(str(argv[0])).name.casefold() == "codex.exe":
                return subprocess.CompletedProcess(
                    argv,
                    1,
                    "",
                    "windows sandbox failed: CreateRestrictedToken failed: 87",
                )
            return super().__call__(argv, **kwargs)

    executor, _repository_root, storage = _executor(
        tmp_path, MissingSandboxRunner(), mode="repair"
    )

    result = executor.process(_incident(storage))

    assert result.status == "blocked"
    assert result.reason == "windows_sandbox_backend_unavailable"


def test_outer_interrupt_guard_records_post_codex_validation_interrupt(tmp_path):
    executor, _repository_root, storage = _executor(
        tmp_path, _Runner(), mode="repair"
    )
    executor._changed_files = lambda _worktree: (_ for _ in ()).throw(
        KeyboardInterrupt
    )

    result = executor.process(_incident(storage))

    assert result.status == "interrupted"
    assert result.reason == "operator_interrupted"
    assert json.loads(Path(result.run_path).read_text(encoding="utf-8"))[
        "status"
    ] == "interrupted"


def test_legacy_external_worktrees_are_reported_without_mutation(tmp_path):
    legacy_root = tmp_path / ".legacy-self-healing"
    repository = tmp_path / "repo"
    git_dir = repository / ".git" / "worktrees" / "old-incident"
    git_dir.mkdir(parents=True)
    valid = legacy_root / "old-incident"
    invalid = legacy_root / "not-a-worktree"
    valid.mkdir(parents=True)
    invalid.mkdir()
    (valid / ".git").write_text(f"gitdir: {git_dir}", encoding="utf-8")
    (invalid / ".git").write_text(
        "gitdir: C:/unrelated/.git/worktrees/other", encoding="utf-8"
    )
    before = (valid / ".git").read_text(encoding="utf-8")

    found = list_legacy_worktrees(
        legacy_root, repository_root=repository
    )

    assert found == [str(valid.resolve())]
    assert (valid / ".git").read_text(encoding="utf-8") == before


def test_real_git_creates_repair_branch_in_project_worktree(tmp_path):
    repository = tmp_path / "real-repo"
    repository.mkdir()

    def git(*arguments, cwd=repository):
        return subprocess.run(
            ["git", *arguments],
            cwd=cwd,
            text=True,
            capture_output=True,
            timeout=30,
            shell=False,
            check=True,
        )

    git("init")
    git("config", "user.name", "Codex test")
    git("config", "user.email", "codex-test@example.invalid")
    (repository / ".gitignore").write_text(
        "_worktrees/\nlogs/\n", encoding="utf-8"
    )
    (repository / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
    git("add", ".gitignore", "module.py")
    git("commit", "-m", "fixture")
    revision = git("rev-parse", "HEAD").stdout.strip()

    storage = repository / "logs" / "self_healing"
    worktree_root = repository / "_worktrees" / "self_healing"
    incident = _incident(storage, revision=revision)

    def runner(argv, **kwargs):
        if Path(str(argv[0])).name.casefold() in {"git", "git.exe"}:
            return run_process_tree(argv, **kwargs)
        assert Path(str(argv[0])).name.casefold() == "codex.exe"
        if len(argv) > 1 and argv[1] == "sandbox":
            return subprocess.CompletedProcess(argv, 0, "sandbox-ok", "")
        assert kwargs["input"]
        return subprocess.CompletedProcess(argv, 0, "done", "")

    executor = CodexRepairExecutor(
        CodexRepairConfig(
            repository_root=repository,
            storage_root=storage,
            worktree_root=worktree_root,
            enabled=True,
            mode="repair",
        ),
        command_runner=runner,
        codex_locator=lambda _explicit: "codex.exe",
    )

    result = executor.process(incident)

    assert result.status == "no_change"
    assert Path(result.worktree_path).is_relative_to(repository / "_worktrees")
    assert (
        git("branch", "--show-current", cwd=Path(result.worktree_path)).stdout.strip()
        == result.branch_name
    )
    assert git("rev-parse", "HEAD", cwd=Path(result.worktree_path)).stdout.strip() == revision
    assert git("status", "--porcelain", cwd=repository).stdout.strip() == ""
