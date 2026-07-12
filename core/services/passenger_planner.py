from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from pathlib import Path


ROUTE_DATA_PATH = Path(__file__).resolve().parents[2] / "resources" / "passenger" / "routes.json"


def _load_route_references() -> tuple[dict, dict]:
    try:
        payload = json.loads(ROUTE_DATA_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}, {}
    routes = {
        (row["origin"], row["destination"]): {
            "revenue": int(row["revenue"]),
            "fatigue": int(row["fatigue"]),
            "kind": row.get("kind", "资料路线"),
        }
        for row in payload.get("routes", [])
    }
    return routes, {"version": payload.get("version", ""), "source": payload.get("source", "")}


PASSENGER_ROUTE_REFERENCES, PASSENGER_ROUTE_META = _load_route_references()


def route_reference(origin: str, destination: str) -> dict | None:
    value = PASSENGER_ROUTE_REFERENCES.get((origin, destination))
    return dict(value) if value else None


@dataclass(frozen=True)
class PassengerPlanConfig:
    """Estimate the marginal income from passenger seats that stay on the train.

    The live passenger system has hundreds of passenger/tag combinations, so the
    planner deliberately scales from a player-observed full-train settlement.
    This keeps cleanliness, ratings, furniture and character bonuses inside one
    editable number instead of pretending they can be inferred from cargo data.
    """

    seats: int = 64
    trips_per_week: int = 7
    reference_capacity: int = 512
    # 2026-05-25 passenger workbook: Wulin -> Lanxin 6,003,222 and
    # Lanxin -> Wulin 5,785,811 for 512 seats. Use the alternating-direction
    # mean so a weekly plan does not assume every trip starts at Wulin.
    reference_trip_revenue: int = 5_894_517
    occupancy_percent: int = 100
    fatigue_per_trip: int = 95


def estimate_passenger_plan(config: PassengerPlanConfig = PassengerPlanConfig()) -> dict:
    seats = max(0, int(config.seats))
    trips = max(0, int(config.trips_per_week))
    capacity = max(1, int(config.reference_capacity))
    occupancy = min(100, max(0, int(config.occupancy_percent)))
    reference_revenue = max(0, int(config.reference_trip_revenue))

    revenue_per_seat = reference_revenue / capacity
    passengers_per_trip = seats * occupancy / 100
    trip_revenue = round(revenue_per_seat * passengers_per_trip)
    weekly_revenue = trip_revenue * trips
    weekly_fatigue = max(0, int(config.fatigue_per_trip)) * trips
    return {
        "seats": seats,
        "trips_per_week": trips,
        "occupancy_percent": occupancy,
        "passengers_per_trip": round(passengers_per_trip, 2),
        "reference_capacity": capacity,
        "reference_trip_revenue": reference_revenue,
        "revenue_per_seat": round(revenue_per_seat, 2),
        "trip_revenue": trip_revenue,
        "weekly_revenue": weekly_revenue,
        "fatigue_per_trip": max(0, int(config.fatigue_per_trip)),
        "weekly_fatigue": weekly_fatigue,
        "config": asdict(config),
        "assumption": "客位不占货舱；按每天最多一次满载长途客运，并单独扣除该路线疲劳。",
    }
