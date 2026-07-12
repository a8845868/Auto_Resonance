import numpy as np

from auto.inventory import _home_iron_currency, _parse_primary_currency_grid


def box(x, y, text):
    return {"text": text, "position": ((x, y), (x + 70, y), (x + 70, y + 20), (x, y + 20))}


def test_home_assets_balance_maps_to_iron_currency():
    assets = _home_iron_currency([box(100, 100, "资产"), box(180, 100, "32379183")])
    assert [(item.name, item.count) for item in assets] == [("铁盟币", 32379183)]


def test_primary_asset_grid_maps_icon_only_currency_counts():
    image = np.zeros((720, 1280, 3), dtype=np.uint8)
    items = [
        box(600, 199, "1280"), box(840, 199, "32379183"),
        box(995, 199, "1125"), box(460, 335, "64710"),
    ]
    assets = _parse_primary_currency_grid(image, items)
    assert {item.name: item.count for item in assets} == {
        "桦石": 1280, "铁盟币": 32379183, "绝命奖章": 1125, "赴命奖章": 64710,
    }
