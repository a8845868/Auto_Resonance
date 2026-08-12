"""Cross-process ownership and graceful-stop helpers for automation runtimes."""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import psutil


ROOT = Path(__file__).resolve().parents[2]
RUNTIME_DIR = Path(
    os.environ.get("HEIYUE_RUNTIME_DIR", str(ROOT / "logs" / "runtime"))
).expanduser().resolve()
LEASE_PATH = RUNTIME_DIR / "owner.json"
STOP_PATH = RUNTIME_DIR / "stop.json"
DEBUG_STATUS_PATH = RUNTIME_DIR / "debug-status.json"
COMMAND_DIR = RUNTIME_DIR / "commands"
RESPONSE_DIR = RUNTIME_DIR / "responses"


class RuntimeBusyError(RuntimeError):
    """Raised when another GUI/debug process owns the simulator controller."""

    def __init__(self, owner: dict[str, Any]):
        self.owner = owner
        super().__init__(
            f"自动化控制器已被 {owner.get('mode', 'unknown')} 占用 "
            f"(PID {owner.get('pid', '?')})"
        )


def _now_text() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    temporary.replace(path)


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _process_matches(owner: dict[str, Any] | None) -> bool:
    if not owner:
        return False
    try:
        process = psutil.Process(int(owner["pid"]))
        expected = float(owner.get("create_time", 0))
        return process.is_running() and (
            not expected or abs(process.create_time() - expected) < 1.0
        )
    except (KeyError, TypeError, ValueError, psutil.Error):
        return False


def runtime_owner(clean_stale: bool = True) -> dict[str, Any] | None:
    owner = _read_json(LEASE_PATH)
    if _process_matches(owner):
        return owner
    if clean_stale and LEASE_PATH.exists():
        observed = LEASE_PATH.read_bytes()
        if LEASE_PATH.exists() and LEASE_PATH.read_bytes() == observed:
            LEASE_PATH.unlink(missing_ok=True)
    return None


@dataclass
class RuntimeLease:
    mode: str
    token: str
    pid: int
    create_time: float

    @property
    def owner(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "token": self.token,
            "pid": self.pid,
            "create_time": self.create_time,
        }

    def stop_requested(self) -> bool:
        request = _read_json(STOP_PATH)
        return bool(
            request
            and request.get("target_pid") == self.pid
            and request.get("target_token") == self.token
        )

    def release(self) -> None:
        owner = _read_json(LEASE_PATH)
        if owner and owner.get("token") == self.token:
            LEASE_PATH.unlink(missing_ok=True)
        request = _read_json(STOP_PATH)
        if request and request.get("target_token") == self.token:
            STOP_PATH.unlink(missing_ok=True)

    def __enter__(self) -> "RuntimeLease":
        return self

    def __exit__(self, *_exc) -> None:
        self.release()


def acquire_runtime(mode: str) -> RuntimeLease:
    """Acquire exclusive ownership, removing only a verified stale lease."""
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    for _ in range(3):
        owner = runtime_owner(clean_stale=True)
        if owner:
            raise RuntimeBusyError(owner)
        pid = os.getpid()
        lease = RuntimeLease(
            mode=str(mode),
            token=uuid.uuid4().hex,
            pid=pid,
            create_time=psutil.Process(pid).create_time(),
        )
        data = {
            **lease.owner,
            "started_at": _now_text(),
            "root": str(ROOT),
        }
        try:
            descriptor = os.open(
                LEASE_PATH,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            )
        except FileExistsError:
            time.sleep(0.05)
            continue
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
        STOP_PATH.unlink(missing_ok=True)
        return lease
    owner = runtime_owner(clean_stale=False) or {"mode": "unknown", "pid": "?"}
    raise RuntimeBusyError(owner)


def request_runtime_stop(required_mode: str | None = None) -> dict[str, Any] | None:
    """Ask the current owner to exit; never terminates a process forcibly."""
    owner = runtime_owner(clean_stale=True)
    if not owner:
        return None
    if required_mode and owner.get("mode") != required_mode:
        raise RuntimeBusyError(owner)
    _atomic_write_json(
        STOP_PATH,
        {
            "target_pid": owner["pid"],
            "target_token": owner["token"],
            "requested_at": _now_text(),
        },
    )
    return owner


def stop_requested_for_current_process() -> bool:
    owner = runtime_owner(clean_stale=False)
    request = _read_json(STOP_PATH)
    return bool(
        owner
        and request
        and owner.get("pid") == os.getpid()
        and request.get("target_pid") == os.getpid()
        and request.get("target_token") == owner.get("token")
    )


def wait_for_runtime_exit(owner: dict[str, Any], timeout: float = 15.0) -> bool:
    deadline = time.monotonic() + max(0.0, timeout)
    while time.monotonic() < deadline:
        current = runtime_owner(clean_stale=True)
        if not current or current.get("token") != owner.get("token"):
            return True
        time.sleep(0.2)
    return False


def write_debug_status(data: dict[str, Any]) -> None:
    _atomic_write_json(DEBUG_STATUS_PATH, data)


def read_debug_status() -> dict[str, Any] | None:
    return _read_json(DEBUG_STATUS_PATH)


def write_command(data: dict[str, Any]) -> Path:
    command_id = str(data.get("id") or uuid.uuid4().hex)
    payload = {**data, "id": command_id, "created_at": _now_text()}
    path = COMMAND_DIR / f"{command_id}.json"
    _atomic_write_json(path, payload)
    return path


def write_response(command_id: str, data: dict[str, Any]) -> Path:
    path = RESPONSE_DIR / f"{command_id}.json"
    _atomic_write_json(
        path,
        {**data, "id": command_id, "finished_at": _now_text()},
    )
    return path


def read_response(command_id: str) -> dict[str, Any] | None:
    return _read_json(RESPONSE_DIR / f"{command_id}.json")


def pending_commands() -> list[Path]:
    try:
        return sorted(COMMAND_DIR.glob("*.json"), key=lambda item: item.stat().st_mtime)
    except OSError:
        return []
