"""Start the GUI without a console and make startup failures visible."""

from __future__ import annotations

import ctypes
import datetime as dt
import json
import os
import runpy
import subprocess
import sys
import traceback
from pathlib import Path
from typing import Mapping


ROOT = Path(__file__).resolve().parent
LOG_FILE = ROOT / "logs" / "gui-startup-error.log"
CONFIG_FILE = ROOT / "config" / "app.json"


def _shared_repository_root(root: Path) -> Path | None:
    """Return the primary checkout root for a linked Git worktree."""

    git_marker = root / ".git"
    if not git_marker.is_file():
        return None
    try:
        marker = git_marker.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return None
    prefix = "gitdir:"
    if not marker.lower().startswith(prefix):
        return None
    git_dir = Path(marker[len(prefix) :].strip())
    if not git_dir.is_absolute():
        git_dir = (root / git_dir).resolve()
    for candidate in (git_dir, *git_dir.parents):
        if candidate.name.lower() == ".git":
            return candidate.parent
    return None


def _pythonw_candidates(
    root: Path,
    environ: Mapping[str, str] | None = None,
) -> tuple[Path, ...]:
    """Build portable interpreter candidates without assuming a worktree path."""

    environ = os.environ if environ is None else environ
    candidates: list[Path] = []
    override = environ.get("HEIYUE_PYTHONW", "").strip()
    if override:
        candidates.append(Path(override))
    active_venv = environ.get("VIRTUAL_ENV", "").strip()
    if active_venv:
        candidates.append(Path(active_venv) / "Scripts" / "pythonw.exe")
    candidates.append(root / ".venv" / "Scripts" / "pythonw.exe")
    shared_root = _shared_repository_root(root)
    if shared_root is not None:
        candidates.append(shared_root / ".venv" / "Scripts" / "pythonw.exe")

    unique: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = os.path.normcase(os.path.abspath(candidate))
        if key not in seen:
            seen.add(key)
            unique.append(candidate)
    return tuple(unique)


def _same_python_environment(left: Path, right: Path) -> bool:
    """Treat python.exe and pythonw.exe in the same Scripts folder as equal."""

    return os.path.normcase(os.path.realpath(left.parent)) == os.path.normcase(
        os.path.realpath(right.parent)
    )


def _select_project_pythonw(
    root: Path = ROOT,
    current_executable: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> Path | None:
    """Select a project interpreter when the launcher used a global Python."""

    current = Path(sys.executable) if current_executable is None else current_executable
    for candidate in _pythonw_candidates(root, environ):
        if not candidate.is_file():
            continue
        if _same_python_environment(candidate, current):
            return None
        return candidate
    return None


def _relaunch_with_project_python() -> bool:
    target = _select_project_pythonw()
    if target is None:
        return False
    subprocess.Popen(
        [str(target), str(Path(__file__).resolve()), *sys.argv[1:]],
        cwd=str(ROOT),
        close_fds=True,
    )
    return True


def _self_healing_flags() -> tuple[bool, bool]:
    try:
        document = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        settings = document.get("SelfHealing", {})
        if not isinstance(settings, dict):
            return False, False
        return (
            settings.get("Enabled") is True,
            settings.get("AllowIsolatedRepair") is True,
        )
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
        return False, False


def submit_startup_incident(traceback_text: str) -> None:
    """Best-effort startup reporting that also works before the GUI imports."""

    try:
        from core.services.self_healing import submit_incident

        enabled, allow_repair = _self_healing_flags()
        submit_incident(
            {
                "source": "gui_launcher",
                "task_key": "gui_startup",
                "task_name": "图形界面启动",
                "failure_kind": "exception",
                "message": (
                    traceback_text.strip().splitlines()[-1]
                    if traceback_text.strip()
                    else "GUI startup failed"
                ),
                "expected": "图形界面取得运行锁并成功启动",
                "observed": "启动边界抛出异常",
                "traceback": traceback_text,
                "context": {"dispatch_allowed": True},
            },
            dispatch=enabled,
            allow_repair=allow_repair,
        )
    except Exception:
        pass


def report_startup_error() -> None:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    traceback_text = traceback.format_exc()
    details = (
        f"[{dt.datetime.now():%Y-%m-%d %H:%M:%S}]\n"
        f"Python: {sys.executable}\n"
        f"Working directory: {ROOT}\n\n"
        f"{traceback_text}\n"
    )
    LOG_FILE.write_text(details, encoding="utf-8")
    submit_startup_incident(traceback_text)
    ctypes.windll.user32.MessageBoxW(
        0,
        f"图形界面启动失败。\n\n错误详情已保存到：\n{LOG_FILE}",
        "黑月无人驾驶 - 启动失败",
        0x10,
    )


def report_runtime_busy(owner: dict) -> None:
    """Explain an intentional GUI/debug handoff without calling it a crash."""
    mode = str(owner.get("mode", "unknown"))
    pid = owner.get("pid", "未知")
    if mode == "debug":
        reason = f"后台调试正在运行（PID {pid}）"
        guidance = (
            "为避免 GUI 和后台同时操作模拟器，图形界面本次没有启动。\n\n"
            "需要切回 GUI 时，请先执行：\n"
            "debug_runner.py stop --mode debug"
        )
    elif mode == "gui":
        reason = f"图形界面已经在运行（PID {pid}）"
        guidance = "无需重复启动；请切换到已经打开的窗口。"
    else:
        reason = f"自动化控制器正由 {mode} 占用（PID {pid}）"
        guidance = "请先安全结束当前自动化运行实例，再启动图形界面。"
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    LOG_FILE.write_text(
        f"[{dt.datetime.now():%Y-%m-%d %H:%M:%S}]\n"
        f"GUI 未启动：{reason}\n"
        "这是运行时互斥保护，不是图形界面崩溃。\n",
        encoding="utf-8",
    )
    ctypes.windll.user32.MessageBoxW(
        0,
        f"{reason}\n\n{guidance}",
        "黑月无人驾驶 - 运行实例提示",
        0x40,
    )


if __name__ == "__main__":
    os.chdir(ROOT)
    LOG_FILE.unlink(missing_ok=True)
    runtime_lease = None
    try:
        if _relaunch_with_project_python():
            raise SystemExit(0)
        from core.services.runtime_control import RuntimeBusyError, acquire_runtime

        try:
            runtime_lease = acquire_runtime("gui")
        except RuntimeBusyError as error:
            report_runtime_busy(error.owner)
        else:
            runpy.run_path(str(ROOT / "gui.py"), run_name="__main__")
    except SystemExit as error:
        if error.code not in (None, 0):
            report_startup_error()
    except BaseException:
        report_startup_error()
    finally:
        if runtime_lease is not None:
            runtime_lease.release()
