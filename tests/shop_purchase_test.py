import numpy as np
import pytest

import auto.shop_purchase as shop_purchase
from auto.shop_purchase import (
    DIALOG_CANCEL_POS,
    DIALOG_CONFIRM_POS,
    HeadquartersBlackMoonAdapter,
    LocatedProduct,
    ShopEvidenceRecorder,
    _batch_purchase_enabled,
    _content_difference,
    _dialog_price,
    _has_complete_quantity_dialog,
    _has_quantity_dialog,
    locate_product,
    parse_limit_text,
    parse_quantity_text,
)
from core.services.shop_catalog import (
    ConfiguredPurchase,
    default_shop_plan,
    load_shop_catalog,
)


def _ocr(text, x, y, width=110, height=24):
    return {
        "text": text,
        "position": [
            [x, y],
            [x + width, y],
            [x + width, y + height],
            [x, y + height],
        ],
    }


def test_parse_limit_and_quantity_dialog_text():
    assert parse_limit_text("每周限购 3/5") == ("weekly", 3, 5)
    assert parse_limit_text("每月限购0/10") == ("monthly", 0, 10)
    assert parse_limit_text("不限购") is None
    assert parse_quantity_text(" 1 / 5 ") == (1, 5)
    assert parse_quantity_text("售价 100") is None


def test_locator_disambiguates_duplicate_name_by_period_limit_and_price():
    catalog = load_shop_catalog()
    target = catalog.item("source_string_monthly_resume")
    ocr_items = [
        _ocr("本源之弦×1", 700, 310),
        _ocr("每月限购1/1", 790, 280, 105),
        _ocr("60", 810, 350, 45),
        _ocr("本源之弦×1", 990, 310),
        _ocr("每月限购4/4", 1110, 280, 105),
        _ocr("5000000", 1120, 350, 90),
    ]

    located = locate_product(ocr_items, target)

    assert located is not None
    assert located.center[0] < 923
    assert located.remaining == 1
    assert located.total == 1


def test_locator_does_not_accept_same_name_with_wrong_currency_price():
    catalog = load_shop_catalog()
    target = catalog.item("source_string_monthly_resume")
    ocr_items = [
        _ocr("本源之弦×1", 700, 310),
        _ocr("每月限购1/1", 790, 280, 105),
        _ocr("5000000", 810, 350, 90),
    ]

    assert locate_product(ocr_items, target) is None


def test_locator_accepts_missing_list_price_before_dialog_price_check():
    catalog = load_shop_catalog()
    target = catalog.item("nebula_8_monthly_resume")
    ocr_items = [
        _ocr("星云物质（8钛）", 1080, 480, 150),
        _ocr("每月限购500/500", 1100, 450, 140),
    ]

    located = locate_product(ocr_items, target)

    assert located is not None
    assert located.remaining == 500
    assert located.total == 500


def test_content_difference_only_uses_product_region():
    before = np.zeros((720, 1280, 3), dtype=np.uint8)
    after = before.copy()
    after[0:50, 0:50] = 255
    assert _content_difference(before, after) == 0

    after[200:250, 700:750] = 255
    assert _content_difference(before, after) > 0


def test_batch_toggle_and_quantity_dialog_safety_guards():
    disabled = np.zeros((720, 1280, 3), dtype=np.uint8)
    enabled = disabled.copy()
    enabled[110:125, 1223:1238] = 255
    dialog_image = disabled.copy()
    dialog_image[372:382, 445:465] = 255
    dialog_image[372:382, 817:837] = 255
    dialog_ocr = [
        _ocr("最少", 340, 355),
        _ocr("最多", 840, 355),
        _ocr("取消", 260, 515),
        _ocr("确定", 900, 515),
        _ocr("1/8", 600, 355),
    ]

    assert _batch_purchase_enabled(disabled) is False
    assert _batch_purchase_enabled(enabled) is True
    assert _has_quantity_dialog(dialog_ocr) is True
    assert _has_complete_quantity_dialog(dialog_image, dialog_ocr) is True
    assert _has_complete_quantity_dialog(disabled, dialog_ocr) is False
    assert _has_quantity_dialog(
        [
            _ocr("最多", 840, 355),
            _ocr("取消", 260, 515),
            _ocr("确定", 900, 515),
            _ocr("1/8", 600, 355),
        ]
    ) is False


def test_dialog_total_uses_observed_ocr_value():
    assert _dialog_price([_ocr("540000", 650, 440)]) == 540000


def test_catalog_probe_continues_after_all_known_items_until_stable_bottom(
    monkeypatch,
):
    catalog = load_shop_catalog()
    item = catalog.item("cactus_energy_weekly_iron")
    shop = catalog.shop(item.shop_id)

    class FakeImage:
        def __init__(self, matrix, ocr_items):
            self.image = matrix
            self._ocr_items = ocr_items

        def ocr(self):
            return self._ocr_items

    first = np.zeros((720, 1280, 3), dtype=np.uint8)
    bottom = first.copy()
    bottom[150:660, 580:1260] = 255
    found_ocr = [
        _ocr(item.name, 700, 310),
        _ocr("每周限购8/8", 790, 280),
        _ocr(str(item.price), 810, 350),
    ]
    frames = iter(
        [
            FakeImage(first, found_ocr),
            FakeImage(bottom, []),
            FakeImage(bottom, []),
            FakeImage(bottom, []),
        ]
    )
    calls = []
    monkeypatch.setattr(shop_purchase, "screenshot", lambda: next(frames))
    monkeypatch.setattr(shop_purchase, "input_swipe", lambda *args, **kwargs: calls.append(args))
    monkeypatch.setattr(shop_purchase.time, "sleep", lambda *_: None)
    adapter = HeadquartersBlackMoonAdapter(
        shop,
        ShopEvidenceRecorder(False, "test"),
    )

    result = adapter.scan(purchases=None)

    assert result["pages"] == 4
    assert result["reached_bottom"] is True
    # This fixture contains only one of the 23 catalog items, so reaching the
    # bottom succeeds as a termination proof but the overall probe is incomplete.
    assert result["success"] is False
    assert result["scan_page_limit"] == shop_purchase.MAX_SCAN_PAGES
    assert len(calls) == 3


class _FakeImage:
    def __init__(self, matrix=None, ocr_items=None):
        self.image = (
            np.zeros((720, 1280, 3), dtype=np.uint8)
            if matrix is None
            else matrix
        )
        self._ocr_items = list(ocr_items or [])

    def ocr(self):
        return list(self._ocr_items)


class _FakeRecorder:
    def capture(self, _label, _image=None, ocr_items=None):
        return list(ocr_items or [])


def test_dialog_price_is_authoritative_when_list_price_was_missing(monkeypatch):
    catalog = load_shop_catalog()
    item = catalog.item("cactus_energy_weekly_iron")
    shop = catalog.shop(item.shop_id)
    located = LocatedProduct(item, (760, 480), 8, 8, ())
    dialog_ocr = [
        _ocr(item.name, 570, 300, 180),
        _ocr("最少", 340, 355),
        _ocr("-1", 430, 355),
        _ocr("+1", 760, 355),
        _ocr("最多", 840, 355),
        _ocr("取消", 260, 515),
        _ocr("确定", 900, 515),
        _ocr("1/8", 600, 355),
        # Deliberately no price: list-card OCR may be absent, but the dialog
        # must never be confirmed without its authoritative price.
    ]
    taps = []
    dialog_matrix = np.zeros((720, 1280, 3), dtype=np.uint8)
    dialog_matrix[372:382, 445:465] = 255
    dialog_matrix[372:382, 817:837] = 255
    monkeypatch.setattr(
        shop_purchase,
        "screenshot",
        lambda: _FakeImage(dialog_matrix, dialog_ocr),
    )
    monkeypatch.setattr(shop_purchase, "input_tap", lambda pos: taps.append(pos))
    monkeypatch.setattr(shop_purchase.time, "sleep", lambda *_: None)
    adapter = HeadquartersBlackMoonAdapter(shop, _FakeRecorder())

    with pytest.raises(RuntimeError, match="商品价格校验失败"):
        adapter.inspect_dialog(located, "one")

    assert DIALOG_CANCEL_POS in taps
    assert DIALOG_CONFIRM_POS not in taps


def test_purchase_writes_ledger_before_confirmation_tap(monkeypatch):
    catalog = load_shop_catalog()
    item = catalog.item("cactus_energy_weekly_iron")
    shop = catalog.shop(item.shop_id)
    located = LocatedProduct(item, (760, 480), 8, 8, ())
    refreshed = LocatedProduct(item, (760, 480), 7, 8, ())
    events = []
    adapter = HeadquartersBlackMoonAdapter(shop, _FakeRecorder())
    monkeypatch.setattr(adapter, "inspect_dialog", lambda *_: (1, item.price))
    monkeypatch.setattr(
        shop_purchase,
        "record_shop_attempt",
        lambda *args, **kwargs: events.append("ledger") or {},
    )
    def dispatch(snapshot, confirmed_item):
        events.append("confirm")
        assert confirmed_item is item
        assert snapshot.item_id == item.id
        assert snapshot.quantity == 1
        assert snapshot.total_cost == item.price
        return object()

    monkeypatch.setattr(shop_purchase, "_dispatch_shop_confirm", dispatch)
    monkeypatch.setattr(shop_purchase, "screenshot", lambda: _FakeImage())
    monkeypatch.setattr(shop_purchase, "locate_product", lambda *_: refreshed)
    monkeypatch.setattr(
        shop_purchase,
        "update_shop_attempt",
        lambda *args, **kwargs: events.append("finalize") or {},
    )
    monkeypatch.setattr(shop_purchase.time, "sleep", lambda *_: None)

    result = adapter.purchase(located, "one", dry_run=False)

    assert result["status"] == "purchased"
    assert events.index("ledger") < events.index("confirm")
    assert events.count("confirm") == 1


def test_purchase_never_confirms_when_write_ahead_ledger_fails(monkeypatch):
    catalog = load_shop_catalog()
    item = catalog.item("cactus_energy_weekly_iron")
    shop = catalog.shop(item.shop_id)
    located = LocatedProduct(item, (760, 480), 8, 8, ())
    taps = []
    adapter = HeadquartersBlackMoonAdapter(shop, _FakeRecorder())
    monkeypatch.setattr(adapter, "inspect_dialog", lambda *_: (1, item.price))

    def fail_ledger(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(shop_purchase, "record_shop_attempt", fail_ledger)
    monkeypatch.setattr(shop_purchase, "input_tap", lambda pos: taps.append(pos))
    monkeypatch.setattr(shop_purchase.time, "sleep", lambda *_: None)

    with pytest.raises(OSError, match="disk full"):
        adapter.purchase(located, "one", dry_run=False)

    assert DIALOG_CANCEL_POS in taps
    assert DIALOG_CONFIRM_POS not in taps


def test_configured_scan_reports_explicit_page_limit(monkeypatch):
    catalog = load_shop_catalog()
    item = catalog.item("cactus_energy_weekly_iron")
    shop = catalog.shop(item.shop_id)
    purchase = ConfiguredPurchase(shop, item, "one")
    first = np.zeros((720, 1280, 3), dtype=np.uint8)
    second = first.copy()
    second[150:660, 580:1260] = 255
    frames = iter([_FakeImage(first), _FakeImage(second)])
    swipes = []
    monkeypatch.setattr(shop_purchase, "screenshot", lambda: next(frames))
    monkeypatch.setattr(shop_purchase, "input_swipe", lambda *args, **kwargs: swipes.append(args))
    monkeypatch.setattr(shop_purchase.time, "sleep", lambda *_: None)
    adapter = HeadquartersBlackMoonAdapter(shop, _FakeRecorder())

    result = adapter.scan([purchase], dry_run=True, max_pages=2)

    assert result["success"] is False
    assert result["page_limit_reached"] is True
    assert result["scan_page_limit"] == 2
    assert result["missing"] == [{"id": item.id, "name": item.name}]
    assert len(swipes) == 1


def test_mixed_period_run_skips_only_the_item_locked_for_its_cycle(monkeypatch):
    catalog = load_shop_catalog()
    daily = catalog.item("self_observation_daily_iron")
    weekly = catalog.item("black_moon_ticket_weekly_birch")
    plan = default_shop_plan(catalog)
    plan["enabled"] = True
    for item in (daily, weekly):
        plan["shops"][item.shop_id]["items"][item.id]["enabled"] = True
    scanned_ids = []

    class FakeAdapter:
        def __init__(self, _shop, _recorder):
            pass

        def open(self):
            pass

        def scan(self, purchases, dry_run=False):
            scanned_ids.extend(purchase.item.id for purchase in purchases)
            return {"success": True, "requires_attention": False}

    monkeypatch.setattr(shop_purchase, "load_shop_catalog", lambda: catalog)
    monkeypatch.setattr(shop_purchase, "load_shop_plan", lambda **_kwargs: plan)
    monkeypatch.setattr(
        shop_purchase,
        "active_shop_attempt",
        lambda item: (
            {"status": "purchased", "blocked_until": "2026-07-20T05:00:00"}
            if item.id == weekly.id
            else None
        ),
    )
    monkeypatch.setitem(shop_purchase.ADAPTERS, "headquarters_black_moon", FakeAdapter)
    monkeypatch.setattr(shop_purchase, "_connected_run", lambda callback: callback())

    result = shop_purchase.run_shop_purchase()

    assert result["success"] is True
    assert scanned_ids == [daily.id]
    assert [entry["id"] for entry in result["blocked_by_period"]] == [weekly.id]


def test_connected_run_falls_back_once_and_always_releases(monkeypatch):
    events = []
    monkeypatch.setattr(
        shop_purchase,
        "connect_adb",
        lambda: events.append("adb") or False,
    )
    monkeypatch.setattr(
        shop_purchase,
        "connect",
        lambda: events.append("fallback") or True,
    )
    monkeypatch.setattr(shop_purchase, "kill", lambda: events.append("kill"))

    result = shop_purchase._connected_run(
        lambda: events.append("callback") or {"transport": "untrusted"}
    )

    assert result["transport"] == "nemu_ipc"
    assert events == ["adb", "fallback", "callback", "kill"]


def test_connected_run_never_retries_callback_after_task_failure(monkeypatch):
    events = []
    monkeypatch.setattr(
        shop_purchase,
        "connect_adb",
        lambda: events.append("adb") or True,
    )
    monkeypatch.setattr(
        shop_purchase,
        "connect",
        lambda: events.append("unexpected-fallback") or True,
    )
    monkeypatch.setattr(shop_purchase, "kill", lambda: events.append("kill"))

    def fail_after_connect():
        events.append("callback")
        raise RuntimeError("task failed")

    with pytest.raises(RuntimeError, match="task failed"):
        shop_purchase._connected_run(fail_after_connect)

    assert events == ["adb", "callback", "kill"]
