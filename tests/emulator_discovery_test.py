import json
import subprocess

import core.control.adb_port as discovery
from core.control.adb_port import EmulatorInfo, EmulatorType


def test_manager_discovery_keeps_stopped_multi_open_instances(monkeypatch):
    payload = {
        "0": {
            "index": "0",
            "name": "运行中",
            "adb_port": 16384,
            "is_process_started": True,
        },
        "5": {
            "index": "5",
            "name": "雷索纳斯",
            "is_process_started": False,
        },
    }

    monkeypatch.setattr(
        discovery,
        "run",
        lambda argv, **_kwargs: subprocess.CompletedProcess(
            argv, 0, stdout=json.dumps(payload).encode(), stderr=b""
        ),
    )

    instances = discovery.get_mumu_manager_info(
        r"C:\MuMu\nx_main\MuMuManager.exe",
        r"C:\MuMu",
        EmulatorType.MUMUV5,
    )

    assert [(item.index, item.port) for item in instances] == [
        (0, 16384),
        (5, None),
    ]


def test_running_process_scan_queries_each_manager_once_and_deduplicates(monkeypatch):
    class Process:
        def exe(self):
            return r"C:\MuMu\nx_device\12.0\shell\MuMuNxDevice.exe"

    calls = []
    instance = EmulatorInfo(
        "雷索纳斯", 16544, r"C:\MuMu", EmulatorType.MUMUV5, 5
    )
    monkeypatch.setattr(discovery.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(discovery.psutil, "process_iter", lambda: [Process(), Process()])
    monkeypatch.setattr(
        discovery,
        "get_emulator_info",
        lambda *_args: calls.append("query") or [instance],
    )

    result = discovery.get_adb_port()

    assert calls == ["query"]
    assert result == [instance]


def test_default_install_is_discovered_when_all_instances_are_stopped(monkeypatch):
    instance = EmulatorInfo(
        "雷索纳斯", None, r"C:\MuMu", EmulatorType.MUMUV5, 5
    )
    custom = EmulatorInfo("自定义", 16384, "", EmulatorType.CUSTOM, 0)
    monkeypatch.setattr(discovery.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(discovery.psutil, "process_iter", lambda: [])
    monkeypatch.setattr(discovery, "get_default_mumu_info", lambda: [instance])

    result = discovery.get_adb_port(custom)

    assert result == [instance]
