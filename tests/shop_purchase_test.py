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
from core.services.read_only_policy import DEFAULT_POLICY_SPECS


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


def test_dialog_price_uses_located_remaining_tier_for_weekly_laplace(monkeypatch):
    catalog = load_shop_catalog()
    item = catalog.item("laplace_weekly_iron")
    shop = catalog.shop(item.shop_id)
    located = LocatedProduct(item, (1182, 203), 2, 3, ())
    dialog_ocr = [
        _ocr(item.name, 580, 306, 121),
        _ocr("最少", 365, 363),
        _ocr("-1", 430, 355),
        _ocr("+1", 760, 355),
        _ocr("最多", 873, 367),
        _ocr("售价", 536, 442),
        _ocr("200000", 647, 442, 74),
        _ocr("取消", 324, 521),
        _ocr("确定", 959, 523),
        _ocr("1/2", 619, 353),
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

    quantity, observed_total = adapter.inspect_dialog(located, "one")

    assert quantity == 1
    assert observed_total == 200000
    assert taps == [located.center]


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


def test_purchase_dismisses_reward_overlay_before_verifying_remaining(
    monkeypatch,
):
    """After confirmation, the reward overlay ('获得物品') covers the shop
    card.  One safe blank-area tap dismisses it; the revealed card is then
    used for the remaining-count check."""
    catalog = load_shop_catalog()
    item = catalog.item("cactus_energy_weekly_iron")
    shop = catalog.shop(item.shop_id)
    located = LocatedProduct(item, (760, 480), 8, 8, ())
    refreshed = LocatedProduct(item, (760, 480), 7, 8, ())
    adapter = HeadquartersBlackMoonAdapter(shop, _FakeRecorder())
    monkeypatch.setattr(adapter, "inspect_dialog", lambda *_: (1, item.price))
    monkeypatch.setattr(
        shop_purchase,
        "record_shop_attempt",
        lambda *args, **kwargs: {},
    )
    monkeypatch.setattr(shop_purchase, "_dispatch_shop_confirm", lambda *args: object())

    # First screenshot: the reward overlay.
    overlay_ocr = [
        {"text": "获得物品", "position": [
            [591, 185], [759, 185], [759, 236], [591, 236]
        ]},
        {"text": "触碰空白区域退出", "position": [
            [603, 674], [712, 674], [712, 692], [603, 692]
        ]},
    ]
    # Second screenshot: the revealed shop card with the new 2/8 limit.
    card_ocr = [
        {"text": item.name, "position": [
            [700, 310], [825, 310], [825, 335], [700, 335]
        ]},
        {"text": "每周限购2/8", "position": [
            [790, 280], [925, 280], [925, 300], [790, 300]
        ]},
        {"text": str(item.price), "position": [
            [810, 350], [875, 350], [875, 370], [810, 370]
        ]},
    ]
    frames = iter([
        _FakeImage(ocr_items=overlay_ocr),
        _FakeImage(ocr_items=card_ocr),
    ])
    taps = []

    class _FakeSafetyMap:
        def __init__(self, candidates):
            self.candidates = candidates

    class _FakeSelector:
        def __init__(self, *args, **kwargs):
            pass

        def select(self, *_args, **_kwargs):
            from core.services.announcement_overlay_handler import SafeBlankRegion
            return _FakeSafetyMap([
                SafeBlankRegion(
                    bbox=(0, 200, 500, 700), point=(180, 420),
                    area=5000, edge_density=0.0,
                ),
            ])

    monkeypatch.setattr(shop_purchase, "AnnouncementSafeRegionSelector", _FakeSelector)
    monkeypatch.setattr(shop_purchase, "screenshot", lambda: next(frames))
    monkeypatch.setattr(shop_purchase.time, "sleep", lambda *_: None)
    monkeypatch.setattr(shop_purchase, "locate_product", lambda *_: refreshed)
    monkeypatch.setattr(
        shop_purchase,
        "update_shop_attempt",
        lambda *args, **kwargs: {},
    )

    def _tap(pos, random_offset=True, **kwargs):
        taps.append((pos, random_offset, kwargs.get("intent")))

    monkeypatch.setattr(shop_purchase, "input_tap", _tap)

    result = adapter.purchase(located, "one", dry_run=False)

    assert result["status"] == "purchased"
    assert result["remaining_after"] == 7
    # One safe dismiss tap from selector candidate, random_offset=False.
    assert len(taps) == 1
    assert taps[0][0] == (180, 420)
    assert taps[0][1] is False  # random_offset
    dismiss_intent = taps[0][2]
    assert dismiss_intent is not None
    assert dismiss_intent.action_key == "dialog_cancel"
    assert dismiss_intent.requested_target == "shop_result_overlay_dismiss"
    assert DIALOG_CONFIRM_POS not in [t[0] for t in taps]


def test_purchase_overlay_no_safe_point_produces_submitted_unverified(
    monkeypatch,
):
    """When the safe-region selector finds no blank region, the result is
    ``submitted_unverified`` and no dismissal tap is sent."""
    catalog = load_shop_catalog()
    item = catalog.item("cactus_energy_weekly_iron")
    shop = catalog.shop(item.shop_id)
    located = LocatedProduct(item, (760, 480), 8, 8, ())
    adapter = HeadquartersBlackMoonAdapter(shop, _FakeRecorder())
    monkeypatch.setattr(adapter, "inspect_dialog", lambda *_: (1, item.price))
    monkeypatch.setattr(shop_purchase, "record_shop_attempt", lambda *a, **kw: {})
    monkeypatch.setattr(shop_purchase, "_dispatch_shop_confirm", lambda *a: object())

    overlay_ocr = [
        {"text": "获得物品", "position": [
            [591, 185], [759, 185], [759, 236], [591, 236]
        ]},
        {"text": "触碰空白区域退出", "position": [
            [603, 674], [712, 674], [712, 692], [603, 692]
        ]},
    ]
    taps = []

    class _EmptySafetyMap:
        candidates = ()

    class _EmptySelector:
        def __init__(self, *args, **kwargs):
            pass

        def select(self, *_args, **_kwargs):
            return _EmptySafetyMap()

    monkeypatch.setattr(shop_purchase, "AnnouncementSafeRegionSelector", _EmptySelector)
    monkeypatch.setattr(shop_purchase, "screenshot", lambda: _FakeImage(ocr_items=overlay_ocr))
    monkeypatch.setattr(shop_purchase.time, "sleep", lambda *_: None)
    monkeypatch.setattr(shop_purchase, "input_tap", lambda *a, **kw: taps.append(a))
    monkeypatch.setattr(shop_purchase, "locate_product", lambda *a: None)
    monkeypatch.setattr(shop_purchase, "update_shop_attempt", lambda *a, **kw: {})

    result = adapter.purchase(located, "one", dry_run=False)

    assert result["status"] == "submitted_unverified"
    assert result["remaining_after"] is None
    assert taps == []


def test_purchase_overlay_stop_execution_after_dismiss_tap_is_propagated(
    monkeypatch,
):
    """StopExecution raised during the post-dismiss snapshot must propagate
    uncaught."""
    catalog = load_shop_catalog()
    item = catalog.item("cactus_energy_weekly_iron")
    shop = catalog.shop(item.shop_id)
    located = LocatedProduct(item, (760, 480), 8, 8, ())
    adapter = HeadquartersBlackMoonAdapter(shop, _FakeRecorder())
    monkeypatch.setattr(adapter, "inspect_dialog", lambda *_: (1, item.price))
    monkeypatch.setattr(shop_purchase, "record_shop_attempt", lambda *a, **kw: {})
    monkeypatch.setattr(shop_purchase, "_dispatch_shop_confirm", lambda *a: object())

    overlay_ocr = [
        {"text": "获得物品", "position": [
            [591, 185], [759, 185], [759, 236], [591, 236]
        ]},
        {"text": "触碰空白区域退出", "position": [
            [603, 674], [712, 674], [712, 692], [603, 692]
        ]},
    ]

    class _FakeSafetyMap:
        def __init__(self, candidates):
            self.candidates = candidates

    class _FakeSelector:
        def __init__(self, *args, **kwargs):
            pass

        def select(self, *_args, **_kwargs):
            from core.services.announcement_overlay_handler import SafeBlankRegion
            return _FakeSafetyMap([
                SafeBlankRegion(
                    bbox=(0, 200, 500, 700), point=(180, 420),
                    area=5000, edge_density=0.0,
                ),
            ])

    from core.exception.exceptions import StopExecution

    screenshots = 0
    finalize_calls = []

    def _fail_second():
        nonlocal screenshots
        screenshots += 1
        if screenshots == 1:
            return _FakeImage(ocr_items=overlay_ocr)
        raise StopExecution()

    monkeypatch.setattr(shop_purchase, "screenshot", _fail_second)
    monkeypatch.setattr(shop_purchase.time, "sleep", lambda *_: None)
    monkeypatch.setattr(shop_purchase, "input_tap", lambda *a, **kw: object())
    monkeypatch.setattr(shop_purchase, "AnnouncementSafeRegionSelector", _FakeSelector)
    monkeypatch.setattr(shop_purchase, "locate_product", lambda *a: None)
    monkeypatch.setattr(
        shop_purchase, "update_shop_attempt", lambda *a: finalize_calls.append(a)
    )

    # Once the overlay is captured, raise StopExecution on the next screenshot.
    with pytest.raises(StopExecution):
        adapter.purchase(located, "one", dry_run=False)

    # The write-ahead ledger must be closed before re-raising.
    assert len(finalize_calls) == 1
    assert finalize_calls[0][0] == item.id
    assert finalize_calls[0][1] == "submitted_unverified"


def test_purchase_overlay_dismiss_denied_returns_submitted_unverified_without_screenshot(
    monkeypatch,
):
    """When the dismiss tap returns literal False (hardware unreachable or
    policy blocked), the result is ``submitted_unverified`` immediately —
    no sleep, no second screenshot."""
    catalog = load_shop_catalog()
    item = catalog.item("cactus_energy_weekly_iron")
    shop = catalog.shop(item.shop_id)
    located = LocatedProduct(item, (760, 480), 8, 8, ())
    adapter = HeadquartersBlackMoonAdapter(shop, _FakeRecorder())
    monkeypatch.setattr(adapter, "inspect_dialog", lambda *_: (1, item.price))
    monkeypatch.setattr(shop_purchase, "record_shop_attempt", lambda *a, **kw: {})
    monkeypatch.setattr(shop_purchase, "_dispatch_shop_confirm", lambda *a: object())

    overlay_ocr = [
        {"text": "获得物品", "position": [
            [591, 185], [759, 185], [759, 236], [591, 236]
        ]},
        {"text": "触碰空白区域退出", "position": [
            [603, 674], [712, 674], [712, 692], [603, 692]
        ]},
    ]
    taps = []
    screenshot_calls = []

    class _FakeSafetyMap:
        def __init__(self, candidates):
            self.candidates = candidates

    class _FakeSelector:
        def __init__(self, *args, **kwargs):
            pass

        def select(self, *_args, **_kwargs):
            from core.services.announcement_overlay_handler import SafeBlankRegion
            return _FakeSafetyMap([
                SafeBlankRegion(
                    bbox=(0, 200, 500, 700), point=(180, 420),
                    area=5000, edge_density=0.0,
                ),
            ])

    monkeypatch.setattr(shop_purchase, "AnnouncementSafeRegionSelector", _FakeSelector)
    # First call: overlay.  Must NOT be called again after dismiss denial.
    monkeypatch.setattr(shop_purchase, "screenshot",
                        lambda: (screenshot_calls.append(1), _FakeImage(ocr_items=overlay_ocr))[1])
    monkeypatch.setattr(shop_purchase.time, "sleep", lambda *_: None)
    monkeypatch.setattr(shop_purchase, "locate_product", lambda *a: None)
    monkeypatch.setattr(shop_purchase, "update_shop_attempt", lambda *a, **kw: {})

    def _tap(pos, random_offset=True, **kwargs):
        taps.append((pos, random_offset, kwargs.get("intent")))
        return False  # hardware unreachable

    monkeypatch.setattr(shop_purchase, "input_tap", _tap)

    result = adapter.purchase(located, "one", dry_run=False)

    assert result["status"] == "submitted_unverified"
    assert result["remaining_after"] is None
    assert "覆盖层退出点击被拒绝" in result["verification_error"]
    # Exactly one tap (dismiss), no extra screenshot.
    assert len(taps) == 1
    assert len(screenshot_calls) == 1


def test_purchase_overlay_re_screenshot_failure_produces_submitted_unverified(
    monkeypatch,
):
    """When the post-dismiss re-screenshot raises an exception (not
    StopExecution), the result is ``submitted_unverified``."""
    catalog = load_shop_catalog()
    item = catalog.item("cactus_energy_weekly_iron")
    shop = catalog.shop(item.shop_id)
    located = LocatedProduct(item, (760, 480), 8, 8, ())
    adapter = HeadquartersBlackMoonAdapter(shop, _FakeRecorder())
    monkeypatch.setattr(adapter, "inspect_dialog", lambda *_: (1, item.price))
    monkeypatch.setattr(shop_purchase, "record_shop_attempt", lambda *a, **kw: {})
    monkeypatch.setattr(shop_purchase, "_dispatch_shop_confirm", lambda *a: object())

    overlay_ocr = [
        {"text": "获得物品", "position": [
            [591, 185], [759, 185], [759, 236], [591, 236]
        ]},
        {"text": "触碰空白区域退出", "position": [
            [603, 674], [712, 674], [712, 692], [603, 692]
        ]},
    ]

    class _FakeSafetyMap:
        def __init__(self, candidates):
            self.candidates = candidates

    class _FakeSelector:
        def __init__(self, *args, **kwargs):
            pass

        def select(self, *_args, **_kwargs):
            from core.services.announcement_overlay_handler import SafeBlankRegion
            return _FakeSafetyMap([
                SafeBlankRegion(
                    bbox=(0, 200, 500, 700), point=(180, 420),
                    area=5000, edge_density=0.0,
                ),
            ])

    calls = {"screenshot": 0}
    recorder_labels = []

    class _LabelTrackingRecorder:
        def capture(self, label, _image=None, ocr_items=None):
            recorder_labels.append(label)
            return list(ocr_items or [])

    adapter = HeadquartersBlackMoonAdapter(shop, _LabelTrackingRecorder())
    monkeypatch.setattr(adapter, "inspect_dialog", lambda *_: (1, item.price))

    def _fail_second_screenshot():
        calls["screenshot"] += 1
        if calls["screenshot"] == 1:
            return _FakeImage(ocr_items=overlay_ocr)
        raise OSError("NEMU disconnected")

    monkeypatch.setattr(shop_purchase, "screenshot", _fail_second_screenshot)
    monkeypatch.setattr(shop_purchase.time, "sleep", lambda *_: None)
    monkeypatch.setattr(shop_purchase, "input_tap", lambda *a, **kw: None)
    monkeypatch.setattr(shop_purchase, "AnnouncementSafeRegionSelector", _FakeSelector)
    monkeypatch.setattr(shop_purchase, "locate_product", lambda *a: None)
    monkeypatch.setattr(shop_purchase, "update_shop_attempt", lambda *a, **kw: {})

    result = adapter.purchase(located, "one", dry_run=False)

    assert result["status"] == "submitted_unverified"
    assert result["remaining_after"] is None
    assert "OSError" in result["verification_error"]
    assert "NEMU disconnected" in result["verification_error"]
    # Must not record a "dismissed" label when re-screenshot failed.
    assert "purchase-result-dismissed-" not in " ".join(recorder_labels)


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


def test_locator_selects_weekly_tier_price_for_laplace_with_remaining_two():
    """Right-card weekly laplace (remaining 2/3, tier price 200000) is
    accepted; the left monthly laplace (1/1 at 100000) is a different period
    and must not shadow it."""
    catalog = load_shop_catalog()
    target = catalog.item("laplace_weekly_iron")
    ocr_items = [
        # Left card: monthly, full 10/10, base price
        _ocr("拉普拉斯协议", 700, 310),
        _ocr("每月限购10/10", 790, 280, 105),
        _ocr("100000", 810, 350, 90),
        # Right card: weekly, remaining 2/3, tier price 200000
        _ocr("拉普拉斯协议", 1125, 310),
        _ocr("每周限购2/3", 1153, 280, 105),
        _ocr("200000", 1163, 350, 90),
    ]

    located = locate_product(ocr_items, target)

    assert located is not None
    assert located.remaining == 2
    assert located.total == 3
    assert located.center[0] > 923


def test_locator_rejects_laplace_when_remaining_tier_price_is_unproven():
    """Weekly laplace at remaining 1/3 has no proven price tier in the
    catalog; the locator must fail closed rather than guess."""
    catalog = load_shop_catalog()
    target = catalog.item("laplace_weekly_iron")
    ocr_items = [
        _ocr("拉普拉斯协议", 1125, 310),
        _ocr("每周限购1/3", 1153, 280, 105),
        _ocr("300000", 1163, 350, 90),
    ]

    assert locate_product(ocr_items, target) is None


def test_locator_rejects_laplace_when_price_conflicts_with_tier():
    """Weekly laplace at remaining 2/3 showing the wrong price (e.g. the
    base 100000 instead of the tier 200000) must fail closed."""
    catalog = load_shop_catalog()
    target = catalog.item("laplace_weekly_iron")
    ocr_items = [
        _ocr("拉普拉斯协议", 1125, 310),
        _ocr("每周限购2/3", 1153, 280, 105),
        _ocr("100000", 1163, 350, 90),
    ]

    assert locate_product(ocr_items, target) is None


def test_shop_swipe_intents_are_present_on_rewind_and_scroll(monkeypatch):
    """Both swipe calls carry a distinct ActionIntent so the shadow journal
    never records them as NO_INTENT."""
    catalog = load_shop_catalog()
    item = catalog.item("cactus_energy_weekly_iron")
    shop = catalog.shop(item.shop_id)
    swipes = []
    monkeypatch.setattr(
        shop_purchase,
        "input_swipe",
        lambda *args, **kwargs: swipes.append(kwargs.get("intent")),
    )
    monkeypatch.setattr(shop_purchase.time, "sleep", lambda *_: None)
    monkeypatch.setattr(
        shop_purchase,
        "screenshot",
        lambda: _FakeImage(np.zeros((720, 1280, 3), dtype=np.uint8)),
    )
    adapter = HeadquartersBlackMoonAdapter(shop, _FakeRecorder())

    adapter._rewind_to_top()

    assert swipes
    assert all(intent is not None for intent in swipes)
    assert swipes[0].action_key == "shop_catalog_rewind"

    # scan() calls the scroll swipe when there are pending items.
    swipes.clear()
    purchase = ConfiguredPurchase(shop, item, "one")
    frames = iter([_FakeImage(np.zeros((720, 1280, 3), dtype=np.uint8))] * 4)
    monkeypatch.setattr(shop_purchase, "screenshot", lambda: next(frames))

    adapter.scan([purchase], dry_run=True, max_pages=2)

    assert swipes
    assert all(intent is not None for intent in swipes)
    assert swipes[0].action_key == "shop_catalog_scroll"


def test_shop_swipe_specs_have_correct_directions_and_resolvable_anchor():
    """The two new specs must survive the real permit issuer: correct swipe
    direction for each action and a resolvable calibrated anchor."""
    from core.services.read_only_policy import (
        ActionIntent,
        AnchorResolver,
        PageObservation,
        ReadOnlyPermitIssuer,
        DisplayGeometry,
        CalibratedStaticRegion,
    )
    from datetime import datetime, timezone

    specs = {
        "shop_catalog_rewind": "DOWN",
        "shop_catalog_scroll": "UP",
    }
    for action_key, expected_direction in specs.items():
        spec = DEFAULT_POLICY_SPECS[action_key]
        assert spec.allowed_swipe_directions == frozenset({expected_direction})
        assert spec.action_kind.value == "SWIPE"
        assert spec.allowed_page_types == frozenset({"shop"})
        assert spec.allowed_region == (580, 150, 1260, 660)
        assert spec.postcondition == "shop_catalog_remains_safe"

    geometry = DisplayGeometry()
    region = CalibratedStaticRegion(
        anchor_id="shop_catalog_content", bbox=(580, 150, 1260, 660),
        page_classifier="shop", allowed_action="shop_catalog_scroll",
        postcondition="shop_catalog_remains_safe",
        geometry_revision=geometry.geometry_revision,
    )
    now = datetime.now(timezone.utc)
    observation = PageObservation(
        observation_id="obs-1", screenshot_hash="a" * 64, page_type="shop",
        markers=("shop_catalog_content",), anchors=(), captured_at=now,
        display_geometry=geometry, static_regions=(region,),
        source_capture_id="capture-1", source_monotonic_sequence=1,
        backend_generation=1, instance_id="0", adb_serial="test-adb-0",
    )
    resolver = AnchorResolver()
    anchor, source = resolver.resolve_with_source(
        observation, "shop_catalog_content",
        action_key="shop_catalog_scroll",
        postcondition="shop_catalog_remains_safe",
    )
    assert anchor.anchor_id == "shop_catalog_content"
    assert source == "CALIBRATED_STATIC"

    # The rewind trajectory (585→365, dy=-220, UP) must be rejected by the
    # scroll spec and accepted by the rewind spec — proving direction is real.
    scroll_spec = DEFAULT_POLICY_SPECS["shop_catalog_scroll"]
    rewind_spec = DEFAULT_POLICY_SPECS["shop_catalog_rewind"]
    # scroll trajectory: START=(800,585) END=(800,365) → UP ✓
    # rewind trajectory: START=(800,365) END=(800,585) → DOWN ✓
    scroll_trajectory = ((800, 585), (800, 365))
    rewind_trajectory = ((800, 365), (800, 585))
    from core.services.read_only_policy import ReadOnlyPermitIssuer
    axis, direction = ReadOnlyPermitIssuer._validate_modality(
        scroll_spec, scroll_trajectory, 650
    )
    assert direction == "UP"
    axis, direction = ReadOnlyPermitIssuer._validate_modality(
        rewind_spec, rewind_trajectory, 650
    )
    assert direction == "DOWN"
