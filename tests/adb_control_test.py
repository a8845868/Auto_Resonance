from adb_shell.exceptions import TcpTimeoutException

import core.control.adb as adb_module


class _TimeoutDevice:
    instances = []

    def __init__(self, *_args, **_kwargs):
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
