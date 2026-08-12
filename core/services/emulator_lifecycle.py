"""MuMu instance and Android game process lifecycle management.

The queue owns a frozen emulator snapshot for its whole run.  This is
deliberate: the user may select another MuMu instance in the GUI while a task
is running, and cleanup must never target that newly selected instance.
"""

from __future__ import annotations

import json
import os
import socket
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
    clean_tool_environment,
    get_adb_port,
    resolve_adb_executable,
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


class AdbReadinessState(str, Enum):
    """Observable readiness phases after the host instance is running."""

    ANDROID_STARTED = "ANDROID_STARTED"
    ADB_PORT_NOT_LISTENING = "ADB_PORT_NOT_LISTENING"
    ADB_CONNECTING = "ADB_CONNECTING"
    ADB_DEVICE_READY = "ADB_DEVICE_READY"
    PACKAGE_STARTING = "PACKAGE_STARTING"
    PACKAGE_READY = "PACKAGE_READY"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


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
    adb_port_ready_timeout: float = 90.0
    adb_device_ready_timeout: float = 60.0
    adb_poll_interval: float = 1.0


def _probe_tcp_endpoint(host: str, port: int, timeout: float) -> bool:
    """Return whether one exact local endpoint accepts TCP connections."""

    try:
        with socket.create_connection((host, int(port)), timeout=max(0.05, timeout)):
            return True
    except OSError:
        return False


def resolve_selected_mumu_configuration(
    configured: EmulatorInfo,
    *,
    discover: Callable[..., list[EmulatorInfo]] = get_adb_port,
) -> EmulatorInfo:
    """Resolve the configured multi-open index to one unique MuMu instance."""

    if configured.is_mumu:
        return snapshot_device(configured)
    target_index = int(configured.index)
    candidates = [
        candidate
        for candidate in discover(preferred=configured)
        if candidate.is_mumu and int(candidate.index) == target_index
    ]
    unique = {device_identity(candidate): candidate for candidate in candidates}
    if not unique:
        raise LifecycleError("selected_instance_configuration_not_found")
    if len(unique) != 1:
        raise LifecycleError("selected_instance_configuration_ambiguous")
    return snapshot_device(next(iter(unique.values())))


def resolve_instance_zero_configuration(
    configured: EmulatorInfo,
    *,
    discover: Callable[..., list[EmulatorInfo]] = get_adb_port,
) -> EmulatorInfo:
    """Backward-compatible strict resolver used by older instance-zero tools."""

    if int(configured.index) != 0:
        raise LifecycleError("personal_runtime_instance_zero_required")
    return resolve_selected_mumu_configuration(configured, discover=discover)


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
            "env": clean_tool_environment(),
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
        tcp_probe: Callable[[str, int, float], bool] | None = None,
        adb_runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
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
        # Injected transports are fake-I/O by design and therefore opt out of
        # the operating-system socket probe unless a fake probe is supplied.
        self.tcp_probe = tcp_probe or (
            _probe_tcp_endpoint
            if adb_factory is AdbDeviceTcp
            else lambda _host, _port, _timeout: True
        )
        self.adb_runner = adb_runner
        self.adb_host = str(self.device.adb_host or "127.0.0.1")
        self.endpoint_source = (
            "mumu_manager_instance_info"
            if self.device.is_mumu
            else "explicit_custom_config"
        )
        self.endpoint_resolution_timestamp: float | None = None
        self.emulator_started_by_us = False
        self.emulator_launch_dispatches = 0
        self.game_started_by_us = False
        self.game_launch_dispatches = 0
        self.instance_state_history: list[dict] = []
        self.readiness_history: list[dict] = []

    @property
    def label(self) -> str:
        if self.device.is_mumu:
            return f"{self.device.name} (instance_id={int(self.device.index)})"
        endpoint = f"{self.device.adb_host}:{self.device.port}" if self.device.port else "未配置"
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
            adb_host=str(info.get("adb_host_ip") or self.device.adb_host or "127.0.0.1"),
        )
        host = str(self.device.adb_host or "127.0.0.1").strip()
        self.adb_host = host or "127.0.0.1"
        if not self.device.adb_path:
            adb = resolve_adb_executable(self.device)
            if adb is not None:
                self.device = replace(self.device, adb_path=str(adb))
        self.endpoint_source = "mumu_manager_instance_info"
        self.endpoint_resolution_timestamp = float(self.monotonic())
        if self.manager is not None:
            self.manager.device = snapshot_device(self.device)

    def _record_readiness(self, state: AdbReadinessState, reason: str) -> None:
        self.readiness_history.append(
            {
                "timestamp": float(self.monotonic()),
                "state": state.value,
                "reason_code": reason,
                "instance_index": int(self.device.index),
                "adb_host": self.adb_host,
                "adb_port": self.device.port,
                "endpoint_source": self.endpoint_source,
            }
        )

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
        """Return host-side readiness only; never connect ADB from this check."""

        return self._emulator_ready(info)

    def _adb_pause(
        self,
        deadline: float,
        cancelled: Callable[[], bool] | None,
    ) -> None:
        self._check_cancelled(cancelled)
        remaining = max(0.0, deadline - self.monotonic())
        if remaining:
            self.sleep(
                min(max(0.01, self.options.adb_poll_interval), remaining)
            )

    @staticmethod
    def _adb_failure_reason(error: BaseException) -> str:
        text = f"{type(error).__name__}: {error}".lower()
        if "unauthorized" in text:
            return "adb_device_unauthorized"
        if "offline" in text:
            return "adb_device_offline"
        return "adb_connect_failed"

    def _run_selected_adb(self, *arguments: str) -> subprocess.CompletedProcess | None:
        """Use the configured vendor ADB for server/device-state coordination."""

        raw_path = str(self.device.adb_path or "").strip().strip('"')
        if not raw_path:
            return None
        executable = Path(raw_path)
        if not executable.is_file():
            raise LifecycleError("adb_executable_not_found")
        kwargs = {
            "shell": False,
            "cwd": str(executable.parent),
            "capture_output": True,
            "text": True,
            "encoding": "utf-8",
            "errors": "replace",
            "timeout": max(0.1, float(self.options.command_timeout)),
            "env": clean_tool_environment(),
        }
        creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        if creation_flags:
            kwargs["creationflags"] = creation_flags
        return self.adb_runner([str(executable), *arguments], **kwargs)

    def _wait_for_adb_ready(
        self,
        cancelled: Callable[[], bool] | None,
    ) -> None:
        """Wait for TCP, then establish one bounded logical ADB connection."""

        if not self.device.port:
            self._record_readiness(AdbReadinessState.FAILED, "adb_endpoint_resolution_failed")
            raise LifecycleError("adb_endpoint_resolution_failed")
        if self.endpoint_resolution_timestamp is None:
            self.endpoint_resolution_timestamp = float(self.monotonic())

        host = self.adb_host
        port = int(self.device.port)
        port_deadline = self.monotonic() + max(
            0.0, self.options.adb_port_ready_timeout
        )
        try:
            while not self.tcp_probe(
                host,
                port,
                min(max(0.05, self.options.adb_timeout), 1.0),
            ):
                self._record_readiness(
                    AdbReadinessState.ADB_PORT_NOT_LISTENING,
                    "adb_endpoint_not_ready",
                )
                if self.monotonic() >= port_deadline:
                    self._record_readiness(
                        AdbReadinessState.FAILED,
                        "adb_port_ready_timeout",
                    )
                    raise LifecycleError("adb_port_ready_timeout")
                self._adb_pause(port_deadline, cancelled)

            self._record_readiness(
                AdbReadinessState.ADB_CONNECTING,
                "adb_tcp_listening",
            )
            selected_connect = self._run_selected_adb(
                "connect", f"{host}:{port}"
            )
            if selected_connect is not None and int(selected_connect.returncode) != 0:
                output = str(selected_connect.stderr or selected_connect.stdout or "")
                reason = self._adb_failure_reason(RuntimeError(output))
                self._record_readiness(AdbReadinessState.FAILED, reason)
                raise LifecycleError(reason)
            timeout = max(0.1, float(self.options.adb_timeout))
            adb = self.adb_factory(
                host,
                port=port,
                default_transport_timeout_s=timeout,
            )
            connected = False
            last_error: BaseException | None = None
            device_deadline = self.monotonic() + max(
                0.0, self.options.adb_device_ready_timeout
            )
            try:
                while True:
                    self._check_cancelled(cancelled)
                    try:
                        if not connected:
                            connected = bool(
                                adb.connect(
                                    transport_timeout_s=timeout,
                                    read_timeout_s=timeout,
                                )
                            )
                        if connected:
                            output = adb.shell(
                                "getprop sys.boot_completed",
                                transport_timeout_s=timeout,
                                read_timeout_s=timeout,
                                timeout_s=timeout,
                            )
                            if isinstance(output, bytes):
                                output = output.decode("utf-8", errors="replace")
                            if str(output or "").strip() == "1":
                                self._record_readiness(
                                    AdbReadinessState.ADB_DEVICE_READY,
                                    "adb_device_ready",
                                )
                                return
                    except Exception as exc:  # noqa: BLE001 - classify transport state
                        last_error = exc
                        reason = self._adb_failure_reason(exc)
                        if reason in {"adb_device_offline", "adb_device_unauthorized"}:
                            self._record_readiness(AdbReadinessState.FAILED, reason)
                            raise LifecycleError(reason) from exc
                    if self.monotonic() >= device_deadline:
                        reason = (
                            self._adb_failure_reason(last_error)
                            if last_error is not None
                            else "adb_connect_failed"
                        )
                        self._record_readiness(AdbReadinessState.FAILED, reason)
                        raise LifecycleError(reason)
                    self._adb_pause(device_deadline, cancelled)
            finally:
                try:
                    adb.close()
                except Exception as exc:  # pragma: no cover - defensive cleanup
                    logger.debug(f"Closing readiness ADB connection failed: {exc}")
        except LifecycleCancelled:
            self._record_readiness(AdbReadinessState.CANCELLED, "personal_startup_cancelled")
            raise

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
                self._wait_for_adb_ready(cancelled)
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
            self._record_readiness(
                AdbReadinessState.ANDROID_STARTED,
                "android_started",
            )
            self._wait_for_adb_ready(cancelled)
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
                    self._record_readiness(
                        AdbReadinessState.ANDROID_STARTED,
                        "android_started",
                    )
                    self._wait_for_adb_ready(cancelled)
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
                self.emulator_launch_dispatches += 1
                launch_dispatched = True
                self.manager.launch_emulator()
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
                self._record_readiness(
                    AdbReadinessState.ANDROID_STARTED,
                    "android_started",
                )
                self._wait_for_adb_ready(cancelled)
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
                self.adb_host,
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
        launch_dispatched = self.game_launch_dispatches > 0
        while True:
            self._check_cancelled(cancelled)
            try:
                if self.is_game_running():
                    self._record_readiness(
                        AdbReadinessState.PACKAGE_READY,
                        "package_ready",
                    )
                    if self.game_started_by_us:
                        logger.info(f"游戏进程已启动: {self.label}")
                    else:
                        logger.info(f"游戏进程已运行: {self.label}")
                    return
            except LifecycleError as exc:
                logger.debug(f"等待游戏 ADB 就绪: {exc}")

            now = self.monotonic()
            if not launch_dispatched:
                self._record_readiness(
                    AdbReadinessState.PACKAGE_STARTING,
                    "package_launch_pending",
                )
                logger.info(f"正在启动游戏进程: {self.label}")
                # Count the command at the invocation boundary: an exception may
                # still mean the manager/ADB received it, so a fallback would be
                # an unsafe duplicate dispatch.
                self.game_launch_dispatches += 1
                self.game_started_by_us = True
                launch_dispatched = True
                if self.manager is not None:
                    try:
                        self.manager.launch_game(GAME_PACKAGE)
                    except Exception as exc:
                        raise LifecycleError(
                            f"game_package_launch_failed: {self.label}: {exc}"
                        ) from exc
                else:
                    try:
                        output = self._adb_shell(
                            f"monkey -p {GAME_PACKAGE} "
                            "-c android.intent.category.LAUNCHER 1"
                        )
                    except Exception as exc:
                        raise LifecycleError(
                            f"game_package_launch_failed: {self.label}: {exc}"
                        ) from exc
                    if "No activities found" in output:
                        raise LifecycleError(f"未找到游戏包 {GAME_PACKAGE}")
                # Re-check immediately; all later iterations only poll state.
                continue
            if now >= deadline:
                break
            self._pause(deadline, cancelled)
        raise LifecycleError(f"game_package_start_timeout: {self.label}")

    def ensure_game_ready(
        self, cancelled: Callable[[], bool] | None = None
    ) -> EmulatorInfo:
        try:
            self.ensure_emulator_ready(cancelled)
            self.start_game(cancelled)
            return snapshot_device(self.device)
        except LifecycleCancelled:
            if not self.readiness_history or self.readiness_history[-1]["state"] != AdbReadinessState.CANCELLED.value:
                self._record_readiness(
                    AdbReadinessState.CANCELLED,
                    "personal_startup_cancelled",
                )
            raise

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
        target_resolver: Callable[[EmulatorInfo], EmulatorInfo] | None = None,
        lifecycle_factory: Callable[..., EmulatorLifecycle] = EmulatorLifecycle,
        release_controller: Callable[[], None] | None = None,
        correlation_id: str | None = None,
    ) -> None:
        self.options = options or LifecycleOptions()
        self.configured_device = snapshot_device(device)
        self.lifecycle = lifecycle
        self.target_resolver = target_resolver
        self.lifecycle_factory = lifecycle_factory
        self.correlation_id = correlation_id
        self.release_controller = release_controller or self._release_global_controller
        self._cleaned = False
        self._prepared = False

    @staticmethod
    def _release_global_controller() -> None:
        from core.control.control import kill

        kill()

    def prepare(self, cancelled: Callable[[], bool] | None = None) -> None:
        if self.lifecycle is None:
            device = snapshot_device(self.configured_device)
            if self.target_resolver is not None:
                device = self.target_resolver(device)
            self.lifecycle = self.lifecycle_factory(
                device,
                options=self.options,
                correlation_id=self.correlation_id,
            )
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
        if self.options.close_game_when_idle and self.lifecycle is not None:
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
            and self.lifecycle is not None
            and bool(getattr(self.lifecycle, "emulator_started_by_us", False))
        )
        if should_close_emulator and self.lifecycle is not None:
            try:
                self.lifecycle.stop_emulator(self.options.cleanup_timeout)
            except Exception as exc:  # noqa: BLE001 - report after all cleanup attempts
                errors.append(exc)
                logger.warning(f"队列结束后关闭模拟器失败: {exc}")
        if errors:
            raise LifecycleError("；".join(str(error) for error in errors))
