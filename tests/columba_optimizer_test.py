import requests
from unittest.mock import patch

from core.services.columba_optimizer import (
    OptimizationConfig,
    _fetch_prices,
    optimize_live_routes,
)


def test_price_timeout_falls_back_to_builtin_metadata():
    with patch(
        "core.services.columba_optimizer.requests.get",
        side_effect=requests.ConnectTimeout("offline"),
    ):
        prices, timestamp = _fetch_prices([{"name": "测试商品"}], ["测试城市"])

    assert prices == {}
    assert timestamp == 0


def test_optimizer_excludes_stations_outside_their_open_window():
    products = [
        {
            "name": "测试商品",
            "buyLot": {"A": 10, "B": 10, "武林源": 10},
            "buyPrices": {"A": 1, "B": 1, "武林源": 1},
            "sellPrices": {"A": 10, "B": 10, "武林源": 100},
        }
    ]
    fatigue = {"A-B": 10, "B-A": 10, "A-武林源": 1, "武林源-A": 1}
    with patch(
        "core.services.columba_optimizer._load_metadata",
        return_value=(products, ["A", "B", "武林源"], ["A", "B", "武林源"], fatigue, {}),
    ), patch(
        "core.services.columba_optimizer._fetch_prices", return_value=({}, 0)
    ), patch(
        "core.services.columba_optimizer.available_stations", return_value=["A", "B"]
    ) as available:
        result = optimize_live_routes(
            OptimizationConfig(
                cargo=10,
                books=0,
                weekly_fatigue=1000,
                passenger_trips_per_week=0,
            )
        )

    available.assert_called_once()
    assert set(result["cycle"]) == {"A", "B"}
    assert all("武林源" not in (leg["from"], leg["to"]) for leg in result["legs"])
