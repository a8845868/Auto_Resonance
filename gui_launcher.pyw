"""Start the GUI without a console and make startup failures visible."""

from __future__ import annotations

import ctypes
import datetime as dt
import os
import runpy
import sys
import traceback
from pathlib import Path


ROOT = Path(__file__).resolve().parent
LOG_FILE = ROOT / "logs" / "gui-startup-error.log"


def report_startup_error() -> None:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    details = (
        f"[{dt.datetime.now():%Y-%m-%d %H:%M:%S}]\n"
        f"Python: {sys.executable}\n"
        f"Working directory: {ROOT}\n\n"
        f"{traceback.format_exc()}\n"
    )
    LOG_FILE.write_text(details, encoding="utf-8")
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
