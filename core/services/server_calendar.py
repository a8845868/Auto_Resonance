"""Shared timezone-aware game server day and week boundaries."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone, tzinfo


SERVER_TIMEZONE_NAME = "Asia/Shanghai"
SERVER_UTC_OFFSET = timedelta(hours=8)
DAILY_RESET_TIME = time(5, 0)
WEEKLY_RESET_WEEKDAY = 0  # Monday; shared with the existing weekly shop reset.


@dataclass(frozen=True)
class GameServerClock:
    # The game server uses a fixed UTC+08:00 clock.  A fixed offset keeps the
    # packaged Windows build deterministic without adding the external tzdata
    # wheel, which is not otherwise a project dependency.
    timezone: tzinfo = field(
        default_factory=lambda: timezone(SERVER_UTC_OFFSET, SERVER_TIMEZONE_NAME)
    )
    daily_reset: time = DAILY_RESET_TIME
    weekly_reset_weekday: int = WEEKLY_RESET_WEEKDAY

    def server_now(self) -> datetime:
        return datetime.now(self.timezone)

    def _server_time(self, value: datetime | None) -> datetime:
        current = value or self.server_now()
        if current.tzinfo is None or current.utcoffset() is None:
            raise ValueError("server timestamps must be timezone-aware")
        return current.astimezone(self.timezone)

    def server_day_date(self, value: datetime | None = None) -> date:
        current = self._server_time(value)
        boundary = datetime.combine(current.date(), self.daily_reset, self.timezone)
        if current < boundary:
            return current.date() - timedelta(days=1)
        return current.date()

    def server_day_id(self, value: datetime | None = None) -> str:
        return self.server_day_date(value).isoformat()

    def server_week_date(self, value: datetime | None = None) -> date:
        server_day = self.server_day_date(value)
        days_since_reset = (server_day.weekday() - self.weekly_reset_weekday) % 7
        return server_day - timedelta(days=days_since_reset)

    def server_week_id(self, value: datetime | None = None) -> str:
        return self.server_week_date(value).isoformat()

    def next_daily_reset(self, value: datetime | None = None) -> datetime:
        current = self._server_time(value)
        target = datetime.combine(current.date(), self.daily_reset, self.timezone)
        if target <= current:
            target += timedelta(days=1)
        return target

    def next_weekly_reset(self, value: datetime | None = None) -> datetime:
        current = self._server_time(value)
        days = (self.weekly_reset_weekday - current.weekday()) % 7
        target_date = current.date() + timedelta(days=days)
        target = datetime.combine(target_date, self.daily_reset, self.timezone)
        if target <= current:
            target += timedelta(days=7)
        return target

    def is_same_server_day(self, left: datetime, right: datetime) -> bool:
        return self.server_day_id(left) == self.server_day_id(right)

    def is_same_server_week(self, left: datetime, right: datetime) -> bool:
        return self.server_week_id(left) == self.server_week_id(right)


SERVER_CLOCK = GameServerClock()


def server_now() -> datetime:
    return SERVER_CLOCK.server_now()


def server_day_id(value: datetime | None = None) -> str:
    return SERVER_CLOCK.server_day_id(value)


def server_week_id(value: datetime | None = None) -> str:
    return SERVER_CLOCK.server_week_id(value)


def next_daily_reset(value: datetime | None = None) -> datetime:
    return SERVER_CLOCK.next_daily_reset(value)


def next_weekly_reset(value: datetime | None = None) -> datetime:
    return SERVER_CLOCK.next_weekly_reset(value)


def is_same_server_day(left: datetime, right: datetime) -> bool:
    return SERVER_CLOCK.is_same_server_day(left, right)


def is_same_server_week(left: datetime, right: datetime) -> bool:
    return SERVER_CLOCK.is_same_server_week(left, right)
