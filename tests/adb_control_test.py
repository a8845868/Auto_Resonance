from adb_shell.exceptions import TcpTimeoutException

import core.control.adb as adb_module
import core.control.control as control_module
from core.control.adb_port import EmulatorInfo, EmulatorType


class _TimeoutDevice:
    instances = []

    def __init__(self, *_args, **_kwargs):
        self.args = _args
        self.kwargs = _kwargs
        self.closed = False
        self.instances.append(self)

    def connect(self):
        raise TcpTimeoutException("test timeout")

    def close(self):
        self.closed = True


def test_adb_connect_timeout_is_closed_and_reported(monkeypatch):
    _TimeoutDevice.instances.clear()
    monkeypatch.setattr(adb_module, "AdbDeviceTcp", _TimeoutDevice)

    control = adb_module.ADB()

    assert control.connect(16544) is False
    assert _TimeoutDevice.instances[-1].closed is True


def test_adb_transport_uses_configured_runtime_host(monkeypatch):
    _TimeoutDevice.instances.clear()
    monkeypatch.setattr(adb_module, "AdbDeviceTcp", _TimeoutDevice)
    control_module.set_runtime_device(
        EmulatorInfo(
            "remote-local-forward",
            16544,
            "",
            EmulatorType.CUSTOM,
            7,
            adb_host="127.0.0.2",
        )
    )
    try:
        assert adb_module.ADB().connect() is False
        assert _TimeoutDevice.instances[-1].args[0] == "127.0.0.2"
        assert _TimeoutDevice.instances[-1].kwargs["port"] == 16544
    finally:
        control_module.clear_runtime_device()
