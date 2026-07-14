from __future__ import annotations

import json
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from core.services.codex_repair import (
    CodexRepairConfig,
    CodexRepairExecutor,
    discover_codex_executable,
    list_run_statuses,
    run_process_tree,
)
from core.services.incident_learning import IncidentLearningStore


REVISION = "a" * 40


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
            raise AssertionError(f"unexpected Git command: {argv}")
        if executable == "codex.exe":
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
        worktree_root=tmp_path / "worktrees",
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
        call for call in runner.calls if Path(call[0][0]).name == "codex.exe"
    )
    assert codex_argv[:4] == ["codex.exe", "-a", "never", "exec"]
    assert "-s" not in codex_argv
    assert "--ignore-user-config" in codex_argv
    assert "--ephemeral" in codex_argv
    assert "--search" not in codex_argv
    assert "--add-dir" not in codex_argv
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
    assert filesystem[str(repository.resolve())] == "deny"
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
    worktree_call = next(
        argv for argv, _kwargs in runner.calls if argv[1:4] == ["worktree", "add", "--detach"]
    )
    assert worktree_call[-1] == REVISION
    codex_call = next(
        argv for argv, _kwargs in runner.calls if Path(argv[0]).name == "codex.exe"
    )
    assert "-s" not in codex_call
    config = _parsed_config(codex_call)
    profile = config["permissions"]["heiyue_repair"]
    assert config["default_permissions"] == "heiyue_repair"
    assert profile["extends"] == ":workspace"
    assert profile["filesystem"][":root"] == "deny"
    assert profile["filesystem"][":workspace_roots"]["."] == "write"
    assert profile["filesystem"][":workspace_roots"][".git"] == "read"
    assert profile["network"]["enabled"] is False
    assert not any(
        Path(argv[0]).name.casefold() in {"python.exe", "python"}
        for argv, _kwargs in runner.calls
    )


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
    worktree = tmp_path / "sandbox-worktree"
    runtime_temp = tmp_path / "sandbox-runtime"
    worktree.mkdir()
    (worktree / ".git").write_text("gitdir: parser-smoke", encoding="utf-8")
    (worktree / ".codex").mkdir()
    (worktree / "AGENTS.md").write_text("smoke", encoding="utf-8")
    runtime_temp.mkdir()
    arguments = executor._permission_arguments(
        worktree, runtime_temp=runtime_temp, writable=True
    )

    completed = subprocess.run(
        [codex, "debug", "prompt-input", *arguments, "permission-smoke"],
        cwd=worktree,
        text=True,
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
        f'<entry access="deny" escalatable="false"><path>'
        f'{executor.config.repository_root}</path></entry>'
        in visible_text
    )


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
