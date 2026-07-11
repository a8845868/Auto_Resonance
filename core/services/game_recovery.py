"""Detect and recover an unexpectedly closed Resonance client."""

import time
from dataclasses import dataclass
from typing import Callable, Optional

from adb_shell.adb_device import AdbDeviceTcp
from loguru import logger

from core.model import app

GAME_PACKAGE = "com.hermes.goda"


@dataclass(frozen=True)
class TradeRecoveryState:
    city: str
    next_action: str


def _adb() -> AdbDeviceTcp:
    device = AdbDeviceTcp("127.0.0.1", port=int(app.Global.device.port))
    if not device.connect():
        raise ConnectionError("无法连接模拟器 ADB")
    return device


def is_game_running() -> bool:
    """Return whether the game package owns a live Android process."""
    device = _adb()
    try:
        output = device.shell(f"pidof {GAME_PACKAGE}")
        return bool(str(output).strip())
    finally:
        device.close()


def start_game() -> None:
    """Launch the game's default activity without depending on its activity name."""
    device = _adb()
    try:
        output = device.shell(
            f"monkey -p {GAME_PACKAGE} -c android.intent.category.LAUNCHER 1"
        )
        if "No activities found" in str(output):
            raise RuntimeError(f"未找到游戏包 {GAME_PACKAGE}")
    finally:
        device.close()


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
