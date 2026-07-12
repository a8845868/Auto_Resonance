from core.services.inventory_assets import classify_asset, merge_assets, parse_amount, parse_ocr_assets


def box(x, y, text):
    return {"text": text, "position": ((x, y), (x + 80, y), (x + 80, y + 25), (x, y + 25))}


def test_parse_amount_supports_game_abbreviations():
    assert parse_amount("1.25万") == 12500
    assert parse_amount("x 2,345") == 2345


def test_parse_ocr_assets_pairs_grid_name_and_count():
    assets = parse_ocr_assets([box(100, 100, "进货采买书"), box(105, 160, "×12")])
    assert [(asset.name, asset.count, asset.category) for asset in assets] == [("进货采买书", 12, "补给与票券")]


def test_inline_currency_and_merge_keep_largest_snapshot():
    first = parse_ocr_assets([box(20, 20, "里程点 × 80")])
    second = parse_ocr_assets([box(20, 20, "里程点 × 120")])
    assert merge_assets(first, second)[0].count == 120
    assert classify_asset("里程点") == "货币"
