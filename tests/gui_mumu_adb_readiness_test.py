from __future__ import annotations

from types import SimpleNamespace

import pytest

from core.control.adb_port import EmulatorInfo, EmulatorType
from core.services.emulator_lifecycle import (
    AdbReadinessState,
    EmulatorLifecycle,
    EmulatorQueueLifecycle,
    LifecycleCancelled,
    LifecycleError,
    LifecycleOptions,
    resolve_instance_zero_configuration,
    resolve_selected_mumu_configuration,
)


def device(index=0, port=16384, kind=EmulatorType.MUMUV5, path=r"C:\MuMu"):
    return EmulatorInfo(f"MuMu {index}", port, path, kind, index)


class Clock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class Manager:
    def __init__(self, target, states, *, game_running=True):
        self.device = target
        self.states = list(states)
        self.last = self.states[-1]
        self.launches = 0
        self.game_launches = 0
        self.game_running = game_running
        self.shell_commands = []

    def info(self):
        if self.states:
            self.last = self.states.pop(0)
        return dict(self.last)

    def launch_emulator(self):
        self.launches += 1

    def game_info(self, _package):
        return {"state": "running" if self.game_running else "stopped"}

    def launch_game(self, _package):
        self.game_launches += 1
        self.game_running = True

    def shell(self, command):
        self.shell_commands.append(command)
        return "2468" if command.startswith("pidof ") and self.game_running else ""


class AdbHarness:
    def __init__(self, boot_values=("1",), connect_values=(True,)):
        self.boot_values = list(boot_values)
        self.connect_values = list(connect_values)
        self.factory_calls = []
        self.connect_calls = 0
        self.close_calls = 0

    def factory(self, host, *, port, **_kwargs):
        self.factory_calls.append((host, port))
        harness = self

        class Adb:
            def connect(self, **_kwargs):
                harness.connect_calls += 1
                result = harness.connect_values.pop(0) if harness.connect_values else True
                if isinstance(result, BaseException):
                    raise result
                return result

            def shell(self, command, **_kwargs):
                assert command == "getprop sys.boot_completed"
                result = harness.boot_values.pop(0) if harness.boot_values else "1"
                if isinstance(result, BaseException):
                    raise result
                return result

            def close(self):
                harness.close_calls += 1

        return Adb()


def ready_state(port=17000, *, android=True, host="127.0.0.1"):
    return {
        "index": "0",
        "name": "MuMu 0",
        "is_process_started": True,
        "is_android_started": android,
        "adb_host_ip": host,
        "adb_port": port,
    }


def lifecycle(manager, adb, clock, *, tcp_probe=lambda _h, _p, _t: True, **options):
    return EmulatorLifecycle(
        device(port=None),
        manager=manager,
        adb_factory=adb.factory,
        tcp_probe=tcp_probe,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
        options=LifecycleOptions(
            poll_interval=0.1,
            adb_poll_interval=0.1,
            adb_port_ready_timeout=0.3,
            adb_device_ready_timeout=0.3,
            **options,
        ),
    )


def test_custom_gui_target_resolves_unique_mumu_instance_zero_only():
    configured = device(kind=EmulatorType.CUSTOM, path="")
    observed = []

    def discover(*, preferred):
        observed.append(preferred)
        return [device(index=6, port=16640), device(index=0, port=17000)]

    resolved = resolve_instance_zero_configuration(configured, discover=discover)

    assert resolved.is_mumu
    assert resolved.index == 0
    assert resolved.port == 17000
    assert observed == [configured]


def test_custom_gui_target_stops_on_ambiguous_instance_zero():
    configured = device(kind=EmulatorType.CUSTOM, path="")
    with pytest.raises(LifecycleError, match="selected_instance_configuration_ambiguous"):
        resolve_instance_zero_configuration(
            configured,
            discover=lambda **_kwargs: [
                device(path=r"C:\MuMu-A"),
                device(path=r"C:\MuMu-B"),
            ],
        )


def test_selected_mumu_resolver_supports_sparse_nonzero_instance():
    configured = device(index=7, kind=EmulatorType.CUSTOM, path="")
    resolved = resolve_selected_mumu_configuration(
        configured,
        discover=lambda **_kwargs: [device(index=0), device(index=7, port=17000)],
    )

    assert resolved.index == 7
    assert resolved.port == 17000


def test_queue_defers_resolution_and_lifecycle_creation_until_prepare():
    events = []
    configured = device(kind=EmulatorType.CUSTOM, path="")

    class Session:
        emulator_started_by_us = False

        def ensure_game_ready(self, _cancelled):
            events.append("ensure")
            return device(port=17000)

    def resolve(_configured):
        events.append("resolve")
        return device(port=17000)

    def create(resolved, **_kwargs):
        events.append(("create", resolved.index, resolved.port))
        return Session()

    queue = EmulatorQueueLifecycle(
        configured,
        target_resolver=resolve,
        lifecycle_factory=create,
        release_controller=lambda: None,
        options=LifecycleOptions(close_game_when_idle=False),
    )
    assert events == []

    queue.prepare()

    assert events == ["resolve", ("create", 0, 17000), "ensure"]


def test_android_not_started_never_probes_or_connects_adb():
    clock = Clock()
    adb = AdbHarness()
    tcp_calls = []
    manager = Manager(device(port=None), [ready_state(android=False)])
    subject = lifecycle(
        manager,
        adb,
        clock,
        tcp_probe=lambda *args: tcp_calls.append(args) or True,
        emulator_start_timeout=0,
        auto_start_emulator=False,
    )

    with pytest.raises(LifecycleError):
        subject.ensure_emulator_ready()

    assert tcp_calls == []
    assert adb.factory_calls == []
    assert manager.launches == 0


def test_cold_instance_launches_once_then_waits_for_android_and_tcp():
    clock = Clock()
    adb = AdbHarness()
    tcp_results = iter([False, False, True])
    manager = Manager(
        device(port=None),
        [
            {"index": "0", "is_process_started": False, "adb_port": None},
            ready_state(android=False, port=None),
            ready_state(port=17001),
        ],
    )
    subject = lifecycle(
        manager,
        adb,
        clock,
        tcp_probe=lambda _h, _p, _t: next(tcp_results),
    )

    ready = subject.ensure_emulator_ready()

    assert ready.port == 17001
    assert manager.launches == 1
    assert adb.factory_calls == [("127.0.0.1", 17001)]
    assert [item["state"] for item in subject.readiness_history][-2:] == [
        AdbReadinessState.ADB_CONNECTING.value,
        AdbReadinessState.ADB_DEVICE_READY.value,
    ]


def test_instance_launch_delivery_unknown_is_still_counted_once():
    clock = Clock()
    adb = AdbHarness()

    class FailingLaunchManager(Manager):
        def launch_emulator(self):
            self.launches += 1
            raise OSError("manager delivery unknown")

    manager = FailingLaunchManager(
        device(port=None),
        [{"index": "0", "is_process_started": False, "adb_port": None}],
    )
    subject = lifecycle(manager, adb, clock)

    with pytest.raises(OSError, match="delivery unknown"):
        subject.ensure_emulator_ready()

    assert manager.launches == 1
    assert subject.emulator_launch_dispatches == 1


def test_tcp_refusal_is_bounded_and_never_constructs_adb_client():
    clock = Clock()
    adb = AdbHarness()
    manager = Manager(device(port=None), [ready_state(port=17002)])
    subject = lifecycle(manager, adb, clock, tcp_probe=lambda *_args: False)

    with pytest.raises(LifecycleError, match="adb_port_ready_timeout"):
        subject.ensure_emulator_ready()

    assert adb.factory_calls == []
    assert subject.readiness_history[-1]["reason_code"] == "adb_port_ready_timeout"


def test_endpoint_is_refreshed_from_current_manager_mapping():
    clock = Clock()
    adb = AdbHarness()
    manager = Manager(
        device(port=16384),
        [ready_state(port=17003, host="127.0.0.2")],
    )
    subject = lifecycle(manager, adb, clock)

    ready = subject.ensure_emulator_ready()

    assert ready.port == 17003
    assert adb.factory_calls == [("127.0.0.2", 17003)]
    assert subject.endpoint_source == "mumu_manager_instance_info"
    assert subject.endpoint_resolution_timestamp == 0.0


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (RuntimeError("device offline"), "adb_device_offline"),
        (RuntimeError("device unauthorized"), "adb_device_unauthorized"),
    ],
)
def test_adb_device_failures_have_explicit_reason(error, reason):
    clock = Clock()
    adb = AdbHarness(connect_values=(error,))
    manager = Manager(device(port=None), [ready_state()])
    subject = lifecycle(manager, adb, clock)

    with pytest.raises(LifecycleError, match=reason):
        subject.ensure_emulator_ready()

    assert adb.close_calls == 1


def test_stop_cancels_tcp_wait_without_connect_or_package_launch():
    clock = Clock()
    adb = AdbHarness()
    manager = Manager(device(port=None), [ready_state()], game_running=False)
    cancelled = {"value": False}

    def probe(*_args):
        cancelled["value"] = True
        return False

    subject = lifecycle(manager, adb, clock, tcp_probe=probe)

    with pytest.raises(LifecycleCancelled):
        subject.ensure_game_ready(lambda: cancelled["value"])

    assert adb.factory_calls == []
    assert manager.game_launches == 0
    assert subject.readiness_history[-1]["state"] == AdbReadinessState.CANCELLED.value


def test_stop_cancels_adb_device_wait_and_closes_single_client():
    clock = Clock()
    adb = AdbHarness(boot_values=("0", "0"))
    manager = Manager(device(port=None), [ready_state()])
    cancelled = {"value": False}

    def stop_after_first_sleep(seconds):
        clock.sleep(seconds)
        cancelled["value"] = True

    subject = lifecycle(manager, adb, clock)
    subject.sleep = stop_after_first_sleep

    with pytest.raises(LifecycleCancelled):
        subject.ensure_emulator_ready(lambda: cancelled["value"])

    assert len(adb.factory_calls) == 1
    assert adb.connect_calls == 1
    assert adb.close_calls == 1


def test_package_launch_is_at_most_once_after_adb_ready():
    clock = Clock()
    adb = AdbHarness()
    manager = Manager(device(port=None), [ready_state()], game_running=False)
    subject = lifecycle(manager, adb, clock)

    subject.ensure_game_ready()

    assert manager.launches == 0
    assert manager.game_launches == 1
    assert subject.game_launch_dispatches == 1
    assert subject.readiness_history[-1]["state"] == AdbReadinessState.PACKAGE_READY.value


def test_selected_adb_executable_is_used_for_endpoint_connect(tmp_path):
    adb_executable = tmp_path / "adb.exe"
    adb_executable.write_bytes(b"")
    target = device(port=None)
    target.adb_path = str(adb_executable)
    manager = Manager(target, [ready_state(port=17004)])
    clock = Clock()
    adb = AdbHarness()
    commands = []

    def runner(argv, **kwargs):
        commands.append((argv, kwargs))
        return SimpleNamespace(returncode=0, stdout="connected", stderr="")

    subject = EmulatorLifecycle(
        target,
        manager=manager,
        adb_factory=adb.factory,
        tcp_probe=lambda *_args: True,
        adb_runner=runner,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
        options=LifecycleOptions(adb_poll_interval=0.1),
    )

    subject.ensure_emulator_ready()

    assert commands[0][0] == [str(adb_executable), "connect", "127.0.0.1:17004"]
    assert commands[0][1]["cwd"] == str(tmp_path)
    assert not any(
        key.upper().startswith("QT_") for key in commands[0][1]["env"]
    )


def test_stop_cancels_package_wait_after_single_launch():
    clock = Clock()
    adb = AdbHarness()

    class StartingManager(Manager):
        def launch_game(self, _package):
            self.game_launches += 1

    manager = StartingManager(device(port=None), [ready_state()], game_running=False)
    cancelled = {"value": False}

    def stop_after_first_sleep(seconds):
        clock.sleep(seconds)
        cancelled["value"] = True

    subject = lifecycle(manager, adb, clock)
    subject.sleep = stop_after_first_sleep

    with pytest.raises(LifecycleCancelled):
        subject.ensure_game_ready(lambda: cancelled["value"])

    assert manager.game_launches == 1
    assert subject.game_launch_dispatches == 1
    assert subject.readiness_history[-1]["state"] == AdbReadinessState.CANCELLED.value
