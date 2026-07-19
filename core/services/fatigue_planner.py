"""Daily fatigue-resource schedule, independent from trading execution."""

import json
import os
import uuid
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from enum import Enum
from pathlib import Path

from core.services.runtime_control import RUNTIME_DIR
from core.services.server_calendar import SERVER_CLOCK


FATIGUE_PLAN_TIMES = (
    (SERVER_CLOCK.daily_reset.hour, SERVER_CLOCK.daily_reset.minute),
    (12, 0),
    (18, 0),
)
FATIGUE_USAGE_PATH = RUNTIME_DIR / "fatigue-usage.json"


def _legacy_server_time(value: datetime) -> tuple[datetime, bool]:
    was_naive = value.tzinfo is None or value.utcoffset() is None
    if was_naive:
        local_zone = datetime.now().astimezone().tzinfo
        return value.replace(tzinfo=local_zone).astimezone(SERVER_CLOCK.timezone), True
    return value.astimezone(SERVER_CLOCK.timezone), False


class FatiguePlanStatus(str, Enum):
    ACTION_NOW = "ACTION_NOW"
    DEFER_UNTIL_FATIGUE = "DEFER_UNTIL_FATIGUE"
    DEFER_UNTIL_WAYPOINT = "DEFER_UNTIL_WAYPOINT"
    DEFER_UNTIL_RELEASE = "DEFER_UNTIL_RELEASE"
    COMPLETE_FOR_DAY = "COMPLETE_FOR_DAY"
    BLOCKED = "BLOCKED"
    UNKNOWN = "UNKNOWN"
    CANCELLED = "CANCELLED"


@dataclass(frozen=True)
class SodaPriceTier:
    use_index: int
    currency_type: str
    cost: int
    allowed: bool


@dataclass(frozen=True)
class FatigueSnapshot:
    server_day_id: str
    observed_at: datetime
    fatigue_used: int
    fatigue_cap: int
    current_city_id: str
    current_station_id: str
    current_amenities: frozenset[str]
    soda_uses_used: int
    soda_uses_remaining: int | None
    soda_reduction_per_use: int
    soda_price_tiers: tuple[SodaPriceTier | str, ...]
    bento_batches_available: int
    bento_total_reduction_available: int
    next_bento_release_at: datetime | None
    natural_recovery_at: datetime | None
    source_confidence: str
    fatigue_confidence: str = ""
    station_confidence: str = ""
    amenity_confidence: str = ""
    soda_tier_confidence: str = ""
    soda_remaining_confidence: str = ""
    bento_inventory_confidence: str = ""
    bento_value_confidence: str = ""
    current_soda_facility_available: bool | None = None
    soda_daily_limit: int = 6
    soda_uses_confirmed_used: int = 0
    soda_uses_confirmed_remaining: int | None = None
    current_price_tiers: tuple[SodaPriceTier | str, ...] = ()
    current_tier_observable: bool | None = None

    def confidence(self, field: str) -> str:
        return str(getattr(self, field) or self.source_confidence).upper()


@dataclass(frozen=True)
class RouteLeg:
    origin: str
    destination: str
    fatigue_increase: int
    destination_amenities: frozenset[str]
    destination_amenity_confidence: str = "HIGH"


@dataclass(frozen=True)
class TradeRouteContext:
    route_id: str
    legs: tuple[RouteLeg, ...]
    current_leg_index: int = 0


@dataclass(frozen=True)
class FatigueAction:
    kind: str
    count: int = 1
    waypoint_id: str | None = None
    reduction: int = 0
    reobserve_after_each: bool = False


@dataclass(frozen=True)
class FatiguePlan:
    snapshot: FatigueSnapshot
    immediate_actions: tuple[FatigueAction, ...]
    deferred_actions: tuple[FatigueAction, ...]
    expected_fatigue_by_waypoint: dict[str, int]
    expected_resource_usage: dict[str, int]
    expected_waste: int
    next_trigger: dict[str, object]
    status: FatiguePlanStatus
    reason: str


def _allowed_soda_uses(
    snapshot: FatigueSnapshot,
    allow_premium_soda: bool,
    max_iron_soda_cost: int,
) -> int:
    """Return the contiguous, explicitly allowed prefix of observed price tiers."""

    confirmed_remaining = (
        snapshot.soda_uses_confirmed_remaining
        if snapshot.soda_uses_confirmed_remaining is not None
        else snapshot.soda_uses_remaining
    )
    if confirmed_remaining is None:
        return 0
    tiers = (
        snapshot.current_price_tiers
        if snapshot.current_tier_observable is not None
        else snapshot.soda_price_tiers
    )
    allowed = 0
    for raw in tiers[: max(0, confirmed_remaining)]:
        if isinstance(raw, SodaPriceTier):
            currency = raw.currency_type.upper()
            tier_allowed = bool(raw.allowed)
            if currency == "IRON" and int(raw.cost) > max(0, int(max_iron_soda_cost)):
                tier_allowed = False
        else:
            currency = str(raw).upper()
            tier_allowed = currency in {"FREE", "IRON"}
        if currency in {"PREMIUM", "SILVER", "银枝"}:
            tier_allowed = tier_allowed and allow_premium_soda
        elif currency not in {"FREE", "IRON", "免费", "铁盟币"}:
            tier_allowed = False
        if not tier_allowed:
            break
        allowed += 1
    return allowed


def _immediate_sequences(
    snapshot: FatigueSnapshot,
    allow_premium_soda: bool,
    max_iron_soda_cost: int,
) -> list[tuple[tuple[FatigueAction, ...], int, dict[str, int]]]:
    sequences = []
    for order in (("bento", "soda"), ("soda", "bento")):
        fatigue = max(0, min(snapshot.fatigue_cap, snapshot.fatigue_used))
        actions: list[FatigueAction] = []
        soda_used = 0
        bento_used = 0
        for resource in order:
            if resource == "bento":
                reduction = max(0, snapshot.bento_total_reduction_available)
                if (
                    snapshot.bento_batches_available > 0
                    and reduction
                    and fatigue + reduction <= snapshot.fatigue_cap
                ):
                    actions.append(FatigueAction("USE_ALL_BENTOS", reduction=reduction))
                    fatigue += reduction
                    bento_used = snapshot.bento_batches_available
            elif (
                "REST_AREA" in snapshot.current_amenities
                and snapshot.soda_reduction_per_use > 0
                and _allowed_soda_uses(snapshot, allow_premium_soda, max_iron_soda_cost) > 0
            ):
                safe = min(
                    max(0, snapshot.soda_uses_remaining or 0),
                    _allowed_soda_uses(snapshot, allow_premium_soda, max_iron_soda_cost),
                    (snapshot.fatigue_cap - fatigue)
                    // snapshot.soda_reduction_per_use,
                )
                if safe:
                    reduction = safe * snapshot.soda_reduction_per_use
                    actions.append(
                        FatigueAction(
                            "DRINK_SODA",
                            count=safe,
                            reduction=reduction,
                            reobserve_after_each=True,
                        )
                    )
                    fatigue += reduction
                    soda_used = safe
        sequences.append(
            (
                tuple(actions),
                fatigue - snapshot.fatigue_used,
                {"soda_uses": soda_used, "bento_batches": bento_used},
            )
        )
    return sequences


def plan_fatigue_recovery(
    snapshot: FatigueSnapshot,
    route: TradeRouteContext | None = None,
    *,
    allow_premium_soda: bool = False,
    max_iron_soda_cost: int = 500,
) -> FatiguePlan:
    if snapshot.observed_at.tzinfo is None or snapshot.observed_at.utcoffset() is None:
        raise ValueError("fatigue observations must be timezone-aware")
    confidence_blockers = []
    for field in ("fatigue_confidence", "station_confidence", "amenity_confidence"):
        if snapshot.confidence(field) != "HIGH":
            confidence_blockers.append(field)
    current_soda_facility_available = (
        snapshot.current_soda_facility_available
        if snapshot.current_soda_facility_available is not None
        else "REST_AREA" in snapshot.current_amenities
    )
    soda_may_be_available = (
        current_soda_facility_available is not False
        or snapshot.confidence("amenity_confidence") != "HIGH"
    )
    if soda_may_be_available:
        for field in ("soda_tier_confidence", "soda_remaining_confidence"):
            if snapshot.confidence(field) != "HIGH":
                confidence_blockers.append(field)
    if snapshot.confidence("bento_inventory_confidence") != "HIGH":
        confidence_blockers.append("bento_inventory_confidence")
    if (
        snapshot.bento_batches_available > 0
        and snapshot.confidence("bento_value_confidence") != "HIGH"
    ):
        confidence_blockers.append("bento_value_confidence")
    if confidence_blockers:
        return FatiguePlan(
            snapshot, (), (), {}, {}, 0, {"reobserve": True}, FatiguePlanStatus.UNKNOWN,
            "fatigue_snapshot_unknown:" + ",".join(confidence_blockers),
        )
    actions, reduced, usage = max(
        _immediate_sequences(snapshot, allow_premium_soda, max_iron_soda_cost),
        key=lambda item: (
            item[1],
            bool(item[0] and item[0][0].kind == "DRINK_SODA"),
            len(item[0]),
        ),
    )
    if actions:
        return FatiguePlan(
            snapshot,
            actions,
            (),
            {},
            usage,
            0,
            {"reobserve_after_each_action": True},
            FatiguePlanStatus.ACTION_NOW,
            "safe_zero_waste_actions_available",
        )

    thresholds = []
    if (
        (snapshot.soda_uses_remaining or 0) > 0
        and snapshot.soda_reduction_per_use > 0
        and _allowed_soda_uses(snapshot, allow_premium_soda, max_iron_soda_cost) > 0
    ):
        thresholds.append(snapshot.soda_reduction_per_use)
    if snapshot.bento_batches_available > 0 and snapshot.bento_total_reduction_available > 0:
        thresholds.append(snapshot.bento_total_reduction_available)
    threshold = min(thresholds) if thresholds else None
    expected: dict[str, int] = {}
    if route is not None:
        fatigue = snapshot.fatigue_used
        soda_remaining_unknown = snapshot.soda_uses_confirmed_remaining is None
        soda_could_remain_today = (
            soda_remaining_unknown
            and snapshot.soda_uses_confirmed_used < snapshot.soda_daily_limit
        ) or (snapshot.soda_uses_confirmed_remaining or 0) > 0
        route_has_recovery_waypoint = any(
            leg.destination_amenity_confidence.upper() == "HIGH"
            and "REST_AREA" in leg.destination_amenities
            for leg in route.legs[route.current_leg_index :]
        )
        for leg in route.legs[route.current_leg_index :]:
            fatigue = min(snapshot.fatigue_cap, fatigue + max(0, leg.fatigue_increase))
            expected[leg.destination] = fatigue
            if leg.destination_amenity_confidence.upper() != "HIGH":
                return FatiguePlan(
                    snapshot,
                    (),
                    (),
                    expected,
                    {},
                    0,
                    {"reobserve": True, "waypoint_id": leg.destination},
                    FatiguePlanStatus.UNKNOWN,
                    "destination_amenity_unknown",
                )
            if (
                "REST_AREA" in leg.destination_amenities
                and soda_could_remain_today
                and snapshot.soda_reduction_per_use > 0
                and fatigue + snapshot.soda_reduction_per_use
                <= snapshot.fatigue_cap
                and (
                    current_soda_facility_available is False
                    or soda_remaining_unknown
                    or snapshot.current_tier_observable is False
                )
            ):
                return FatiguePlan(
                    snapshot,
                    (),
                    (
                        FatigueAction(
                            "REOBSERVE_RECOVERY_AT_WAYPOINT",
                            waypoint_id=leg.destination,
                        ),
                    ),
                    expected,
                    {},
                    0,
                    {"waypoint_id": leg.destination, "reobserve": True},
                    FatiguePlanStatus.DEFER_UNTIL_WAYPOINT,
                    "reobserve_recovery_resources_at_future_rest_area",
                )
            soda_ready = (
                "REST_AREA" in leg.destination_amenities
                and (snapshot.soda_uses_remaining or 0) > 0
                and fatigue + snapshot.soda_reduction_per_use
                <= snapshot.fatigue_cap
                and _allowed_soda_uses(snapshot, allow_premium_soda, max_iron_soda_cost) > 0
            )
            bento_ready = (
                snapshot.bento_batches_available > 0
                and snapshot.bento_total_reduction_available > 0
                and fatigue + snapshot.bento_total_reduction_available
                <= snapshot.fatigue_cap
            )
            if soda_ready or bento_ready:
                kind = "DRINK_SODA" if soda_ready else "USE_ALL_BENTOS"
                count = (
                    min(
                        snapshot.soda_uses_remaining or 0,
                        _allowed_soda_uses(snapshot, allow_premium_soda, max_iron_soda_cost),
                        (snapshot.fatigue_cap - fatigue)
                        // snapshot.soda_reduction_per_use,
                    )
                    if soda_ready
                    else 1
                )
                return FatiguePlan(
                    snapshot,
                    (),
                    (FatigueAction(kind, count=count, waypoint_id=leg.destination, reobserve_after_each=soda_ready),),
                    expected,
                    {},
                    0,
                    {
                        "waypoint_id": leg.destination,
                        "fatigue_at_most": snapshot.fatigue_cap - (threshold or 0),
                    },
                    FatiguePlanStatus.DEFER_UNTIL_WAYPOINT,
                    "route_reaches_zero_waste_recovery_threshold",
                )
        if soda_could_remain_today and not route_has_recovery_waypoint:
            return FatiguePlan(
                snapshot,
                (),
                (),
                expected,
                {},
                0,
                {"blocker": "route_has_no_recovery_waypoint"},
                FatiguePlanStatus.BLOCKED,
                "route_has_no_recovery_waypoint",
            )
    if threshold is not None:
        reason = "wait_for_zero_waste_threshold"
        if route is not None and not any(
            "REST_AREA" in leg.destination_amenities for leg in route.legs
        ):
            reason += ":no_recovery_waypoint"
        return FatiguePlan(
            snapshot,
            (),
            (),
            expected,
            {},
            0,
            {"fatigue_at_most": snapshot.fatigue_cap - threshold},
            FatiguePlanStatus.DEFER_UNTIL_FATIGUE,
            reason,
        )
    if snapshot.next_bento_release_at is not None:
        return FatiguePlan(
            snapshot, (), (), expected, {}, 0,
            {"at": snapshot.next_bento_release_at.isoformat()},
            FatiguePlanStatus.DEFER_UNTIL_RELEASE,
            "waiting_for_next_bento_release",
        )
    return FatiguePlan(
        snapshot, (), (), expected, {}, 0, {}, FatiguePlanStatus.COMPLETE_FOR_DAY,
        "no_daily_recovery_resources_remaining",
    )


def fatigue_cycle(now: datetime | None = None) -> str:
    """Return the game-day key; drinks and lunches refresh at 05:00."""
    current = now or SERVER_CLOCK.server_now()
    current, _ = _legacy_server_time(current)
    return SERVER_CLOCK.server_day_id(current)


def lunch_release_schedule(now: datetime | None = None) -> dict[str, str]:
    """Describe which of today's three lunch issues have reached release time."""
    current = now or SERVER_CLOCK.server_now()
    aware, _was_naive = _legacy_server_time(current)
    cycle_date = date.fromisoformat(fatigue_cycle(aware))
    return {
        f"{hour:02d}:{minute:02d}": (
            "released"
            if aware
            >= datetime.combine(cycle_date, time(hour, minute), SERVER_CLOCK.timezone)
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
    aware, was_naive = _legacy_server_time(current)
    for hour, minute in FATIGUE_PLAN_TIMES:
        target = aware.replace(
            hour=hour,
            minute=minute,
            second=0,
            microsecond=0,
        )
        if target > aware:
            return target.replace(tzinfo=None) if was_naive else target
    target = (aware + timedelta(days=1)).replace(
        hour=FATIGUE_PLAN_TIMES[0][0],
        minute=FATIGUE_PLAN_TIMES[0][1],
        second=0,
        microsecond=0,
    )
    return target.replace(tzinfo=None) if was_naive else target


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
