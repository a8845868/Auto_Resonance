import requests
from unittest.mock import patch

from core.services.columba_optimizer import _fetch_prices


def test_price_timeout_falls_back_to_builtin_metadata():
    with patch(
        "core.services.columba_optimizer.requests.get",
        side_effect=requests.ConnectTimeout("offline"),
    ):
        prices, timestamp = _fetch_prices([{"name": "测试商品"}], ["测试城市"])

    assert prices == {}
    assert timestamp == 0
