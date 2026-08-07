import importlib.util
import os
import subprocess
from importlib.machinery import SourceFileLoader
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]


def _load_launcher():
    loader = SourceFileLoader("gui_launcher_under_test", str(ROOT / "gui_launcher.pyw"))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def _capture_message_boxes(monkeypatch, module):
    calls = []
    user32 = SimpleNamespace(
        MessageBoxW=lambda *args: calls.append(args),
    )
    monkeypatch.setattr(module.ctypes, "windll", SimpleNamespace(user32=user32))
    return calls


def test_runtime_busy_is_reported_as_intentional_handoff(tmp_path, monkeypatch):
    launcher = _load_launcher()
    launcher.LOG_FILE = tmp_path / "gui-startup-error.log"
    messages = _capture_message_boxes(monkeypatch, launcher)

    launcher.report_runtime_busy({"mode": "debug", "pid": 2468})

    log = launcher.LOG_FILE.read_text(encoding="utf-8")
    assert "后台调试正在运行（PID 2468）" in log
    assert "不是图形界面崩溃" in log
    assert len(messages) == 1
    assert "debug_runner.py stop --mode debug" in messages[0][1]


def test_startup_error_writes_traceback_and_shows_error(tmp_path, monkeypatch):
    launcher = _load_launcher()
    launcher.LOG_FILE = tmp_path / "gui-startup-error.log"
    messages = _capture_message_boxes(monkeypatch, launcher)
    incidents = []
    monkeypatch.setattr(launcher, "submit_startup_incident", incidents.append)

    try:
        raise RuntimeError("startup exploded")
    except RuntimeError:
        launcher.report_startup_error()

    log = launcher.LOG_FILE.read_text(encoding="utf-8")
    assert "RuntimeError: startup exploded" in log
    assert len(incidents) == 1
    assert "RuntimeError: startup exploded" in incidents[0]
    assert len(messages) == 1
    assert "图形界面启动失败" in messages[0][1]


def test_startup_report_reads_both_self_healing_switches(tmp_path):
    launcher = _load_launcher()
    launcher.CONFIG_FILE = tmp_path / "app.json"
    launcher.CONFIG_FILE.write_text(
        '{"SelfHealing":{"Enabled":true,"AllowIsolatedRepair":false}}',
        encoding="utf-8",
    )

    assert launcher._self_healing_flags() == (True, False)


def test_shared_repository_root_resolves_linked_worktree_gitdir(tmp_path):
    launcher = _load_launcher()
    repository = tmp_path / "repository"
    git_dir = repository / ".git" / "worktrees" / "feature"
    git_dir.mkdir(parents=True)
    worktree = tmp_path / "feature-worktree"
    worktree.mkdir()
    (worktree / ".git").write_text(f"gitdir: {git_dir}\n", encoding="utf-8")

    assert launcher._shared_repository_root(worktree) == repository


def test_project_pythonw_uses_shared_repository_venv(tmp_path):
    launcher = _load_launcher()
    repository = tmp_path / "repository"
    git_dir = repository / ".git" / "worktrees" / "feature"
    git_dir.mkdir(parents=True)
    pythonw = repository / ".venv" / "Scripts" / "pythonw.exe"
    pythonw.parent.mkdir(parents=True)
    pythonw.touch()
    worktree = tmp_path / "feature-worktree"
    worktree.mkdir()
    (worktree / ".git").write_text(f"gitdir: {git_dir}\n", encoding="utf-8")

    selected = launcher._select_project_pythonw(
        root=worktree,
        current_executable=tmp_path / "global" / "pythonw.exe",
        environ={},
    )

    assert selected == pythonw


def test_project_pythonw_does_not_relaunch_same_environment(tmp_path):
    launcher = _load_launcher()
    worktree = tmp_path / "worktree"
    pythonw = worktree / ".venv" / "Scripts" / "pythonw.exe"
    pythonw.parent.mkdir(parents=True)
    pythonw.touch()

    selected = launcher._select_project_pythonw(
        root=worktree,
        current_executable=pythonw.with_name("python.exe"),
        environ={},
    )

    assert selected is None


def test_explicit_pythonw_override_has_priority(tmp_path):
    launcher = _load_launcher()
    worktree = tmp_path / "worktree"
    local_pythonw = worktree / ".venv" / "Scripts" / "pythonw.exe"
    local_pythonw.parent.mkdir(parents=True)
    local_pythonw.touch()
    override = tmp_path / "portable" / "pythonw.exe"
    override.parent.mkdir(parents=True)
    override.touch()

    selected = launcher._select_project_pythonw(
        root=worktree,
        current_executable=tmp_path / "global" / "pythonw.exe",
        environ={"HEIYUE_PYTHONW": str(override)},
    )

    assert selected == override


def test_start_gui_cmd_honors_explicit_pythonw_override(tmp_path):
    pythonw = tmp_path / "portable" / "pythonw.exe"
    pythonw.parent.mkdir(parents=True)
    pythonw.touch()
    environment = os.environ.copy()
    environment["HEIYUE_PYTHONW"] = str(pythonw)

    result = subprocess.run(
        [
            environment.get("COMSPEC", "cmd.exe"),
            "/d",
            "/c",
            str(ROOT / "start-gui.cmd"),
            "--print-python",
        ],
        cwd=ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == str(pythonw)
