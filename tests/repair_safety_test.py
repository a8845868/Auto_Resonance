import pytest

import core.control.control as control_module
from core.control.adb import ADB
from core.control.adb_port import EmulatorInfo, EmulatorType
from core.services.emulator_lifecycle import MuMuManagerClient
from core.services.repair_safety import (
    RepairSafetyError,
    ensure_automation_allowed,
    is_repair_process,
)


def test_repair_marker_blocks_live_automation(monkeypatch):
    monkeypatch.setenv("HEIYUE_CODEX_REPAIR", "1")

    assert is_repair_process() is True
    with pytest.raises(RepairSafetyError, match="禁止访问模拟器"):
        ensure_automation_allowed("test operation")


def test_direct_adb_input_is_blocked_before_shell_call(monkeypatch):
    monkeypatch.setenv("HEIYUE_CODEX_REPAIR", "true")
    adb = ADB()
    calls = []
    monkeypatch.setattr(adb.device, "shell", lambda *args, **kwargs: calls.append(args))

    with pytest.raises(RepairSafetyError):
        adb.input_tap(1, 2)
    with pytest.raises(RepairSafetyError):
        adb.kill()

    assert calls == []


def test_high_level_connect_is_blocked_before_device_discovery(monkeypatch):
    monkeypatch.setenv("HEIYUE_CODEX_REPAIR", "on")
    monkeypatch.setattr(
        control_module,
        "get_runtime_device",
        lambda: (_ for _ in ()).throw(AssertionError("device was inspected")),
    )

    with pytest.raises(RepairSafetyError):
        control_module.connect()


def test_mumu_manager_is_blocked_before_subprocess(monkeypatch, tmp_path):
    monkeypatch.setenv("HEIYUE_CODEX_REPAIR", "yes")
    calls = []
    launcher_dir = tmp_path / "nx_main"
    launcher_dir.mkdir()
    (launcher_dir / "MuMuManager.exe").write_bytes(b"")
    device = EmulatorInfo(
        name="repair-test",
        port=16384,
        path=str(tmp_path),
        type=EmulatorType.MUMUV5,
        index=0,
    )
    manager = MuMuManagerClient(
        device,
        runner=lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    with pytest.raises(RepairSafetyError):
        manager.info()

    assert calls == []


def test_normal_process_is_not_blocked(monkeypatch):
    monkeypatch.delenv("HEIYUE_CODEX_REPAIR", raising=False)

    assert is_repair_process() is False
    ensure_automation_allowed("read-only unit test")
