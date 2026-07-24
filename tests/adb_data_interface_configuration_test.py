from __future__ import annotations

from types import SimpleNamespace

import app.view.adb_data_interface as module
from PySide6.QtWidgets import QApplication
from core.control.adb_port import EmulatorInfo, EmulatorType


class TextField:
    def __init__(self, value=""):
        self.value = value

    def text(self):
        return self.value

    def setText(self, value):
        self.value = str(value)


def draft(kind=EmulatorType.MUMUV5):
    return SimpleNamespace(
        emulatorTypeCombo=SimpleNamespace(currentData=lambda: kind),
        installPathEdit=TextField(r"C:\Program Files\NetEase\MuMu"),
        adbPathEdit=TextField(r"C:\Program Files\NetEase\MuMu\nx_main\adb.exe"),
        instanceSpin=SimpleNamespace(value=lambda: 7),
        adbHostEdit=TextField("127.0.0.1"),
        portSpin=SimpleNamespace(value=lambda: 17000),
    )


def test_gui_connection_draft_keeps_install_adb_instance_and_endpoint():
    info = module.ADBDataInterface._connection_info(draft())

    assert info.type is EmulatorType.MUMUV5
    assert info.path == r"C:\Program Files\NetEase\MuMu"
    assert info.adb_path.endswith(r"nx_main\adb.exe")
    assert info.index == 7
    assert info.adb_host == "127.0.0.1"
    assert info.port == 17000


def test_gui_custom_adb_can_leave_port_for_runtime_resolution():
    form = draft(EmulatorType.CUSTOM)
    form.portSpin = SimpleNamespace(value=lambda: 0)

    info = module.ADBDataInterface._connection_info(form)

    assert info.name == "自定义 ADB"
    assert info.port is None


def test_selecting_discovered_instance_preserves_explicit_adb_override(monkeypatch):
    current = EmulatorInfo(
        "MuMu 0",
        16384,
        r"C:\MuMu",
        EmulatorType.MUMUV5,
        0,
        adb_path=r"C:\platform-tools\adb.exe",
    )
    selected = EmulatorInfo(
        "MuMu 7",
        17000,
        r"C:\MuMu",
        EmulatorType.MUMUV5,
        7,
        adb_path=r"C:\MuMu\nx_main\adb.exe",
    )
    saved = []
    monkeypatch.setattr(module.cfg.device, "value", current)
    monkeypatch.setattr(module.qconfig, "set", lambda item, value: saved.append(value))
    target = SimpleNamespace(load_connection_configuration=lambda: None)

    module.ADBDataInterface.set_port(target, selected)

    assert saved[0].index == 7
    assert saved[0].adb_path == current.adb_path


def test_device_connection_interface_constructs_without_starting_scan(monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    application = QApplication.instance() or QApplication([])
    target = module.ADBDataInterface()

    assert target.scanWorker is None
    assert target.emulatorTypeCombo.count() == 3
    assert target.instanceSpin.minimum() == 0
    assert target.adbPathEdit is not None

    target.close()
    assert application is not None
