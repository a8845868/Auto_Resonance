"""Time-window availability for limited world-map stations."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from loguru import logger

from core.utils.utils import RESOURCES_PATH


LOCAL_TIMEZONE = timezone(timedelta(hours=8), name="Asia/Shanghai")
AVAILABILITY_PATH = RESOURCES_PATH / "stations" / "availability.json"
KNOWN_LIMITED_STATIONS = frozenset({"武林源"})


def _local_time(at: datetime | None = None) -> datetime:
    value = at or datetime.now(LOCAL_TIMEZONE)
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(
            tzinfo=datetime.now().astimezone().tzinfo
        ).astimezone(LOCAL_TIMEZONE)
    return value.astimezone(LOCAL_TIMEZONE)


def _load_registry(path: Path | None = None) -> dict:
    target = path or AVAILABILITY_PATH
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        logger.error(f"限时站点开放配置读取失败: {exc}")
        return {}
    return data if isinstance(data, dict) else {}


def _parse_boundary(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(LOCAL_TIMEZONE)


def station_unavailable_reason(
    station: str,
    at: datetime | None = None,
    *,
    registry: dict | None = None,
) -> str | None:
    """Return why a station is unavailable; ordinary stations default open."""
    rules = _load_registry() if registry is None else registry
    rule = rules.get(station)
    if rule is None:
        if station in KNOWN_LIMITED_STATIONS:
            return f"{station} 缺少限时开放配置"
        return None
    if not isinstance(rule, dict) or rule.get("limited") is not True:
        return f"{station} 的限时开放配置无效"
    windows = rule.get("windows")
    if not isinstance(windows, list) or not windows:
        return f"{station} 未配置有效开放窗口"

    now = _local_time(at)
    valid_window_seen = False
    for window in windows:
        if not isinstance(window, dict):
            continue
        start = _parse_boundary(window.get("start"))
        end = _parse_boundary(window.get("end_exclusive"))
        if start is None or end is None or end <= start:
            continue
        valid_window_seen = True
        if start <= now < end:
            return None
    if not valid_window_seen:
        return f"{station} 的开放窗口配置无效"
    if rule.get("next_open"):
        return f"{station} 当前不在开放窗口，下次开放信息: {rule['next_open']}"
    return f"{station} 当前未开放，下一次开放时间未定"


def is_station_available(
    station: str,
    at: datetime | None = None,
    *,
    registry: dict | None = None,
) -> bool:
    return station_unavailable_reason(station, at, registry=registry) is None


def available_stations(
    stations,
    at: datetime | None = None,
    *,
    registry: dict | None = None,
) -> list[str]:
    rules = _load_registry() if registry is None else registry
    return [name for name in stations if is_station_available(name, at, registry=rules)]


def unavailable_stations(
    stations,
    at: datetime | None = None,
    *,
    registry: dict | None = None,
) -> list[str]:
    rules = _load_registry() if registry is None else registry
    return [name for name in stations if not is_station_available(name, at, registry=rules)]


def station_availability_evidence(
    stations,
    at: datetime | None = None,
    *,
    registry: dict | None = None,
    ttl: timedelta = timedelta(minutes=5),
) -> dict[str, object]:
    """Return timestamped registry evidence without conflating closed and unknown."""

    now = _local_time(at)
    rules = _load_registry() if registry is None else registry
    available: list[str] = []
    closed: list[str] = []
    unknown: list[str] = []
    for raw_station in stations:
        station = str(raw_station)
        reason = station_unavailable_reason(station, now, registry=rules)
        if reason is None:
            available.append(station)
            continue
        if any(
            marker in reason
            for marker in ("缺少", "配置无效", "未配置", "下一次开放时间未定")
        ):
            unknown.append(station)
        else:
            closed.append(station)
    return {
        "available": available,
        "closed": closed,
        "unknown": unknown,
        "source": "station_registry",
        "observed_at": now.isoformat(timespec="seconds"),
        "valid_until": (now + ttl).isoformat(timespec="seconds"),
    }
