from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from app.view.shop_planner_interface import ShopPlannerInterface
from core.services.shop_catalog import (
    audit_read_only_shop_catalog_evidence,
    load_read_only_shop_catalog,
    load_shop_catalog,
)


def test_bureau_read_only_catalog_has_22_evidence_bound_items():
    catalog = load_shop_catalog()
    shop = catalog.shop("bureau_exchange")

    assert shop.automation_supported is False
    assert shop.items == ()
    observed = load_read_only_shop_catalog(
        shop.read_only_catalog,
        catalog.currencies,
    )

    assert len(observed.items) == 22
    assert len({item.id for item in observed.items}) == 22
    assert {cost.currency for item in observed.items for cost in item.costs} == {
        "fu_ming",
        "jue_ming",
        "iron_coin",
    }
    warehouse = next(
        item for item in observed.items if item.id == "bureau_warehouse_expansion"
    )
    assert [(cost.currency, cost.amount) for cost in warehouse.costs] == [
        ("jue_ming", 300),
        ("fu_ming", 1500),
        ("iron_coin", 1_000_000),
    ]


def test_bureau_catalog_replays_all_declared_source_ocr_without_input():
    catalog = load_shop_catalog()
    shop = catalog.shop("bureau_exchange")
    observed = load_read_only_shop_catalog(
        shop.read_only_catalog,
        catalog.currencies,
    )

    result = audit_read_only_shop_catalog_evidence(observed)

    assert result == {
        "shop_id": "bureau_exchange",
        "expected_items": 22,
        "matched_items": 22,
        "failures": [],
        "result": "PASS",
    }


def test_bureau_gui_displays_read_only_cards_without_purchase_controls():
    application = QApplication.instance() or QApplication([])
    page = ShopPlannerInterface()

    page.showShop("bureau_exchange")

    assert page.productGrid.count() == 22
    assert page.itemCards == {}
    assert "只读目录 22 项" in page.shopDescription.text()
    assert "自动兑换未启用" in page.productGrid.itemAt(0).widget().findChildren(
        type(page.shopDescription)
    )[-1].text()
    assert application is not None
    page.deleteLater()
