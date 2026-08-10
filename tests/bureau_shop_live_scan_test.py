from __future__ import annotations

import json
import os
from pathlib import Path

import numpy as np

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
    scroll_spec = DEFAULT_POLICY_SPECS["shop_catalog_scroll"]

    assert open_spec.allowed_page_types == frozenset({"shop"})
    assert open_spec.allowed_region == (940, 12, 1065, 68)
    assert scroll_spec.allowed_swipe_directions == frozenset({"UP"})
    assert "shop_confirm" not in {
        "shop_bureau_open", "shop_catalog_scroll", "shop_catalog_rewind"
    }


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
