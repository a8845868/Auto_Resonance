"""Daily fatigue-resource schedule, independent from trading execution."""

import json
import os
import uuid
from datetime import date, datetime, time, timedelta
from pathlib import Path

from core.services.runtime_control import RUNTIME_DIR


FATIGUE_RESOURCE_REFRESH_HOUR = 5
FATIGUE_RESOURCE_REFRESH_MINUTE = 0
FATIGUE_PLAN_TIMES = ((5, 0), (12, 0), (18, 0))
FATIGUE_USAGE_PATH = RUNTIME_DIR / "fatigue-usage.json"


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


def lunch_release_schedule(now: datetime | None = None) -> dict[str, str]:
    """Describe which of today's three lunch issues have reached release time."""
    current = now or datetime.now()
    cycle_date = date.fromisoformat(fatigue_cycle(current))
    return {
        f"{hour:02d}:{minute:02d}": (
            "released"
            if current >= datetime.combine(cycle_date, time(hour, minute))
            else "pending"
        )
        for hour, minute in FATIGUE_PLAN_TIMES
    }


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


def load_fatigue_usage(
    now: datetime | None = None,
    path: Path | None = None,
) -> dict[str, object]:
    """Load usage for the current 05:00 game day, resetting stale totals."""
    cycle = fatigue_cycle(now)
    usage_path = path or FATIGUE_USAGE_PATH
    try:
        document = json.loads(usage_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        document = {}
    if not isinstance(document, dict) or document.get("cycle") != cycle:
        document = {}
    # Release times are deterministic and should advance even before the next
    # cabinet visit; the inventory count remains the last observed UI value.
    schedule = lunch_release_schedule(now)
    return {
        "cycle": cycle,
        "bubble_water_uses": max(0, int(document.get("bubble_water_uses", 0))),
        "lunch_batches": max(0, int(document.get("lunch_batches", 0))),
        "lunch_fatigue_restored": max(
            0, int(document.get("lunch_fatigue_restored", 0))
        ),
        "lunches_remaining": document.get("lunches_remaining"),
        "lunch_schedule": schedule,
    }


def record_fatigue_usage(
    *,
    bubble_water_uses: int = 0,
    lunch_batches: int = 0,
    lunch_fatigue_restored: int = 0,
    lunches_remaining: int | None = None,
    lunch_schedule: dict[str, str] | None = None,
    now: datetime | None = None,
    path: Path | None = None,
) -> dict[str, object]:
    """Persist irreversible recovery usage for later planning and display."""
    usage_path = path or FATIGUE_USAGE_PATH
    usage = load_fatigue_usage(now, usage_path)
    for key, increment in (
        ("bubble_water_uses", bubble_water_uses),
        ("lunch_batches", lunch_batches),
        ("lunch_fatigue_restored", lunch_fatigue_restored),
    ):
        usage[key] = int(usage[key]) + max(0, int(increment))
    if lunches_remaining is not None:
        usage["lunches_remaining"] = max(0, int(lunches_remaining))
    if lunch_schedule is not None:
        usage["lunch_schedule"] = dict(lunch_schedule)
    usage_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = usage_path.with_name(
        f"{usage_path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    )
    temporary.write_text(
        json.dumps(usage, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    temporary.replace(usage_path)
    return usage


def fatigue_plan_lines(
    use_silver_branch: bool = False,
    usage: dict[str, object] | None = None,
) -> list[str]:
    silver = "允许银枝" if use_silver_branch else "只用免费/500 铁盟币，不用银枝"
    today = usage or load_fatigue_usage()
    schedule = today.get("lunch_schedule") or lunch_release_schedule()
    if not isinstance(schedule, dict):
        schedule = lunch_release_schedule()
    released = [slot for slot, status in schedule.items() if status == "released"]
    pending = [slot for slot, status in schedule.items() if status == "pending"]
    remaining = today.get("lunches_remaining")
    remaining_text = "未知" if remaining is None else str(remaining)
    return [
        f"今日已用：气泡水 {today['bubble_water_uses']}/6 次；"
        f"便当 {today['lunch_batches']} 批（恢复 {today['lunch_fatigue_restored']} 疲劳）",
        f"便当柜：已发放 {', '.join(released) or '无'}；"
        f"待发放 {', '.join(pending) or '无'}；当前剩余 {remaining_text}",
        "气泡水：所有城市合计每日 6 次，05:00 刷新；优先排在跑商之前",
        "站点：仅在设有休息区的核心城市喝；附属/活动站点暂缓，且不先吃便当",
        "便当：每日 05:00、12:00、18:00 发放；每次发放后检查",
        f"气泡水：每次恢复 50；仅在不会溢出时连续使用；{silver}",
        "便当：重新读取疲劳后，只有全部便当不会溢出才一次性全部使用",
    ]
