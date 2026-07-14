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
from subprocess import run

import psutil
from loguru import logger


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
        return EmulatorInfo(
            name=data["name"],
            port=data.get("port"),
            path=data["path"],
            type=EmulatorType(data["type"]),
            index=data.get("index", 0),
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
    if device.type == EmulatorType.MUMUV5:
        manager_path = path.join(device.path, "nx_main", "MuMuManager.exe")
    else:
        manager_path = path.join(device.path, "shell", "MuMuManager.exe")
        if not path.isfile(manager_path):
            manager_path = path.join(device.path, "MuMuManager.exe")
    if not path.isfile(manager_path):
        return []
    return get_mumu_manager_info(manager_path, device.path, device.type)


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
