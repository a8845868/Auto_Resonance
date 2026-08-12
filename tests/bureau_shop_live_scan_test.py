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
        data.append(_ocr(str(total), 515 + index * 70, 285, 42, 22))
    return data


def test_v6_terminal_dot_keeps_arrest_quantity_frame_bound_to_session():
    """Live q=88 OCR appends a dot to ``88/100``; it is still the same dialog."""
    from auto.shop_purchase import (
        _validate_increment_session_frame,
        parse_quantity_text,
    )

    catalog = load_shop_catalog()
    shop = catalog.shop("bureau_exchange")
    observed = load_read_only_shop_catalog(
        shop.read_only_catalog, catalog.currencies
    )
    item = next(
        value for value in observed.items
        if value.id == "bureau_arrest_warrant"
    )
    data = _dialog_ocr(item.name, 88, 100, [1760])
    quantity_token = next(value for value in data if value["text"] == "88/100")
    quantity_token["text"] = "88/100."
    frame = _DialogFrame(data)

    assert parse_quantity_text("88/100.") == (88, 100)
    assert parse_quantity_text("88/100x") is None
    assert _validate_increment_session_frame(
        frame,
        frame.ocr(),
        item,
        expected_quantity=88,
        expected_maximum=100,
        channel_order=(item.costs[0].currency,),
    ) is True


def test_bureau_name_matches_reordered_v6_ocr_token():
    from auto.shop_purchase import _bureau_name_matches_alias

    assert _bureau_name_matches_alias(
        "改造凭证×1一般武",
        ("般武装改造凭证", "一般武装改造凭证"),
    ) is True
    assert _bureau_name_matches_alias(
        "殊武装改造特许",
        ("殊武装改造特许", "特殊武装改造特许"),
    ) is True
    assert _bureau_name_matches_alias(
        "逮捕令",
        ("逮捕令",),
    ) is True
    # Live V6 evidence form "正×1一般武装改造" must match the narrow
    # evidence alias "一般武装改造" via substring matching.
    assert _bureau_name_matches_alias(
        "正×1一般武装改造",
        ("一般武装改造", "般武装改造凭证"),
    ) is True
    # Short aliases do not use the character-set fallback.
    assert _bureau_name_matches_alias("xyz", ("abc", "def")) is False


def test_bureau_alias_matching_is_unique_across_full_catalog():
    """No live OCR text may match multiple catalog items via character overlap."""
    from auto.shop_purchase import _bureau_name_matches_alias

    catalog = load_shop_catalog()
    shop = catalog.shop("bureau_exchange")
    observed = load_read_only_shop_catalog(
        shop.read_only_catalog, catalog.currencies
    )

    # Synthesize one representative live-ocr form per item.
    live_forms: dict[str, str] = {}
    for item in observed.items:
        live_forms[item.id] = item.ocr_aliases[0]

    # Add in plausible V6-reordered forms.
    live_forms["bureau_general_weapon_fu"] = "改造凭证×1一般武"
    live_forms["bureau_special_weapon_fu"] = "殊武装改造特许"
    live_forms["bureau_general_weapon_jue"] = "改造凭证×1一般武"
    live_forms["bureau_special_weapon_jue"] = "殊武装改造特许"

    mismatch_ids: set[str] = set()
    for test_id, live_text in live_forms.items():
        test_item = next(i for i in observed.items if i.id == test_id)
        matches = [
            item.id
            for item in observed.items
            if _bureau_name_matches_alias(live_text, item.ocr_aliases)
            and sorted(c.amount for c in item.costs)
            == sorted(c.amount for c in test_item.costs)
        ]
        if matches != [test_id]:
            mismatch_ids.add(test_id)

    # purchase_book and advertising each have weekly+monthly variants that
    # share the exact same aliases AND cost multiset — period is the sole
    # disambiguator for those pairs, and the period-ambiguity guard handles
    # them downstream.
    expected = {
        "bureau_purchase_book_weekly", "bureau_purchase_book_monthly",
        "bureau_advertising_weekly", "bureau_advertising_monthly",
    }
    assert mismatch_ids == expected, (
        f"unexpected alias+same-cost cross-matches: {sorted(mismatch_ids - expected)}"
    )


def test_bureau_period_ambiguous_ids_includes_same_cost_same_name_pairs():
    from auto.shop_purchase import _bureau_period_ambiguous_ids

    ambiguous = _bureau_period_ambiguous_ids()

    assert "bureau_purchase_book_weekly" in ambiguous
    assert "bureau_purchase_book_monthly" in ambiguous
    assert "bureau_advertising_weekly" in ambiguous
    assert "bureau_advertising_monthly" in ambiguous
    # Weapons share the same alias but differ in cost → not ambiguous.
    assert "bureau_general_weapon_fu" not in ambiguous
    assert "bureau_general_weapon_jue" not in ambiguous


def test_period_unknown_rejected_for_ambiguous_same_cost_probe():
    """Purchase book with unknown period must be rejected because
    weekly/monthly share the same name and cost."""
    from auto.shop_purchase import (
        _bureau_period_ambiguous_ids,
        locate_read_only_bureau_item,
    )

    catalog = load_shop_catalog()
    shop = catalog.shop("bureau_exchange")
    observed = load_read_only_shop_catalog(
        shop.read_only_catalog, catalog.currencies
    )
    item = next(
        i for i in observed.items if i.id == "bureau_purchase_book_weekly"
    )
    assert item.id in _bureau_period_ambiguous_ids()
    # OCR row: purchase book with "剩余5次" — no period prefix.
    ocr_items = [
        _ocr("进货采买书", 710, 660, 120),
        _ocr("65.3k/150", 855, 660, 90),
        _ocr("剩余5次", 1190, 660, 70),
    ]

    assert locate_read_only_bureau_item(ocr_items, item) is None


def test_period_known_allows_ambiguous_pair_despite_same_cost():
    """Purchase book WITH period prefix must still match."""
    from auto.shop_purchase import locate_read_only_bureau_item

    catalog = load_shop_catalog()
    shop = catalog.shop("bureau_exchange")
    observed = load_read_only_shop_catalog(
        shop.read_only_catalog, catalog.currencies
    )
    item = next(
        i for i in observed.items if i.id == "bureau_purchase_book_weekly"
    )
    ocr_items = [
        _ocr("进货采买书", 710, 660, 120),
        _ocr("65.3k/150", 855, 660, 90),
        _ocr("本周剩余5次", 1190, 660, 70),
    ]

    result = locate_read_only_bureau_item(ocr_items, item)

    assert result is not None
    assert result["id"] == "bureau_purchase_book_weekly"


def test_general_weapon_fu_matches_live_v6_ocr_form():
    """Live V6 token '正×1一般武装改造' + cost 50 + '当日剩余5次' → match."""
    from auto.shop_purchase import locate_read_only_bureau_item

    catalog = load_shop_catalog()
    shop = catalog.shop("bureau_exchange")
    observed = load_read_only_shop_catalog(
        shop.read_only_catalog, catalog.currencies
    )
    item = next(
        i for i in observed.items if i.id == "bureau_general_weapon_fu"
    )
    # Verify the new evidence alias is in the catalog JSON.
    assert "一般武装改造" in item.ocr_aliases

    ocr_items = [
        _ocr("正×1一般武装改造", 711, 450, 180),
        _ocr("65.3k/50", 855, 450, 90),
        _ocr("当日剩余5次", 1190, 450, 80),
    ]

    result = locate_read_only_bureau_item(ocr_items, item)

    assert result is not None
    assert result["id"] == "bureau_general_weapon_fu"


def test_general_weapon_alias_does_not_match_special_weapon():
    from auto.shop_purchase import _bureau_name_matches_alias

    assert _bureau_name_matches_alias(
        "改造凭证×1一般武",
        ("殊武装改造特许", "特殊武装改造特许"),
    ) is False
    assert _bureau_name_matches_alias(
        "殊武装改造特许",
        ("般武装改造凭证", "一般武装改造凭证"),
    ) is False


def test_nebula_4_matched_via_reassembled_parenthesized_name():
    """V6 splits '(4钛)' — reassembly must bind the unique correct item."""
    from auto.shop_purchase import locate_read_only_bureau_item

    catalog = load_shop_catalog()
    shop = catalog.shop("bureau_exchange")
    observed = load_read_only_shop_catalog(
        shop.read_only_catalog, catalog.currencies
    )
    item = next(i for i in observed.items if i.id == "bureau_nebula_4")

    # Real live evidence: two adjacent OCR tokens plus cost and limit.
    ocr_items = [
        _ocr("67.1k/100", 855, 530, 90),
        _ocr("星云物质", 670, 530, 85),
        _ocr("(4钛) ×1", 755, 530, 80),
        _ocr("当日剩余6次", 1190, 530, 80),
    ]

    result = locate_read_only_bureau_item(ocr_items, item)

    assert result is not None
    assert result["id"] == "bureau_nebula_4"


def test_general_weapon_jue_matched_by_added_alias_cost_10():
    """V6 token '正×1一般武装改造' + cost 10 must bind jue version,
    not the fu version (cost 50), and stay unavailable without limit."""
    from auto.shop_purchase import locate_read_only_bureau_item

    catalog = load_shop_catalog()
    shop = catalog.shop("bureau_exchange")
    observed = load_read_only_shop_catalog(
        shop.read_only_catalog, catalog.currencies
    )
    jue_item = next(
        i for i in observed.items if i.id == "bureau_general_weapon_jue"
    )
    fu_item = next(
        i for i in observed.items if i.id == "bureau_general_weapon_fu"
    )

    # Verify the shared alias does not create ambiguity.
    assert "一般武装改造" in jue_item.ocr_aliases
    assert "一般武装改造" in fu_item.ocr_aliases
    assert {c.amount for c in jue_item.costs} == {10}
    assert {c.amount for c in fu_item.costs} == {50}

    ocr_items = [
        _ocr("正×1一般武装改造", 711, 480, 160),
        _ocr("1.1k/10", 855, 480, 80),
    ]

    result = locate_read_only_bureau_item(ocr_items, jue_item)

    assert result is not None
    assert result["id"] == "bureau_general_weapon_jue"


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


def test_bureau_unknown_remaining_uses_verified_dialog_maximum_once(monkeypatch):
    catalog = load_shop_catalog()
    shop = catalog.shop("bureau_exchange")
    observed = load_read_only_shop_catalog(
        shop.read_only_catalog, catalog.currencies
    )
    item = next(
        item for item in observed.items
        if item.id == "bureau_general_weapon_jue"
    )
    frames = iter((
        _DialogFrame(_dialog_ocr(item.name, 1, 2, [10])),
        _DialogFrame(_dialog_ocr(item.name, 2, 2, [25])),
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
        {
            "id": item.id,
            "observed_limit": "未稳定识别",
            "exchange_point": (1175, 210),
        },
        item,
    )

    assert result["status"] == "price_schedule_validated"
    assert result["dialog_maximum"] == 2
    assert result["price_observations"] == [
        {
            "quantity": 1,
            "costs": [{
                "currency": "jue_ming",
                "marginal_cost": 10,
                "cumulative_cost": 10,
            }],
        },
        {
            "quantity": 2,
            "costs": [{
                "currency": "jue_ming",
                "marginal_cost": 15,
                "cumulative_cost": 25,
            }],
        },
    ]
    action_keys = [kwargs["intent"].action_key for _, kwargs in taps]
    assert action_keys == [
        "shop_bureau_quantity_open",
        "shop_quantity_increment",
        "shop_quantity_cancel",
    ]
    assert all(kwargs["random_offset"] is False for _, kwargs in taps)
    assert adapter.dialog_open_dispatches == 1
    assert adapter.quantity_increment_dispatches == 1
    assert adapter.dialog_cancel_dispatches == 1
    assert "shop_confirm" not in action_keys


def test_arrest_warrant_has_single_item_99_increment_probe_budget(monkeypatch):
    """Only arrest warrant may prove its complete 1/100 marginal schedule."""
    catalog = load_shop_catalog()
    shop = catalog.shop("bureau_exchange")
    observed = load_read_only_shop_catalog(
        shop.read_only_catalog, catalog.currencies
    )
    item = next(
        item for item in observed.items if item.id == "bureau_arrest_warrant"
    )
    other = next(
        item for item in observed.items if item.id == "bureau_general_weapon_jue"
    )
    assert shop_purchase.MAX_QUANTITY_PROBE_INCREMENTS == 10
    assert shop_purchase._bureau_quantity_probe_increment_limit(item) == 99
    assert shop_purchase._bureau_quantity_probe_increment_limit(other) == 10

    frames = iter([
        _DialogFrame(_dialog_ocr(item.name, quantity, 100, [20 * quantity]))
        for quantity in range(1, 101)
    ] + [_Frame(_historical_frames()[0], 20)])
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
            "id": item.id,
            "observed_limit": "未稳定识别",
            "exchange_point": (1175, 210),
        },
        item,
    )

    assert result["status"] == "price_schedule_validated"
    assert result["dialog_maximum"] == 100
    assert len(result["price_observations"]) == 100
    assert result["price_observations"][0]["costs"] == [{
        "currency": "fu_ming",
        "marginal_cost": 20,
        "cumulative_cost": 20,
    }]
    assert result["price_observations"][-1]["costs"] == [{
        "currency": "fu_ming",
        "marginal_cost": 20,
        "cumulative_cost": 2000,
    }]
    action_keys = [kwargs["intent"].action_key for _, kwargs in taps]
    assert action_keys.count("shop_bureau_quantity_open") == 1
    assert action_keys.count("shop_quantity_increment") == 99
    assert action_keys.count("shop_quantity_cancel") == 1
    assert "shop_confirm" not in action_keys
    assert all(kwargs["random_offset"] is False for _, kwargs in taps)
    assert adapter.dialog_open_dispatches == 1
    assert adapter.quantity_increment_dispatches == 99
    assert adapter.dialog_cancel_dispatches == 1


def test_non_arrest_bureau_item_keeps_global_10_increment_budget(monkeypatch):
    catalog = load_shop_catalog()
    shop = catalog.shop("bureau_exchange")
    observed = load_read_only_shop_catalog(
        shop.read_only_catalog, catalog.currencies
    )
    item = next(
        item for item in observed.items if item.id == "bureau_general_weapon_jue"
    )
    frames = iter((
        _DialogFrame(_dialog_ocr(item.name, 1, 12, [10])),
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
        {
            "id": item.id,
            "observed_limit": "未稳定识别",
            "exchange_point": (1175, 210),
        },
        item,
    )

    assert result["status"] == "price_probe_failed"
    assert "预算 10" in result["error"]
    action_keys = [kwargs["intent"].action_key for _, kwargs in taps]
    assert action_keys == [
        "shop_bureau_quantity_open",
        "shop_quantity_cancel",
    ]
    assert adapter.quantity_increment_dispatches == 0


def test_bureau_unknown_remaining_without_unique_control_never_clicks(monkeypatch):
    catalog = load_shop_catalog()
    shop = catalog.shop("bureau_exchange")
    observed = load_read_only_shop_catalog(
        shop.read_only_catalog, catalog.currencies
    )
    item = next(
        item for item in observed.items
        if item.id == "bureau_general_weapon_jue"
    )
    monkeypatch.setattr(
        shop_purchase,
        "input_tap",
        lambda *_args, **_kwargs: pytest.fail(
            "unknown remaining without a unique control must not click"
        ),
    )
    adapter = shop_purchase.BureauReadOnlyCatalogAdapter(
        shop, observed, _Recorder()
    )

    result = adapter._probe_price_schedule(
        {
            "id": item.id,
            "observed_limit": "未稳定识别",
            "exchange_point": None,
        },
        item,
    )

    assert result["status"] == "price_probe_unavailable"
    assert "未唯一识别本行兑换控件" in result["error"]
    assert result["price_observations"] == []
    assert adapter.dialog_open_dispatches == 0


def test_bureau_unknown_remaining_without_bound_identity_never_clicks(monkeypatch):
    catalog = load_shop_catalog()
    shop = catalog.shop("bureau_exchange")
    observed = load_read_only_shop_catalog(
        shop.read_only_catalog, catalog.currencies
    )
    item = next(
        item for item in observed.items
        if item.id == "bureau_general_weapon_jue"
    )
    monkeypatch.setattr(
        shop_purchase,
        "input_tap",
        lambda *_args, **_kwargs: pytest.fail(
            "unknown remaining without bound identity must not click"
        ),
    )
    adapter = shop_purchase.BureauReadOnlyCatalogAdapter(
        shop, observed, _Recorder()
    )

    result = adapter._probe_price_schedule(
        {
            "id": "bureau_general_weapon_fu",
            "observed_limit": "未稳定识别",
            "exchange_point": (1175, 210),
        },
        item,
    )

    assert result["status"] == "price_probe_unavailable"
    assert "商品身份未唯一绑定" in result["error"]
    assert result["price_observations"] == []
    assert adapter.dialog_open_dispatches == 0


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


def test_bureau_three_currency_warehouse_probe_reads_all_channels(monkeypatch):
    """Costs at cx≈354,517,635 — the leftmost (354) is below x=470."""
    catalog = load_shop_catalog()
    shop = catalog.shop("bureau_exchange")
    observed = load_read_only_shop_catalog(
        shop.read_only_catalog, catalog.currencies
    )
    original = next(
        item for item in observed.items
        if item.id == "bureau_warehouse_expansion"
    )
    item = replace(original, observed_limit="今日剩余2次")

    def warehouse_dialog_ocr(quantity, totals):
        """Exact evidence from 088-bureau-price-01-bureau_warehouse_expansion.

        300 @ cx≈389 (left of the old x=470 ROI boundary),
        1500 @ cx≈517, 1000000 @ cx≈635."""
        return [
            _ocr(item.name, 560, 285, 180),
            _ocr(f"{quantity}/2", 610, 345, 70),
            _ocr("最少", 350, 350, 55),
            _ocr("最多", 855, 350, 55),
            _ocr(f"确认消耗以上素材兑换{item.name}", 480, 435, 260, 22),
            _ocr("取消", 300, 520, 55),
            _ocr("确定", 930, 520, 55),
            _ocr(str(totals[0]), 375, 285, 42, 22),   # cx≈396
            _ocr(str(totals[1]), 495, 285, 44, 22),   # cx≈517
            _ocr(str(totals[2]), 595, 285, 80, 22),   # cx≈635
        ]

    frames = iter((
        _DialogFrame(warehouse_dialog_ocr(1, [300, 1500, 1000000])),
        _DialogFrame(warehouse_dialog_ocr(2, [700, 3100, 2100000])),
        _Frame(_historical_frames()[5], 20),
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
        {"observed_limit": "今日剩余2次", "exchange_point": (1190, 260)},
        item,
    )

    assert result["status"] == "price_schedule_validated"
    assert [kwargs["intent"].action_key for _point, kwargs in taps] == [
        "shop_bureau_quantity_open",
        "shop_quantity_increment",
        "shop_quantity_cancel",
    ]
    # Verify all three channels were probed.
    obs = result["price_observations"]
    assert len(obs) == 2
    assert obs[0]["costs"] == [
        {"currency": "jue_ming", "marginal_cost": 300, "cumulative_cost": 300},
        {"currency": "fu_ming", "marginal_cost": 1500, "cumulative_cost": 1500},
        {"currency": "iron_coin", "marginal_cost": 1000000, "cumulative_cost": 1000000},
    ]
    # The leftmost cost at cx≈396 proves the widened ROI (x≥280) is
    # essential — the old x≥470 boundary would miss this channel.


def test_increment_session_frame_accepts_blank_confirmation_line():
    """Continuity frame validator and _bureau_dialog_snapshot both accept
    a blank confirmation line in post-increment mode."""
    from auto.shop_purchase import (
        _validate_increment_session_frame,
        _bureau_dialog_snapshot,
    )

    catalog = load_shop_catalog()
    shop = catalog.shop("bureau_exchange")
    observed = load_read_only_shop_catalog(
        shop.read_only_catalog, catalog.currencies
    )
    item_id = "bureau_general_weapon_fu"
    item = next(i for i in observed.items if i.id == item_id)
    item = replace(item, observed_limit="当日剩余5次")

    def frame(q, with_conf=True):
        items = [
            _ocr("改造凭证×1一般武", 560, 285, 200),
            _ocr(f"{q}/5", 610, 345, 70),
            _ocr("最少", 350, 350, 55),
            _ocr("最多", 855, 350, 55),
            _ocr(str(q * 50), 630, 285, 50),
            _ocr("取消", 300, 520, 55),
            _ocr("确定", 930, 520, 55),
        ]
        if with_conf:
            items.append(_ocr(
                "确认消耗以上素材兑换一般武装改造凭证" + str(q) + "吗？",
                480, 435, 300, 22,
            ))
        return _DialogFrame(items)

    # Helper: blank confirmation accepted.
    f3 = frame(3, False)
    assert _validate_increment_session_frame(
        f3, f3.ocr(), item,
        expected_quantity=3, expected_maximum=5,
        channel_order=("fu_ming",),
    ) is True

    # _bureau_dialog_snapshot with channel_order: blank line must not
    # trigger _bureau_dialog_item_visible rejection (P1 regression).
    snap = _bureau_dialog_snapshot(
        f3, f3.ocr(), item,
        expected_quantity=3, expected_maximum=5,
        channel_order=("fu_ming",),
    )
    assert snap["quantity"] == 3
    assert snap["maximum"] == 5
    assert isinstance(snap["channel_x_order"], tuple)
    assert len(snap["channel_x_order"]) == 1

    # Helper: wrong item confirmation rejected.
    wrong = [
        _ocr("特殊武装改造特许", 560, 285, 200),
        _ocr("3/5", 610, 345, 70),
        _ocr("最少", 350, 350, 55),
        _ocr("最多", 855, 350, 55),
        _ocr("450", 630, 285, 50),
        _ocr("取消", 300, 520, 55),
        _ocr("确定", 930, 520, 55),
        _ocr("确认消耗以上素材兑换特殊武装改造特许3吗？", 480, 435, 300, 22),
    ]
    wrong_frame = _DialogFrame(wrong)
    assert _validate_increment_session_frame(
        wrong_frame, wrong_frame.ocr(), item,
        expected_quantity=3, expected_maximum=5,
        channel_order=("fu_ming",),
    ) is False


def test_split_confirmation_text_is_not_misread_as_absent():
    """Two OCR tokens that together contain '确认消耗'+'兑换'+wrong item
    must be rejected, not treated as 'confirmation absent'."""
    from auto.shop_purchase import _validate_increment_session_frame

    catalog = load_shop_catalog()
    shop = catalog.shop("bureau_exchange")
    observed = load_read_only_shop_catalog(
        shop.read_only_catalog, catalog.currencies
    )
    item = next(i for i in observed.items if i.id == "bureau_general_weapon_fu")
    item = replace(item, observed_limit="当日剩余5次")

    items = [
        _ocr("改造凭证×1一般武", 560, 285, 200),
        _ocr("3/5", 610, 345, 70),
        _ocr("最少", 350, 350, 55),
        _ocr("最多", 855, 350, 55),
        _ocr("150", 630, 285, 50),
        _ocr("取消", 300, 520, 55),
        _ocr("确定", 930, 520, 55),
        _ocr("确认消耗以上素材", 480, 440, 150, 22),
        _ocr("兑换特殊武装改造特许3吗？", 630, 441, 180, 22),
    ]
    f = _DialogFrame(items)

    # Must reject: merged text contains "确认消耗"+"兑换"+"特殊武装改造特许"
    # which is NOT the current session's item.
    assert _validate_increment_session_frame(
        f, f.ocr(), item,
        expected_quantity=3, expected_maximum=5,
        channel_order=("fu_ming",),
    ) is False


def test_overlapping_v6_confirmation_suffix_preserves_bureau_item_identity():
    """Live q=5 form splits at an overlapping '质'; retain and de-duplicate it."""
    from auto.shop_purchase import _validate_increment_session_frame

    catalog = load_shop_catalog()
    shop = catalog.shop("bureau_exchange")
    observed = load_read_only_shop_catalog(
        shop.read_only_catalog, catalog.currencies
    )
    item = next(i for i in observed.items if i.id == "bureau_nebula_4")
    item = replace(item, observed_limit="剩余6次")
    data = [
        _ocr("星云物质（4钛）", 560, 285, 180),
        _ocr("5/6", 610, 345, 70),
        _ocr("最少", 350, 350, 55),
        _ocr("最多", 855, 350, 55),
        _ocr("500", 515, 285, 42),
        _ocr("取消", 300, 520, 55),
        _ocr("确定", 930, 520, 55),
        _ocr("确认消耗以上素材兑换星云物质", 480, 429, 232, 27),
        _ocr("质（4钛）×5吗？", 700, 430, 145, 26),
        # Nearby decoration on another visual row must not join the identity.
        _ocr("O", 738, 402, 12, 14),
    ]
    frame = _DialogFrame(data)

    assert _validate_increment_session_frame(
        frame,
        frame.ocr(),
        item,
        expected_quantity=5,
        expected_maximum=6,
        channel_order=(item.costs[0].currency,),
    ) is True


def test_v6_dot_before_parenthesized_suffix_preserves_bureau_item_identity():
    """Live q=3 form inserts a middle dot before '(4钛)'; treat it as OCR noise."""
    from auto.shop_purchase import (
        _normalize_bureau_dialog_identity,
        _validate_increment_session_frame,
    )

    catalog = load_shop_catalog()
    shop = catalog.shop("bureau_exchange")
    observed = load_read_only_shop_catalog(
        shop.read_only_catalog, catalog.currencies
    )
    item = next(i for i in observed.items if i.id == "bureau_nebula_4")
    item = replace(item, observed_limit="剩余6次")
    data = [
        _ocr("星云物质（4钛）", 560, 285, 180),
        _ocr("3/6", 610, 345, 70),
        _ocr("最少", 350, 350, 55),
        _ocr("最多", 855, 350, 55),
        _ocr("300", 515, 285, 42),
        _ocr("取消", 300, 520, 55),
        _ocr("确定", 930, 520, 55),
        _ocr("确认消耗以上素材兑换星云物质·(4钛）3吗？", 480, 435, 365, 24),
    ]
    frame = _DialogFrame(data)

    assert _normalize_bureau_dialog_identity("星云物质·(4钛）") == "星云物质（4钛）"
    assert _normalize_bureau_dialog_identity("特供·救世") == "特供·救世"
    assert _validate_increment_session_frame(
        frame,
        frame.ocr(),
        item,
        expected_quantity=3,
        expected_maximum=6,
        channel_order=(item.costs[0].currency,),
    ) is True

    wrong_suffix = [
        dict(value)
        if "星云物质·" not in str(value.get("text"))
        else {**value, "text": "确认消耗以上素材兑换星云物质·(8钛）3吗？"}
        for value in data
    ]
    wrong_frame = _DialogFrame(wrong_suffix)
    assert _validate_increment_session_frame(
        wrong_frame,
        wrong_frame.ocr(),
        item,
        expected_quantity=3,
        expected_maximum=6,
        channel_order=(item.costs[0].currency,),
    ) is False


def test_full_probe_accepts_blank_confirmation_frame_in_3_step_sequence(
    monkeypatch,
):
    """State machine: strong first frame, blank third frame, 1→2→3/5."""
    catalog = load_shop_catalog()
    shop = catalog.shop("bureau_exchange")
    observed = load_read_only_shop_catalog(
        shop.read_only_catalog, catalog.currencies
    )
    item_id = "bureau_general_weapon_fu"
    item = next(i for i in observed.items if i.id == item_id)
    item = replace(item, observed_limit="当日剩余3次")

    def dialog(q, with_conf=True):
        items = [
            _ocr("改造凭证×1一般武", 560, 285, 200),
            _ocr(f"{q}/3", 610, 345, 70),
            _ocr("最少", 350, 350, 55),
            _ocr("最多", 855, 350, 55),
            _ocr(str(q * 50), 630, 285, 50),
            _ocr("取消", 300, 520, 55),
            _ocr("确定", 930, 520, 55),
        ]
        if with_conf:
            items.append(_ocr(
                "确认消耗以上素材兑换一般武装改造凭证" + str(q) + "吗？",
                480, 435, 300, 22,
            ))
        return _DialogFrame(items)

    shop_frame = _Frame([
        _ocr("赴命商店", 760, 25),
        _ocr("赴命商店", 960, 25),
    ], 20)
    frame_iter = iter((
        dialog(1, True),    # open → first snapshot
        dialog(2, True),    # +1 → q=2
        dialog(3, False),   # +1 → q=3 (confirmation dropped)
        shop_frame,         # cancel → verify bureau page
        shop_frame,         # safety net
    ))
    taps = []
    monkeypatch.setattr(
        shop_purchase, "screenshot", lambda: next(frame_iter),
    )
    monkeypatch.setattr(
        shop_purchase,
        "input_tap",
        lambda point, **kwargs: taps.append((point, kwargs)) or True,
    )
    monkeypatch.setattr(shop_purchase.time, "sleep", lambda _seconds: None)
    adapter = shop_purchase.BureauReadOnlyCatalogAdapter(
        shop, observed, _Recorder(),
    )

    result = adapter._probe_price_schedule(
        {"observed_limit": "当日剩余3次", "exchange_point": (1190, 225)},
        item,
    )

    assert result["status"] == "price_schedule_validated"
    assert len(result["price_observations"]) == 3
    assert [kwargs["intent"].action_key for _point, kwargs in taps] == [
        "shop_bureau_quantity_open",
        "shop_quantity_increment",
        "shop_quantity_increment",
        "shop_quantity_cancel",
    ]
    assert adapter.dialog_cancel_dispatches == 1
    assert shop_purchase.DIALOG_CONFIRM_POS not in [
        point for point, _ in taps
    ]


def test_cost_channel_x_shift_beyond_30px_is_rejected(monkeypatch):
    """If a subsequent frame's cost channel drifts >30 px, fail-closed."""
    catalog = load_shop_catalog()
    shop = catalog.shop("bureau_exchange")
    observed = load_read_only_shop_catalog(
        shop.read_only_catalog, catalog.currencies
    )
    item_id = "bureau_general_weapon_fu"
    item = next(i for i in observed.items if i.id == item_id)
    item = replace(item, observed_limit="当日剩余2次")

    frame_iter = iter((
        _DialogFrame([
            _ocr("改造凭证×1一般武", 560, 285, 200),
            _ocr("1/2", 610, 345, 70),
            _ocr("最少", 350, 350, 55),
            _ocr("最多", 855, 350, 55),
            _ocr("50", 630, 285, 50),
            _ocr("取消", 300, 520, 55),
            _ocr("确定", 930, 520, 55),
            _ocr("确认消耗以上素材兑换一般武装改造凭证1吗？", 480, 435, 300, 22),
        ]),
        _DialogFrame([
            _ocr("改造凭证×1一般武", 560, 285, 200),
            _ocr("2/2", 610, 345, 70),
            _ocr("最少", 350, 350, 55),
            _ocr("最多", 855, 350, 55),
            _ocr("100", 630 + 35, 285, 50),
            _ocr("取消", 300, 520, 55),
            _ocr("确定", 930, 520, 55),
            _ocr("确认消耗以上素材兑换一般武装改造凭证2吗？", 480, 435, 300, 22),
        ]),
        _Frame([
            _ocr("赴命商店", 760, 25),
            _ocr("赴命商店", 960, 25),
        ], 20),
        _Frame([
            _ocr("赴命商店", 760, 25),
            _ocr("赴命商店", 960, 25),
        ], 20),
    ))
    monkeypatch.setattr(
        shop_purchase, "screenshot", lambda: next(frame_iter),
    )
    monkeypatch.setattr(
        shop_purchase, "input_tap",
        lambda point, **kwargs: True,
    )
    monkeypatch.setattr(shop_purchase.time, "sleep", lambda _seconds: None)
    adapter = shop_purchase.BureauReadOnlyCatalogAdapter(
        shop, observed, _Recorder(),
    )

    with pytest.raises(shop_purchase.BlockedBySafetyError, match="身份不明"):
        adapter._probe_price_schedule(
            {"observed_limit": "当日剩余2次", "exchange_point": (1190, 225)},
            item,
        )
    # Channel x-shift makes the page unverifiable; the probe must not
    # emit a cancel tap on an unverified dialog frame.
    assert adapter.dialog_cancel_dispatches == 0


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
