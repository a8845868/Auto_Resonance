"""Hard safety boundary for Codex repair subprocesses.

The self-healing agent may inspect and edit an isolated Git worktree, but it
must never attach to or control the user's live emulator. Prompt rules provide
context; this environment-backed guard adds a defense against accidental I/O.
It is not a substitute for the Codex sandbox or operating-system isolation.
"""

from __future__ import annotations

import os
from collections.abc import Mapping


REPAIR_ENV_VAR = "HEIYUE_CODEX_REPAIR"


class RepairSafetyError(RuntimeError):
    """Raised when a repair child attempts live automation I/O."""


def is_repair_process(env: Mapping[str, str] | None = None) -> bool:
    values = os.environ if env is None else env
    return str(values.get(REPAIR_ENV_VAR, "")).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def ensure_automation_allowed(operation: str) -> None:
    if is_repair_process():
        raise RepairSafetyError(
            f"Codex 隔离修复进程禁止访问模拟器或游戏：{operation}"
        )
