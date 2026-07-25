import json
from types import SimpleNamespace
from unittest.mock import call, patch

import auto.inventory as inventory
import numpy as np
from auto.inventory import _is_train_in_transit
from core.services.inventory_assets import classify_asset, merge_assets, parse_amount, parse_ocr_assets


def box(x, y, text):
    return {"text": text, "position": ((x, y), (x + 80, y), (x + 80, y + 25), (x, y + 25))}


def centered_box(x, y, text):
    return box(x - 40, y - 12.5, text)


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


def test_restock_book_detail_card_rejects_order_request_book():
    items = [
        box(530, 215, "\u8ba2\u5355\u8bf7\u6c42\u4e66"),
        box(950, 175, "\u62e5\u6709\uff1a17"),
        box(930, 218, "\u83b7\u53d6\u9014\u5f84"),
    ]

    assert inventory._restock_book_count_from_items(items) is None


def detail_items(name="进货采买书", count="拥有：17"):
    return [
        box(530, 215, name),
        box(950, 175, count),
        box(930, 218, "获取途径"),
        box(600, 672, "触碰空白区域退出"),
    ]


def test_strict_restock_book_detail_rejects_other_book_and_unowned_numbers():
    assert inventory._restock_book_detail_count(detail_items("订单请求书")) is None
    assert inventory._restock_book_detail_count([
        box(530, 215, "进货采买书"),
        box(930, 218, "获取途径"),
        box(600, 260, "售价：17"),
        box(600, 672, "触碰空白区域退出"),
    ]) is None


class OcrFrame:
    def __init__(self, items):
        self.items = items
        self.image = np.zeros((720, 1280, 3), dtype=np.uint8)

    def ocr(self):
        return self.items


class FrameProvider:
    def __init__(self, frames):
        self.frames = list(frames)
        self.last = self.frames[-1]
        self.calls = 0

    def __call__(self):
        self.calls += 1
        if self.frames:
            self.last = self.frames.pop(0)
        return self.last


class Clock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def _home_frame():
    return OcrFrame([box(190, 675, "资产"), box(1040, 130, "任务")])


def _inventory_frame():
    return OcrFrame([
        box(1100, 30, "道具"),
        box(1100, 100, "材料"),
        box(1100, 170, "装备"),
        box(1100, 240, "载货"),
    ])


def _candidate(x=1101, y=51, score=1.0):
    return inventory.AssetsEntryCandidate(
        point=(x, y), bbox=(x - 22, y - 22, x + 22, y + 22), score=score
    )


def _geometry():
    return SimpleNamespace(physical_width=1280, physical_height=720)


def test_assets_entry_dispatch_and_backpack_postcondition_share_evidence():
    provider = FrameProvider([_home_frame(), _inventory_frame()])
    clock = Clock()
    dispatches = []
    evidence = []

    result = inventory._open_assets_entry(
        frame_provider=provider,
        dispatcher=lambda point, **kwargs: dispatches.append((point, kwargs)) or True,
        candidate_resolver=lambda _image: [_candidate()],
        geometry_provider=_geometry,
        evidence_recorder=evidence.append,
        timeout=2,
        poll_interval=0.4,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )

    assert result is True
    assert len(dispatches) == 1
    assert dispatches[0][0] == (1101, 51)
    assert dispatches[0][1]["random_offset"] is False
    assert len(evidence) == 1
    document = evidence[0].to_dict()
    assert document["dispatch_acknowledged"] is True
    assert document["post_state"] == "INVENTORY"
    assert document["postcondition_result"] == "PASS"
    assert document["coordinate_chain_complete"] is True


def test_assets_entry_home_unchanged_fails_after_one_dispatch():
    provider = FrameProvider([_home_frame(), _home_frame()])
    clock = Clock()
    dispatches = []
    evidence = []

    result = inventory._open_assets_entry(
        frame_provider=provider,
        dispatcher=lambda point, **kwargs: dispatches.append(point) or True,
        candidate_resolver=lambda _image: [_candidate()],
        geometry_provider=_geometry,
        evidence_recorder=evidence.append,
        timeout=0.8,
        poll_interval=0.4,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )

    assert result is False
    assert dispatches == [(1101, 51)]
    assert evidence[0].to_dict()["reason_codes"] == ("home_unchanged",)


def test_assets_entry_unexpected_page_fails_without_retry():
    unexpected = OcrFrame([box(500, 300, "活动总览")])
    provider = FrameProvider([_home_frame(), unexpected])
    clock = Clock()
    dispatches = []
    evidence = []

    result = inventory._open_assets_entry(
        frame_provider=provider,
        dispatcher=lambda point, **kwargs: dispatches.append(point) or True,
        candidate_resolver=lambda _image: [_candidate()],
        geometry_provider=_geometry,
        evidence_recorder=evidence.append,
        timeout=2,
        poll_interval=0.4,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )

    assert result is False
    assert len(dispatches) == 1
    assert evidence[0].to_dict()["reason_codes"] == ("unexpected_page",)


def test_assets_entry_dispatch_rejection_stops_before_post_observation():
    provider = FrameProvider([_home_frame()])
    evidence = []

    result = inventory._open_assets_entry(
        frame_provider=provider,
        dispatcher=lambda *_args, **_kwargs: False,
        candidate_resolver=lambda _image: [_candidate()],
        geometry_provider=_geometry,
        evidence_recorder=evidence.append,
    )

    assert result is False
    assert provider.calls == 1
    assert evidence[0].dispatch_result == "dispatch_rejected"
    assert evidence[0].post_observations == []


def test_assets_entry_distinguishes_postcondition_detector_failure():
    class BrokenOcrFrame(OcrFrame):
        def ocr(self):
            raise RuntimeError("offline detector failure")

    provider = FrameProvider([_home_frame(), BrokenOcrFrame([])])
    clock = Clock()
    evidence = []

    result = inventory._open_assets_entry(
        frame_provider=provider,
        dispatcher=lambda *_args, **_kwargs: True,
        candidate_resolver=lambda _image: [_candidate()],
        geometry_provider=_geometry,
        evidence_recorder=evidence.append,
        timeout=0.4,
        poll_interval=0.4,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )

    assert result is False
    assert evidence[0].dispatch_acknowledged is True
    assert evidence[0].to_dict()["reason_codes"] == (
        "postcondition_detector_error",
    )


def test_assets_entry_multiple_candidates_blocks_all_dispatch():
    provider = FrameProvider([_home_frame()])
    dispatches = []
    evidence = []

    result = inventory._open_assets_entry(
        frame_provider=provider,
        dispatcher=lambda point, **kwargs: dispatches.append(point) or True,
        candidate_resolver=lambda _image: [_candidate(), _candidate(1220, 51, 0.91)],
        geometry_provider=_geometry,
        evidence_recorder=evidence.append,
    )

    assert result is False
    assert dispatches == []
    assert evidence[0].candidate_count == 2
    assert evidence[0].dispatch_requested is False
    assert evidence[0].to_dict()["reason_codes"] == ("candidate_ambiguous",)


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


def test_restock_book_high_confidence_icon_uses_detail_when_grid_count_is_missing():
    grid = [box(1100, 30, "道具"), box(1100, 100, "材料")]
    detail = detail_items()
    frames = [
        OcrFrame([]),  # pre-navigation transit check
        OcrFrame(grid),  # page frame
        OcrFrame(grid),
        OcrFrame(grid),  # grid-count confirmation remains unreadable
        OcrFrame(detail),
        OcrFrame(detail),
        OcrFrame(detail),  # detail count is confirmed in multiple frames
    ]
    with patch.object(inventory, "connect", return_value=True), patch.object(
        inventory, "go_home", return_value=True
    ), patch.object(inventory, "_open_assets_entry", return_value=True), patch.object(
        inventory, "screenshot", side_effect=frames
    ), patch.object(
        inventory, "_find_restock_book_icon", return_value=((598, 306), 0.956)
    ), patch.object(inventory, "input_tap") as tap, patch.object(
        inventory, "input_swipe"
    ) as swipe, patch.object(inventory.time, "sleep"):
        assert inventory.read_restock_book_count(max_pages=3) == 17

    tap.assert_called_once_with((598, 306))
    swipe.assert_not_called()


def test_restock_book_rejects_order_request_detail_and_continues_scanning():
    false_grid = [box(870, 710, "17")]
    wrong_detail = detail_items("订单请求书")
    book_page = [box(100, 100, "进货采买书"), box(105, 160, "×12")]
    frames = [
        OcrFrame([]),  # pre-navigation transit check
        OcrFrame(false_grid),
        OcrFrame(false_grid),
        OcrFrame(false_grid),  # false icon grid count confirms as 17
        OcrFrame(wrong_detail),
        OcrFrame(wrong_detail),
        OcrFrame(wrong_detail),  # exact detail identity rejects it
        OcrFrame(book_page),
        OcrFrame(book_page),
        OcrFrame(book_page),  # next page confirms the real book
    ]
    with patch.object(inventory, "connect", return_value=True), patch.object(
        inventory, "go_home", return_value=True
    ), patch.object(inventory, "_open_assets_entry", return_value=True), patch.object(
        inventory, "screenshot", side_effect=frames
    ), patch.object(
        inventory, "_find_restock_book_icon", return_value=((870, 676), 0.854)
    ), patch.object(inventory, "input_tap") as tap, patch.object(
        inventory, "input_swipe"
    ) as swipe, patch.object(inventory.time, "sleep"):
        assert inventory.read_restock_book_count(max_pages=3) == 12

    assert tap.call_args_list == [
        call((870, 676)),
        call((640, 600)),
    ]
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


def test_full_page_icon_index_resolves_only_clear_name_and_count_pairs():
    image = np.zeros((720, 1280, 3), dtype=np.uint8)
    items = [
        centered_box(500, 350, "8"),
        centered_box(700, 350, "17"),
        centered_box(1000, 350, "23"),
    ]
    matches = [
        {"name": "桦石", "location": (500, 305), "score": 0.96},
        {"name": "黑月采购券", "location": (505, 306), "score": 0.85},
        {"name": "进货采买书", "location": (700, 305), "score": 0.93},
        {"name": "再交涉请求书", "location": (704, 307), "score": 0.91},
        {"name": "广告投放券", "location": (850, 305), "score": 0.95},
    ]

    with patch.object(inventory, "_match_inventory_icons", return_value=matches):
        assets, probes = inventory._index_inventory_icon_page(image, items)

    assert [(asset.name, asset.count) for asset in assets] == [("桦石", 8)]
    assert {(probe["location"], probe["reason"]) for probe in probes} == {
        ((700, 305), "template_conflict"),
        ((850, 305), "missing_count"),
        ((1000, 310), "unknown_icon"),
    }


def test_generic_inventory_detail_reads_confirmed_unknown_name_and_owned_count():
    assert inventory._inventory_detail_asset(detail_items("新式补给箱", "拥有：27")) == inventory.Asset(
        "新式补给箱", 27, "补给与票券"
    )
    assert inventory._inventory_detail_asset([
        box(530, 215, "新式补给箱"),
        box(600, 260, "售价：27"),
        box(600, 672, "触碰空白区域退出"),
    ]) is None


def test_unknown_inventory_probe_learns_only_after_multiframe_detail_confirmation():
    page_image = np.zeros((720, 1280, 3), dtype=np.uint8)
    frames = [OcrFrame(detail_items("新式补给箱", "拥有：27")) for _ in range(3)]
    probe = {"location": (598, 306), "candidates": (), "reason": "unknown_icon"}

    with patch.object(inventory, "screenshot", side_effect=frames), patch.object(
        inventory, "input_tap"
    ) as tap, patch.object(inventory.time, "sleep"), patch.object(
        inventory, "_inventory_icon_manifest", return_value={}
    ), patch.object(inventory, "_learn_inventory_icon", return_value=True) as learn:
        asset = inventory._probe_inventory_item(page_image, probe)

    assert asset == inventory.Asset("新式补给箱", 27, "补给与票券")
    learn.assert_called_once_with("新式补给箱", page_image, (598, 306))
    assert tap.call_args_list == [call((598, 306)), call((640, 600))]


def test_learned_inventory_icon_is_saved_and_registered_atomically(tmp_path, monkeypatch):
    root = tmp_path / "resources"
    icon_root = root / "currency"
    icon_root.mkdir(parents=True)
    (icon_root / "manifest.json").write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(inventory, "RESOURCES_PATH", root)
    image = np.zeros((720, 1280, 3), dtype=np.uint8)
    image[258:354, 550:646] = (20, 120, 240)

    assert inventory._learn_inventory_icon("新式/补给箱", image, (598, 306))

    manifest = json.loads((icon_root / "manifest.json").read_text(encoding="utf-8"))
    filename = manifest["新式/补给箱"]
    learned = inventory._read_unicode_image(icon_root / filename, inventory.cv.IMREAD_UNCHANGED)
    assert learned.shape == (96, 96, 4)
    assert np.all(learned[:72, :, 3] == 255)
    assert np.all(learned[72:, :, 3] == 0)
    assert "/" not in filename
    assert not inventory._learn_inventory_icon("新式/补给箱", image, (598, 306))


def test_learned_rgb_inventory_icon_can_be_matched_again():
    canvas = np.zeros((720, 1280, 3), dtype=np.uint8)
    rng = np.random.default_rng(42)
    template = rng.integers(0, 256, (96, 96, 3), dtype=np.uint8)
    canvas[258:354, 550:646] = template

    location, score = inventory._match_inventory_template(canvas, template)

    assert score > 0.99
    assert abs(location[0] - 598) <= 2
    assert abs(location[1] - 306) <= 2


def test_full_manifest_match_finds_multiple_icons_on_the_same_page():
    names = {
        "进货采买书": "进货采买书.png",
        "再交涉请求书": "再交涉请求书.png",
    }
    canvas = np.full((720, 1280, 3), 35, dtype=np.uint8)
    placements = {"进货采买书": (550, 258), "再交涉请求书": (812, 393)}
    for name, filename in names.items():
        template = inventory._read_unicode_image(
            inventory.RESOURCES_PATH / "currency" / filename,
            inventory.cv.IMREAD_UNCHANGED,
        )
        x, y = placements[name]
        alpha = template[:, :, 3:4].astype(np.float32) / 255.0
        canvas[y : y + 96, x : x + 96] = (
            template[:, :, :3] * alpha
            + canvas[y : y + 96, x : x + 96] * (1.0 - alpha)
        ).astype(np.uint8)

    with patch.object(inventory, "_inventory_icon_manifest", return_value=names):
        matches = inventory._match_inventory_icons(canvas)

    by_name = {match["name"]: match for match in matches}
    assert set(by_name) == set(names)
    assert by_name["进货采买书"]["score"] > 0.9
    assert by_name["再交涉请求书"]["score"] > 0.9


def test_full_inventory_scan_merges_pages_and_probes_only_uncertain_cells():
    page_items = [box(1100, 30, "道具"), centered_box(500, 350, "8")]
    frames = [
        OcrFrame([]),  # transit guard
        OcrFrame([]),  # home currencies
        OcrFrame(page_items),
        OcrFrame(page_items),
    ]
    probe = {"location": (700, 305), "candidates": (), "reason": "unknown_icon"}
    indexed = [
        ([inventory.Asset("桦石", 8, "货币")], [probe]),
        ([inventory.Asset("进货采买书", 12, "补给与票券")], []),
    ]
    with patch.object(inventory, "connect", return_value=True), patch.object(
        inventory, "go_home", return_value=True
    ), patch.object(inventory, "_open_assets_entry", return_value=True), patch.object(
        inventory, "screenshot", side_effect=frames
    ), patch.object(
        inventory, "_parse_primary_currency_grid", return_value=[]
    ), patch.object(
        inventory, "_index_inventory_icon_page", side_effect=indexed
    ), patch.object(
        inventory, "_probe_inventory_item",
        return_value=inventory.Asset("新式补给箱", 27, "补给与票券"),
    ) as probe_item, patch.object(inventory, "input_swipe") as swipe, patch.object(
        inventory.time, "sleep"
    ):
        assets = inventory.scan_inventory_assets(max_pages=2)

    assert {(asset.name, asset.count) for asset in assets} >= {
        ("桦石", 8),
        ("进货采买书", 12),
        ("新式补给箱", 27),
    }
    probe_item.assert_called_once_with(frames[2].image, probe)
    swipe.assert_called_once_with((930, 640), (930, 285), swipe_time=600)
