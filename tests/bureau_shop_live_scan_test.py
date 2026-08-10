from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from app.view import shop_planner_interface as shop_interface
from auto import shop_purchase
from core.services.read_only_policy import DEFAULT_POLICY_SPECS
from core.services.shop_catalog import (
    load_read_only_shop_catalog,
    load_shop_catalog,
)


ROOT = Path(__file__).resolve().parents[1]
EVIDENCE_ROOT = ROOT / "artifacts" / "shop_discovery" / "2026-07-13"


class _Frame:
    def __init__(self, ocr_items: list[dict], level: int):
        self._ocr_items = ocr_items
        self.image = np.full((720, 1280, 3), level, dtype=np.uint8)

    def ocr(self):
        return self._ocr_items


class _DialogFrame(_Frame):
    def __init__(self, ocr_items: list[dict]):
        super().__init__(ocr_items, 0)
        self.image[365:395, 435:475] = 255
        self.image[365:395, 807:847] = 255


class _Recorder:
    enabled = False

    def capture(self, *_args, **_kwargs):
        return []


def _historical_frames() -> list[list[dict]]:
    return [
        json.loads(
            (EVIDENCE_ROOT / f"bureau-full-{index:02d}.ocr.json").read_text(
                encoding="utf-8"
            )
        )
        for index in range(8)
    ]


def _ocr(text: str, x: int, y: int, width: int = 70, height: int = 22) -> dict:
    return {
        "text": text,
        "position": [[x, y], [x + width, y], [x + width, y + height], [x, y + height]],
    }


def _dialog_ocr(
    name: str,
    quantity: int,
    maximum: int,
    totals: list[int],
) -> list[dict]:
    data = [
        _ocr(name, 560, 285, 180),
        _ocr(f"{quantity}/{maximum}", 610, 345, 70),
        _ocr("最少", 350, 350, 55),
        _ocr("最多", 855, 350, 55),
        # The bureau classifier requires "确认消耗" AND "兑换" in one text block.
        _ocr(f"确认消耗以上素材兑换{name}", 480, 435, 260, 22),
        _ocr("取消", 300, 520, 55),
        _ocr("确定", 930, 520, 55),
    ]
    for index, total in enumerate(totals):
        data.append(_ocr(str(total), 620 + index * 150, 440, 90))
    return data


def test_bureau_live_matcher_replays_all_22_evidence_bound_items():
    catalog = load_shop_catalog()
    shop = catalog.shop("bureau_exchange")
    observed = load_read_only_shop_catalog(
        shop.read_only_catalog, catalog.currencies
    )
    frames = _historical_frames()

    matched = {
        item.id
        for item in observed.items
        if any(
            shop_purchase.locate_read_only_bureau_item(frame, item) is not None
            for frame in frames
        )
    }

    assert matched == {item.id for item in observed.items}


def test_bureau_scan_finds_all_items_with_swipes_and_zero_item_taps(monkeypatch):
    catalog = load_shop_catalog()
    shop = catalog.shop("bureau_exchange")
    observed = load_read_only_shop_catalog(
        shop.read_only_catalog, catalog.currencies
    )
    ocr_frames = _historical_frames()
    levels = [0, 20, 40, 60, 80, 100, 100, 100]
    frames = iter(
        _Frame(ocr_items, level)
        for ocr_items, level in zip(ocr_frames, levels, strict=True)
    )
    swipes = []
    taps = []
    monkeypatch.setattr(shop_purchase, "screenshot", lambda: next(frames))
    monkeypatch.setattr(shop_purchase, "input_swipe", lambda *args, **kwargs: swipes.append(kwargs["intent"]))
    monkeypatch.setattr(shop_purchase, "input_tap", lambda *args, **kwargs: taps.append((args, kwargs)))
    monkeypatch.setattr(shop_purchase.time, "sleep", lambda _seconds: None)
    adapter = shop_purchase.BureauReadOnlyCatalogAdapter(
        shop, observed, _Recorder()
    )

    result = adapter.scan(max_pages=10)

    assert result["success"] is True
    assert result["reached_bottom"] is True
    assert len(result["results"]) == 22
    assert result["missing"] == []
    assert result["business_actions"] == 0
    assert result["exchange_actions"] == 0
    assert taps == []
    assert len(swipes) == 7
    assert all(intent.action_key == "shop_catalog_scroll" for intent in swipes)


def test_bureau_dialog_classifier_tolerates_lower_confirm_button():
    """The bureau "确定" button is ~10px lower than the HQ dialog.

    The evidence frame ``005-bureau-price-01-bureau_self_observation`` has
    centre y ≈ 590.5 — 0.5 px beyond the HQ classifier's 590 cap.  The
    bureau-specific classifier accepts it when the exchange confirm text
    and item alias are also present.
    """
    from auto.shop_purchase import (
        _has_bureau_quantity_dialog,
        _has_complete_bureau_quantity_dialog,
        _has_complete_quantity_dialog,
    )

    catalog = load_shop_catalog()
    shop = catalog.shop("bureau_exchange")
    observed = load_read_only_shop_catalog(
        shop.read_only_catalog, catalog.currencies
    )
    item = next(item for item in observed.items if item.id == "bureau_self_observation")

    # Real evidence OCR from 005-bureau-price-01-bureau_self_observation.
    # "确定" centre y = 590.5 — 0.5 px beyond the HQ cap of 590.
    evidence = [
        _ocr("自观测胶卷", 516, 289, 48),
        _ocr("1/2", 618, 353, 45, 24),
        _ocr("最少", 360, 370, 52),          # cx≈386 cy≈381
        _ocr("最多", 867, 370, 50),           # cx≈892 cy≈381
        _ocr("确认消耗以上素材兑换自观测胶卷（1次）", 486, 447, 297, 21),
        _ocr("取消", 320, 547, 57, 20),        # cx≈348.5 cy≈557
        _ocr("确定", 932, 579, 101, 23),       # cx≈982.5 cy≈590.5 > 590
        _ocr("抵", 620, 692, 35),
    ]
    step_image = np.zeros((720, 1280, 3), dtype=np.uint8)
    step_image[365:395, 435:475] = 255
    step_image[365:395, 807:847] = 255

    # HQ classifier rejects: 590.5 > 590.
    assert _has_complete_quantity_dialog(step_image, evidence) is False

    # Bureau classifier accepts: 590.5 ≤ 600, plus preamble + item alias.
    assert _has_bureau_quantity_dialog(evidence) is True
    assert _has_complete_bureau_quantity_dialog(step_image, evidence, item) is True

    # Without item alias in dialog, rejects.
    wrong_item = next(
        i for i in observed.items if i.id == "bureau_nebula_4"
    )
    assert _has_complete_bureau_quantity_dialog(
        step_image, evidence, wrong_item
    ) is False


def test_bureau_dialog_classifier_still_rejects_malformed_bureau_dialog():
    """No false positives: missing quantity or missing bureau preamble → reject."""
    from auto.shop_purchase import (
        _has_complete_bureau_quantity_dialog,
    )

    catalog = load_shop_catalog()
    shop = catalog.shop("bureau_exchange")
    observed = load_read_only_shop_catalog(
        shop.read_only_catalog, catalog.currencies
    )
    item = next(item for item in observed.items if item.id == "bureau_self_observation")
    evidence = [
        _ocr("自观测胶卷", 516, 289, 48),
        _ocr("最少", 360, 370, 52),
        _ocr("最多", 867, 370, 50),
        _ocr("确认消耗以上素材兑换自观测胶卷（1次）", 486, 447, 297, 21),
        _ocr("取消", 320, 547, 57, 20),
        _ocr("确定", 932, 579, 101, 23),       # cx≈982.5 cy≈590.5
    ]
    step_image = np.zeros((720, 1280, 3), dtype=np.uint8)
    step_image[365:395, 435:475] = 255
    step_image[365:395, 807:847] = 255

    # Missing quantity "1/2" → reject.
    assert _has_complete_bureau_quantity_dialog(step_image, evidence, item) is False


def test_hq_layout_without_bureau_preamble_is_rejected_by_bureau_classifier():
    """HQ visual layout + no exchange-confirmation text → reject.

    Even though ``_has_quantity_dialog`` accepts the HQ button layout, the
    bureau classifier must still require the exchange preamble and item alias.
    """

    from auto.shop_purchase import _has_complete_bureau_quantity_dialog

    catalog = load_shop_catalog()
    shop = catalog.shop("bureau_exchange")
    observed = load_read_only_shop_catalog(
        shop.read_only_catalog, catalog.currencies
    )
    item = catalog.item("cactus_energy_weekly_iron")
    bureau_item = next(
        i for i in observed.items if i.id == "bureau_self_observation"
    )
    # HQ purchase dialog: standard button layout with "售价" — no "确认消耗"
    # or "兑换" text anywhere on the page.
    dialog_ocr = [
        _ocr(item.name, 560, 290, 200),
        _ocr("1/8", 605, 355, 65),
        _ocr("最少", 360, 360, 50),
        _ocr("最多", 870, 360, 50),
        _ocr("售价", 525, 440, 55),
        _ocr("10000", 650, 440, 70),
        _ocr("取消", 310, 525, 55),
        _ocr("确定", 930, 525, 55),
    ]
    dialog_image = np.zeros((720, 1280, 3), dtype=np.uint8)
    dialog_image[372:382, 445:465] = 255
    dialog_image[372:382, 817:837] = 255

    # Rejected: HQ layout matches but no bureau preamble.
    assert _has_complete_bureau_quantity_dialog(
        dialog_image, dialog_ocr, bureau_item
    ) is False


def test_bureau_preamble_without_item_name_in_same_line_is_rejected():
    """The item alias and exchange-confirmation text must be in the same line.

    A generic "确认消耗以上素材兑换" line without the item alias, plus a
    separate OCR line containing the alias by coincidence, must not pass.
    """

    from auto.shop_purchase import _has_complete_bureau_quantity_dialog

    catalog = load_shop_catalog()
    shop = catalog.shop("bureau_exchange")
    observed = load_read_only_shop_catalog(
        shop.read_only_catalog, catalog.currencies
    )
    item = next(
        i for i in observed.items if i.id == "bureau_self_observation"
    )
    evidence = [
        _ocr("自观测胶卷", 516, 289, 48),
        _ocr("1/2", 618, 353, 45, 24),
        _ocr("最少", 360, 370, 52),
        _ocr("最多", 867, 370, 50),
        _ocr("确认消耗以上素材兑换", 486, 447, 180, 21),
        _ocr("取消", 320, 547, 57, 20),
        _ocr("确定", 932, 579, 101, 23),
    ]
    step_image = np.zeros((720, 1280, 3), dtype=np.uint8)
    step_image[365:395, 435:475] = 255
    step_image[365:395, 807:847] = 255

    # Rejected: the confirmation line does not name the item.
    assert _has_complete_bureau_quantity_dialog(
        step_image, evidence, item
    ) is False


def test_standard_dialog_classifier_unchanged_for_headquarters_shop():
    """The bureau change must not alter HQ shop dialog detection."""

    from auto.shop_purchase import _has_complete_quantity_dialog

    catalog = load_shop_catalog()
    item = catalog.item("cactus_energy_weekly_iron")
    dialog_ocr = [
        _ocr(item.name, 560, 290, 200),
        _ocr("最少", 360, 360, 50),
        _ocr("最多", 870, 360, 50),
        _ocr("售价", 525, 440, 55),
        _ocr("10000", 650, 440, 70),
        _ocr("取消", 310, 525, 55),
        _ocr("确定", 930, 525, 55),
        _ocr("1/8", 605, 355, 65),
    ]
    dialog_image = np.zeros((720, 1280, 3), dtype=np.uint8)
    dialog_image[372:382, 445:465] = 255
    dialog_image[372:382, 817:837] = 255

    assert _has_complete_quantity_dialog(dialog_image, dialog_ocr) is True


def test_bureau_quantity_probe_reads_all_marginals_and_cancels_once(monkeypatch):
    catalog = load_shop_catalog()
    shop = catalog.shop("bureau_exchange")
    observed = load_read_only_shop_catalog(
        shop.read_only_catalog, catalog.currencies
    )
    original = next(item for item in observed.items if item.id == "bureau_nebula_4")
    item = replace(original, observed_limit="当日剩余3次")
    frames = iter((
        _DialogFrame(_dialog_ocr(item.name, 1, 3, [100])),
        _DialogFrame(_dialog_ocr(item.name, 2, 3, [250])),
        _DialogFrame(_dialog_ocr(item.name, 3, 3, [450])),
        _Frame(_historical_frames()[1], 20),
    ))
    taps = []
    monkeypatch.setattr(shop_purchase, "screenshot", lambda: next(frames))
    monkeypatch.setattr(
        shop_purchase,
        "input_tap",
        lambda point, **kwargs: taps.append((point, kwargs)) or True,
    )
    monkeypatch.setattr(shop_purchase.time, "sleep", lambda _seconds: None)
    adapter = shop_purchase.BureauReadOnlyCatalogAdapter(
        shop, observed, _Recorder()
    )

    result = adapter._probe_price_schedule(
        {
            "observed_limit": "当日剩余3次",
            "exchange_point": (1175, 210),
        },
        item,
    )

    assert result["status"] == "price_schedule_validated"
    assert result["dialog_maximum"] == 3
    assert result["price_observations"] == [
        {"quantity": 1, "costs": [{
            "currency": "fu_ming", "marginal_cost": 100,
            "cumulative_cost": 100,
        }]},
        {"quantity": 2, "costs": [{
            "currency": "fu_ming", "marginal_cost": 150,
            "cumulative_cost": 250,
        }]},
        {"quantity": 3, "costs": [{
            "currency": "fu_ming", "marginal_cost": 200,
            "cumulative_cost": 450,
        }]},
    ]
    assert [kwargs["intent"].action_key for _point, kwargs in taps] == [
        "shop_bureau_quantity_open",
        "shop_quantity_increment",
        "shop_quantity_increment",
        "shop_quantity_cancel",
    ]
    assert all(kwargs["random_offset"] is False for _point, kwargs in taps)
    assert shop_purchase.DIALOG_CONFIRM_POS not in [point for point, _ in taps]
    assert adapter.dialog_open_dispatches == 1
    assert adapter.quantity_increment_dispatches == 2
    assert adapter.dialog_cancel_dispatches == 1


def test_bureau_quantity_probe_rejects_decreasing_marginal_then_cancels(monkeypatch):
    catalog = load_shop_catalog()
    shop = catalog.shop("bureau_exchange")
    observed = load_read_only_shop_catalog(
        shop.read_only_catalog, catalog.currencies
    )
    original = next(item for item in observed.items if item.id == "bureau_nebula_4")
    item = replace(original, observed_limit="当日剩余3次")
    frames = iter((
        _DialogFrame(_dialog_ocr(item.name, 1, 3, [100])),
        _DialogFrame(_dialog_ocr(item.name, 2, 3, [250])),
        _DialogFrame(_dialog_ocr(item.name, 3, 3, [380])),
        _Frame(_historical_frames()[1], 20),
    ))
    taps = []
    monkeypatch.setattr(shop_purchase, "screenshot", lambda: next(frames))
    monkeypatch.setattr(
        shop_purchase,
        "input_tap",
        lambda point, **kwargs: taps.append((point, kwargs)) or True,
    )
    monkeypatch.setattr(shop_purchase.time, "sleep", lambda _seconds: None)
    adapter = shop_purchase.BureauReadOnlyCatalogAdapter(
        shop, observed, _Recorder()
    )

    result = adapter._probe_price_schedule(
        {"observed_limit": "当日剩余3次", "exchange_point": (1175, 210)},
        item,
    )

    assert result["status"] == "price_probe_failed"
    assert "单调递增" in result["error"]
    assert result["price_observations"] == []
    assert [kwargs["intent"].action_key for _point, kwargs in taps].count(
        "shop_quantity_cancel"
    ) == 1
    assert shop_purchase.DIALOG_CONFIRM_POS not in [point for point, _ in taps]


def test_bureau_unverified_post_page_stops_without_cancel_or_retry(monkeypatch):
    catalog = load_shop_catalog()
    shop = catalog.shop("bureau_exchange")
    observed = load_read_only_shop_catalog(
        shop.read_only_catalog, catalog.currencies
    )
    item = next(item for item in observed.items if item.id == "bureau_nebula_4")
    taps = []
    monkeypatch.setattr(
        shop_purchase, "screenshot", lambda: _Frame(_historical_frames()[1], 0)
    )
    monkeypatch.setattr(
        shop_purchase,
        "input_tap",
        lambda point, **kwargs: taps.append((point, kwargs)) or True,
    )
    monkeypatch.setattr(shop_purchase.time, "sleep", lambda _seconds: None)
    adapter = shop_purchase.BureauReadOnlyCatalogAdapter(
        shop, observed, _Recorder()
    )

    with pytest.raises(shop_purchase.BlockedBySafetyError, match="未出现数量弹窗"):
        adapter._probe_price_schedule(
            {"observed_limit": "当日剩余6次", "exchange_point": (1175, 210)},
            item,
        )

    assert len(taps) == 1
    assert taps[0][1]["intent"].action_key == "shop_bureau_quantity_open"
    assert adapter.dialog_cancel_dispatches == 0


def test_bureau_single_remaining_uses_row_cost_without_item_click(monkeypatch):
    catalog = load_shop_catalog()
    shop = catalog.shop("bureau_exchange")
    observed = load_read_only_shop_catalog(
        shop.read_only_catalog, catalog.currencies
    )
    item = next(item for item in observed.items if item.id == "bureau_hulton_balloon")
    monkeypatch.setattr(
        shop_purchase,
        "input_tap",
        lambda *_args, **_kwargs: pytest.fail("one-unit schedule must not click"),
    )
    adapter = shop_purchase.BureauReadOnlyCatalogAdapter(
        shop, observed, _Recorder()
    )

    result = adapter._probe_price_schedule(
        {"observed_limit": "今日剩余1次", "exchange_point": (1175, 210)},
        item,
    )

    assert result["status"] == "price_schedule_validated"
    assert result["price_observations"][0]["costs"] == [
        {"currency": "jue_ming", "marginal_cost": 500, "cumulative_cost": 500},
        {"currency": "fu_ming", "marginal_cost": 2500, "cumulative_cost": 2500},
    ]


def test_bureau_sold_out_item_is_reported_unavailable_without_click(monkeypatch):
    catalog = load_shop_catalog()
    shop = catalog.shop("bureau_exchange")
    observed = load_read_only_shop_catalog(
        shop.read_only_catalog, catalog.currencies
    )
    item = next(item for item in observed.items if item.id == "bureau_nebula_4")
    monkeypatch.setattr(
        shop_purchase,
        "input_tap",
        lambda *_args, **_kwargs: pytest.fail("sold-out item must not click"),
    )
    adapter = shop_purchase.BureauReadOnlyCatalogAdapter(
        shop, observed, _Recorder()
    )

    result = adapter._probe_price_schedule(
        {"observed_limit": "当日剩余0次", "exchange_point": (1175, 210)},
        item,
    )

    assert result["status"] == "price_probe_unavailable"
    assert "已售罄" in result["error"]
    assert result["price_observations"] == []


def test_bureau_multi_currency_probe_preserves_channel_order(monkeypatch):
    catalog = load_shop_catalog()
    shop = catalog.shop("bureau_exchange")
    observed = load_read_only_shop_catalog(
        shop.read_only_catalog, catalog.currencies
    )
    original = next(
        item for item in observed.items if item.id == "bureau_hulton_balloon"
    )
    item = replace(original, observed_limit="今日剩余2次")
    frames = iter((
        _DialogFrame(_dialog_ocr(item.name, 1, 2, [500, 2500])),
        _DialogFrame(_dialog_ocr(item.name, 2, 2, [1100, 5200])),
        _Frame(_historical_frames()[0], 20),
    ))
    taps = []
    monkeypatch.setattr(shop_purchase, "screenshot", lambda: next(frames))
    monkeypatch.setattr(
        shop_purchase,
        "input_tap",
        lambda point, **kwargs: taps.append((point, kwargs)) or True,
    )
    monkeypatch.setattr(shop_purchase.time, "sleep", lambda _seconds: None)
    adapter = shop_purchase.BureauReadOnlyCatalogAdapter(
        shop, observed, _Recorder()
    )

    result = adapter._probe_price_schedule(
        {"observed_limit": "今日剩余2次", "exchange_point": (1175, 210)},
        item,
    )

    assert result["status"] == "price_schedule_validated"
    assert result["price_observations"] == [
        {"quantity": 1, "costs": [
            {"currency": "jue_ming", "marginal_cost": 500, "cumulative_cost": 500},
            {"currency": "fu_ming", "marginal_cost": 2500, "cumulative_cost": 2500},
        ]},
        {"quantity": 2, "costs": [
            {"currency": "jue_ming", "marginal_cost": 600, "cumulative_cost": 1100},
            {"currency": "fu_ming", "marginal_cost": 2700, "cumulative_cost": 5200},
        ]},
    ]
    assert [kwargs["intent"].action_key for _point, kwargs in taps] == [
        "shop_bureau_quantity_open",
        "shop_quantity_increment",
        "shop_quantity_cancel",
    ]


def test_bureau_unknown_page_after_increment_never_guesses_cancel(monkeypatch):
    catalog = load_shop_catalog()
    shop = catalog.shop("bureau_exchange")
    observed = load_read_only_shop_catalog(
        shop.read_only_catalog, catalog.currencies
    )
    original = next(item for item in observed.items if item.id == "bureau_nebula_4")
    item = replace(original, observed_limit="当日剩余2次")
    frames = iter((
        _DialogFrame(_dialog_ocr(item.name, 1, 2, [100])),
        _Frame(_historical_frames()[1], 20),
    ))
    taps = []
    monkeypatch.setattr(shop_purchase, "screenshot", lambda: next(frames))
    monkeypatch.setattr(
        shop_purchase,
        "input_tap",
        lambda point, **kwargs: taps.append((point, kwargs)) or True,
    )
    monkeypatch.setattr(shop_purchase.time, "sleep", lambda _seconds: None)
    adapter = shop_purchase.BureauReadOnlyCatalogAdapter(
        shop, observed, _Recorder()
    )

    with pytest.raises(shop_purchase.BlockedBySafetyError, match="页面身份不明"):
        adapter._probe_price_schedule(
            {"observed_limit": "当日剩余2次", "exchange_point": (1175, 210)},
            item,
        )

    assert [kwargs["intent"].action_key for _point, kwargs in taps] == [
        "shop_bureau_quantity_open",
        "shop_quantity_increment",
    ]
    assert adapter.dialog_cancel_dispatches == 0


def test_bureau_scan_rebinds_next_item_to_fresh_list_frame(monkeypatch):
    catalog = load_shop_catalog()
    shop = catalog.shop("bureau_exchange")
    full_catalog = load_read_only_shop_catalog(
        shop.read_only_catalog, catalog.currencies
    )
    observed = replace(full_catalog, items=full_catalog.items[:2])

    def page(token: str, level: int) -> _Frame:
        return _Frame([
            _ocr("赴命商店", 780, 25),
            _ocr("赴命商店", 950, 25),
            _ocr(token, 700, 200),
        ], level)

    frames = iter((page("A", 0), page("B", 20), page("C", 40)))
    monkeypatch.setattr(shop_purchase, "screenshot", lambda: next(frames))
    monkeypatch.setattr(shop_purchase.time, "sleep", lambda _seconds: None)

    def locate(values, item):
        texts = {entry["text"] for entry in values}
        if item.id == observed.items[0].id and "A" in texts:
            return {"observed_limit": "今日剩余2次", "exchange_point": (1175, 210)}
        if item.id == observed.items[1].id and "B" in texts:
            return {"observed_limit": "今日剩余2次", "exchange_point": (1175, 310)}
        return None

    monkeypatch.setattr(shop_purchase, "locate_read_only_bureau_item", locate)
    adapter = shop_purchase.BureauReadOnlyCatalogAdapter(
        shop, observed, _Recorder()
    )

    def probe(_match, item):
        adapter.dialog_open_dispatches += 1
        return {
            "id": item.id,
            "name": item.name,
            "status": "price_schedule_validated",
            "price_observations": [],
        }

    monkeypatch.setattr(adapter, "_probe_price_schedule", probe)

    result = adapter.scan(max_pages=1, probe_prices=True)

    assert [entry["id"] for entry in result["results"]] == [
        item.id for item in observed.items
    ]
    assert result["dialog_open_dispatches"] == 2


def test_bureau_open_from_store_selector_taps_only_the_bureau_tab(monkeypatch):
    catalog = load_shop_catalog()
    shop = catalog.shop("bureau_exchange")
    observed = load_read_only_shop_catalog(
        shop.read_only_catalog, catalog.currencies
    )
    selector = [
        {"text": "总部商店", "position": [[821, 25], [895, 25], [895, 52], [821, 52]]},
        {"text": "赴命商店", "position": [[965, 29], [1039, 29], [1039, 52], [965, 52]]},
    ]
    bureau = _historical_frames()[0]
    frames = iter((_Frame(selector, 0), _Frame(bureau, 20)))
    taps = []
    monkeypatch.setattr(shop_purchase, "screenshot", lambda: next(frames))
    monkeypatch.setattr(
        shop_purchase,
        "input_tap",
        lambda point, **kwargs: taps.append((point, kwargs)),
    )
    monkeypatch.setattr(
        shop_purchase.BureauReadOnlyCatalogAdapter,
        "_rewind_to_top",
        lambda self: None,
    )
    adapter = shop_purchase.BureauReadOnlyCatalogAdapter(
        shop, observed, _Recorder()
    )

    adapter.open()

    assert len(taps) == 1
    assert taps[0][0] == shop_purchase.BUREAU_TAB_POS
    assert taps[0][1]["random_offset"] is False
    assert taps[0][1]["intent"].action_key == "shop_bureau_open"
    assert shop_purchase.BureauReadOnlyCatalogAdapter.key not in shop_purchase.ADAPTERS


def test_bureau_navigation_specs_are_read_only_and_narrow():
    open_spec = DEFAULT_POLICY_SPECS["shop_bureau_open"]
    quantity_open_spec = DEFAULT_POLICY_SPECS["shop_bureau_quantity_open"]
    increment_spec = DEFAULT_POLICY_SPECS["shop_quantity_increment"]
    cancel_spec = DEFAULT_POLICY_SPECS["shop_quantity_cancel"]
    scroll_spec = DEFAULT_POLICY_SPECS["shop_catalog_scroll"]

    assert open_spec.allowed_page_types == frozenset({"shop"})
    assert open_spec.allowed_region == (940, 12, 1065, 68)
    assert quantity_open_spec.allowed_region == (1100, 130, 1255, 705)
    assert quantity_open_spec.allowed_post_page_types == frozenset(
        {"shop_quantity_dialog"}
    )
    assert increment_spec.allowed_region == (790, 350, 850, 410)
    assert cancel_spec.allowed_region == (180, 490, 640, 585)
    assert cancel_spec.allowed_post_page_types == frozenset({"shop"})
    assert scroll_spec.allowed_swipe_directions == frozenset({"UP"})
    assert "shop_confirm" not in {
        "shop_bureau_open", "shop_bureau_quantity_open",
        "shop_quantity_increment", "shop_quantity_cancel",
        "shop_catalog_scroll", "shop_catalog_rewind",
    }


def test_bureau_product_entry_enables_price_probe_without_exchange(monkeypatch):
    calls = []

    class _Adapter:
        def __init__(self, *_args):
            pass

        def open(self):
            calls.append("open")

        def scan(self, *, probe_prices=False):
            calls.append(("scan", probe_prices))
            return {
                "success": True,
                "requires_attention": False,
                "results": [],
                "missing": [],
                "confirm_dispatches": 0,
                "exchange_actions": 0,
            }

    monkeypatch.setattr(
        shop_purchase, "BureauReadOnlyCatalogAdapter", _Adapter
    )
    monkeypatch.setattr(
        shop_purchase, "_connected_run", lambda callback: callback()
    )

    result = shop_purchase.probe_bureau_shop_catalog(capture_evidence=False)

    assert calls == ["open", ("scan", True)]
    assert result["shops"][0]["confirm_dispatches"] == 0
    assert result["shops"][0]["exchange_actions"] == 0


def test_gui_bureau_scan_uses_read_only_entry_and_updates_cards(monkeypatch):
    application = QApplication.instance() or QApplication([])
    monkeypatch.setattr(
        shop_purchase,
        "probe_bureau_shop_catalog",
        lambda capture_evidence=True: {
            "success": True,
            "dry_run": True,
            "read_only": True,
            "requires_attention": False,
            "shops": [{
                "shop": "bureau_exchange",
                "mode": "read_only_catalog",
                "pages": 6,
                "results": [{
                    "id": "bureau_hulton_balloon",
                    "name": "胡尔顿气球",
                    "status": "validated",
                    "observed_limit": "今日剩余1次",
                }],
                "missing": [],
            }],
        },
    )
    page = shop_interface.ShopPlannerInterface()
    page.showShop("bureau_exchange")
    monkeypatch.setattr(shop_interface.ShopDryRunWorker, "start", lambda self: None)
    page.startDryRun()

    assert page.dryRunWorker is not None
    assert page.dryRunWorker.shop_id == "bureau_exchange"

    result = shop_interface._run_shop_dry_run("bureau_exchange")
    page._dryRunSucceeded(result)
    page._dryRunFinished()

    card = page.readOnlyItemCards["bureau_hulton_balloon"]
    assert "已核验" in card.liveObservation.text()
    assert "今日剩余1次" in card.liveObservation.text()
    assert "6 页" in page.dryRunStatus.text()
    assert "不兑换" in page.dryRunButton.text()
    assert application is not None
    page.deleteLater()


def test_gui_bureau_card_shows_multicurrency_schedule_and_failure():
    application = QApplication.instance() or QApplication([])
    catalog = load_shop_catalog()
    shop = catalog.shop("bureau_exchange")
    observed = load_read_only_shop_catalog(
        shop.read_only_catalog, catalog.currencies
    )
    item = next(
        item for item in observed.items if item.id == "bureau_hulton_balloon"
    )
    card = shop_interface.ReadOnlyShopItemCard(item, catalog.currencies)

    card.setLiveObservation({
        "status": "price_schedule_validated",
        "observed_limit": "今日剩余2次",
        "price_observations": [{
            "quantity": 1,
            "costs": [
                {"currency": "jue_ming", "marginal_cost": 500, "cumulative_cost": 500},
                {"currency": "fu_ming", "marginal_cost": 2500, "cumulative_cost": 2500},
            ],
        }],
    })
    assert "数量 1" in card.liveObservation.text()
    assert "绝命奖章 边际 500" in card.liveObservation.text()
    assert "赴命奖章 边际 2,500" in card.liveObservation.text()

    card.setLiveObservation({
        "status": "price_probe_failed",
        "error": "边际成本不满足单调递增合同",
    })
    assert "本次价格探针未完成" in card.liveObservation.text()
    assert "单调递增" in card.liveObservation.text()
    assert application is not None
    card.deleteLater()
