import json
import subprocess

import pytest

import app.view.adb_data_interface as adb_data_interface
import core.services.emulator_lifecycle as emulator_lifecycle
from app.common.config import EmulatorSerializer
from core.control.adb_port import EmulatorInfo, EmulatorType
from core.services.emulator_lifecycle import (
    EmulatorLifecycle,
    LifecycleError,
    LifecycleOptions,
    MuMuManagerClient,
)
from tools.mumu_instance_smoke import _stable_signature, main as smoke_main


def _mumu_device(path, *, index=0, port=None):
    return EmulatorInfo(
        name="雷索纳斯",
        port=port,
        path=str(path),
        type=EmulatorType.MUMUV5,
        index=index,
    )


def test_instance_zero_is_valid():
    device = EmulatorInfo.from_dict(
        {
            "name": "雷索纳斯",
            "port": None,
            "path": r"C:\Program Files\NetEase\MuMu",
            "type": "MuMuV5",
            "index": "0",
        }
    )

    assert device.index == 0
    assert isinstance(device.index, int)
    assert device.is_mumu is True


def test_config_roundtrip_preserves_instance_zero():
    serializer = EmulatorSerializer()
    original = _mumu_device(r"C:\Program Files\NetEase\MuMu", index=0)

    payload = json.loads(json.dumps(serializer.serialize(original)))
    restored = serializer.deserialize(payload)

    assert restored.index == 0
    assert restored.type is EmulatorType.MUMUV5
    assert restored.path == original.path


def test_sparse_instance_id_is_not_row_index(monkeypatch):
    sparse_instances = [
        _mumu_device(r"C:\MuMu", index=index) for index in [0, 6, 7, 8, 9, 10]
    ]
    selected = []
    monkeypatch.setattr(
        adb_data_interface.qconfig,
        "set",
        lambda item, value: selected.append((item, value)),
    )

    adb_data_interface.ADBDataInterface.set_port(object(), sparse_instances[4])

    assert selected[0][1].index == 9


def test_nx_main_directory_with_spaces_resolves_launcher(tmp_path):
    nx_main = tmp_path / "Program Files" / "NetEase" / "MuMu" / "nx_main"
    nx_main.mkdir(parents=True)
    executable = nx_main / "MuMuManager.exe"
    executable.write_bytes(b"")

    client = MuMuManagerClient(_mumu_device(nx_main))

    assert client.executable == executable


def test_explicit_launcher_executable_is_supported(tmp_path):
    nx_main = tmp_path / "Program Files" / "NetEase" / "MuMu" / "nx_main"
    nx_main.mkdir(parents=True)
    executable = nx_main / "mumu-cli.exe"
    executable.write_bytes(b"")

    client = MuMuManagerClient(_mumu_device(executable))

    assert client.executable == executable


def test_invalid_install_path_returns_actionable_error(tmp_path):
    missing = tmp_path / "missing" / "nx_main"

    with pytest.raises(LifecycleError, match="MuMu 安装路径无效|未找到受支持的启动器"):
        MuMuManagerClient(_mumu_device(missing))


def test_launcher_command_uses_argument_list_and_launcher_cwd(tmp_path):
    nx_main = tmp_path / "Program Files" / "NetEase" / "MuMu" / "nx_main"
    nx_main.mkdir(parents=True)
    executable = nx_main / "MuMuManager.exe"
    executable.write_bytes(b"")
    calls = []

    def runner(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout=json.dumps(
                {
                    "index": "0",
                    "name": "雷索纳斯",
                    "is_process_started": False,
                    "is_android_started": False,
                }
            ),
            stderr="",
        )

    client = MuMuManagerClient(_mumu_device(nx_main), runner=runner)
    client.info()

    argv, kwargs = calls[0]
    assert argv == [str(executable), "info", "-v", "0"]
    assert kwargs["shell"] is False
    assert kwargs["cwd"] == str(nx_main)


def test_default_launcher_runner_has_one_bounded_timeout(monkeypatch, tmp_path):
    nx_main = tmp_path / "MuMu" / "nx_main"
    nx_main.mkdir(parents=True)
    executable = nx_main / "MuMuManager.exe"
    executable.write_bytes(b"")
    calls = []

    def run_tree(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout=json.dumps({"index": "0", "name": "雷索纳斯"}),
            stderr="",
        )

    monkeypatch.setattr(emulator_lifecycle, "_run_subprocess_tree", run_tree)
    client = MuMuManagerClient(_mumu_device(nx_main), timeout=7)

    client.info()

    assert calls[0][1]["timeout"] == 7


def test_custom_adb_label_does_not_claim_mumu_instance_zero():
    lifecycle = EmulatorLifecycle(
        EmulatorInfo(
            name="自定义端口",
            port=16384,
            path="",
            type=EmulatorType.CUSTOM,
            index=0,
        )
    )

    assert "index=" not in lifecycle.label
    assert "127.0.0.1:16384" in lifecycle.label


def test_offline_custom_adb_fails_before_game_launch_wait():
    attempts = []

    class OfflineAdb:
        def connect(self, **_kwargs):
            attempts.append("connect")
            raise ConnectionRefusedError("offline")

        def close(self):
            attempts.append("close")

    lifecycle = EmulatorLifecycle(
        EmulatorInfo(
            name="自定义端口",
            port=16384,
            path="",
            type=EmulatorType.CUSTOM,
            index=0,
        ),
        adb_factory=lambda *_args, **_kwargs: OfflineAdb(),
        options=LifecycleOptions(game_start_timeout=180, poll_interval=2),
    )

    with pytest.raises(
        LifecycleError,
        match="自定义 ADB.*无法自动启动.*ADB信息.*MuMu",
    ):
        lifecycle.ensure_game_ready()

    assert attempts == ["connect", "close"]


def test_smoke_comparison_ignores_running_elapsed_time():
    before = {
        "pid": 45392,
        "created_timestamp": 123,
        "launch_time": 1000,
        "is_process_started": True,
        "is_android_started": True,
        "adb_port": 16576,
    }
    after = {**before, "launch_time": 2000}

    assert _stable_signature(before) == _stable_signature(after)


def test_smoke_launch_game_requires_running_game(monkeypatch, capsys):
    class Manager:
        executable = "MuMuManager.exe"

        def __init__(self, *_args, **_kwargs):
            pass

        def all_info(self):
            return {
                "0": {
                    "pid": 123,
                    "created_timestamp": 456,
                    "is_process_started": True,
                    "is_android_started": True,
                    "adb_port": 16384,
                }
            }

    class Lifecycle:
        def __init__(self, device, **_kwargs):
            self.device = device

        def ensure_game_ready(self):
            self.device.port = 16384
            return self.device

        def android_boot_completed(self):
            return True

        def is_game_running(self):
            return False

    monkeypatch.setattr("tools.mumu_instance_smoke.MuMuManagerClient", Manager)
    monkeypatch.setattr("tools.mumu_instance_smoke.EmulatorLifecycle", Lifecycle)
    monkeypatch.setattr(
        "sys.argv",
        [
            "mumu_instance_smoke.py",
            "--install-path",
            r"C:\MuMu",
            "--instance-id",
            "0",
            "--launch-game",
        ],
    )

    assert smoke_main() == 1
    assert json.loads(capsys.readouterr().out)["game_running"] is False
