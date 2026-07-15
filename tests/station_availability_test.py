from datetime import datetime, timezone

from core.services.station_availability import (
    available_stations,
    is_station_available,
    station_unavailable_reason,
    unavailable_stations,
)


REGISTRY = {
    "限时站": {
        "limited": True,
        "windows": [
            {
                "start": "2026-05-13T05:00:00+08:00",
                "end_exclusive": "2026-07-15T12:00:00+08:00",
            },
            {
                "start": "2026-08-01T05:00:00+08:00",
                "end_exclusive": "2026-08-03T05:00:00+08:00",
            },
        ],
        "next_open": None,
    }
}


def test_limited_station_uses_half_open_shanghai_windows_and_can_reopen():
    assert not is_station_available(
        "限时站", datetime(2026, 5, 13, 4, 59, 59), registry=REGISTRY
    )
    assert is_station_available(
        "限时站", datetime(2026, 5, 13, 5, 0, 0), registry=REGISTRY
    )
    assert is_station_available(
        "限时站", datetime(2026, 7, 15, 11, 59, 59), registry=REGISTRY
    )
    assert not is_station_available(
        "限时站", datetime(2026, 7, 15, 12, 0, 0), registry=REGISTRY
    )
    assert is_station_available(
        "限时站", datetime(2026, 8, 1, 5, 0, 0), registry=REGISTRY
    )


def test_station_window_compares_timezone_aware_instants():
    assert is_station_available(
        "限时站",
        datetime(2026, 5, 12, 21, 0, 0, tzinfo=timezone.utc),
        registry=REGISTRY,
    )
    assert not is_station_available(
        "限时站",
        datetime(2026, 7, 15, 4, 0, 0, tzinfo=timezone.utc),
        registry=REGISTRY,
    )


def test_ordinary_stations_default_open_and_limited_malformed_rules_fail_closed():
    malformed = {
        "坏站": {"limited": True, "windows": [{"start": "not-a-date"}]},
    }
    assert is_station_available("普通站", registry=malformed)
    assert not is_station_available("坏站", registry=malformed)
    assert "配置无效" in station_unavailable_reason("坏站", registry=malformed)
    assert available_stations(["普通站", "坏站"], registry=malformed) == ["普通站"]
    assert unavailable_stations(["普通站", "坏站"], registry=malformed) == ["坏站"]
    assert not is_station_available("武林源", registry={})
