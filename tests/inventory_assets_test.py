from unittest.mock import patch

import auto.inventory as inventory
import numpy as np
from auto.inventory import _is_train_in_transit
from core.services.inventory_assets import classify_asset, merge_assets, parse_amount, parse_ocr_assets


def box(x, y, text):
    return {"text": text, "position": ((x, y), (x + 80, y), (x + 80, y + 25), (x, y + 25))}


def test_train_in_transit_detection_blocks_inventory_navigation():
    assert _is_train_in_transit([{"text": "自动巡航中"}, {"text": "剩余行程：830km"}])
    assert _is_train_in_transit([{"text": "目的地：武林源"}, {"text": "车厢内"}])
    assert not _is_train_in_transit([{"text": "目的地"}, {"text": "资产"}])


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
    assert classify_asset("交子") == "货币"


def test_restock_book_count_is_spatially_paired():
    items = [
        box(100, 100, "进货采买书"),
        box(105, 160, "×12"),
        box(500, 160, "9999"),
    ]
    assert inventory._restock_book_count_from_items(items)[0] == 12


def test_assets_text_only_guards_station_home_and_is_not_the_click_target():
    items = [
        box(1040, 130, "资产"),
        box(190, 675, "资产"),
    ]

    assert inventory._find_assets_text_entry(items, 1280, 720) == (230, 687)


def test_assets_inventory_screen_requires_category_rail():
    assert inventory._is_assets_inventory_screen([
        box(1100, 30, "道具"),
        box(1100, 100, "材料"),
        box(1100, 170, "装备"),
        box(1100, 240, "载货"),
    ])
    assert not inventory._is_assets_inventory_screen([
        box(190, 675, "资产"),
        box(1040, 130, "任务"),
    ])


def test_restock_book_detail_card_reads_owned_count_far_from_name():
    items = [
        box(530, 215, "进货采买书"),
        box(950, 175, "拥有：8"),
    ]

    assert inventory._restock_book_count_from_items(items) == (8, "拥有：8")


class OcrFrame:
    def __init__(self, items):
        self.items = items
        self.image = np.zeros((720, 1280, 3), dtype=np.uint8)

    def ocr(self):
        return self.items


def test_restock_book_scan_scrolls_past_first_page_and_confirms_twice():
    first_page = [box(100, 100, "桦石"), box(105, 160, "1280")]
    book_page = [box(100, 100, "进货采买书"), box(105, 160, "×12")]
    frames = [
        OcrFrame([]),  # pre-navigation transit check
        OcrFrame(first_page),
        OcrFrame(book_page),
        OcrFrame(book_page),
        OcrFrame(book_page),
    ]
    with patch.object(inventory, "connect", return_value=True), patch.object(
        inventory, "go_home", return_value=True
    ), patch.object(inventory, "_open_assets_entry", return_value=True), patch.object(
        inventory, "screenshot", side_effect=frames
    ), patch.object(inventory, "input_swipe") as swipe, patch.object(
        inventory.time, "sleep"
    ):
        assert inventory.read_restock_book_count(max_pages=5) == 12

    swipe.assert_called_once_with((930, 640), (930, 285), swipe_time=600)


def test_restock_book_icon_count_is_paired_with_its_own_cell():
    items = [
        box(650, 330, "8"),
        box(790, 330, "70"),
        box(650, 500, "2711780"),
    ]

    assert inventory._restock_book_count_near_icon(items, (690, 285)) == (8, "8")


def test_restock_book_icon_template_finds_hidden_name_grid_item():
    template = inventory._read_unicode_image(
        inventory.RESOURCES_PATH / "currency" / "进货采买书.png",
        inventory.cv.IMREAD_UNCHANGED,
    )
    canvas = np.full((720, 1280, 3), 35, dtype=np.uint8)
    x, y = 680, 250
    alpha = template[:, :, 3:4].astype(np.float32) / 255.0
    canvas[y : y + 96, x : x + 96] = (
        template[:, :, :3] * alpha
        + canvas[y : y + 96, x : x + 96] * (1.0 - alpha)
    ).astype(np.uint8)

    location, score = inventory._find_restock_book_icon(canvas)

    assert score > 0.9
    assert abs(location[0] - (x + 48)) <= 5
    assert abs(location[1] - (y + 48)) <= 5
