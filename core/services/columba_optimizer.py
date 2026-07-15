from __future__ import annotations

import itertools
import math
from dataclasses import asdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import requests
from loguru import logger

from core.utils.utils import RESOURCES_PATH, read_json
from core.services.passenger_planner import PassengerPlanConfig, estimate_passenger_plan
from core.services.station_availability import available_stations


PRICE_API = "https://www.resonance-columba.com/api/get-prices"
GOODS_PATH = RESOURCES_PATH / "goods"


@dataclass(frozen=True)
class OptimizationConfig:
    cargo: int = 1121
    books: int = 10
    weekly_fatigue: int = 5292
    trade_level: int = 20
    prestige: dict[str, int] = field(default_factory=dict)
    max_bargain_tries: int = 5
    max_raise_tries: int = 5
    max_cycle_length: int = 2
    # Aggregated role/event modifiers, expressed in Columba's percentage-point units.
    bargain_count_bonus: int = 0
    raise_count_bonus: int = 0
    bargain_rate_bonus: float = 0.0
    raise_rate_bonus: float = 0.0
    bargain_success_bonus: float = 0.0
    raise_success_bonus: float = 0.0
    first_try_success_bonus: float = 0.0
    after_failed_success_bonus: float = 0.0
    failed_fatigue_reduction: float = 0.0
    tax_cut_percent: float = 0.0
    extra_buy_percent: float = 0.0
    drive_fatigue_reduction: int = 0
    passenger_seats: int = 64
    passenger_trips_per_week: int = 7
    passenger_reference_capacity: int = 512
    passenger_reference_trip_revenue: int = 5_894_517
    passenger_occupancy_percent: int = 100
    passenger_fatigue_per_trip: int = 95


def _normalize_city(name: str) -> str:
    return "7号自由港" if name == "七号自由港" else name


def _load_metadata():
    products = read_json(GOODS_PATH / "ColumbaProducts.json")
    upstream_cities = read_json(GOODS_PATH / "ColumbaCities.json")
    cities = [_normalize_city(city) for city in upstream_cities]
    fatigue = read_json(GOODS_PATH / "CityTiredData.json")
    belongs_to = read_json(GOODS_PATH / "AttachedToCityData.json")
    belongs_to = {_normalize_city(k): _normalize_city(v) for k, v in belongs_to.items()}
    return products, upstream_cities, cities, fatigue, belongs_to


def _fetch_prices(products: list[dict], upstream_cities: list[str]) -> tuple[dict, int]:
    try:
        response = requests.get(PRICE_API, timeout=15)
        response.raise_for_status()
        compressed = response.json()["data"]
    except (requests.RequestException, KeyError, TypeError, ValueError) as exc:
        # Product metadata already contains buy/sell base prices.  A temporary
        # Columba outage must not erase a verified restock-book inventory and
        # silently rebuild the weekly plan with zero books.
        logger.warning(
            "科伦巴实时价格不可用，改用内置基础价格离线规划；"
            f"进货书分配仍会保留（{type(exc).__name__}: {exc}）"
        )
        return {}, 0
    decoded: dict[str, dict[str, dict[str, dict[str, Any]]]] = {}
    latest = 0
    for product_id, product_data in compressed.items():
        product_index = int(product_id) - 1
        if not 0 <= product_index < len(products):
            continue
        product_name = products[product_index]["name"]
        decoded[product_name] = {}
        for short_type, type_data in product_data.items():
            price_type = "buy" if short_type == "b" else "sell"
            decoded[product_name][price_type] = {}
            for city_id, price_data in type_data.items():
                city_index = int(city_id) - 1
                if not 0 <= city_index < len(upstream_cities):
                    continue
                city = _normalize_city(upstream_cities[city_index])
                decoded[product_name][price_type][city] = price_data
                latest = max(latest, int(price_data.get("ti", 0)))
    return decoded, latest


def _prestige_stats(level: int, city: str) -> tuple[float, float]:
    """Return Columba buy/sell tax rate and extra-buy multiplier."""
    level = min(20, max(1, int(level)))
    tax = 0.10 - 0.005 * (level // 2)
    if city in {"7号自由港", "阿妮塔能源研究所"}:
        tax -= 0.03
    return tax, level * 0.1


def _js_round(value: float) -> int:
    """Match JavaScript Math.round for the positive values used by Columba."""
    return math.floor(value + 0.5)


def _max_tries(level: int, bonus: int, bargain: bool) -> int:
    thresholds = (2, 5, 9) if bargain else (3, 6, 10)
    return 2 + sum(level >= threshold for threshold in thresholds) + bonus


def _simulate_negotiation(
    *, level: int, trade_level: int, requested_tries: int, bargain: bool, config: OptimizationConfig
) -> tuple[float, float]:
    """Port of Columba's expected negotiation rate/fatigue calculation."""
    if requested_tries <= 0:
        return 0.0, 0.0
    count_bonus = config.bargain_count_bonus if bargain else config.raise_count_bonus
    rate_bonus = config.bargain_rate_bonus if bargain else config.raise_rate_bonus
    success_bonus = config.bargain_success_bonus if bargain else config.raise_success_bonus
    per_success = (3.0 if bargain else 2.0) + trade_level * 0.1 + rate_bonus
    tries = min(10, requested_tries, _max_tries(level, count_bonus, bargain))
    successes_to_cap = max(1, math.ceil(20.0 / per_success))
    expected_rate = 0.0
    expected_fatigue = 0.0

    def walk(index: int, successes: int, failed_before: bool, probability: float, fatigue: float):
        nonlocal expected_rate, expected_fatigue
        if index == tries or successes == successes_to_cap:
            expected_rate += min(20.0, successes * per_success) * probability / 100.0
            expected_fatigue += fatigue * probability
            return
        success_rate = 67.0 + level * 0.5 + success_bonus - successes * 10.0
        if successes == 0:
            success_rate += config.first_try_success_bonus
        if failed_before:
            success_rate += config.after_failed_success_bonus
        success_rate = min(100.0, max(0.0, success_rate)) / 100.0
        walk(index + 1, successes + 1, failed_before, probability * success_rate, fatigue + 8.0)
        failed_fatigue = max(0.0, 8.0 - config.failed_fatigue_reduction)
        walk(index + 1, successes, True, probability * (1.0 - success_rate), fatigue + failed_fatigue)

    walk(0, 0, False, 1.0, 0.0)
    return expected_rate, expected_fatigue


def _distribute_books(gains: list[list[int]], total_books: int) -> tuple[int, list[int]]:
    """Allocate books among repeated visits using dynamic programming."""
    minus_inf = -10**30
    dp = [minus_inf] * (total_books + 1)
    paths = [[] for _ in range(total_books + 1)]
    dp[0] = 0
    for visit_gains in gains:
        nxt = [minus_inf] * (total_books + 1)
        nxt_paths = [[] for _ in range(total_books + 1)]
        for used in range(total_books + 1):
            if dp[used] == minus_inf:
                continue
            for add in range(total_books - used + 1):
                value = dp[used] + visit_gains[add]
                if value > nxt[used + add]:
                    nxt[used + add] = value
                    nxt_paths[used + add] = paths[used] + [add]
        dp, paths = nxt, nxt_paths
    best_books = max(range(total_books + 1), key=lambda value: dp[value])
    return dp[best_books], paths[best_books]


def optimize_live_routes(
    config: OptimizationConfig = OptimizationConfig(),
    *,
    at: datetime | None = None,
) -> dict:
    products, upstream_cities, cities, fatigue, belongs_to = _load_metadata()
    cities = available_stations(cities, at)
    prices, latest_timestamp = _fetch_prices(products, upstream_cities)
    passenger_fatigue = max(0, config.passenger_trips_per_week) * max(0, config.passenger_fatigue_per_trip)
    freight_fatigue_budget = max(0, config.weekly_fatigue - passenger_fatigue)

    def master(city: str) -> str:
        return belongs_to.get(city, city)

    def prestige_level(city: str) -> int:
        return min(20, max(1, int(config.prestige.get(master(city), 20))))

    def price_of(product: dict, price_type: str, city: str) -> float | None:
        live = prices.get(product["name"], {}).get(price_type, {}).get(city, {})
        if live.get("p") is not None:
            return float(live["p"])
        base_key = "buyPrices" if price_type == "buy" else "sellPrices"
        value = product.get(base_key, {}).get(city)
        if value is None and city == "7号自由港":
            value = product.get(base_key, {}).get("七号自由港")
        return float(value) if value not in (None, 99999) else None

    negotiation_cache: dict[tuple[str, bool], tuple[float, float]] = {}

    def negotiation(city: str, bargain: bool) -> tuple[float, float]:
        key = (city, bargain)
        if key not in negotiation_cache:
            negotiation_cache[key] = _simulate_negotiation(
                level=prestige_level(city),
                trade_level=config.trade_level,
                requested_tries=config.max_bargain_tries if bargain else config.max_raise_tries,
                bargain=bargain,
                config=config,
            )
        return negotiation_cache[key]

    leg_cache: dict[tuple[str, str, int], dict] = {}

    def calculate_leg(from_city: str, to_city: str, books: int) -> dict:
        key = (from_city, to_city, books)
        if key in leg_cache:
            return leg_cache[key]
        bargain_rate, bargain_fatigue = negotiation(from_city, True)
        raise_rate, raise_fatigue = negotiation(to_city, False)
        buy_tax, extra_buy = _prestige_stats(prestige_level(from_city), master(from_city))
        sell_tax, _ = _prestige_stats(prestige_level(to_city), master(to_city))
        tax_cut = config.tax_cut_percent / 100.0
        buy_tax = max(0.0, buy_tax - tax_cut)
        sell_tax = max(0.0, sell_tax - tax_cut)
        choices = []
        for product in products:
            buy_lots = product.get("buyLot", {})
            base_lot = buy_lots.get(from_city)
            if base_lot is None and from_city == "7号自由港":
                base_lot = buy_lots.get("七号自由港")
            if not base_lot:
                continue
            buy = price_of(product, "buy", from_city)
            sell = price_of(product, "sell", to_city)
            if buy is None or sell is None:
                continue
            adjusted_buy = buy * (1.0 - bargain_rate)
            adjusted_sell = sell * (1.0 + raise_rate)
            unit_profit = adjusted_sell - adjusted_buy
            unit_profit -= unit_profit * sell_tax
            unit_profit -= buy * buy_tax
            if unit_profit <= 0:
                continue
            available_once = _js_round(
                int(base_lot) * (1.0 + extra_buy + config.extra_buy_percent / 100.0)
            )
            choices.append(
                {
                    "name": product["name"],
                    "unit_profit": unit_profit,
                    "available": available_once * (books + 1),
                }
            )
        choices.sort(key=lambda item: item["unit_profit"], reverse=True)
        remaining = config.cargo
        profit = 0.0
        buys = []
        for choice in choices:
            if remaining <= 0:
                break
            count = min(remaining, choice["available"])
            if count <= 0:
                continue
            profit += count * choice["unit_profit"]
            remaining -= count
            buys.append({"name": choice["name"], "count": count, "unit_profit": round(choice["unit_profit"])})
        route_fatigue = max(
            0, int(fatigue.get(f"{from_city}-{to_city}", 0)) - config.drive_fatigue_reduction
        )
        result = {
            "from": from_city,
            "to": to_city,
            "books": books,
            "profit": _js_round(profit),
            "cargo": config.cargo - remaining,
            "buys": buys,
            "travel_fatigue": route_fatigue,
            "bargain_expected_percent": round(bargain_rate * 100, 2),
            "raise_expected_percent": round(raise_rate * 100, 2),
            "bargain_fatigue": round(bargain_fatigue, 2),
            "raise_fatigue": round(raise_fatigue, 2),
            "fatigue": route_fatigue + bargain_fatigue + raise_fatigue,
        }
        leg_cache[key] = result
        return result

    best: dict | None = None
    max_length = min(max(2, config.max_cycle_length), 3)
    for length in range(2, max_length + 1):
        for cycle in itertools.permutations(cities, length):
            if cycle[0] != min(cycle):
                continue
            base_legs = [calculate_leg(cycle[i], cycle[(i + 1) % length], 0) for i in range(length)]
            cycle_fatigue = sum(leg["fatigue"] for leg in base_legs)
            if cycle_fatigue <= 0:
                continue
            repeats = int(freight_fatigue_budget // cycle_fatigue)
            if repeats <= 0:
                continue
            base_profit = repeats * sum(leg["profit"] for leg in base_legs)
            visit_gains: list[list[int]] = []
            visit_meta: list[tuple[str, str]] = []
            for _ in range(repeats):
                for index, base_leg in enumerate(base_legs):
                    from_city = cycle[index]
                    to_city = cycle[(index + 1) % length]
                    visit_gains.append(
                        [calculate_leg(from_city, to_city, b)["profit"] - base_leg["profit"] for b in range(config.books + 1)]
                    )
                    visit_meta.append((from_city, to_city))
            book_gain, visit_books = _distribute_books(visit_gains, config.books)
            plan = []
            for (from_city, to_city), books in zip(visit_meta, visit_books):
                if books:
                    plan.append({"from": from_city, "to": to_city, "books": books})
            runs = []
            for repeat in range(repeats):
                offset = repeat * length
                runs.append({cycle[i]: visit_books[offset + i] for i in range(length)})
            execution_batches = []
            for run_books in runs:
                if execution_batches and execution_batches[-1]["books"] == run_books:
                    execution_batches[-1]["runs"] += 1
                else:
                    execution_batches.append({"runs": 1, "books": run_books})
            total_profit = base_profit + book_gain
            result = {
                "objective": "weekly_profit",
                "cycle": list(cycle),
                "repeats": repeats,
                "allocation": [sum(item["books"] for item in plan if item["from"] == city) for city in cycle],
                "book_plan": plan,
                "execution_batches": execution_batches,
                "books_used": sum(visit_books),
                "profit": round(total_profit),
                "fatigue": round(repeats * cycle_fatigue, 2),
                "cycle_fatigue": round(cycle_fatigue, 2),
                "profit_per_fatigue": round(total_profit / (repeats * cycle_fatigue)),
                "legs": base_legs,
            }
            if best is None or result["profit"] > best["profit"]:
                best = result

    if best is None:
        raise RuntimeError("没有找到可用的科伦巴实时周计划")
    best["price_timestamp"] = latest_timestamp
    best["price_time"] = (
        datetime.fromtimestamp(latest_timestamp).strftime("%Y-%m-%d %H:%M:%S")
        if latest_timestamp
        else "离线基础价格"
    )
    best["price_source"] = "live" if latest_timestamp else "builtin"
    best["api"] = PRICE_API if latest_timestamp else ""
    best["assumptions"] = {
        "cargo": config.cargo,
        "weekly_fatigue": config.weekly_fatigue,
        "books": config.books,
        "trade_level": config.trade_level,
        "max_bargain_tries": config.max_bargain_tries,
        "max_raise_tries": config.max_raise_tries,
    }
    best["optimizer_config"] = asdict(config)
    passenger_plan = estimate_passenger_plan(
        PassengerPlanConfig(
            seats=config.passenger_seats,
            trips_per_week=config.passenger_trips_per_week,
            reference_capacity=config.passenger_reference_capacity,
            reference_trip_revenue=config.passenger_reference_trip_revenue,
            occupancy_percent=config.passenger_occupancy_percent,
            fatigue_per_trip=config.passenger_fatigue_per_trip,
        )
    )
    best["cargo_profit"] = best["profit"]
    best["passenger_plan"] = passenger_plan
    best["passenger_profit"] = passenger_plan["weekly_revenue"]
    best["combined_profit"] = best["cargo_profit"] + best["passenger_profit"]
    best["cargo_fatigue"] = best["fatigue"]
    best["passenger_fatigue"] = passenger_plan["weekly_fatigue"]
    best["fatigue"] = best["cargo_fatigue"] + best["passenger_fatigue"]
    return best
