"""Optional Windows smoke test for one exact MuMu multi-open instance."""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.control.adb_port import EmulatorInfo, EmulatorType  # noqa: E402
from core.services.emulator_lifecycle import (  # noqa: E402
    EmulatorLifecycle,
    LifecycleOptions,
    MuMuManagerClient,
)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="验证一个明确 MuMu 实例的启动、ADB 和 Android 就绪状态",
    )
    parser.add_argument("--install-path", required=True)
    parser.add_argument("--instance-id", required=True, type=int)
    parser.add_argument("--name", default="MuMu target")
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument(
        "--launch-game",
        action="store_true",
        help="Android 就绪后继续启动雷索纳斯游戏包",
    )
    parser.add_argument(
        "--persist-selection",
        action="store_true",
        help="验收成功后通过 qconfig 保存为 AutoResonance 当前设备",
    )
    return parser.parse_args()


def _signature(info: dict) -> dict:
    return {
        "pid": info.get("pid"),
        "created_timestamp": info.get("created_timestamp"),
        "launch_time": info.get("launch_time"),
        "process_started": bool(info.get("is_process_started")),
        "android_started": bool(info.get("is_android_started")),
        "adb_port": info.get("adb_port"),
    }


def _stable_signature(info: dict) -> dict:
    signature = _signature(info)
    signature.pop("launch_time", None)
    return signature


def main() -> int:
    args = _parse_args()
    correlation_id = uuid.uuid4().hex
    device = EmulatorInfo(
        name=args.name,
        port=None,
        path=args.install_path,
        type=EmulatorType.MUMUV5,
        index=args.instance_id,
    )
    options = LifecycleOptions(
        emulator_start_timeout=max(1.0, args.timeout),
        game_start_timeout=max(1.0, args.timeout),
    )
    manager = MuMuManagerClient(
        device,
        timeout=options.command_timeout,
        correlation_id=correlation_id,
    )
    before = manager.all_info()
    lifecycle = EmulatorLifecycle(
        device,
        options=options,
        manager=manager,
        correlation_id=correlation_id,
    )
    ready = (
        lifecycle.ensure_game_ready()
        if args.launch_game
        else lifecycle.ensure_emulator_ready()
    )
    after = manager.all_info()
    target_key = str(args.instance_id)
    before_others = {
        key: _signature(value)
        for key, value in before.items()
        if key != target_key and isinstance(value, dict)
    }
    after_others = {
        key: _signature(value)
        for key, value in after.items()
        if key != target_key and isinstance(value, dict)
    }
    other_instances_unchanged = all(
        key in after and isinstance(after[key], dict)
        and _stable_signature(value) == _stable_signature(after[key])
        for key, value in before.items()
        if key != target_key and isinstance(value, dict)
    )
    boot_completed = "1" if lifecycle.android_boot_completed() else "0"
    if args.persist_selection:
        from app.common.config import cfg, qconfig

        qconfig.set(cfg.device, ready)
    game_running = lifecycle.is_game_running()
    result = {
        "correlation_id": correlation_id,
        "instance_id": args.instance_id,
        "launcher": str(manager.executable),
        "before_target": before.get(target_key),
        "after_target": after.get(target_key),
        "adb_endpoint": f"127.0.0.1:{ready.port}",
        "boot_completed": boot_completed,
        "game_running": game_running,
        "other_instances_unchanged": other_instances_unchanged,
        "other_instances_before": before_others,
        "other_instances_after": after_others,
        "selection_persisted": bool(args.persist_selection),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    game_requirement_met = not args.launch_game or game_running
    return (
        0
        if boot_completed == "1"
        and other_instances_unchanged
        and game_requirement_met
        else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())
