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


if __name__ == "__main__":
    os.chdir(ROOT)
    LOG_FILE.unlink(missing_ok=True)
    try:
        runpy.run_path(str(ROOT / "gui.py"), run_name="__main__")
    except SystemExit as error:
        if error.code not in (None, 0):
            report_startup_error()
    except BaseException:
        report_startup_error()
