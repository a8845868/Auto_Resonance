"""Daily fatigue-resource schedule, independent from trading execution."""

from datetime import datetime, timedelta


FATIGUE_RESOURCE_REFRESH_HOUR = 5
FATIGUE_RESOURCE_REFRESH_MINUTE = 0
FATIGUE_PLAN_TIMES = ((5, 0), (12, 0), (18, 0))


def fatigue_cycle(now: datetime | None = None) -> str:
    """Return the game-day key; drinks and lunches refresh at 05:00."""
    current = now or datetime.now()
    boundary = current.replace(
        hour=FATIGUE_RESOURCE_REFRESH_HOUR,
        minute=FATIGUE_RESOURCE_REFRESH_MINUTE,
        second=0,
        microsecond=0,
    )
    if current < boundary:
        current -= timedelta(days=1)
    return current.date().isoformat()


def next_fatigue_refresh(now: datetime | None = None) -> datetime:
    """Return the next fatigue-planning slot after ``now``.

    Bubble-water quota belongs to the 05:00 game day.  Work lunches are issued
    at 05:00, 12:00, and 18:00, so every issue needs its own safe batch check.
    """
    current = now or datetime.now()
    for hour, minute in FATIGUE_PLAN_TIMES:
        target = current.replace(
            hour=hour,
            minute=minute,
            second=0,
            microsecond=0,
        )
        if target > current:
            return target
    return (current + timedelta(days=1)).replace(
        hour=FATIGUE_PLAN_TIMES[0][0],
        minute=FATIGUE_PLAN_TIMES[0][1],
        second=0,
        microsecond=0,
    )


def fatigue_plan_lines(use_silver_branch: bool = False) -> list[str]:
    silver = "允许银枝" if use_silver_branch else "只用免费/500 铁盟币，不用银枝"
    return [
        "气泡水：所有城市合计每日 6 次，05:00 刷新；优先排在跑商之前",
        "站点：仅在设有休息区的核心城市喝；附属/活动站点暂缓，且不先吃便当",
        "便当：每日 05:00、12:00、18:00 发放；每次发放后检查",
        f"气泡水：每次恢复 50；仅在不会溢出时连续使用；{silver}",
        "便当：重新读取疲劳后，只有全部便当不会溢出才一次性全部使用",
    ]
