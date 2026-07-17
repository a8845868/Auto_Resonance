"""
Author: Night-stars-1 nujj1042633805@gmail.com
Date: 2024-07-09 13:16:26
LastEditTime: 2025-02-05 18:05:49
LastEditors: Night-stars-1 nujj1042633805@gmail.com
"""

import json
import os
import subprocess
import time
from dataclasses import dataclass
from enum import Enum
from os import path
from pathlib import Path
from subprocess import run

import psutil
from loguru import logger

from core.services.repair_safety import ensure_automation_allowed


class EmulatorType(Enum):
    MUMUV5 = "MuMuV5"
    MUMUV4 = "MuMuV4"
    CUSTOM = "Custom"


@dataclass
class EmulatorDataItem:
    name: str
    manager_path: str
    dir_path: str
    params: str
    type: EmulatorType


@dataclass
class EmulatorInfo:
    name: str
    port: int | None
    path: str
    type: EmulatorType
    index: int

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "port": self.port,
            "path": self.path,
            "type": self.type.value,
            "index": self.index,
        }

    @staticmethod
    def from_dict(data: dict) -> "EmulatorInfo":
        raw_index = data.get("index", 0)
        if raw_index is None:
            raise ValueError("MuMu 实例 ID 不能为空；0 是合法实例 ID")
        return EmulatorInfo(
            name=data["name"],
            port=data.get("port"),
            path=data["path"],
            type=EmulatorType(data["type"]),
            index=int(raw_index),
        )
    
    @property
    def is_mumu(self):
        return self.type in [EmulatorType.MUMUV4, EmulatorType.MUMUV5]


EMULATOR_DATA = {
    "MuMuNxDevice.exe": EmulatorDataItem(
        name="MuMu模拟器 V5",
        manager_path="../../../../nx_main/MuMuManager.exe",
        dir_path="../../../../",
        params="info -v all",
        type=EmulatorType.MUMUV5,
    ),
    "MuMuPlayer.exe": EmulatorDataItem(
        name="MuMu模拟器 V4",
        manager_path="../MuMuManager.exe",
        dir_path="../../",
        params="info -v all",
        type=EmulatorType.MUMUV4,
    ),
}


class EmulatorPathError(ValueError):
    """Raised when a configured emulator path cannot identify a launcher."""


@dataclass(frozen=True)
class MuMuLauncher:
    executable: Path
    install_root: Path


def resolve_mumu_launcher(device: EmulatorInfo) -> MuMuLauncher:
    """Resolve a MuMu install root, launcher directory, or explicit launcher.

    MuMu V5 installations expose identical ``MuMuManager.exe`` and
    ``mumu-cli.exe`` entry points in ``nx_main``.  Resolution is deliberately
    finite and name-based; never select an arbitrary executable recursively.
    """

    raw_path = str(device.path or "").strip().strip('"')
    if not raw_path:
        raise EmulatorPathError("MuMu 安装路径无效：配置为空")
    expanded = os.path.expandvars(os.path.expanduser(raw_path))
    configured = Path(os.path.abspath(expanded))
    supported_names = {"mumumanager.exe", "mumu-cli.exe"}

    if configured.is_file():
        if configured.name.lower() not in supported_names:
            raise EmulatorPathError(
                f"未找到受支持的启动器：{raw_path}（仅支持 MuMuManager.exe 或 mumu-cli.exe）"
            )
        if device.type == EmulatorType.MUMUV5:
            if configured.parent.name.lower() != "nx_main":
                raise EmulatorPathError(
                    f"MuMu V5 启动器不在 nx_main 目录：{raw_path}"
                )
            install_root = configured.parent.parent
        else:
            install_root = (
                configured.parent.parent
                if configured.parent.name.lower() == "shell"
                else configured.parent
            )
        return MuMuLauncher(configured, install_root)

    if not configured.is_dir():
        raise EmulatorPathError(f"MuMu 安装路径无效：{raw_path}")

    if device.type == EmulatorType.MUMUV5:
        launcher_dir = (
            configured
            if configured.name.lower() == "nx_main"
            else configured / "nx_main"
        )
        install_root = launcher_dir.parent
    else:
        launcher_dir = (
            configured if configured.name.lower() == "shell" else configured / "shell"
        )
        install_root = launcher_dir.parent

    candidates = (
        launcher_dir / "MuMuManager.exe",
        launcher_dir / "mumu-cli.exe",
    )
    if device.type == EmulatorType.MUMUV4:
        candidates += (
            configured / "MuMuManager.exe",
            configured / "mumu-cli.exe",
        )
    executable = next((item for item in candidates if item.is_file()), None)
    if executable is None:
        raise EmulatorPathError(
            f"未找到受支持的启动器：{raw_path}（已检查 MuMuManager.exe 和 mumu-cli.exe）"
        )
    return MuMuLauncher(executable, install_root)


def get_mumu_info(exe_path: str, data: EmulatorDataItem):
    """获取MuMu模拟器信息"""
    cmd_path = path.abspath(path.join(exe_path, data.manager_path))
    root_path = path.abspath(path.join(exe_path, data.dir_path))
    return get_mumu_manager_info(cmd_path, root_path, data.type)


def get_mumu_manager_info(
    manager_path: str,
    root_path: str,
    emulator_type: EmulatorType,
) -> list[EmulatorInfo]:
    """Read both the flat single-index and nested all-index JSON formats."""

    ensure_automation_allowed("查询 MuMuManager 实例")

    try:
        creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        shell_result = run(
            [manager_path, "info", "-v", "all"],
            shell=False,
            capture_output=True,
            text=False,
            creationflags=creation_flags,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if shell_result.returncode != 0:
        return []

    try:
        result = json.loads(shell_result.stdout)
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    if not isinstance(result, dict):
        return []
    if "index" in result:
        entries = [result]
    else:
        entries = [item for item in result.values() if isinstance(item, dict)]
    return [
        EmulatorInfo(
            name=i["name"],
            port=i.get("adb_port"),
            path=root_path,
            type=emulator_type,
            index=int(i.get("index", "0")),
        )
        for i in entries
        if i.get("name") is not None
    ]


def get_configured_mumu_info(device: EmulatorInfo) -> list[EmulatorInfo]:
    """Discover stopped instances using the persisted MuMu installation path."""

    if not device.is_mumu or not device.path:
        return []
    try:
        launcher = resolve_mumu_launcher(device)
    except EmulatorPathError:
        return []
    return get_mumu_manager_info(
        str(launcher.executable),
        str(launcher.install_root),
        device.type,
    )


def get_default_mumu_info() -> list[EmulatorInfo]:
    """Find a standard MuMu V5 install even when every instance is stopped."""

    roots = []
    for variable in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)"):
        base = os.environ.get(variable)
        if base:
            roots.append(path.join(base, "NetEase", "MuMu"))
    # `ProgramFiles` may be absent in stripped test/packaged environments.
    roots.append(r"C:\Program Files\NetEase\MuMu")
    result: list[EmulatorInfo] = []
    seen: set[str] = set()
    for root in roots:
        normalized = path.normcase(path.abspath(root))
        if normalized in seen:
            continue
        seen.add(normalized)
        manager_path = path.join(root, "nx_main", "MuMuManager.exe")
        if path.isfile(manager_path):
            result.extend(
                get_mumu_manager_info(manager_path, root, EmulatorType.MUMUV5)
            )
    return result


def get_emulator_info(exe_path: str, data: EmulatorDataItem):
    """获取模拟器信息"""
    if data.type == EmulatorType.MUMUV5:
        return get_mumu_info(exe_path, data)
    if data.type == EmulatorType.MUMUV4:
        return get_mumu_info(exe_path, data)
    return []


def get_adb_port(preferred: EmulatorInfo | None = None) -> list[EmulatorInfo]:
    time.sleep(1)
    logger.info("开始获取ADB端口")
    result: list[EmulatorInfo] = []
    queried_managers: set[str] = set()
    for p in psutil.process_iter():
        try:
            exe_path = p.exe()
            exe_name = path.basename(exe_path)
            exe_data = EMULATOR_DATA.get(exe_name)
            if exe_data:
                manager_path = path.normcase(
                    path.abspath(path.join(exe_path, exe_data.manager_path))
                )
                if manager_path in queried_managers:
                    continue
                queried_managers.add(manager_path)
                info = get_emulator_info(exe_path, exe_data)
                result.extend(info)
        except (
            PermissionError,
            psutil.AccessDenied,
            psutil.NoSuchProcess,
            psutil.ZombieProcess,
        ):
            pass
    preferred_install_discovered = False
    if preferred is not None and preferred.is_mumu:
        preferred_path = path.normcase(path.abspath(preferred.path))
        preferred_install_discovered = any(
            info.type == preferred.type
            and path.normcase(path.abspath(info.path)) == preferred_path
            for info in result
        )
    if preferred is not None and not preferred_install_discovered:
        result.extend(get_configured_mumu_info(preferred))
    if not result:
        result.extend(get_default_mumu_info())

    deduplicated: dict[tuple[str, EmulatorType, int], EmulatorInfo] = {}
    for info in result:
        key = (path.normcase(path.abspath(info.path)), info.type, int(info.index))
        current = deduplicated.get(key)
        if current is None or (not current.port and info.port):
            deduplicated[key] = info
    return sorted(
        deduplicated.values(),
        key=lambda item: (item.type.value, path.normcase(item.path), item.index),
    )
