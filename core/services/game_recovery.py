"""Detect and recover an unexpectedly closed Resonance client."""

import time
from dataclasses import dataclass
from typing import Callable, Optional

from loguru import logger

from core.control.control import (
    get_runtime_auto_start_emulator,
    get_runtime_device,
    has_runtime_device,
    set_runtime_device,
)
from core.model import app
from core.services.emulator_lifecycle import (
    GAME_PACKAGE,
    EmulatorLifecycle,
    LifecycleOptions,
    snapshot_device,
)


@dataclass(frozen=True)
class TradeRecoveryState:
    city: str
    next_action: str


def _current_lifecycle() -> EmulatorLifecycle:
    return EmulatorLifecycle(
        snapshot_device(get_runtime_device()),
        options=LifecycleOptions(
            auto_start_emulator=get_runtime_auto_start_emulator(),
            close_game_when_idle=False,
        ),
    )


def _activate_ready_device(device) -> None:
    if has_runtime_device():
        set_runtime_device(device)
    else:
        app.Global.device = snapshot_device(device)


def is_game_running() -> bool:
    """Return whether the game package owns a live Android process."""
    return _current_lifecycle().is_game_running()


def start_game() -> None:
    """Ensure the selected emulator and its game process are running."""
    lifecycle = _current_lifecycle()
    _activate_ready_device(lifecycle.ensure_emulator_ready())
    lifecycle.start_game()


def stop_game() -> None:
    """Close only the selected instance's game package, keeping MuMu alive."""
    _current_lifecycle().stop_game()


def restart_game() -> None:
    """Restart only the selected instance's game package."""
    lifecycle = _current_lifecycle()
    _activate_ready_device(lifecycle.ensure_emulator_ready())
    lifecycle.restart_game()


def recover_game(
    inspect_station: Callable[[], Optional[str]],
    expected_cities: set[str],
    timeout: float = 180.0,
    poll_interval: float = 5.0,
) -> Optional[TradeRecoveryState]:
    """Restart the client and inspect the in-game trade position.

    Station recognition is deliberately the source of truth: a run is only
    resumed when the client has reached a known endpoint. This prevents an
    uncertain restart from buying twice or recording an unfinished run.
    """
    logger.warning("检测到游戏意外退出，正在重新启动游戏")
    start_game()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if not is_game_running():
                start_game()
                time.sleep(poll_interval)
                continue
            city = inspect_station()
            if city in expected_cities:
                state = TradeRecoveryState(city=city, next_action="从当前城市重新检查并继续跑商")
                logger.info(f"游戏恢复成功；当前跑商位置: {state.city}；{state.next_action}")
                return state
        except Exception as exc:
            logger.debug(f"等待游戏恢复: {exc}")
        time.sleep(poll_interval)
    logger.error("游戏重启后未能在限定时间内识别当前跑商状态，安全停止")
    return None
