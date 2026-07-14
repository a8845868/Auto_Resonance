"""Persistent, windowless debug runtime and its lightweight command client."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
import traceback
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
os.chdir(ROOT)
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.services.runtime_control import (  # noqa: E402
    DEBUG_STATUS_PATH,
    RuntimeBusyError,
    acquire_runtime,
    pending_commands,
    read_debug_status,
    read_response,
    request_runtime_stop,
    runtime_owner,
    wait_for_runtime_exit,
    write_command,
    write_debug_status,
    write_response,
)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _serializable(value: object) -> object:
    if isinstance(value, (dict, list, str, int, float, bool, type(None))):
        return value
    return str(value)


def _base_status(lease, **updates) -> dict[str, Any]:
    previous = read_debug_status() or {}
    return {
        "mode": "debug",
        "pid": lease.pid,
        "started_at": previous.get("started_at", _now()),
        "heartbeat": _now(),
        "state": "idle",
        "current_task": "",
        "last_command": previous.get("last_command", ""),
        "last_success": previous.get("last_success"),
        "last_result": previous.get("last_result"),
        "last_error": previous.get("last_error", ""),
        **updates,
    }


def _run_debug_task(lease, command: dict[str, Any]) -> dict[str, Any]:
    from app.common.config import cfg
    from core.control.control import reset_stop
    from core.exception.exceptions import StopExecution
    from core.logger import logger
    from core.services.debug_tasks import resolve_task
    from core.services.emulator_lifecycle import (
        EmulatorQueueLifecycle,
        LifecycleOptions,
    )
    from core.services.task_schedule_state import (
        record_task_execution,
        task_result_succeeded,
    )

    command_id = str(command["id"])
    task_name = str(command.get("task", ""))
    try:
        task = resolve_task(task_name)
    except KeyError as error:
        return {"success": False, "task": task_name, "error": str(error)}

    reset_stop()
    if lease.stop_requested():
        return {
            "success": False,
            "task": task.key,
            "result": None,
            "error": "后台调试进程正在停止，任务未启动",
            "cleanup_error": "",
        }
    write_debug_status(
        _base_status(
            lease,
            state="running",
            current_task=task.key,
            last_command=command_id,
            last_error="",
        )
    )
    logger.info(f"后台调试开始: {task.name} ({task.key})")
    result = None
    success = False
    error_text = ""
    cleanup_error = ""
    lifecycle = None
    try:
        if bool(cfg.enableAutoGameLifecycle.value):
            lifecycle = EmulatorQueueLifecycle(
                cfg.device.value,
                options=LifecycleOptions(
                    auto_start_emulator=bool(cfg.autoStartEmulator.value),
                    close_game_when_idle=True,
                    close_emulator_when_idle=bool(
                        cfg.closeEmulatorWhenIdle.value
                    ),
                ),
            )
            lifecycle.prepare(lambda: lease.stop_requested())
        if lease.stop_requested():
            raise StopExecution()
        result = task.run()
        success = task_result_succeeded(result)
        if not success:
            error_text = "任务未返回明确成功结果"
            logger.warning(f"后台调试未完成: {task.name}；{error_text}")
    except StopExecution:
        error_text = "任务收到停止请求"
        logger.warning(f"后台调试已停止: {task.name}")
    except Exception as error:  # noqa: BLE001 - debug boundary must report all failures
        error_text = f"{type(error).__name__}: {error}"
        logger.exception(f"后台调试执行失败: {task.name}")
    finally:
        if lifecycle is not None:
            try:
                lifecycle.cleanup()
            except Exception as error:  # noqa: BLE001 - keep task result authoritative
                cleanup_error = f"{type(error).__name__}: {error}"
                logger.exception("后台调试任务结束后的游戏资源清理失败")

    if bool(command.get("record")):
        record_task_execution(
            task.key,
            task.name,
            success,
            task.next_run_after(success),
            result,
        )
    response = {
        "success": success,
        "task": task.key,
        "result": _serializable(result),
        "error": error_text,
        "cleanup_error": cleanup_error,
    }
    write_debug_status(
        _base_status(
            lease,
            state="idle",
            current_task="",
            last_command=command_id,
            last_success=success,
            last_result=_serializable(result),
            last_error=error_text or cleanup_error,
        )
    )
    return response


def run_daemon() -> int:
    from core.control.control import kill, stop
    from core.logger import logger

    try:
        lease = acquire_runtime("debug")
    except RuntimeBusyError as error:
        print(str(error), file=sys.stderr)
        return 2

    stop_event = threading.Event()

    def watch_stop_request() -> None:
        while not stop_event.wait(0.25):
            if lease.stop_requested():
                logger.info("后台调试进程收到退出请求")
                stop()
                stop_event.set()

    watcher = threading.Thread(
        target=watch_stop_request,
        name="debug-stop-watcher",
        daemon=True,
    )
    watcher.start()
    write_debug_status(_base_status(lease, started_at=_now()))
    logger.info(f"后台调试模式已启动，PID {lease.pid}；等待调试命令")
    try:
        while not stop_event.is_set():
            handled = False
            for command_path in pending_commands():
                if stop_event.is_set():
                    break
                try:
                    command = json.loads(command_path.read_text(encoding="utf-8"))
                    command_path.unlink(missing_ok=True)
                    command_id = str(command.get("id") or command_path.stem)
                    if command.get("type") != "run":
                        response = {
                            "success": False,
                            "error": f"未知命令类型: {command.get('type')!r}",
                        }
                    else:
                        response = _run_debug_task(lease, command)
                    write_response(command_id, response)
                except Exception as error:  # noqa: BLE001 - keep daemon alive
                    command_id = command_path.stem
                    command_path.unlink(missing_ok=True)
                    write_response(
                        command_id,
                        {
                            "success": False,
                            "error": f"命令处理失败: {type(error).__name__}: {error}",
                            "traceback": traceback.format_exc(),
                        },
                    )
                    logger.exception("后台调试命令处理失败")
                handled = True
            if not handled:
                write_debug_status(_base_status(lease))
                stop_event.wait(0.25)
    finally:
        stop_event.set()
        try:
            kill()
        finally:
            write_debug_status(
                _base_status(lease, state="stopped", current_task="")
            )
            lease.release()
        logger.info("后台调试模式已退出")
    return 0


def start_daemon(timeout: float = 10.0) -> int:
    owner = runtime_owner()
    if owner:
        print(
            f"已有运行实例: {owner.get('mode')} (PID {owner.get('pid')})"
        )
        return 0 if owner.get("mode") == "debug" else 2
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    executable = pythonw if pythonw.exists() else Path(sys.executable)
    creation_flags = 0
    if sys.platform == "win32":
        creation_flags = (
            subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
        )
    subprocess.Popen(
        [str(executable), str(Path(__file__).resolve()), "daemon"],
        cwd=ROOT,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        creationflags=creation_flags,
    )
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        owner = runtime_owner()
        if owner and owner.get("mode") == "debug":
            print(f"后台调试模式已启动，PID {owner['pid']}")
            return 0
        time.sleep(0.2)
    print("后台调试进程未在限定时间内就绪，请检查 logs/debug.log", file=sys.stderr)
    return 1


def enqueue_task(name: str, record: bool, wait_seconds: float) -> int:
    owner = runtime_owner()
    if not owner or owner.get("mode") != "debug":
        if owner:
            print(
                f"当前由 {owner.get('mode')} (PID {owner.get('pid')}) 占用；"
                "请先切换到后台调试模式",
                file=sys.stderr,
            )
        else:
            print("后台调试模式未启动；请先运行 debug_runner.py start", file=sys.stderr)
        return 2
    command_id = uuid.uuid4().hex
    write_command(
        {
            "id": command_id,
            "type": "run",
            "task": name,
            "record": bool(record),
        }
    )
    print(f"已投递命令 {command_id}: {name}")
    if wait_seconds <= 0:
        return 0
    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        response = read_response(command_id)
        if response:
            print(json.dumps(response, ensure_ascii=False, indent=2))
            return 0 if response.get("success") else 1
        if not runtime_owner():
            print("后台调试进程已意外退出", file=sys.stderr)
            return 1
        time.sleep(0.2)
    print(f"命令仍在运行；可用 wait {command_id} 继续等待")
    return 0


def wait_command(command_id: str, timeout: float) -> int:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = read_response(command_id)
        if response:
            print(json.dumps(response, ensure_ascii=False, indent=2))
            return 0 if response.get("success") else 1
        time.sleep(0.2)
    print("等待命令结果超时", file=sys.stderr)
    return 1


def show_status(as_json: bool = False) -> int:
    owner = runtime_owner()
    status = read_debug_status() if owner and owner.get("mode") == "debug" else None
    payload = {"owner": owner, "debug": status}
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    elif not owner:
        print("当前没有 GUI 或后台调试进程占用自动化控制器")
    elif owner.get("mode") == "debug":
        current = (status or {}).get("current_task") or "空闲"
        heartbeat = (status or {}).get("heartbeat", "未知")
        print(
            f"后台调试运行中，PID {owner['pid']}，当前任务: {current}，"
            f"心跳: {heartbeat}"
        )
    else:
        print(f"GUI 运行中，PID {owner['pid']}")
    return 0


def stop_runtime(mode: str, timeout: float) -> int:
    required = None if mode == "any" else mode
    try:
        owner = request_runtime_stop(required)
    except RuntimeBusyError as error:
        print(str(error), file=sys.stderr)
        return 2
    if not owner:
        print("当前没有运行实例")
        return 0
    print(f"已请求 {owner.get('mode')} (PID {owner.get('pid')}) 安全退出")
    if wait_for_runtime_exit(owner, timeout):
        print("运行实例已退出")
        return 0
    print("运行实例尚未退出；未执行强制终止", file=sys.stderr)
    return 1


def list_tasks() -> int:
    from core.services.debug_tasks import task_registry
    from core.services.task_schedule_state import is_task_due, task_timing

    for task in task_registry().values():
        enabled = bool(task.enabled())
        due = bool(is_task_due(task.key)) if task.key not in {"screen", "station"} else True
        next_run = task_timing(task.key).get("next_run", "")
        print(
            f"{task.key:28} enabled={str(enabled).lower():5} "
            f"due={str(due).lower():5} next={next_run or '-'}  {task.name}"
        )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="黑月无人驾驶后台调试控制器")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("daemon", help=argparse.SUPPRESS)
    start = commands.add_parser("start", help="启动常驻后台调试进程")
    start.add_argument("--timeout", type=float, default=10.0)
    status = commands.add_parser("status", help="查看 GUI/后台进程状态")
    status.add_argument("--json", action="store_true")
    listing = commands.add_parser("list", help="列出可投递任务")
    listing.set_defaults(_listing=True)
    run = commands.add_parser("run", help="向后台进程投递一个任务")
    run.add_argument("task")
    run.add_argument("--record", action="store_true", help="写入正式调度完成状态")
    run.add_argument(
        "--wait",
        nargs="?",
        type=float,
        const=120.0,
        default=0.0,
        metavar="SECONDS",
        help="等待结果；省略秒数时等待 120 秒",
    )
    wait = commands.add_parser("wait", help="等待已投递命令的结果")
    wait.add_argument("id")
    wait.add_argument("--timeout", type=float, default=120.0)
    stop = commands.add_parser("stop", help="请求运行实例安全退出")
    stop.add_argument("--mode", choices=("debug", "gui", "any"), default="debug")
    stop.add_argument("--timeout", type=float, default=15.0)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "daemon":
        return run_daemon()
    if args.command == "start":
        return start_daemon(args.timeout)
    if args.command == "status":
        return show_status(args.json)
    if args.command == "list":
        return list_tasks()
    if args.command == "run":
        return enqueue_task(args.task, args.record, args.wait)
    if args.command == "wait":
        return wait_command(args.id, args.timeout)
    if args.command == "stop":
        return stop_runtime(args.mode, args.timeout)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
