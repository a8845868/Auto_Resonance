from datetime import datetime, timezone

import pytest

from core.services.server_calendar import GameServerClock


def test_server_day_uses_0500_boundary():
    clock = GameServerClock()
    before = datetime(2026, 7, 13, 4, 59, 59, tzinfo=clock.timezone)
    after = datetime(2026, 7, 13, 5, 0, 0, tzinfo=clock.timezone)

    assert clock.server_day_id(before) == "2026-07-12"
    assert clock.server_day_id(after) == "2026-07-13"
    assert clock.next_daily_reset(before) == after


def test_server_week_rotates_at_monday_0500_not_midnight():
    clock = GameServerClock()
    before = datetime(2026, 7, 13, 4, 59, 59, tzinfo=clock.timezone)
    after = datetime(2026, 7, 13, 5, 0, 0, tzinfo=clock.timezone)

    assert clock.server_week_id(before) == "2026-07-06"
    assert clock.server_week_id(after) == "2026-07-13"
    assert clock.next_weekly_reset(before) == after


def test_server_ids_are_independent_of_local_timezone():
    clock = GameServerClock()
    utc = datetime(2026, 7, 12, 21, 0, tzinfo=timezone.utc)

    assert clock.server_day_id(utc) == "2026-07-13"
    assert clock.server_week_id(utc) == "2026-07-13"


def test_server_clock_rejects_naive_timestamps():
    with pytest.raises(ValueError, match="timezone-aware"):
        GameServerClock().server_day_id(datetime(2026, 7, 13, 5, 0))
