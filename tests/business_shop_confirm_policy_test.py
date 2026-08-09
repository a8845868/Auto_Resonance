"""Regression contracts for the narrowly scoped shop-confirm business session."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pytest

import auto.shop_purchase as shop_purchase
import core.control.control as control_module
from core.services.business_action_policy import (
    BusinessActionSnapshot,
    ProductionBusinessActionSession,
)
from core.services.dispatch_outcome import DispatchStatus
from core.services.read_only_policy import (
    ActionIntent,
    BoundDeviceIdentity,
    CalibratedStaticRegion,
    DisplayGeometry,
    PageObservation,
    PageObserver,
    installed_read_only_guard,
)
from core.services.shop_catalog import (
    active_shop_attempt,
    load_shop_catalog,
    load_shop_plan,
    shop_attempt_digest,
    shop_catalog_digest,
    shop_plan_digest,
)


NOW = datetime(2026, 8, 9, 12, tzinfo=timezone.utc)


class _Backend:
    ratio = 1.0
    dsize = (1280, 720)

    def __init__(self):
        self.taps: list[tuple[int, int]] = []

    def input_tap(self, x, y):
        self.taps.append((x, y))

    def input_swipe(self, *_args):
        raise AssertionError("business shop confirmation never swipes")


def _identity():
    return BoundDeviceIdentity(
        emulator_backend="_Backend", instance_id="0", adb_serial="test-adb-0",
        backend_generation=1, backend_object_identity="_Backend:test",
        display_geometry_revision=DisplayGeometry().geometry_revision,
        connected_at=NOW,
    )


def _observation(sequence: int, *, page="shop_quantity_dialog"):
    source = page == "shop_quantity_dialog"
    markers = (
        "shop_quantity_dialog", "shop_item=item-a", "shop_quantity=1",
        "shop_total_cost=40", "shop_currency=iron",
    ) if source else ("shop_confirmation_resolved",)
    regions = (
        CalibratedStaticRegion(
            anchor_id="shop_confirm_button", bbox=(800, 480, 1120, 590),
            page_classifier="shop_quantity_dialog", allowed_action="shop_confirm",
            postcondition="shop_confirmation_resolved",
            geometry_revision=DisplayGeometry().geometry_revision,
        ),
    ) if source else ()
    return PageObservation(
        observation_id=f"shop-{sequence}", screenshot_hash=f"{sequence:064x}",
        page_type=page, markers=markers, anchors=(),
        captured_at=NOW - timedelta(seconds=1) + timedelta(microseconds=sequence),
        display_geometry=DisplayGeometry(), static_regions=regions,
        source_capture_id=f"capture-{sequence}", source_monotonic_sequence=sequence,
        backend_generation=1, instance_id="0", adb_serial="test-adb-0",
    )


def _snapshot():
    return BusinessActionSnapshot(
        item_id="item-a", shop_id="shop-a", currency="iron", quantity_mode="one",
        quantity=1, total_cost=40, catalog_digest="catalog", plan_digest="plan",
        ledger_digest="ledger",
    )


def _production_session(monkeypatch, observations, validator=lambda _obs, _snap: None):
    backend = _Backend()
    identity = _identity()
    monkeypatch.setattr(control_module, "control", backend)
    monkeypatch.setattr(control_module, "current_bound_device_identity", lambda: identity)
    session = control_module._create_production_business_action_session(
        PageObserver(lambda: next(observations)), snapshot=_snapshot(),
        context_validator=validator, now=lambda: NOW,
    )
    return backend, session


def test_business_session_dispatches_only_one_verified_confirm_without_offset(monkeypatch):
    backend, session = _production_session(
        monkeypatch, iter((_observation(1), _observation(2), _observation(3, page="shop_purchase_result"))),
    )
    with installed_read_only_guard(session):
        outcome = control_module.input_tap(
            (960, 535), random_offset=False,
            intent=ActionIntent("shop_confirm", "shop_confirm_button", "item-a"),
        )
    assert outcome
    assert outcome.status is DispatchStatus.DISPATCHED_VERIFIED
    assert backend.taps == [(960, 535)]


def test_business_context_denial_is_literal_false_and_sends_no_input(monkeypatch):
    backend, session = _production_session(
        monkeypatch, iter((_observation(1),)),
        validator=lambda _obs, _snap: (_ for _ in ()).throw(PermissionError("plan changed")),
    )
    with installed_read_only_guard(session):
        result = control_module.input_tap(
            (960, 535), random_offset=False,
            intent=ActionIntent("shop_confirm", "shop_confirm_button", "item-a"),
        )
    assert result is False
    assert backend.taps == []
    assert session.journal[-2].stage == "ISSUE_DENIED"


def test_direct_business_session_is_not_a_production_authority():
    session = ProductionBusinessActionSession(lambda _point: None)
    with pytest.raises((TypeError, PermissionError), match="production|provenance"):
        control_module.activate_action_policy(session)


def test_business_session_allow_list_rejects_unmodelled_business_action(monkeypatch):
    backend, session = _production_session(monkeypatch, iter(()))
    with installed_read_only_guard(session):
        result = control_module.input_tap(
            (960, 535), random_offset=False,
            intent=ActionIntent("reward_claim", "reward_claim", "item-a"),
        )
    assert result is False
    assert backend.taps == []


def test_business_postcondition_failure_is_truthy_and_never_repeats_confirm(monkeypatch):
    backend, session = _production_session(
        monkeypatch,
        iter((_observation(1), _observation(2), _observation(3), _observation(4), _observation(5))),
    )
    with installed_read_only_guard(session):
        outcome = control_module.input_tap(
            (960, 535), random_offset=False,
            intent=ActionIntent("shop_confirm", "shop_confirm_button", "item-a"),
        )
    assert outcome
    assert outcome.status is DispatchStatus.DISPATCHED_UNVERIFIED
    assert backend.taps == [(960, 535)]


def test_shop_context_rejects_changed_plan_or_write_ahead_entry(monkeypatch):
    catalog = load_shop_catalog()
    item = catalog.item("cactus_energy_weekly_iron")
    plan = load_shop_plan(catalog=catalog)
    plan["enabled"] = True
    plan["shops"][item.shop_id]["enabled"] = True
    plan["shops"][item.shop_id]["items"][item.id] = {"enabled": True, "quantity": "one"}
    entry = {"status": "prepared", "quantity": 1, "cost": item.price}
    snapshot = BusinessActionSnapshot(
        item_id=item.id, shop_id=item.shop_id, currency=item.currency, quantity_mode="one",
        quantity=1, total_cost=item.price, catalog_digest=shop_catalog_digest(catalog),
        plan_digest=shop_plan_digest(plan, catalog), ledger_digest=shop_attempt_digest(entry),
    )
    observation = _observation(1)
    observation = PageObservation(
        **{**observation.__dict__, "markers": (
            "shop_quantity_dialog", f"shop_item={item.id}", "shop_quantity=1",
            f"shop_total_cost={item.price}", f"shop_currency={item.currency}",
        )}
    )
    monkeypatch.setattr(shop_purchase, "load_shop_catalog", lambda: catalog)
    monkeypatch.setattr(shop_purchase, "load_shop_plan", lambda **_kwargs: plan)
    monkeypatch.setattr(shop_purchase, "active_shop_attempt", lambda _item: entry)
    shop_purchase._validate_shop_confirm_context(observation, snapshot)
    entry["cost"] += 1
    with pytest.raises(PermissionError, match="write-ahead"):
        shop_purchase._validate_shop_confirm_context(observation, snapshot)


def test_shop_dispatch_helper_installs_business_guard_and_disables_random_offset(monkeypatch):
    item = load_shop_catalog().item("cactus_energy_weekly_iron")
    seen = {}

    class _Guard:
        pass

    def fake_factory(observer, *, snapshot, context_validator):
        seen["observer"] = observer
        seen["snapshot"] = snapshot
        seen["validator"] = context_validator
        return _Guard()

    class _Context:
        def __enter__(self):
            return None

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(shop_purchase, "_create_production_business_action_session", fake_factory)
    monkeypatch.setattr(shop_purchase, "installed_read_only_guard", lambda _guard: _Context())
    monkeypatch.setattr(shop_purchase, "input_tap", lambda point, **kwargs: seen.update(point=point, **kwargs) or object())
    outcome = shop_purchase._dispatch_shop_confirm(_snapshot(), item)
    assert outcome
    assert seen["point"] == shop_purchase.DIALOG_CONFIRM_POS
    assert seen["random_offset"] is False
    assert seen["intent"].action_key == "shop_confirm"


def test_price_icon_slot_present_with_complete_layout(monkeypatch):
    """Icon slot detected when '售价', price number, and visible gap content
    are all present — matching the real dialog layout."""
    # Dark dialog background with a brighter circular icon in the gap.
    frame = np.full((720, 1280, 3), 28, dtype=np.uint8)
    gap_y, gap_x = 440, 590
    rr, cc = np.ogrid[:34, :48]
    mask = (rr - 16) ** 2 + (cc - 17) ** 2 <= 144
    icon_overlay = np.minimum(frame[gap_y:gap_y + 34, gap_x:gap_x + 48] + 35, 255)
    icon_overlay[mask] = np.clip(icon_overlay[mask] + 20, 0, 255)
    frame[gap_y:gap_y + 34, gap_x:gap_x + 48] = icon_overlay

    ocr_items = [
        {"text": "售价", "position": [
            [535, 441], [579, 441], [579, 466], [535, 466]
        ]},
        {"text": "100000", "position": [
            [648, 442], [721, 442], [721, 465], [648, 465]
        ]},
    ]
    assert shop_purchase._price_icon_slot_present(frame, ocr_items) is True


def test_price_icon_slot_absent_without_price_label(monkeypatch):
    """Returns False when no '售价' OCR item exists."""
    frame = np.zeros((720, 1280, 3), dtype=np.uint8)
    ocr_items = [
        {"text": "100000", "position": [
            [648, 442], [721, 442], [721, 465], [648, 465]
        ]},
        {"text": "确定", "position": [
            [900, 515], [980, 515], [980, 535], [900, 535]
        ]},
    ]
    assert shop_purchase._price_icon_slot_present(frame, ocr_items) is False


def test_price_icon_slot_absent_when_gap_is_uniform_background(monkeypatch):
    """Returns False when the gap between label and price is uniform dark bg."""
    frame = np.full((720, 1280, 3), 28, dtype=np.uint8)
    # Deliberately blank gap — no icon content.
    ocr_items = [
        {"text": "售价", "position": [
            [535, 441], [579, 441], [579, 466], [535, 466]
        ]},
        {"text": "100000", "position": [
            [648, 442], [721, 442], [721, 465], [648, 465]
        ]},
    ]
    assert shop_purchase._price_icon_slot_present(frame, ocr_items) is False
