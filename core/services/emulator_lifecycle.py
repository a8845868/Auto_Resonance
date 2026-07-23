"""MuMu instance and Android game process lifecycle management.

The queue owns a frozen emulator snapshot for its whole run.  This is
deliberate: the user may select another MuMu instance in the GUI while a task
is running, and cleanup must never target that newly selected instance.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
import uuid
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from typing import Callable, Protocol

import psutil
from adb_shell.adb_device import AdbDeviceTcp
from loguru import logger

from core.control.adb_port import (
    EmulatorInfo,
    EmulatorPathError,
    EmulatorType,
    resolve_mumu_launcher,
)
from core.services.repair_safety import ensure_automation_allowed


GAME_PACKAGE = "com.hermes.goda"
MANAGER_INFO_TIMEOUT_ATTEMPTS = 3


class LifecycleError(RuntimeError):
    """Raised when an emulator or game lifecycle transition fails."""


class LifecycleCancelled(LifecycleError):
    """Raised when the queue is stopped while waiting for a lifecycle step."""


class UnsupportedEmulatorOperation(LifecycleError):
    """Raised when a custom ADB target is asked to control its host emulator."""


class EmulatorInstanceState(str, Enum):
    STOPPED = "STOPPED"
    STOPPING = "STOPPING"
    STARTING = "STARTING"
    ANDROID_READY = "ANDROID_READY"
    FAILED = "FAILED"


def _run_subprocess_tree(
    argv: list[str],
    *,
    timeout: float,
    correlation_id: str,
    **kwargs,
) -> subprocess.CompletedProcess:
    """Run a CLI and tear down descendants before collecting timed-out pipes."""

    popen_kwargs = dict(kwargs)
    popen_kwargs.pop("capture_output", None)
    popen_kwargs.pop("timeout", None)
    popen_kwargs["stdout"] = subprocess.PIPE
    popen_kwargs["stderr"] = subprocess.PIPE
    process = subprocess.Popen(argv, **popen_kwargs)
    logger.info(
        "MuMuManager 进程已创建: "
        f"correlation_id={correlation_id} pid={process.pid}"
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        descendants = []
        try:
            descendants = psutil.Process(process.pid).children(recursive=True)
        except (psutil.Error, OSError):
            pass
        for child in reversed(descendants):
            try:
                child.kill()
            except (psutil.Error, OSError):
                pass
        try:
            process.kill()
        except OSError:
            pass
        try:
            stdout, stderr = process.communicate(timeout=2.0)
        except subprocess.TimeoutExpired:
            stdout, stderr = exc.output, exc.stderr
        raise subprocess.TimeoutExpired(
            argv,
            timeout,
            output=stdout,
            stderr=stderr,
        ) from exc
    return subprocess.CompletedProcess(
        argv,
        int(process.returncode or 0),
        stdout,
        stderr,
    )


class QueueLifecycle(Protocol):
    """Small worker-facing protocol, also convenient for unit-test fakes."""

    def prepare(self, cancelled: Callable[[], bool] | None = None) -> None: ...

    def cleanup(self) -> None: ...


@dataclass(frozen=True)
class LifecycleOptions:
    auto_start_emulator: bool = True
    close_game_when_idle: bool = True
    close_emulator_when_idle: bool = False
    emulator_start_timeout: float = 180.0
    stopping_settle_timeout: float = 60.0
    android_boot_timeout: float | None = None
    game_start_timeout: float = 180.0
    cleanup_timeout: float = 10.0
    poll_interval: float = 2.0
    command_timeout: float = 10.0
    adb_timeout: float = 5.0


def snapshot_device(device: EmulatorInfo) -> EmulatorInfo:
    """Copy a mutable config value before handing it to a background worker."""

    return EmulatorInfo.from_dict(device.to_dict())


def device_identity(device: EmulatorInfo) -> tuple[str, str, int]:
    """Stable identity for a MuMu instance; ADB ports can change after launch."""

    return (
        os.path.normcase(os.path.abspath(device.path or "")),
        device.type.value,
        int(device.index),
    )


class MuMuManagerClient:
    """Thin, index-scoped wrapper around the official MuMuManager CLI."""

    def __init__(
        self,
        device: EmulatorInfo,
        *,
        runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
        timeout: float = 10.0,
        correlation_id: str | None = None,
    ) -> None:
        if not device.is_mumu:
            raise UnsupportedEmulatorOperation("该设备不是 MuMu 多开实例")
        self.device = snapshot_device(device)
        self.runner = runner
        self.timeout = max(0.1, float(timeout))
        self.correlation_id = correlation_id or uuid.uuid4().hex
        try:
            launcher = resolve_mumu_launcher(self.device)
        except EmulatorPathError as exc:
            raise LifecycleError(str(exc)) from exc
        self.executable = launcher.executable
        self.install_root = launcher.install_root

    @staticmethod
    def _manager_path(device: EmulatorInfo) -> Path:
        try:
            return resolve_mumu_launcher(device).executable
        except EmulatorPathError as exc:
            raise LifecycleError(str(exc)) from exc

    def _run(self, *arguments: str) -> subprocess.CompletedProcess:
        ensure_automation_allowed("执行 MuMuManager 命令")
        argv = [str(self.executable), *map(str, arguments)]
        kwargs = {
            "shell": False,
            "cwd": str(self.executable.parent),
            "capture_output": True,
            "text": True,
            "encoding": "utf-8",
            "errors": "replace",
            "timeout": self.timeout,
        }
        creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        if creation_flags:
            kwargs["creationflags"] = creation_flags
        started = time.monotonic()
        logger.info(
            "MuMuManager 命令开始: "
            f"correlation_id={self.correlation_id} backend={self.device.type.value} "
            f"instance_id={int(self.device.index)} "
            f"configured_path={self.device.path!r} launcher={str(self.executable)!r} "
            f"argv={argv!r} cwd={str(self.executable.parent)!r}"
        )
        try:
            if self.runner is subprocess.run:
                default_kwargs = dict(kwargs)
                default_kwargs.pop("timeout", None)
                result = _run_subprocess_tree(
                    argv,
                    timeout=self.timeout,
                    correlation_id=self.correlation_id,
                    **default_kwargs,
                )
            else:
                result = self.runner(argv, **kwargs)
        except subprocess.TimeoutExpired as exc:
            raise LifecycleError(
                f"MuMuManager 命令超时: {' '.join(argv[1:])}"
            ) from exc
        except OSError as exc:
            raise LifecycleError(
                f"无法执行 MuMuManager: {self.executable} ({exc})"
            ) from exc
        if int(result.returncode) != 0:
            details = str(result.stderr or result.stdout or "").strip()
            raise LifecycleError(
                f"MuMuManager 命令失败 ({result.returncode}): {' '.join(argv[1:])}"
                + (f"；{details}" if details else "")
            )
        logger.info(
            "MuMuManager 命令完成: "
            f"correlation_id={self.correlation_id} "
            f"instance_id={int(self.device.index)} returncode={int(result.returncode)} "
            f"elapsed={time.monotonic() - started:.3f}s"
        )
        return result

    def _run_json(self, *arguments: str) -> dict:
        result = self._run(*arguments)
        output = result.stdout
        if isinstance(output, bytes):
            output = output.decode("utf-8", errors="replace")
        try:
            payload = json.loads(str(output or "").strip())
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise LifecycleError("MuMuManager 返回了无效 JSON") from exc
        if not isinstance(payload, dict):
            raise LifecycleError("MuMuManager 状态格式不是对象")
        return payload

    def info(self) -> dict:
        payload = self._run_json("info", "-v", str(self.device.index))
        if str(payload.get("index", "")) == str(self.device.index):
            info = payload
        else:
            nested = payload.get(str(self.device.index))
            if not isinstance(nested, dict):
                raise LifecycleError(
                    f"MuMuManager 未返回多开实例 {self.device.index} 的状态"
                )
            info = nested
        logger.info(
            "MuMuManager 目标状态: "
            f"correlation_id={self.correlation_id} "
            f"instance_id={int(self.device.index)} pid={info.get('pid')} "
            f"process_started={bool(info.get('is_process_started'))} "
            f"android_started={bool(info.get('is_android_started'))} "
            f"adb_endpoint={info.get('adb_host_ip', '127.0.0.1')}:{info.get('adb_port')}"
        )
        return info

    def all_info(self) -> dict:
        """Return the manager's complete instance map for diagnostics."""

        return self._run_json("info", "-v", "all")

    def launch_emulator(self) -> None:
        self._run("control", "-v", str(self.device.index), "launch")

    def shutdown_emulator(self) -> None:
        self._run("control", "-v", str(self.device.index), "shutdown")

    def restart_emulator(self) -> None:
        self._run("control", "-v", str(self.device.index), "restart")

    def launch_game(self, package: str = GAME_PACKAGE) -> None:
        self._run(
            "control",
            "-v",
            str(self.device.index),
            "app",
            "launch",
            "-pkg",
            package,
        )

    def close_game(self, package: str = GAME_PACKAGE) -> None:
        self._run(
            "control",
            "-v",
            str(self.device.index),
            "app",
            "close",
            "-pkg",
            package,
        )

    def game_info(self, package: str = GAME_PACKAGE) -> dict:
        return self._run_json(
            "control",
            "-v",
            str(self.device.index),
            "app",
            "info",
            "-pkg",
            package,
        )

    def shell(self, command: str) -> str:
        """Run an Android shell command inside this exact MuMu instance."""

        result = self._run(
            "sh",
            "-v",
            str(self.device.index),
            "-c",
            command,
        )
        output = result.stdout
        if isinstance(output, bytes):
            return output.decode("utf-8", errors="replace")
        return str(output or "")


class EmulatorLifecycle:
    """Control one explicit emulator instance and one Android package."""

    def __init__(
        self,
        device: EmulatorInfo,
        *,
        options: LifecycleOptions | None = None,
        manager: MuMuManagerClient | None = None,
        adb_factory: Callable[..., AdbDeviceTcp] = AdbDeviceTcp,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        correlation_id: str | None = None,
    ) -> None:
        self.device = snapshot_device(device)
        self.options = options or LifecycleOptions()
        self.correlation_id = correlation_id or uuid.uuid4().hex
        self.manager = manager or (
            MuMuManagerClient(
                self.device,
                timeout=self.options.command_timeout,
                correlation_id=self.correlation_id,
            )
            if self.device.is_mumu
            else None
        )
        self.adb_factory = adb_factory
        self.monotonic = monotonic
        self.sleep = sleep
        self.emulator_started_by_us = False
        self.emulator_launch_dispatches = 0
        self.game_started_by_us = False
        self.instance_state_history: list[dict] = []

    @property
    def label(self) -> str:
        if self.device.is_mumu:
            return f"{self.device.name} (instance_id={int(self.device.index)})"
        endpoint = f"127.0.0.1:{self.device.port}" if self.device.port else "未配置"
        return f"{self.device.name} (ADB {endpoint})"

    @staticmethod
    def _check_cancelled(cancelled: Callable[[], bool] | None) -> None:
        if cancelled is not None and cancelled():
            raise LifecycleCancelled("等待模拟器或游戏启动时收到停止请求")

    def _pause(self, deadline: float, cancelled: Callable[[], bool] | None) -> None:
        self._check_cancelled(cancelled)
        remaining = max(0.0, deadline - self.monotonic())
        if remaining:
            self.sleep(min(max(0.01, self.options.poll_interval), remaining))

    def _update_device_from_info(self, info: dict) -> None:
        port = info.get("adb_port")
        try:
            normalized_port = int(port) if port not in (None, "") else None
        except (TypeError, ValueError):
            normalized_port = None
        self.device = replace(
            self.device,
            name=str(info.get("name") or self.device.name),
            port=normalized_port,
        )
        if self.manager is not None:
            self.manager.device = snapshot_device(self.device)

    @staticmethod
    def _emulator_ready(info: dict) -> bool:
        try:
            port_ready = int(info.get("adb_port") or 0) > 0
        except (TypeError, ValueError):
            port_ready = False
        android_ready = (
            bool(info.get("is_android_started"))
            if "is_android_started" in info
            else True
        )
        return bool(info.get("is_process_started")) and android_ready and port_ready

    @classmethod
    def _instance_state(cls, info: dict) -> EmulatorInstanceState:
        raw_player_state = str(info.get("player_state") or "").strip().lower()
        if int(info.get("launch_err_code") or 0) != 0 or raw_player_state in {
            "failed",
            "error",
        }:
            return EmulatorInstanceState.FAILED
        if cls._emulator_ready(info):
            return EmulatorInstanceState.ANDROID_READY
        if raw_player_state == "stopping":
            return EmulatorInstanceState.STOPPING
        if raw_player_state in {"stopped", "stop_finished"}:
            return EmulatorInstanceState.STOPPED
        if not bool(info.get("is_process_started")):
            return EmulatorInstanceState.STOPPED
        return EmulatorInstanceState.STARTING

    def _record_instance_state(
        self,
        info: dict,
        *,
        launch_dispatched: bool,
        reason_code: str,
    ) -> EmulatorInstanceState:
        state = self._instance_state(info)
        self.instance_state_history.append(
            {
                "timestamp": float(self.monotonic()),
                "state": state.value,
                "player_state": str(info.get("player_state") or ""),
                "android_started": bool(info.get("is_android_started")),
                "process_present": bool(info.get("is_process_started")),
                "launch_dispatched": bool(launch_dispatched),
                "reason_code": reason_code,
            }
        )
        return state

    def _adb_boot_completed(self) -> bool:
        try:
            return self._adb_shell("getprop sys.boot_completed").strip() == "1"
        except LifecycleError as exc:
            logger.debug(f"等待目标实例 ADB/Android 就绪: {exc}")
            return False

    def _target_ready(self, info: dict) -> bool:
        if not self._emulator_ready(info):
            return False
        # Current MuMu V5 reports Android state explicitly.  Once it does,
        # verify the freshly discovered target port rather than trusting any
        # unrelated device returned by a global ADB scan.
        if "is_android_started" in info:
            return self._adb_boot_completed()
        return True

    def android_boot_completed(self) -> bool:
        """Refresh the exact MuMu target, then verify boot over its own ADB."""

        if self.manager is not None:
            info = self.manager.info()
            self._update_device_from_info(info)
            if not self._emulator_ready(info):
                return False
        return self._adb_boot_completed()

    def _wait_for_emulator_info(
        self,
        deadline: float,
        cancelled: Callable[[], bool] | None,
    ) -> dict:
        last_error: LifecycleError | None = None
        for attempt in range(1, MANAGER_INFO_TIMEOUT_ATTEMPTS + 1):
            self._check_cancelled(cancelled)
            try:
                return self.manager.info()  # type: ignore[union-attr]
            except LifecycleError as exc:
                if not isinstance(exc.__cause__, subprocess.TimeoutExpired):
                    raise
                last_error = exc
                if attempt >= MANAGER_INFO_TIMEOUT_ATTEMPTS or self.monotonic() >= deadline:
                    break
                logger.warning(
                    "MuMuManager 状态查询超时，"
                    f"将在间隔后重试 ({attempt}/{MANAGER_INFO_TIMEOUT_ATTEMPTS}): {exc}"
                )
            self._pause(deadline, cancelled)
        raise LifecycleError(
            f"MuMuManager 状态查询连续超时 {attempt} 次: {last_error}"
        ) from last_error

    def emulator_state(self) -> dict:
        if self.manager is None:
            raise UnsupportedEmulatorOperation("自定义 ADB 不支持查询宿主模拟器状态")
        return self.manager.info()

    def ensure_emulator_ready(
        self, cancelled: Callable[[], bool] | None = None
    ) -> EmulatorInfo:
        if self.manager is None:
            if not self.device.port:
                raise LifecycleError("自定义 ADB 端口为空，无法连接游戏")
            try:
                self._adb_shell("getprop sys.boot_completed")
            except LifecycleError as exc:
                raise LifecycleError(
                    f"自定义 ADB {self.label} 当前不可用，且无法自动启动宿主模拟器；"
                    "请在“ADB信息”选择目标 MuMu 多开实例（例如 #0 雷索纳斯），"
                    f"或先手动启动自定义端口对应的模拟器。原始错误：{exc}"
                ) from exc
            return snapshot_device(self.device)

        self._check_cancelled(cancelled)
        initial_deadline = self.monotonic() + max(
            0.0, self.options.emulator_start_timeout
        )
        info = self._wait_for_emulator_info(initial_deadline, cancelled)
        self._update_device_from_info(info)
        state = self._record_instance_state(
            info, launch_dispatched=False, reason_code="initial_observation"
        )
        if self._target_ready(info):
            logger.info(f"MuMu 多开实例已运行: {self.label}，ADB {self.device.port}")
            return snapshot_device(self.device)

        if state is EmulatorInstanceState.FAILED:
            raise LifecycleError(f"MuMu 多开实例报告失败状态: {self.label}")

        if state is EmulatorInstanceState.STOPPING:
            settle_deadline = self.monotonic() + max(
                0.0, self.options.stopping_settle_timeout
            )
            logger.info(f"MuMu 多开实例正在停止，等待状态收敛: {self.label}")
            while state is EmulatorInstanceState.STOPPING:
                if self.monotonic() >= settle_deadline:
                    raise LifecycleError(
                        f"等待 MuMu stopping 状态收敛超时: {self.label}"
                    )
                self._pause(settle_deadline, cancelled)
                info = self._wait_for_emulator_info(settle_deadline, cancelled)
                self._update_device_from_info(info)
                state = self._record_instance_state(
                    info,
                    launch_dispatched=False,
                    reason_code="stopping_settle_observation",
                )
                if self._target_ready(info):
                    logger.info(
                        f"MuMu 多开实例 stopping 后已就绪: {self.label}，ADB {self.device.port}"
                    )
                    return snapshot_device(self.device)
            if state is EmulatorInstanceState.FAILED:
                raise LifecycleError(f"MuMu 多开实例停止阶段失败: {self.label}")

        boot_timeout = (
            self.options.emulator_start_timeout
            if self.options.android_boot_timeout is None
            else self.options.android_boot_timeout
        )
        boot_deadline = self.monotonic() + max(0.0, boot_timeout)
        launch_dispatched = False
        while True:
            self._check_cancelled(cancelled)
            if state is EmulatorInstanceState.STOPPED and not launch_dispatched:
                if not self.options.auto_start_emulator:
                    raise LifecycleError(
                        f"MuMu 多开实例未启动且自动启动已关闭: {self.label}"
                    )
                logger.info(f"正在启动 MuMu 多开实例: {self.label}")
                self.emulator_started_by_us = True
                self.manager.launch_emulator()
                self.emulator_launch_dispatches += 1
                launch_dispatched = True
            if self.monotonic() >= boot_deadline:
                break
            self._pause(boot_deadline, cancelled)
            info = self._wait_for_emulator_info(boot_deadline, cancelled)
            self._update_device_from_info(info)
            state = self._record_instance_state(
                info,
                launch_dispatched=launch_dispatched,
                reason_code="android_boot_observation",
            )
            if self._target_ready(info):
                logger.info(f"MuMu 多开实例就绪: {self.label}，ADB {self.device.port}")
                return snapshot_device(self.device)
            if state is EmulatorInstanceState.FAILED:
                raise LifecycleError(f"MuMu 多开实例启动失败: {self.label}")
        raise LifecycleError(f"等待 MuMu 多开实例启动超时: {self.label}")

    def _adb_shell(self, command: str) -> str:
        ensure_automation_allowed("执行模拟器 ADB shell 命令")
        if not self.device.port:
            raise LifecycleError(f"{self.label} 没有可用的 ADB 端口")
        adb = None
        timeout = max(0.1, float(self.options.adb_timeout))
        try:
            adb = self.adb_factory(
                "127.0.0.1",
                port=int(self.device.port),
                default_transport_timeout_s=timeout,
            )
            if not adb.connect(
                transport_timeout_s=timeout,
                read_timeout_s=timeout,
            ):
                raise LifecycleError(
                    f"无法连接 {self.label} 的 ADB 端口 {self.device.port}"
                )
            output = adb.shell(
                command,
                transport_timeout_s=timeout,
                read_timeout_s=timeout,
                timeout_s=timeout,
            )
            if isinstance(output, bytes):
                return output.decode("utf-8", errors="replace")
            return str(output or "")
        except LifecycleError:
            raise
        except Exception as exc:  # noqa: BLE001 - normalize adb-shell failures
            raise LifecycleError(
                f"{self.label} 执行 ADB 命令失败 {command!r}: "
                f"{type(exc).__name__}: {exc}"
            ) from exc
        finally:
            if adb is not None:
                try:
                    adb.close()
                except Exception as exc:  # pragma: no cover - defensive driver cleanup
                    logger.debug(f"关闭临时 ADB 连接失败: {exc}")

    def _target_shell(self, command: str) -> str:
        """Address MuMu by immutable index; use a port only for custom ADB."""

        if self.manager is not None:
            return self.manager.shell(command)
        return self._adb_shell(command)

    def is_game_running(self) -> bool:
        if self.manager is not None:
            payload = self.manager.game_info(GAME_PACKAGE)
            state = str(payload.get("state") or "").strip().lower()
            if state in {"stopped", "not_installed"}:
                return False
            if state in {"running", "starting"}:
                return True
        return bool(self._target_shell(f"pidof {GAME_PACKAGE}").strip())

    def start_game(self, cancelled: Callable[[], bool] | None = None) -> None:
        self._check_cancelled(cancelled)
        deadline = self.monotonic() + max(0.0, self.options.game_start_timeout)
        next_launch_attempt = self.monotonic()
        manager_failed = False
        announced_start = False
        while True:
            self._check_cancelled(cancelled)
            try:
                if self.is_game_running():
                    if self.game_started_by_us:
                        logger.info(f"游戏进程已启动: {self.label}")
                    else:
                        logger.info(f"游戏进程已运行: {self.label}")
                    return
            except LifecycleError as exc:
                logger.debug(f"等待游戏 ADB 就绪: {exc}")

            now = self.monotonic()
            if now >= next_launch_attempt:
                if not announced_start:
                    logger.info(f"正在启动游戏进程: {self.label}")
                    announced_start = True
                launch_requested = False
                if self.manager is not None and not manager_failed:
                    try:
                        self.manager.launch_game(GAME_PACKAGE)
                        launch_requested = True
                    except LifecycleError as exc:
                        manager_failed = True
                        logger.warning(f"MuMuManager 启动游戏失败，尝试 ADB: {exc}")
                if not launch_requested:
                    try:
                        output = self._target_shell(
                            f"monkey -p {GAME_PACKAGE} "
                            "-c android.intent.category.LAUNCHER 1"
                        )
                        if "No activities found" in output:
                            raise LifecycleError(f"未找到游戏包 {GAME_PACKAGE}")
                        launch_requested = True
                    except LifecycleError as exc:
                        logger.debug(f"请求启动游戏失败，稍后重试: {exc}")
                if launch_requested:
                    self.game_started_by_us = True
                    next_launch_attempt = now + max(
                        5.0, self.options.poll_interval
                    )
                    # Re-check immediately; do not impose an unnecessary poll
                    # delay when MuMu reports the new PID synchronously.
                    continue
                next_launch_attempt = now + max(1.0, self.options.poll_interval)
            if now >= deadline:
                break
            self._pause(deadline, cancelled)
        raise LifecycleError(f"等待游戏进程启动超时: {self.label}")

    def ensure_game_ready(
        self, cancelled: Callable[[], bool] | None = None
    ) -> EmulatorInfo:
        self.ensure_emulator_ready(cancelled)
        self.start_game(cancelled)
        return snapshot_device(self.device)

    def _refresh_manager_target(self) -> bool | None:
        """Refresh the target port; never use a persisted MuMu port destructively."""

        if self.manager is None:
            return True
        try:
            info = self.manager.info()
        except LifecycleError as exc:
            logger.warning(f"关闭游戏前无法刷新 MuMu 实例状态: {exc}")
            return None
        self._update_device_from_info(info)
        if not info.get("is_process_started") or (
            "is_android_started" in info and not info.get("is_android_started")
        ):
            return False
        return True if self.device.port else None

    def _manager_game_stopped(self) -> bool | None:
        if self.manager is None:
            return None
        try:
            payload = self.manager.game_info(GAME_PACKAGE)
        except LifecycleError as exc:
            logger.debug(f"MuMuManager 无法确认游戏状态: {exc}")
            return None
        raw_state = payload.get("state")
        if raw_state in (None, ""):
            return None
        return str(raw_state).strip().lower() in {"stopped", "not_installed"}

    def stop_game(
        self,
        timeout: float | None = None,
        *,
        require_confirmation: bool = False,
    ) -> None:
        target_ready = self._refresh_manager_target()
        if target_ready is False:
            logger.info(f"模拟器 Android 未运行，游戏进程无需清理: {self.label}")
            return

        adb_running: bool | None = None
        if target_ready is True:
            try:
                adb_running = self.is_game_running()
            except LifecycleError as exc:
                logger.debug(f"关闭游戏前无法读取 ADB 进程状态: {exc}")
        manager_stopped = self._manager_game_stopped()
        if adb_running is False or (adb_running is None and manager_stopped is True):
            logger.info(f"游戏进程已关闭: {self.label}")
            return

        logger.info(f"正在关闭游戏进程: {self.label}")
        close_requested = False
        errors: list[Exception] = []
        if self.manager is not None:
            try:
                self.manager.close_game(GAME_PACKAGE)
                close_requested = True
            except LifecycleError as exc:
                errors.append(exc)
                logger.warning(f"MuMuManager 关闭游戏失败，继续尝试索引 Shell: {exc}")

        if self.manager is not None:
            # Never send a destructive command to a MuMu ADB port. Ports can
            # change or be reused while an instance exits. MuMuManager's shell
            # command keeps the immutable multi-instance index in scope.
            try:
                self._target_shell(f"am force-stop {GAME_PACKAGE}")
                close_requested = True
            except LifecycleError as exc:
                errors.append(exc)
                logger.warning(f"MuMuManager 索引 Shell force-stop 游戏失败: {exc}")
        else:
            # Custom ADB has no manager identity to refresh; its explicit port
            # is the user's target and remains the only available control path.
            try:
                self._target_shell(f"am force-stop {GAME_PACKAGE}")
                close_requested = True
            except LifecycleError as exc:
                errors.append(exc)

        if not close_requested:
            raise LifecycleError("；".join(str(error) for error in errors))

        wait_timeout = self.options.cleanup_timeout if timeout is None else timeout
        deadline = self.monotonic() + max(0.0, wait_timeout)
        while True:
            manager_stopped = self._manager_game_stopped()
            adb_running = None
            if target_ready is not False:
                try:
                    adb_running = self.is_game_running()
                except LifecycleError as exc:
                    logger.debug(f"等待游戏关闭时 ADB 暂不可用: {exc}")
            if adb_running is False or (
                adb_running is None and manager_stopped is True
            ):
                logger.info(f"游戏进程已关闭: {self.label}")
                return
            if self.monotonic() >= deadline:
                break
            self._pause(deadline, None)
        if not require_confirmation:
            logger.warning(f"关闭命令已发送，但未能再次确认游戏状态: {self.label}")
            return
        raise LifecycleError(f"等待游戏进程关闭超时: {self.label}")

    def restart_game(self, cancelled: Callable[[], bool] | None = None) -> None:
        self.stop_game(require_confirmation=True)
        self.start_game(cancelled)

    def stop_emulator(self, timeout: float | None = None) -> None:
        if self.manager is None:
            raise UnsupportedEmulatorOperation("自定义 ADB 不支持自动关闭宿主模拟器")
        if not self.manager.info().get("is_process_started"):
            logger.info(f"MuMu 多开实例已关闭: {self.label}")
            return
        logger.info(f"正在关闭 MuMu 多开实例: {self.label}")
        self.manager.shutdown_emulator()
        wait_timeout = self.options.cleanup_timeout if timeout is None else timeout
        deadline = self.monotonic() + max(0.0, wait_timeout)
        while True:
            if not self.manager.info().get("is_process_started"):
                logger.info(f"MuMu 多开实例已关闭: {self.label}")
                return
            if self.monotonic() >= deadline:
                break
            self._pause(deadline, None)
        raise LifecycleError(f"等待 MuMu 多开实例关闭超时: {self.label}")

    def restart_emulator(
        self, cancelled: Callable[[], bool] | None = None
    ) -> EmulatorInfo:
        if self.manager is None:
            raise UnsupportedEmulatorOperation("自定义 ADB 不支持自动重启宿主模拟器")
        logger.info(f"正在重启 MuMu 多开实例: {self.label}")
        # A direct asynchronous `restart` can still report the old ready PID
        # on the first info poll.  Observe a complete down -> up transition so
        # callers never continue against the dying instance.
        self.stop_emulator(self.options.emulator_start_timeout)
        self.emulator_started_by_us = True
        self.manager.launch_emulator()
        deadline = self.monotonic() + max(0.0, self.options.emulator_start_timeout)
        while True:
            self._check_cancelled(cancelled)
            info = self.manager.info()
            self._update_device_from_info(info)
            if self._emulator_ready(info):
                return snapshot_device(self.device)
            if self.monotonic() >= deadline:
                break
            self._pause(deadline, cancelled)
        raise LifecycleError(f"等待 MuMu 多开实例重启超时: {self.label}")


class EmulatorQueueLifecycle:
    """One emulator/game session shared by every task in a queue snapshot."""

    def __init__(
        self,
        device: EmulatorInfo,
        *,
        options: LifecycleOptions | None = None,
        lifecycle: EmulatorLifecycle | None = None,
        release_controller: Callable[[], None] | None = None,
        correlation_id: str | None = None,
    ) -> None:
        self.options = options or LifecycleOptions()
        self.lifecycle = lifecycle or EmulatorLifecycle(
            device,
            options=self.options,
            correlation_id=correlation_id,
        )
        self.release_controller = release_controller or self._release_global_controller
        self._cleaned = False
        self._prepared = False

    @staticmethod
    def _release_global_controller() -> None:
        from core.control.control import kill

        kill()

    def prepare(self, cancelled: Callable[[], bool] | None = None) -> None:
        device = self.lifecycle.ensure_game_ready(cancelled)
        # Existing automation modules intentionally use one global controller.
        # Activate only the frozen queue target, after its current ADB port is
        # known, and never read the mutable GUI selection again during cleanup.
        from core.control.control import (
            set_runtime_auto_start_emulator,
            set_runtime_device,
        )

        set_runtime_device(device)
        set_runtime_auto_start_emulator(self.options.auto_start_emulator)
        self._prepared = True

    def cleanup(self) -> None:
        if self._cleaned:
            return
        self._cleaned = True
        errors: list[Exception] = []
        try:
            self.release_controller()
        except Exception as exc:  # noqa: BLE001 - cleanup must continue
            errors.append(exc)
            logger.warning(f"释放模拟器控制连接失败: {exc}")
        finally:
            from core.control.control import clear_runtime_device

            clear_runtime_device()
        if self.options.close_game_when_idle:
            try:
                self.lifecycle.stop_game(
                    self.options.cleanup_timeout,
                    require_confirmation=True,
                )
            except Exception as exc:  # noqa: BLE001 - optional emulator cleanup follows
                errors.append(exc)
                logger.warning(f"队列结束后关闭游戏失败: {exc}")
        should_close_emulator = self.options.close_emulator_when_idle or (
            not self._prepared
            and bool(getattr(self.lifecycle, "emulator_started_by_us", False))
        )
        if should_close_emulator:
            try:
                self.lifecycle.stop_emulator(self.options.cleanup_timeout)
            except Exception as exc:  # noqa: BLE001 - report after all cleanup attempts
                errors.append(exc)
                logger.warning(f"队列结束后关闭模拟器失败: {exc}")
        if errors:
            raise LifecycleError("；".join(str(error) for error in errors))
