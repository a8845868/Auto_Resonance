"""Station-facility knowledge used to avoid impossible UI operations."""

from typing import Optional


# Public station data describes rest areas as a core-city facility. Keep the
# confirmed list explicit: special-event and attached stations must not be
# treated as core cities just because they have a trading post.
KNOWN_REST_AREA_CITIES = frozenset(
    {
        "7号自由港",
        "修格里城",
        "澄明数据中心",
        "阿妮塔发射中心",
        "曼德矿场",
        "海角城",
        "贡露城",
        "岚心城",
    }
)

KNOWN_NO_REST_AREA_STATIONS = frozenset(
    {
        "铁盟哨站",
        "荒原站",
        "阿妮塔能源研究所",
        "阿妮塔战备工厂",
        "淘金乐园",
        "汇流塔",
        "远星大桥",
        "维蒂林场",
        "栖羽站",
        "云岫桥基地",
        "黑月游乐城",
        "塔图站",
        "武林源",
    }
)

_ALIASES = {"七号自由港": "7号自由港"}
_runtime_availability: dict[str, bool] = {}


def normalize_station_name(station_name: str | None) -> str:
    name = str(station_name or "").strip()
    return _ALIASES.get(name, name)


def rest_area_availability(station_name: str | None) -> Optional[bool]:
    """Return known availability, or ``None`` for a new/unknown station."""
    name = normalize_station_name(station_name)
    if not name:
        return None
    if name in KNOWN_REST_AREA_CITIES:
        return True
    if name in KNOWN_NO_REST_AREA_STATIONS:
        return False
    return _runtime_availability.get(name)


def remember_rest_area_availability(
    station_name: str | None, available: bool
) -> None:
    """Cache an OCR-confirmed result for an unknown station in this process."""
    name = normalize_station_name(station_name)
    if (
        not name
        or name in KNOWN_REST_AREA_CITIES
        or name in KNOWN_NO_REST_AREA_STATIONS
    ):
        return
    _runtime_availability[name] = bool(available)


def clear_runtime_facility_cache() -> None:
    """Test/helper hook; static station knowledge is intentionally retained."""
    _runtime_availability.clear()
