import json
import subprocess
from types import SimpleNamespace

import pytest

from core.control.adb_port import EmulatorInfo, EmulatorType
from core.services.emulator_lifecycle import (
    GAME_PACKAGE,
    EmulatorLifecycle,
    EmulatorQueueLifecycle,
    LifecycleCancelled,
    LifecycleError,
    LifecycleOptions,
    MuMuManagerClient,
    UnsupportedEmulatorOperation,
    device_identity,
    snapshot_device,
)


def _device(index=5, port=16544, emulator_type=EmulatorType.MUMUV5):
    return EmulatorInfo(
        name=f"账号{index}",
        port=port,
        path=r"C:\Program Files\NetEase\MuMu",
        type=emulator_type,
        index=index,
    )


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class FakeAdbState:
    def __init__(self, running=False):
        self.running = running
        self.commands = []
        self.ports = []
        self.closed = 0

    def factory(self, _host, *, port, **_kwargs):
        state = self
        state.ports.append(port)

        class Device:
            def connect(self, **_kwargs):
                return True

            def shell(self, command, **_kwargs):
                state.commands.append((port, command))
                if command == f"pidof {GAME_PACKAGE}":
                    return "2468" if state.running else ""
                if command.startswith("monkey "):
                    state.running = True
                if command == f"am force-stop {GAME_PACKAGE}":
                    state.running = False
                return ""

            def close(self):
                state.closed += 1

        return Device()


class FakeManager:
    def __init__(self, device, adb_state, *, running=False):
        self.device = device
        self.running_port = device.port
        self.adb_state = adb_state
        self.running = running
        self.events = []
        self.shell_commands = []

    def info(self):
        return {
            "index": str(self.device.index),
            "name": self.device.name,
            "is_process_started": self.running,
            "adb_port": self.running_port if self.running else None,
        }

    def launch_emulator(self):
        self.events.append(("launch_emulator", self.device.index))
        self.running = True

    def shutdown_emulator(self):
        self.events.append(("shutdown_emulator", self.device.index))
        self.running = False

    def restart_emulator(self):
        self.events.append(("restart_emulator", self.device.index))
        self.running = True

    def launch_game(self, package):
        self.events.append(("launch_game", self.device.index, package))
        self.adb_state.running = True

    def close_game(self, package):
        self.events.append(("close_game", self.device.index, package))
        self.adb_state.running = False

    def game_info(self, _package):
        return {"state": "running" if self.adb_state.running else "stopped"}

    def shell(self, command):
        self.shell_commands.append((self.device.index, command))
        if command == f"pidof {GAME_PACKAGE}":
            return "2468" if self.adb_state.running else ""
        if command.startswith("monkey "):
            self.adb_state.running = True
        if command == f"am force-stop {GAME_PACKAGE}":
            self.adb_state.running = False
        return ""


def test_mumu_manager_targets_exact_v5_index_and_parses_flat_info():
    calls = []

    def runner(argv, **_kwargs):
        calls.append(argv)
        stdout = "{}"
        if argv[1:3] == ["info", "-v"]:
            stdout = json.dumps(
                {"index": "5", "name": "雷索纳斯", "is_process_started": False}
            )
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")

    client = MuMuManagerClient(_device(), runner=runner)
    assert client.info()["index"] == "5"
    client.launch_emulator()
    client.shutdown_emulator()
    client.restart_emulator()
    client.launch_game()
    client.close_game()
    client.game_info()
    client.shell(f"pidof {GAME_PACKAGE}")

    suffixes = [call[1:] for call in calls]
    assert suffixes == [
        ["info", "-v", "5"],
        ["control", "-v", "5", "launch"],
        ["control", "-v", "5", "shutdown"],
        ["control", "-v", "5", "restart"],
        ["control", "-v", "5", "app", "launch", "-pkg", GAME_PACKAGE],
        ["control", "-v", "5", "app", "close", "-pkg", GAME_PACKAGE],
        ["control", "-v", "5", "app", "info", "-pkg", GAME_PACKAGE],
        ["sh", "-v", "5", "-c", f"pidof {GAME_PACKAGE}"],
    ]
    assert str(client.executable).endswith(r"nx_main\MuMuManager.exe")


def test_mumu_manager_supports_nested_info_and_v4_path():
    def runner(argv, **_kwargs):
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout=json.dumps({"2": {"index": "2", "name": "二号"}}),
            stderr="",
        )

    client = MuMuManagerClient(
        _device(index=2, emulator_type=EmulatorType.MUMUV4), runner=runner
    )
    assert client.info()["name"] == "二号"
    assert str(client.executable).endswith(r"shell\MuMuManager.exe")


def test_manager_errors_are_explicit():
    def runner(argv, **_kwargs):
        return subprocess.CompletedProcess(argv, 7, stdout="", stderr="bad index")

    with pytest.raises(LifecycleError, match="bad index"):
        MuMuManagerClient(_device(), runner=runner).info()


def test_manager_timeout_is_wrapped_as_lifecycle_error():
    def runner(argv, **_kwargs):
        raise subprocess.TimeoutExpired(argv, 1)

    with pytest.raises(LifecycleError, match="命令超时"):
        MuMuManagerClient(_device(), runner=runner, timeout=1).info()


def test_ensure_ready_cold_starts_exact_instance_and_game():
    device = _device(index=5)
    adb = FakeAdbState(running=False)
    manager = FakeManager(device, adb, running=False)
    clock = FakeClock()
    lifecycle = EmulatorLifecycle(
        device,
        manager=manager,
        adb_factory=adb.factory,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
        options=LifecycleOptions(poll_interval=0.1),
    )

    ready = lifecycle.ensure_game_ready()

    assert ready.index == 5
    assert ready.port == 16544
    assert manager.events == [
        ("launch_emulator", 5),
        ("launch_game", 5, GAME_PACKAGE),
    ]
    assert lifecycle.emulator_started_by_us is True
    assert lifecycle.game_started_by_us is True
    assert adb.ports == []


def test_existing_game_start_is_idempotent():
    device = _device()
    adb = FakeAdbState(running=True)
    manager = FakeManager(device, adb, running=True)
    lifecycle = EmulatorLifecycle(device, manager=manager, adb_factory=adb.factory)

    lifecycle.ensure_game_ready()

    assert manager.events == []


def test_game_restart_is_close_force_stop_then_launch():
    device = _device()
    adb = FakeAdbState(running=True)
    manager = FakeManager(device, adb, running=True)
    lifecycle = EmulatorLifecycle(device, manager=manager, adb_factory=adb.factory)

    lifecycle.restart_game()

    assert manager.events == [
        ("close_game", 5, GAME_PACKAGE),
        ("launch_game", 5, GAME_PACKAGE),
    ]
    force_stop = (5, f"am force-stop {GAME_PACKAGE}")
    assert force_stop in manager.shell_commands
    assert adb.commands == []
    assert adb.running is True


def test_stop_game_never_touches_another_instance_port():
    state_a = FakeAdbState(running=True)
    state_b = FakeAdbState(running=True)
    device_a = _device(index=1, port=16416)
    device_b = _device(index=5, port=16544)
    manager_a = FakeManager(device_a, state_a, running=True)
    manager_b = FakeManager(device_b, state_b, running=True)
    lifecycle_a = EmulatorLifecycle(
        device_a, manager=manager_a, adb_factory=state_a.factory
    )
    EmulatorLifecycle(device_b, manager=manager_b, adb_factory=state_b.factory)

    lifecycle_a.stop_game()

    assert state_a.ports == []
    assert (1, f"am force-stop {GAME_PACKAGE}") in manager_a.shell_commands
    assert state_b.commands == []
    assert state_b.running is True


def test_auto_start_disabled_rejects_stopped_emulator():
    device = _device()
    adb = FakeAdbState()
    manager = FakeManager(device, adb, running=False)
    lifecycle = EmulatorLifecycle(
        device,
        manager=manager,
        adb_factory=adb.factory,
        options=LifecycleOptions(auto_start_emulator=False),
    )

    with pytest.raises(LifecycleError, match="自动启动已关闭"):
        lifecycle.ensure_emulator_ready()
    assert manager.events == []


def test_running_v5_waits_for_android_boot_without_auto_launch():
    device = _device()
    adb = FakeAdbState()
    states = [
        {
            "index": "5",
            "name": device.name,
            "is_process_started": True,
            "is_android_started": False,
            "adb_port": device.port,
        },
        {
            "index": "5",
            "name": device.name,
            "is_process_started": True,
            "is_android_started": True,
            "adb_port": device.port,
        },
    ]

    class BootingManager(FakeManager):
        def info(self):
            return states.pop(0) if len(states) > 1 else states[0]

    manager = BootingManager(device, adb, running=True)
    clock = FakeClock()
    lifecycle = EmulatorLifecycle(
        device,
        manager=manager,
        adb_factory=adb.factory,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
        options=LifecycleOptions(
            auto_start_emulator=False,
            poll_interval=0.1,
        ),
    )

    ready = lifecycle.ensure_emulator_ready()

    assert ready.port == 16544
    assert manager.events == []


def test_v5_android_false_is_not_considered_ready():
    device = _device()
    adb = FakeAdbState()

    class StuckManager(FakeManager):
        def info(self):
            return {
                "index": "5",
                "name": device.name,
                "is_process_started": True,
                "is_android_started": False,
                "adb_port": device.port,
            }

    clock = FakeClock()
    lifecycle = EmulatorLifecycle(
        device,
        manager=StuckManager(device, adb, running=True),
        monotonic=clock.monotonic,
        sleep=clock.sleep,
        options=LifecycleOptions(
            auto_start_emulator=False,
            emulator_start_timeout=0,
        ),
    )

    with pytest.raises(LifecycleError, match="启动超时"):
        lifecycle.ensure_emulator_ready()


def test_adb_transport_errors_are_wrapped_and_connection_is_bounded():
    calls = []

    class Device:
        def connect(self, **kwargs):
            calls.append(kwargs)
            raise TimeoutError("transport stalled")

        def close(self):
            calls.append("closed")

    lifecycle = EmulatorLifecycle(
        EmulatorInfo(
            name="custom",
            port=5555,
            path="",
            type=EmulatorType.CUSTOM,
            index=0,
        ),
        adb_factory=lambda *_args, **_kwargs: Device(),
        options=LifecycleOptions(adb_timeout=1.5),
    )

    with pytest.raises(LifecycleError, match="TimeoutError"):
        lifecycle.is_game_running()
    assert calls[0]["transport_timeout_s"] == 1.5
    assert calls[0]["read_timeout_s"] == 1.5
    assert calls[-1] == "closed"


def test_mumu_stop_never_falls_back_to_a_bare_adb_port():
    adb_state = FakeAdbState(running=True)
    device = _device()
    manager = FakeManager(device, adb_state, running=True)

    lifecycle = EmulatorLifecycle(
        device,
        manager=manager,
        adb_factory=lambda *_args, **_kwargs: pytest.fail("bare ADB must not be used"),
    )

    lifecycle.stop_game()

    assert manager.events == [("close_game", 5, GAME_PACKAGE)]
    assert (5, f"am force-stop {GAME_PACKAGE}") in manager.shell_commands


def test_stop_game_never_uses_cached_port_when_fresh_info_has_no_port():
    device = _device(port=16544)
    adb_calls = []

    class StartingManager:
        def __init__(self):
            self.device = device
            self.closed = False

        def info(self):
            return {
                "index": "5",
                "name": device.name,
                "is_process_started": True,
                "is_android_started": False,
            }

        def game_info(self, _package):
            return {"state": "stopped" if self.closed else "running"}

        def close_game(self, _package):
            self.closed = True

        def shell(self, command):
            if command == f"am force-stop {GAME_PACKAGE}":
                self.closed = True
            return ""

    lifecycle = EmulatorLifecycle(
        device,
        manager=StartingManager(),
        adb_factory=lambda *_args, **_kwargs: adb_calls.append("adb"),
    )

    lifecycle.stop_game(require_confirmation=True)

    assert adb_calls == []
    assert lifecycle.device.port is None


def test_stop_game_skips_shell_when_android_is_not_started():
    device = _device(port=16544)
    adb_state = FakeAdbState(running=True)

    class HostOnlyManager(FakeManager):
        def info(self):
            return {
                "index": "5",
                "name": device.name,
                "is_process_started": True,
                "is_android_started": False,
                "adb_port": device.port,
            }

    manager = HostOnlyManager(device, adb_state, running=True)
    lifecycle = EmulatorLifecycle(
        device,
        manager=manager,
        adb_factory=lambda *_args, **_kwargs: pytest.fail("ADB must not be used"),
    )

    lifecycle.stop_game(require_confirmation=True)

    assert manager.events == []
    assert manager.shell_commands == []


def test_restart_emulator_observes_shutdown_then_fresh_launch():
    device = _device()
    adb = FakeAdbState()
    manager = FakeManager(device, adb, running=True)
    lifecycle = EmulatorLifecycle(device, manager=manager, adb_factory=adb.factory)

    ready = lifecycle.restart_emulator()

    assert ready.index == 5
    assert manager.events == [
        ("shutdown_emulator", 5),
        ("launch_emulator", 5),
    ]


def test_wait_is_cancellable_without_running_business_code():
    device = _device()
    adb = FakeAdbState()
    manager = FakeManager(device, adb, running=False)
    lifecycle = EmulatorLifecycle(device, manager=manager, adb_factory=adb.factory)

    with pytest.raises(LifecycleCancelled):
        lifecycle.ensure_game_ready(cancelled=lambda: True)
    assert manager.events == []


def test_custom_adb_game_control_is_supported_but_host_control_is_not():
    device = EmulatorInfo(
        name="自定义",
        port=5555,
        path="",
        type=EmulatorType.CUSTOM,
        index=0,
    )
    adb = FakeAdbState(running=False)
    lifecycle = EmulatorLifecycle(device, adb_factory=adb.factory)

    lifecycle.ensure_game_ready()
    assert adb.running is True
    with pytest.raises(UnsupportedEmulatorOperation):
        lifecycle.stop_emulator()


def test_device_identity_uses_path_type_and_index_not_adb_port():
    first = _device(index=5, port=16544)
    relaunched = _device(index=5, port=26544)
    other = _device(index=4, port=16544)

    assert device_identity(first) == device_identity(relaunched)
    assert device_identity(first) != device_identity(other)


def test_nemu_kill_disconnects_once():
    from core.control.nemu import NEMU

    calls = []
    nemu = object.__new__(NEMU)
    nemu.nemu = SimpleNamespace(nemu_disconnect=lambda handle: calls.append(handle))
    nemu.connect_id = 123

    nemu.kill()
    nemu.kill()

    assert calls == [123]


def test_queue_lifecycle_activates_snapshot_and_cleans_in_safe_order():
    from core.control.control import get_runtime_device, has_runtime_device
    from core.model import app

    events = []
    original = _device(index=5, port=16544)
    previous = snapshot_device(app.Global.device)

    class Session:
        def ensure_game_ready(self, _cancelled):
            events.append("ensure")
            return _device(index=5, port=26544)

        def stop_game(self, _timeout, **_kwargs):
            events.append("stop_game")

        def stop_emulator(self, _timeout):
            events.append("stop_emulator")

    queue_lifecycle = EmulatorQueueLifecycle(
        original,
        lifecycle=Session(),
        options=LifecycleOptions(close_emulator_when_idle=True),
        release_controller=lambda: events.append("release_controller"),
    )
    # A later GUI selection/mutation must not affect the frozen queue target.
    original.index = 1
    original.port = 16416

    try:
        queue_lifecycle.prepare()
        assert get_runtime_device().index == 5
        assert get_runtime_device().port == 26544
        # Simulate the config watcher applying a new GUI selection mid-queue.
        app.Global.device = _device(index=1, port=16416)
        assert get_runtime_device().index == 5
        queue_lifecycle.cleanup()
        queue_lifecycle.cleanup()

        assert has_runtime_device() is False
        assert events == [
            "ensure",
            "release_controller",
            "stop_game",
            "stop_emulator",
        ]
    finally:
        app.Global.device = previous


def test_failed_prepare_compensates_for_emulator_started_by_queue():
    events = []

    class FailedSession:
        emulator_started_by_us = True

        def ensure_game_ready(self, _cancelled):
            raise LifecycleError("game did not start")

        def stop_game(self, _timeout, **_kwargs):
            events.append("stop_game")

        def stop_emulator(self, _timeout):
            events.append("stop_emulator")

    queue_lifecycle = EmulatorQueueLifecycle(
        _device(),
        lifecycle=FailedSession(),
        release_controller=lambda: events.append("release_controller"),
    )

    with pytest.raises(LifecycleError):
        queue_lifecycle.prepare()
    queue_lifecycle.cleanup()

    assert events == ["release_controller", "stop_game", "stop_emulator"]


def test_runtime_target_shields_control_and_recovery_from_gui_selection(monkeypatch):
    import core.control.control as control
    import core.services.game_recovery as recovery
    from core.model import app

    previous = snapshot_device(app.Global.device)
    selected = []

    class FakeControl:
        def __init__(self, device):
            selected.append(device.index)

        def connect(self, _port=None):
            return True

    try:
        control.set_runtime_device(_device(index=5, port=16544))
        control.set_runtime_auto_start_emulator(False)
        app.Global.device = _device(index=1, port=16416)
        monkeypatch.setattr(control, "NEMU", FakeControl)

        assert control.connect() is True
        lifecycle = recovery._current_lifecycle()

        assert selected == [5]
        assert lifecycle.device.index == 5
        assert lifecycle.options.auto_start_emulator is False
    finally:
        control.clear_runtime_device()
        app.Global.device = previous
