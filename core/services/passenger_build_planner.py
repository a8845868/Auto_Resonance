from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path


MAX_TOTAL_CARRIAGES = 11
FIXED_FUNCTION_CARRIAGES = 2
FIXED_PASSENGER_CARRIAGES = 1
FIXED_FREIGHT_CARRIAGES = 1
CONFIGURABLE_CARRIAGES = 7
SEATS_PER_PASSENGER_CARRIAGE = 64
SEAT_GROUPS_PER_PASSENGER_CARRIAGE = 16
EXTRA_PASSENGER_CARRIAGE_COST = 3_000_000
SEAT_GROUP_COST = 400_000
FULL_BUILD_REFERENCE_COST = 185_272_000
BUILD_DURATION = timedelta(hours=6)
BUILD_PLAN_PATH = Path("config/passenger_build_plan.json")


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.astimezone()
    return value.isoformat(timespec="seconds")


def create_build_monitor_plan(
    *,
    target_carriages: int,
    current_carriages: int,
    path: Path = BUILD_PLAN_PATH,
    automation_safe: bool | None = None,
    safety_source: str = "",
    safety_reason: str = "",
) -> dict:
    """Create a durable sequential passenger-carriage construction plan."""
    target = min(8, max(1, int(target_carriages)))
    current = min(target, max(1, int(current_carriages)))
    state = {
        "version": 1,
        "target_carriages": target,
        "completed_carriages": current,
        "status": "completed" if current >= target else "pending",
        "active_started_at": None,
        "active_due_at": None,
        "history": [],
        "premium_currency_required": False,
        "currency_type": "IRON",
        # A local plan is not safety evidence. A production observer must fill
        # these fields after verifying the in-game build action and currency.
        "automation_safe": automation_safe is True,
        "automation_safety_source": safety_source or "UNKNOWN",
        "automation_safety_reason": safety_reason or "not_game_observed",
        "config_revision": f"target:{target}:current:{current}",
        "evidence_config_revision": "",
        "evidence_target_carriages": None,
        "evidence_completed_carriages": None,
        "evidence_status": "",
        "observed_at": _iso(datetime.now().astimezone()),
    }
    save_build_monitor_plan(state, path)
    return state


def load_build_monitor_plan(path: Path = BUILD_PLAN_PATH) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None


def save_build_monitor_plan(state: dict, path: Path = BUILD_PLAN_PATH) -> dict:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)
    return state


def record_carriage_started(
    state: dict,
    *,
    started_at: datetime | None = None,
    remaining_seconds: int | None = None,
    path: Path = BUILD_PLAN_PATH,
) -> dict:
    """Record the real successful click time, which is the next due-time source."""
    if state.get("active_started_at"):
        raise ValueError("已有一节客厢正在建造")
    if int(state["completed_carriages"]) >= int(state["target_carriages"]):
        state["status"] = "completed"
        return save_build_monitor_plan(state, path)
    started = started_at or datetime.now().astimezone()
    duration = (
        timedelta(seconds=max(0, int(remaining_seconds)))
        if remaining_seconds is not None
        else BUILD_DURATION
    )
    due = started + duration
    sequence = int(state["completed_carriages"]) + 1
    state["active_started_at"] = _iso(started)
    state["active_due_at"] = _iso(due)
    state["status"] = "building"
    state.setdefault("history", []).append({
        "carriage_number": sequence,
        "started_at": _iso(started),
        "due_at": _iso(due),
        "completed_at": None,
    })
    return save_build_monitor_plan(state, path)


def resync_active_build(
    state: dict,
    *,
    remaining_seconds: int,
    observed_at: datetime | None = None,
    path: Path = BUILD_PLAN_PATH,
) -> dict:
    """Trust the in-game countdown and correct a stale local due time."""
    if not state.get("active_started_at"):
        raise ValueError("当前没有正在建造的客厢")
    observed = observed_at or datetime.now().astimezone()
    state["active_due_at"] = _iso(
        observed + timedelta(seconds=max(0, int(remaining_seconds)))
    )
    state["history"][-1]["due_at"] = state["active_due_at"]
    return save_build_monitor_plan(state, path)


def record_carriage_completed(
    state: dict, *, completed_at: datetime | None = None, path: Path = BUILD_PLAN_PATH
) -> dict:
    if not state.get("active_started_at"):
        raise ValueError("当前没有正在建造的客厢")
    completed = completed_at or datetime.now().astimezone()
    due = datetime.fromisoformat(state["active_due_at"])
    if completed < due:
        raise ValueError("客厢尚未到达完成时间")
    state["completed_carriages"] = min(
        int(state["target_carriages"]), int(state["completed_carriages"]) + 1
    )
    state["history"][-1]["completed_at"] = _iso(completed)
    state["active_started_at"] = None
    state["active_due_at"] = None
    state["status"] = (
        "completed"
        if int(state["completed_carriages"]) >= int(state["target_carriages"])
        else "pending"
    )
    return save_build_monitor_plan(state, path)


def build_monitor_summary(state: dict | None, now: datetime | None = None) -> dict:
    if not state:
        return {"active": False, "message": "尚未创建连续建造计划"}
    remaining = max(0, int(state["target_carriages"]) - int(state["completed_carriages"]))
    due = datetime.fromisoformat(state["active_due_at"]) if state.get("active_due_at") else None
    current = now or datetime.now().astimezone()
    return {
        "active": state.get("status") != "completed",
        "remaining": remaining,
        "due": due,
        "due_now": bool(due and current >= due),
        "message": (
            f"已完成目标：{state['completed_carriages']}/{state['target_carriages']} 节客厢"
            if remaining == 0
            else (f"第 {int(state['completed_carriages']) + 1} 节建造中，预计 {due:%m-%d %H:%M:%S} 完成" if due else f"待立即开工，还需 {remaining} 节")
        ),
    }


def calculate_passenger_build_plan(
    *,
    target_passenger_carriages: int,
    built_extra_passenger_carriages: int,
    installed_seat_groups: int,
    current_iron: int,
    comfort: int = 0,
    food: int = 0,
    entertainment: int = 0,
    pets: int = 0,
    aquarium: int = 0,
    plants: int = 0,
    medical: int = 0,
) -> dict:
    passenger = min(8, max(1, int(target_passenger_carriages)))
    freight = 9 - passenger
    required_extra = passenger - FIXED_PASSENGER_CARRIAGES
    built_extra = min(CONFIGURABLE_CARRIAGES, max(0, int(built_extra_passenger_carriages)))
    missing_extra = max(0, required_extra - built_extra)
    required_seat_groups = passenger * SEAT_GROUPS_PER_PASSENGER_CARRIAGE
    installed_groups = max(0, int(installed_seat_groups))
    missing_seat_groups = max(0, required_seat_groups - installed_groups)
    direct_cost = missing_extra * EXTRA_PASSENGER_CARRIAGE_COST + missing_seat_groups * SEAT_GROUP_COST
    full_build_gap = max(0, FULL_BUILD_REFERENCE_COST - max(0, int(current_iron))) if passenger == 8 else direct_cost
    ratings = {
        "舒适": (max(0, int(comfort)), 42_000),
        "美味": (max(0, int(food)), 7_000),
        "娱乐": (max(0, int(entertainment)), 7_000),
        "宠物": (max(0, int(pets)), 7_000),
        "水族": (max(0, int(aquarium)), 7_000),
        "绿植": (max(0, int(plants)), 7_000),
        "医疗": (max(0, int(medical)), 7_000),
    }
    missing_ratings = [name for name, (value, target) in ratings.items() if value < target]
    return {
        "passenger_carriages": passenger,
        "freight_carriages": freight,
        "function_carriages": FIXED_FUNCTION_CARRIAGES,
        "seats": passenger * SEATS_PER_PASSENGER_CARRIAGE,
        "required_extra_passenger_carriages": required_extra,
        "missing_extra_passenger_carriages": missing_extra,
        "required_seat_groups": required_seat_groups,
        "missing_seat_groups": missing_seat_groups,
        "direct_build_cost": direct_cost,
        "full_build_reference_cost": FULL_BUILD_REFERENCE_COST,
        "iron_shortfall": full_build_gap,
        "ratings": ratings,
        "missing_ratings": missing_ratings,
        "build_ready": missing_extra == 0 and missing_seat_groups == 0,
        "rating_ready": not missing_ratings,
    }
