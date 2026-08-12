"""Recurring shop purchases with pluggable shop adapters.

The first adapter targets Headquarters -> Black Moon Shop.  It intentionally
uses the non-batch purchase flow so every configured item gets an explicit
quantity dialog and OCR validation before the final confirmation.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable

import cv2 as cv
import numpy as np
from loguru import logger

from core.control.control import (
    _create_production_business_action_session,
    connect,
    connect_adb,
    current_bound_device_identity,
    current_display_geometry,
    input_swipe,
    input_tap,
    kill,
    screenshot,
)
from core.exception.exceptions import StopExecution
from core.preset.control import go_home
from core.services.runtime_errors import BlockedBySafetyError
from core.services.shop_catalog import (
    ConfiguredPurchase,
    ReadOnlyShopCatalog,
    ReadOnlyShopItem,
    ShopAttemptAlreadyActive,
    ShopDefinition,
    ShopItem,
    active_shop_attempt,
    configured_purchases,
    load_shop_catalog,
    load_shop_plan,
    load_read_only_shop_catalog,
    record_shop_attempt,
    shop_attempt_digest,
    shop_catalog_digest,
    shop_plan_digest,
    update_shop_attempt,
)
from core.services.announcement_overlay_handler import AnnouncementSafeRegionSelector
from core.services.business_action_policy import BusinessActionSnapshot
from core.services.read_only_policy import (
    ActionIntent,
    CalibratedStaticRegion,
    PageObservation,
    PageObserver,
    installed_read_only_guard,
)


SHOP_ENTRY_POS = (260, 30)
HEADQUARTERS_TAB_POS = (858, 40)
BUREAU_TAB_POS = (1002, 40)
PRODUCT_REGION = (580, 150, 1260, 660)
BUREAU_PRODUCT_REGION = (580, 130, 1260, 705)
BUREAU_EXCHANGE_REGION = (1100, 130, 1255, 705)
BUREAU_DIALOG_COST_REGION = (280, 260, 680, 340)
PRODUCT_SCROLL_START = (800, 585)
PRODUCT_SCROLL_END = (800, 365)
PRODUCT_REWIND_START = PRODUCT_SCROLL_END
PRODUCT_REWIND_END = PRODUCT_SCROLL_START
DIALOG_CANCEL_POS = (320, 535)
DIALOG_CONFIRM_POS = (960, 535)
DIALOG_PLUS_POS = (818, 380)
DIALOG_MAX_POS = (892, 380)
BATCH_TOGGLE_POS = (1230, 117)
MAX_SCAN_PAGES = 30
MAX_QUANTITY_PROBE_INCREMENTS = 10
BUREAU_ITEM_QUANTITY_PROBE_INCREMENT_LIMITS = {
    # The live bureau dialog proves a 1/100 quantity range for this one item.
    # Keep the global cap unchanged and make the larger read-only input budget
    # explicit, auditable, and impossible to inherit by another catalog item.
    "bureau_arrest_warrant": 99,
}


def _bureau_quantity_probe_increment_limit(item: ReadOnlyShopItem) -> int:
    return BUREAU_ITEM_QUANTITY_PROBE_INCREMENT_LIMITS.get(
        item.id,
        MAX_QUANTITY_PROBE_INCREMENTS,
    )


def _shop_swipe_observation(action_key: str) -> PageObservation:
    """Classify a shop-catalog frame for a swipe intent (rewind or scroll)."""

    frame = screenshot()
    ocr_items = frame.ocr()
    identity = current_bound_device_identity()
    geometry = current_display_geometry()
    source_id = str(getattr(frame, "source_capture_id", ""))
    source_sequence = int(getattr(frame, "capture_sequence", 0) or 0)
    captured_at = getattr(frame, "captured_at", None)
    raw_hash = str(getattr(frame, "raw_frame_hash", ""))
    if not source_id or source_sequence <= 0 or captured_at is None or not raw_hash:
        raise PermissionError("shop catalog swipe requires trusted capture provenance")
    in_shop = any(
        _normalize_text(item.get("text")) in (
            "总部商店", "黑月商店", "NIGHTCHAINSSTORE", "赴命商店",
            "EXCHANGESTATION",
        )
        for item in ocr_items
    )
    page_type = "shop" if in_shop else "unknown"
    markers = ("shop_catalog_content",) if in_shop else ()
    regions = ()
    if in_shop:
        regions = (CalibratedStaticRegion(
            anchor_id="shop_catalog_content", bbox=PRODUCT_REGION,
            page_classifier="shop", allowed_action=action_key,
            postcondition="shop_catalog_remains_safe",
            geometry_revision=geometry.geometry_revision,
        ),)
    return PageObservation(
        observation_id=f"shop-swipe:{source_id}", screenshot_hash=raw_hash,
        page_type=page_type, markers=markers, anchors=(), captured_at=captured_at,
        display_geometry=geometry, static_regions=regions,
        capture_sequence=source_sequence, source_capture_id=source_id,
        source_monotonic_sequence=source_sequence,
        backend_generation=identity.backend_generation,
        instance_id=identity.instance_id, adb_serial=identity.adb_serial,
        content_marker_hash=hashlib.sha256("|".join(markers).encode("utf-8")).hexdigest()[:16],
    )


def _shop_swipe_observer(action_key: str) -> PageObserver:
    """A page observer bound to the shop-catalog swipe classifier."""

    return PageObserver(lambda: _shop_swipe_observation(action_key))


def _shop_confirmation_result_markers(ocr_items: Iterable[dict]) -> tuple[str, ...]:
    """Return markers only for an affirmative purchase-result overlay."""

    texts = {
        _normalize_text(value.get("text"))
        for value in ocr_items
        if _normalize_text(value.get("text"))
    }
    if {"获得物品", "触碰空白区域退出"}.issubset(texts):
        return ("shop_confirmation_resolved", "shop_purchase_reward_overlay")
    return ()


def _shop_confirm_observation(item: ShopItem) -> PageObservation:
    """Classify a new capture for the final shop confirmation policy only."""

    frame = screenshot()
    try:
        ocr_items = frame.ocr()
    except StopExecution:
        raise
    except Exception as error:
        logger.warning(
            "商店确认后置 OCR 失败，按未知页面保守处理: {}: {}",
            type(error).__name__, error,
        )
        ocr_items = ()
    identity = current_bound_device_identity()
    geometry = current_display_geometry()
    source_id = str(getattr(frame, "source_capture_id", ""))
    source_sequence = int(getattr(frame, "capture_sequence", 0) or 0)
    captured_at = getattr(frame, "captured_at", None)
    raw_hash = str(getattr(frame, "raw_frame_hash", ""))
    if not source_id or source_sequence <= 0 or captured_at is None or not raw_hash:
        raise PermissionError("shop confirmation requires trusted capture provenance")
    quantity = _dialog_quantity(ocr_items)
    price = _dialog_price(ocr_items)
    is_source = bool(
        _has_complete_quantity_dialog(frame.image, ocr_items)
        and _dialog_has_item(ocr_items, item)
        and quantity is not None
        and price is not None
    )
    result_markers = () if is_source else _shop_confirmation_result_markers(ocr_items)
    markers = ["shop_quantity_dialog"] if is_source else list(result_markers)
    if is_source:
        currency = load_shop_catalog().currencies.get(item.currency)
        visible_currency = bool(currency) and (
            any(
                _normalize_text(value.get("text")) == _normalize_text(currency.name)
                for value in ocr_items
            )
            or _price_icon_slot_present(frame.image, ocr_items)
        )
        markers.extend((
            f"shop_item={item.id}", f"shop_quantity={quantity[0]}",
            f"shop_max_quantity={quantity[1]}", f"shop_total_cost={price}",
        ))
        # Never infer a dialog currency from the caller/catalog.  The marker is
        # emitted only when the current OCR independently names it; otherwise
        # the context gate rejects before physical input.
        if visible_currency:
            markers.append(f"shop_currency={item.currency}")
    regions = ()
    if is_source:
        regions = (CalibratedStaticRegion(
            anchor_id="shop_confirm_button", bbox=(800, 480, 1120, 590),
            page_classifier="shop_quantity_dialog", allowed_action="shop_confirm",
            postcondition="shop_confirmation_resolved",
            geometry_revision=geometry.geometry_revision,
        ),)
    return PageObservation(
        observation_id=f"shop-confirm:{source_id}", screenshot_hash=raw_hash,
        page_type=(
            "shop_quantity_dialog" if is_source
            else "shop_purchase_result" if result_markers
            else "unknown"
        ),
        markers=tuple(markers), anchors=(), captured_at=captured_at,
        display_geometry=geometry, static_regions=regions,
        capture_sequence=source_sequence, source_capture_id=source_id,
        source_monotonic_sequence=source_sequence,
        backend_generation=identity.backend_generation,
        instance_id=identity.instance_id, adb_serial=identity.adb_serial,
        content_marker_hash=hashlib.sha256("|".join(markers).encode("utf-8")).hexdigest()[:16],
    )


def _shop_confirm_snapshot(
    item: ShopItem, quantity_mode: str, quantity: int, total_cost: int, ledger_entry: dict,
) -> BusinessActionSnapshot:
    catalog = load_shop_catalog()
    plan = load_shop_plan(catalog=catalog)
    return BusinessActionSnapshot(
        item_id=item.id, shop_id=item.shop_id, currency=item.currency,
        quantity_mode=quantity_mode, quantity=int(quantity), total_cost=int(total_cost),
        catalog_digest=shop_catalog_digest(catalog), plan_digest=shop_plan_digest(plan, catalog),
        ledger_digest=shop_attempt_digest(ledger_entry),
    )


def _price_icon_slot_present(frame_image, ocr_items):
    """Return True when a non-background icon region sits between the price
    label and the price number in the dialog.

    The currency **type** is already cryptographically bound by the
    product card → catalog → currency mapping; this function only proves
    that the gap between the price label (e.g. "售价") and the price
    number (e.g. "100000") contains an icon — any icon — rather than
    empty background.  Template matching on a 22-px NEMU frame cannot
    distinguish coin art from one another; trying to do so causes false
    negatives on every icon-only dialog.

    Structural evidence required:
    * Exactly one price-label OCR item ("售价").
    * Exactly one price-number OCR item immediately to the right.
    * The pixel strip between them has local variance significantly above
      the uniform dark dialog background.
    """
    # 1. Locate the price label ("售价") in the dialog.
    price_labels: list[tuple] = []
    for raw in ocr_items:
        text = _normalize_text(raw.get("text"))
        if text == _normalize_text("售价"):
            pos = raw.get("position", [])
            if len(pos) >= 4:
                cx, cy = _center(raw)
                if cy >= 420:
                    price_labels.append((int(cx), int(cy), pos))
    if len(price_labels) != 1:
        return False

    # 2. Locate the price number — the only numeric OCR item in the same
    #    vertical band as the label.
    label_cx, label_cy, _ = price_labels[0]
    price_numbers: list[tuple[int, int, float]] = []
    for raw in ocr_items:
        value = _numeric_value(raw.get("text"))
        if value is None:
            continue
        pos = raw.get("position", [])
        if len(pos) < 4:
            continue
        cx, cy = _center(raw)
        # Must be to the right of the label, in the same vertical band.
        if cx > label_cx and abs(cy - label_cy) <= 12:
            # Center coordinates of the box for gap calculation.
            price_numbers.append((int(cx), int(cy), float(value)))
    if len(price_numbers) != 1:
        return False

    # 3. Compute the gap between the label's right edge and the number's
    #    left edge.
    label_right = max(int(pt[0]) for pt in price_labels[0][2])
    num_pts: list[tuple[int, int]] = []
    for raw in ocr_items:
        if _numeric_value(raw.get("text")) is not None:
            for pt in raw.get("position", [[0, 0], [0, 0], [0, 0], [0, 0]]):
                num_pts.append((int(pt[0]), int(pt[1])))
    if not num_pts:
        return False
    num_left = min(pt[0] for pt in num_pts)

    # The gap must be wide enough for an icon (>12 px) but not absurdly wide.
    gap = num_left - label_right
    if not (12 <= gap <= 120):
        return False

    # 4. The gap region must have local variance significantly above the
    #    uniform dark dialog background, proving an icon is present.
    min_y = min(int(pt[1]) for pt in num_pts)
    max_y = max(int(pt[1]) for pt in num_pts)
    strip_top = max(0, min_y - 4)
    strip_bottom = min(frame_image.shape[0], max_y + 4)
    strip_left = max(0, label_right)
    strip_right = min(frame_image.shape[1], num_left)

    gap_strip = frame_image[strip_top:strip_bottom, strip_left:strip_right]
    if gap_strip.size == 0:
        return False
    gap_gray = cv.cvtColor(gap_strip, cv.COLOR_BGR2GRAY)

    # The dialog background is uniform dark (< 2 px std after
    # median-filter); an icon boosts local variance well above 10.
    # Absolute threshold avoids fragile reference-region selection.
    from scipy import ndimage
    gap_std = float(ndimage.median_filter(gap_gray, size=3).std())
    return gap_std >= 15.0


def _validate_shop_confirm_context(
    observation: PageObservation, snapshot: BusinessActionSnapshot,
) -> None:
    """Re-read all authorization facts; no caller-provided value is trusted."""

    catalog = load_shop_catalog()
    plan = load_shop_plan(catalog=catalog)
    item = catalog.item(snapshot.item_id)
    if (
        item.shop_id != snapshot.shop_id or item.currency != snapshot.currency
        or shop_catalog_digest(catalog) != snapshot.catalog_digest
        or shop_plan_digest(plan, catalog) != snapshot.plan_digest
    ):
        raise PermissionError("shop confirmation catalog or plan changed")
    shop_rule = plan["shops"].get(item.shop_id, {})
    item_rule = shop_rule.get("items", {}).get(item.id, {})
    if not (
        plan.get("enabled") and shop_rule.get("enabled") and item_rule.get("enabled")
        and item_rule.get("quantity") == snapshot.quantity_mode
    ):
        raise PermissionError("shop confirmation plan is no longer enabled")
    entry = active_shop_attempt(item)
    if (
        not entry or entry.get("status") != "prepared"
        or shop_attempt_digest(entry) != snapshot.ledger_digest
        or int(entry.get("quantity", -1)) != snapshot.quantity
        or int(entry.get("cost", -1)) != snapshot.total_cost
    ):
        raise PermissionError("shop confirmation write-ahead entry changed")
    expected_markers = {
        "shop_quantity_dialog", f"shop_item={item.id}",
        f"shop_quantity={snapshot.quantity}", f"shop_total_cost={snapshot.total_cost}",
        f"shop_currency={item.currency}",
    }
    if not expected_markers.issubset(set(observation.markers)):
        raise PermissionError("shop confirmation dialog facts changed")


def _dispatch_shop_confirm(snapshot: BusinessActionSnapshot, item: ShopItem):
    """Use the one-action business session for the only irreversible tap."""

    session = _create_production_business_action_session(
        PageObserver(lambda: _shop_confirm_observation(item)),
        snapshot=snapshot,
        context_validator=_validate_shop_confirm_context,
    )
    with installed_read_only_guard(session):
        return input_tap(
            DIALOG_CONFIRM_POS,
            random_offset=False,
            intent=ActionIntent("shop_confirm", "shop_confirm_button", item.id),
        )


def _normalize_text(value: object) -> str:
    text = str(value or "")
    return re.sub(r"[\s×xX*]+1$", "", re.sub(r"\s+", "", text)).replace(
        "(", "（"
    ).replace(")", "）")


def _normalize_bureau_dialog_identity(value: object) -> str:
    """Remove only dot-like OCR noise immediately before a parenthesized suffix."""

    return re.sub(r"[·・•]+(?=（)", "", _normalize_text(value))


def _center(item: dict) -> tuple[float, float]:
    position = item["position"]
    return (
        (float(position[0][0]) + float(position[2][0])) / 2,
        (float(position[0][1]) + float(position[2][1])) / 2,
    )


def parse_limit_text(text: object) -> tuple[str, int, int] | None:
    match = re.search(r"(每日|每周|每月)限购\s*(\d+)\s*/\s*(\d+)", str(text))
    if not match:
        return None
    period = {"每日": "daily", "每周": "weekly", "每月": "monthly"}[match[1]]
    return period, int(match[2]), int(match[3])


def parse_quantity_text(text: object) -> tuple[int, int] | None:
    # PP-OCRv6 can attach a sentence-ending dot to the otherwise exact
    # quantity token (observed live as ``88/100.``).  Accept only terminal
    # punctuation after the denominator; all non-punctuation suffixes remain
    # rejected so this does not weaken quantity identity.
    match = re.fullmatch(
        r"\s*(\d+)\s*/\s*(\d+)\s*[\.。·]?\s*",
        str(text),
    )
    return (int(match[1]), int(match[2])) if match else None


def _numeric_value(text: object) -> int | None:
    cleaned = str(text).strip().replace(",", "")
    match = re.fullmatch(r"\D{0,2}(\d+(?:\.\d+)?)([kKmM]?)", cleaned)
    if not match:
        return None
    value = float(match[1])
    suffix = match[2].lower()
    if suffix == "k":
        value *= 1_000
    elif suffix == "m":
        value *= 1_000_000
    return int(round(value))


@dataclass(frozen=True)
class LocatedProduct:
    item: ShopItem
    center: tuple[float, float]
    remaining: int
    total: int
    context: tuple[str, ...]


def _ambiguous_sibling_ids() -> frozenset[str]:
    """Return IDs of catalog items that share name+period+max_limit with another."""
    from core.services.shop_catalog import load_shop_catalog

    catalog = load_shop_catalog()
    by_key: dict[tuple[str, str, int], list[str]] = {}
    for item in catalog.items:
        key = (item.name, item.period, item.max_limit)
        by_key.setdefault(key, []).append(item.id)
    return frozenset(
        item_id
        for ids in by_key.values()
        if len(set(ids)) > 1
        for item_id in ids
    )


def _name_match_anchors(data: list[dict], expected_name: str) -> list[dict]:
    """Return exact or safely reassembled same-line product-name anchors.

    PP-OCRv6 may split a parenthesized material grade into a second token, for
    example ``星云物质`` + ``(4钛)``.  Reassembly is deliberately narrow: the
    base must be at least four characters, the missing suffix must be exactly
    one parenthesized suffix, and the suffix token must be immediately to the
    right on the same row.  Period, limit and price are still checked later.
    """

    anchors: list[dict] = []
    for item in data:
        observed = _normalize_text(item.get("text"))
        if observed == expected_name:
            anchors.append(item)
            continue
        if len(observed) < 4 or not expected_name.startswith(observed):
            continue
        suffix = expected_name[len(observed):]
        if re.fullmatch(r"（[^）]+）", suffix) is None:
            continue
        item_x, item_y = _center(item)
        suffix_items = [
            candidate
            for candidate in data
            if _normalize_text(candidate.get("text")) == suffix
            and 0 < _center(candidate)[0] - item_x <= 160
            and abs(_center(candidate)[1] - item_y) <= 24
        ]
        if len(suffix_items) != 1:
            continue
        points = list(item.get("position", ())) + list(
            suffix_items[0].get("position", ())
        )
        if not points:
            continue
        x1 = min(float(point[0]) for point in points)
        y1 = min(float(point[1]) for point in points)
        x2 = max(float(point[0]) for point in points)
        y2 = max(float(point[1]) for point in points)
        anchors.append({
            **item,
            "text": expected_name,
            "position": [[x1, y1], [x2, y1], [x2, y2], [x1, y2]],
        })
    return anchors


def _card_price_roi_verify(
    page_image,
    x1: int,
    x2: int,
    match_center_y: float,
    expected_price: int,
) -> bool:
    """Crop, upscale and OCR the card's price region for a second opinion.

    General OCR can misread large prices (e.g. 5,000,000 → 15,000,000) when
    the digit string is long or adjacent to visual noise.  This function
    isolates the price band of a single card column, upscales 4×, applies
    CLAHE contrast enhancement, and returns True when *expected_price* is
    found in the enhanced crop.
    """
    matrix = page_image.image if hasattr(page_image, "image") else page_image
    height, width = matrix.shape[:2]
    # The list-card price sits below the name/limit row, roughly 30-70 px
    # below the matched name centre, within the column bounds.
    roi_x1 = max(0, x1 + 30)
    roi_y1 = max(0, int(match_center_y) + 30)
    roi_x2 = min(width, x2 - 10)
    roi_y2 = min(height, int(match_center_y) + 70)
    if roi_x2 <= roi_x1 or roi_y2 <= roi_y1:
        return False
    roi = matrix[roi_y1:roi_y2, roi_x1:roi_x2]
    if roi.size == 0:
        return False
    try:
        upscaled = cv.resize(roi, None, fx=4, fy=4, interpolation=cv.INTER_CUBIC)
        lab = cv.cvtColor(upscaled, cv.COLOR_BGR2LAB)
        l_channel, a_channel, b_channel = cv.split(lab)
        clahe = cv.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
        l_eq = clahe.apply(l_channel)
        enhanced = cv.cvtColor(
            cv.merge((l_eq, a_channel, b_channel)), cv.COLOR_LAB2BGR,
        )
    except cv.error:
        return False
    from core.image.ocr import predict

    try:
        result = predict(enhanced, cropped_pos1=(0, 0))
    except Exception:
        return False
    for item in result:
        value = _numeric_value(item.get("text"))
        if value is not None and value == expected_price:
            return True
    return False


def locate_product(
    ocr_items: Iterable[dict],
    target: ShopItem,
    *,
    page_image=None,
) -> LocatedProduct | None:
    """Locate one catalog product and disambiguate duplicate names by its card."""
    data = list(ocr_items)
    expected_name = _normalize_text(target.name)
    matches = [
        item
        for item in _name_match_anchors(data, expected_name)
        if PRODUCT_REGION[1] <= _center(item)[1] <= PRODUCT_REGION[3]
    ]
    located: list[LocatedProduct] = []
    for match in matches:
        center_x, center_y = _center(match)
        if center_x < 923:
            x1, x2 = 580, 915
        else:
            x1, x2 = 920, 1260
        context_items = []
        for item in data:
            item_x, item_y = _center(item)
            if x1 <= item_x <= x2 and center_y - 62 <= item_y <= center_y + 62:
                context_items.append(item)
        limits = [
            parsed
            for parsed in (parse_limit_text(item.get("text")) for item in context_items)
            if parsed
        ]
        expected_limit = next(
            (
                parsed
                for parsed in limits
                if parsed[0] == target.period and parsed[2] == target.max_limit
            ),
            None,
        )
        if not expected_limit:
            continue
        remaining = expected_limit[1]
        expected_price = target.price_for_remaining(remaining)
        if expected_price is None:
            # This remaining tier has no proven price in the catalog.
            # Fail closed — never buy at an unverified price.
            continue
        numeric_values = {
            value
            for value in (_numeric_value(item.get("text")) for item in context_items)
            if value is not None
        }
        # List-price OCR occasionally disappears while the name and exact
        # period/limit remain readable (observed on 星云物质（8钛）).  Reject a
        # conflicting observed price, but allow a missing one: the quantity
        # dialog performs the authoritative price check before confirmation.
        if numeric_values and expected_price not in numeric_values:
            # General OCR may misread a large price (e.g. 5,000,000 →
            # 15,000,000).  When a page image is available, try targeted
            # ROI OCR on the card's price band before rejecting.
            verified = (
                page_image is not None
                and _card_price_roi_verify(
                    page_image, x1, x2, center_y, expected_price,
                )
            )
            if not verified:
                continue
        # When another catalog item shares the same name, period and
        # max_limit but differs in price or currency, a missing card price
        # makes disambiguation impossible.  Refuse to match — let the
        # scanner scroll to a position where the price label is visible.
        if target.id in _ambiguous_sibling_ids():
            if not numeric_values or expected_price not in numeric_values:
                continue
        located.append(
            LocatedProduct(
                item=target,
                center=(center_x, center_y),
                remaining=expected_limit[1],
                total=expected_limit[2],
                context=tuple(str(item.get("text", "")) for item in context_items),
            )
        )
    if len(located) > 1:
        logger.warning(f"商品出现多个候选，采用第一个: {target.id}")
    return located[0] if located else None


def _content_difference(
    previous: np.ndarray,
    current: np.ndarray,
    region: tuple[int, int, int, int] = PRODUCT_REGION,
) -> float:
    x1, y1, x2, y2 = region
    before = cv.cvtColor(previous[y1:y2, x1:x2], cv.COLOR_BGR2GRAY)
    after = cv.cvtColor(current[y1:y2, x1:x2], cv.COLOR_BGR2GRAY)
    return float(np.mean(cv.absdiff(before, after)))


def _compact_amount(value: str) -> int | None:
    """Parse a shop cost suffix such as ``500``, ``2.5k`` or ``1m``."""

    match = re.fullmatch(r"(\d+(?:\.\d+)?)([km]?)", value.strip().lower())
    if not match:
        return None
    multiplier = {"": 1, "k": 1_000, "m": 1_000_000}[match.group(2)]
    return int(round(float(match.group(1)) * multiplier))


def _bureau_row_costs(ocr_items: Iterable[dict]) -> tuple[int, ...]:
    """Return denominator amounts from bureau balance/cost OCR strings."""

    costs: list[int] = []
    for item in ocr_items:
        text = str(item.get("text", "")).replace(" ", "")
        for raw in re.findall(r"/(\d+(?:\.\d+)?[kKmM]?)", text):
            amount = _compact_amount(raw)
            if amount is not None:
                costs.append(amount)
    return tuple(costs)


def _bureau_limit_kind(value: str) -> str:
    normalized = _normalize_text(value)
    for source, kind in (
        ("今日", "daily"), ("当日", "daily"),
        ("本周", "weekly"), ("本月", "monthly"),
    ):
        if source in normalized:
            return kind
    return "unknown"


def _bureau_remaining(value: str) -> int | None:
    match = re.search(r"(?:剩余|限购)(\d+)次", _normalize_text(value))
    if match is None:
        return None
    remaining = int(match.group(1))
    return remaining if remaining >= 0 else None


def _bureau_exchange_point(
    ocr_items: Iterable[dict], center_y: float,
) -> tuple[int, int] | None:
    """Resolve the unique exchange control belonging to one catalog row."""

    candidates: list[tuple[int, int]] = []
    for value in ocr_items:
        text = _normalize_text(value.get("text"))
        point_x, point_y = _center(value)
        if not text.startswith("EXC"):
            continue
        if not (
            BUREAU_EXCHANGE_REGION[0] <= point_x <= BUREAU_EXCHANGE_REGION[2]
            and center_y - 90 <= point_y <= center_y - 20
        ):
            continue
        candidates.append((int(round(point_x)), int(round(point_y))))
    return candidates[0] if len(candidates) == 1 else None


def _bureau_name_matches_alias(
    text: str, aliases: tuple[str, ...],
) -> bool:
    """Return True when *text* matches at least one catalog alias.

    PP-OCRv6 may reorder a recognised name (e.g. "改造凭证×1一般武"
    instead of "一般武装改造凭证").  Substring matching handles the
    common case; when it fails, a ≥75 % character-set overlap within
    a 45–220 % length band covers reordered tokens as a last resort.
    """

    for alias in aliases:
        norm = _normalize_text(alias)
        if norm in text:
            return True
        # PP-OCRv6 may reorder characters, e.g. "改造凭证×1一般武"
        # instead of "一般武装改造凭证".  When the alias is long
        # enough, a high-threshold character-set overlap serves as
        # a last-resort fallback.  The cost/period checks downstream
        # provide the authoritative disambiguation.
        if len(norm) >= 5:
            ratio = len(text) / max(1, len(norm))
            if 0.45 < ratio < 2.2:
                overlap = sum(1 for ch in set(norm) if ch in set(text))
                if overlap / max(1, len(set(norm))) >= 0.75:
                    return True
    return False


_BUREAU_PERIOD_AMBIGUOUS_IDS: frozenset[str] | None = None


def _bureau_period_ambiguous_ids() -> frozenset[str]:
    """Return IDs whose cost multiset is shared with another item whose
    aliases overlap at least one of this item's aliases.

    When the period prefix is missing from live OCR for one of these items,
    the match must be rejected — the physical row may belong to a different
    catalog entry that shares the same name and price (e.g. weekly/monthly
    variants of 进货采买书 or 广告投放券).
    """

    global _BUREAU_PERIOD_AMBIGUOUS_IDS
    if _BUREAU_PERIOD_AMBIGUOUS_IDS is not None:
        return _BUREAU_PERIOD_AMBIGUOUS_IDS
    from core.services.shop_catalog import load_read_only_shop_catalog, load_shop_catalog

    catalog = load_shop_catalog()
    shop = catalog.shop("bureau_exchange")
    if not shop.read_only_catalog:
        _BUREAU_PERIOD_AMBIGUOUS_IDS = frozenset()
        return _BUREAU_PERIOD_AMBIGUOUS_IDS
    observed = load_read_only_shop_catalog(
        shop.read_only_catalog, catalog.currencies,
    )
    items = list(observed.items)
    aliases_by_id = {item.id: set(item.ocr_aliases) for item in items}
    cost_by_id = {
        item.id: tuple(sorted(cost.amount for cost in item.costs))
        for item in items
    }
    ambiguous: set[str] = set()
    for i, item_a in enumerate(items):
        for item_b in items[i + 1:]:
            if cost_by_id[item_a.id] != cost_by_id[item_b.id]:
                continue
            if not (aliases_by_id[item_a.id] & aliases_by_id[item_b.id]):
                continue
            ambiguous.add(item_a.id)
            ambiguous.add(item_b.id)
    _BUREAU_PERIOD_AMBIGUOUS_IDS = frozenset(ambiguous)
    return _BUREAU_PERIOD_AMBIGUOUS_IDS


def locate_read_only_bureau_item(
    ocr_items: Iterable[dict],
    item: ReadOnlyShopItem,
) -> dict | None:
    """Bind one live bureau row to an evidence-backed catalog item.

    This function is analysis-only.  It requires a unique semantic name row,
    the exact multiset of catalog cost amounts in that row, and (when the
    historical catalog has a stable period) the same refresh-period kind.
    Currency identity remains inherited from the historical evidence binding;
    no live action or exchange authority is created here.
    """

    values = tuple(ocr_items)
    data = list(values)
    expected_costs = sorted(cost.amount for cost in item.costs)
    expected_kind = _bureau_limit_kind(item.observed_limit)
    # Collect anchors: substring/overlap matching (primary) + name
    # reassembly for parenthesized suffixes split by V6 (e.g.
    # "星云物质" + "(4钛)").
    anchors: list[dict] = []
    seen: set[tuple[float, float]] = set()
    for raw in data:
        text = _normalize_text(raw.get("text"))
        if not _bureau_name_matches_alias(text, item.ocr_aliases):
            continue
        cx, cy = _center(raw)
        anchors.append(raw)
        seen.add((round(float(cx), 3), round(float(cy), 3)))
    for alias in item.ocr_aliases:
        for reassembled in _name_match_anchors(data, alias):
            cx, cy = _center(reassembled)
            ckey = (round(float(cx), 3), round(float(cy), 3))
            if ckey not in seen:
                seen.add(ckey)
                anchors.append(reassembled)
    candidates: list[dict] = []
    for anchor in anchors:
        center_x, center_y = _center(anchor)
        if not (
            BUREAU_PRODUCT_REGION[0] <= center_x <= BUREAU_PRODUCT_REGION[2]
            and BUREAU_PRODUCT_REGION[1] <= center_y <= BUREAU_PRODUCT_REGION[3]
        ):
            continue
        row = tuple(
            value for value in values
            if BUREAU_PRODUCT_REGION[0] <= _center(value)[0] <= BUREAU_PRODUCT_REGION[2]
            and abs(_center(value)[1] - center_y) <= 28
        )
        observed_costs = sorted(_bureau_row_costs(row))
        if observed_costs != expected_costs:
            continue
        limit_texts = tuple(
            str(value.get("text", "")) for value in row
            if re.search(r"(?:今日|当日|本周|本月)?剩余\d+次", str(value.get("text", "")))
        )
        observed_kind = _bureau_limit_kind("|".join(limit_texts))
        if (
            expected_kind != "unknown"
            and observed_kind != "unknown"
            and observed_kind != expected_kind
        ):
            continue
        if (
            observed_kind == "unknown"
            and item.id in _bureau_period_ambiguous_ids()
        ):
            continue
        candidates.append({
            "id": item.id,
            "name": item.name,
            "status": "validated",
            "observed_limit": limit_texts[0] if len(limit_texts) == 1 else "未稳定识别",
            "observed_cost_amounts": observed_costs,
            "exchange_point": _bureau_exchange_point(values, center_y),
            "source": "live_read_only_ocr",
        })
    if len(candidates) != 1:
        return None
    return candidates[0]


def _batch_purchase_enabled(image: object) -> bool:
    """The enabled toggle has a solid white dot; disabled has a dark center."""
    matrix = image.image if hasattr(image, "image") else image
    center_x, center_y = BATCH_TOGGLE_POS
    patch = matrix[center_y - 7 : center_y + 8, center_x - 7 : center_x + 8]
    if patch.size == 0:
        return False
    white_ratio = float(np.mean(np.all(patch > 200, axis=2)))
    return white_ratio >= 0.15


def _has_quantity_dialog(ocr_items: Iterable[dict]) -> bool:
    zones = {
        "最少": (320, 320, 450, 430),
        "最多": (830, 320, 950, 430),
        "取消": (180, 480, 500, 590),
        "确定": (800, 480, 1120, 590),
    }
    found: set[str] = set()
    for item in ocr_items:
        text = _normalize_text(item.get("text"))
        zone = zones.get(text)
        if not zone:
            continue
        center_x, center_y = _center(item)
        if zone[0] <= center_x <= zone[2] and zone[1] <= center_y <= zone[3]:
            found.add(text)
    return found == set(zones)


def _has_quantity_step_buttons(image: object) -> bool:
    """Validate the fixed -1/+1 buttons visually when OCR omits symbols."""
    matrix = image.image if hasattr(image, "image") else image
    if matrix is None or matrix.shape[0] < 395 or matrix.shape[1] < 847:
        return False
    rois = (
        matrix[365:395, 435:475],  # -1
        matrix[365:395, 807:847],  # +1
    )
    for roi in rois:
        gray = cv.cvtColor(roi, cv.COLOR_BGR2GRAY)
        if int(np.count_nonzero(gray > 180)) < 35:
            return False
    return True


def _has_complete_quantity_dialog(image: object, ocr_items: Iterable[dict]) -> bool:
    data = list(ocr_items)
    if not _has_quantity_step_buttons(image):
        return False
    if _has_quantity_dialog(data):
        return True

    # PP-OCRv6 can reduce the decorative "最多" label to "多" while the
    # quantity, cancel and confirm controls remain unambiguous.  The min/max
    # labels are not dispatch targets: the actual -1/+1 controls are verified
    # visually above.  Require the two terminal action labels and a quantity
    # value in their exact zones before accepting this OCR-tolerant path.
    required_actions = {
        "取消": (180, 480, 500, 590),
        "确定": (800, 480, 1120, 590),
    }
    found: set[str] = set()
    for item in data:
        text = _normalize_text(item.get("text"))
        zone = required_actions.get(text)
        if zone is None:
            continue
        center_x, center_y = _center(item)
        if zone[0] <= center_x <= zone[2] and zone[1] <= center_y <= zone[3]:
            found.add(text)
    return found == set(required_actions) and _dialog_quantity(data) is not None


def _validate_increment_session_frame(
    image: object,
    ocr_items: Iterable[dict],
    item: ReadOnlyShopItem,
    *,
    expected_quantity: int,
    expected_maximum: int,
    channel_order: tuple[str, ...],
    channel_x_order: tuple[float, ...] = (),
) -> bool:
    """Weaker post-increment frame check for an in-session quantity probe.

    V6 may intermittently drop the confirmation-text row while the rest of
    the dialog stays identical.  The first frame is always validated by
    :func:`_has_complete_bureau_quantity_dialog`; subsequent frames may
    temporarily omit the item name / confirmation text as long as the
    dialog structure, maximum quantity, quantity step, and cost channels
    all remain consistent with the previously established session.

    A *conflicting* item name still blocks immediately — only absence is
    tolerated.
    """

    data = list(ocr_items)
    if not _has_quantity_step_buttons(image):
        return False
    quantity = _dialog_quantity(data)
    if quantity is None or quantity[0] != expected_quantity:
        return False
    if quantity[1] != expected_maximum:
        return False
    # Cancel and confirm must still be present.
    buttons = {"取消": (180, 520, 500, 595), "确定": (800, 520, 1120, 600)}
    found_buttons: set[str] = set()
    for value in data:
        text = _normalize_text(value.get("text"))
        zone = buttons.get(text)
        if zone is None:
            continue
        cx, cy = _center(value)
        if zone[0] <= cx <= zone[2] and zone[1] <= cy <= zone[3]:
            found_buttons.add(text)
    if found_buttons != set(buttons):
        return False
    # Cost channel count and left-to-right order must match.
    candidates = _bureau_dialog_cost_candidates(data)
    if len(candidates) != len(channel_order):
        return False
    if channel_x_order:
        for (_, x), first_x in zip(candidates, channel_x_order):
            if abs(x - first_x) > 30:
                return False
    # Aggregate confirmation-band OCR text into a single block so V6-split
    # tokens (e.g. "确认消耗以上素材" + "兑换商品X") are not misread as
    # a harmless "confirmation absent".  The confirmation band sits between
    # the quantity row (y≈340-380) and the cancel/confirm buttons (y≈520).
    confirmation_texts = _bureau_confirmation_line_fragments(data)
    if confirmation_texts:
        merged = _merge_overlapping_ocr_texts(confirmation_texts)
        has_bureau_markers = "确认消耗" in merged and "兑换" in merged
        if not has_bureau_markers or not _bureau_dialog_item_visible(
            [{"text": merged}], item,
        ):
            return False
    # When no confirmation tokens at all exist in the band, tolerate
    # (V6 dropped the line entirely while dialog structure is unchanged).
    return True


def _merge_overlapping_ocr_texts(texts: Iterable[str]) -> str:
    """Join left-to-right OCR fragments without duplicating overlap text."""

    merged = ""
    for raw in texts:
        text = _normalize_text(raw)
        if not text:
            continue
        if not merged:
            merged = text
            continue
        overlap = 0
        for size in range(min(len(merged), len(text)), 0, -1):
            if merged.endswith(text[:size]):
                overlap = size
                break
        merged += text[overlap:]
    return merged


def _bureau_confirmation_line_fragments(data: Iterable[dict]) -> list[str]:
    """Return the geometrically connected OCR fragments of a confirmation row.

    PP-OCRv6 sometimes emits the item suffix as a separate token which does
    not itself contain ``确认消耗`` or ``兑换``.  Start from a marker-bearing
    token, then extend only across horizontally contiguous tokens on the same
    visual line.  No marker means the row was dropped entirely, which remains
    the explicitly tolerated continuity case.
    """

    entries: list[tuple[float, float, float, float, str]] = []
    for value in data:
        text = _normalize_text(value.get("text"))
        position = value.get("position", ())
        if not text or len(position) < 4:
            continue
        xs = [float(point[0]) for point in position]
        ys = [float(point[1]) for point in position]
        x1, x2, y1, y2 = min(xs), max(xs), min(ys), max(ys)
        cy = (y1 + y2) / 2
        if 400 <= cy <= 500:
            entries.append((x1, y1, x2, y2, text))

    selected = {
        index
        for index, entry in enumerate(entries)
        if "确认消耗" in entry[4] or "兑换" in entry[4]
    }
    if not selected:
        return []

    changed = True
    while changed:
        changed = False
        for index, candidate in enumerate(entries):
            if index in selected:
                continue
            cx1, cy1, cx2, cy2, _ = candidate
            candidate_height = max(cy2 - cy1, 1.0)
            for selected_index in tuple(selected):
                sx1, sy1, sx2, sy2, _ = entries[selected_index]
                selected_height = max(sy2 - sy1, 1.0)
                vertical_overlap = max(0.0, min(cy2, sy2) - max(cy1, sy1))
                if vertical_overlap / min(candidate_height, selected_height) < 0.45:
                    continue
                horizontal_gap = max(cx1 - sx2, sx1 - cx2, 0.0)
                if horizontal_gap <= 24:
                    selected.add(index)
                    changed = True
                    break

    return [entries[index][4] for index in sorted(selected, key=lambda i: entries[i][0])]


def _has_bureau_quantity_dialog(ocr_items: Iterable[dict]) -> bool:
    """Like :func:`_has_quantity_dialog` with a relaxed confirm-button zone.

    The bureau exchange quantity dialog places the "确定" button ~10 px
    lower than the headquarters shop dialog.  The standard classifier
    rejects a valid dialog when the centre is at y ≈ 590.5 (0.5 px
    beyond the 590 hard cap).  This variant only widens the vertical
    bound for "确定"; the other three controls still use the same zones.
    """

    zones = {
        "最少": (320, 320, 450, 430),
        "最多": (830, 320, 950, 430),
        "取消": (180, 480, 500, 590),
        "确定": (800, 480, 1120, 600),
    }
    found: set[str] = set()
    for item in ocr_items:
        text = _normalize_text(item.get("text"))
        zone = zones.get(text)
        if not zone:
            continue
        center_x, center_y = _center(item)
        if zone[0] <= center_x <= zone[2] and zone[1] <= center_y <= zone[3]:
            found.add(text)
    return found == set(zones)


def _has_complete_bureau_quantity_dialog(
    image: object,
    ocr_items: Iterable[dict],
    item: ReadOnlyShopItem,
) -> bool:
    """Bureau-specific quantity-dialog classifier.

    In addition to the relaxed button zones and visual step-button check,
    this requires the dialog to contain the exchange-confirmation preamble
    and at least one of the catalog's OCR aliases for the item.
    """

    data = list(ocr_items)
    if not _has_quantity_step_buttons(image):
        return False
    layout_valid = _has_quantity_dialog(data) or _has_bureau_quantity_dialog(data)
    if not layout_valid:
        return False
    texts = tuple(_normalize_text(value.get("text")) for value in data)
    has_bureau_confirmation = any(
        "确认消耗" in text
        and "兑换" in text
        and any(
            _normalize_text(alias) in text for alias in item.ocr_aliases
        )
        for text in texts
    )
    return has_bureau_confirmation and _dialog_quantity(data) is not None


class ShopEvidenceRecorder:
    def __init__(self, enabled: bool, label: str):
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        self.enabled = bool(enabled)
        self.root = Path("logs") / "shop_purchase" / f"{timestamp}-{label}"
        self.index = 0

    def capture(
        self,
        label: str,
        image=None,
        ocr_items: list[dict] | None = None,
    ) -> list[dict]:
        if image is None:
            image = screenshot()
        if ocr_items is None:
            ocr_items = image.ocr()
        if not self.enabled:
            return ocr_items
        safe_label = re.sub(r"[^0-9A-Za-z_-]+", "-", label).strip("-") or "step"
        self.root.mkdir(parents=True, exist_ok=True)
        stem = f"{self.index:03d}-{safe_label}"
        self.index += 1
        cv.imwrite(str(self.root / f"{stem}.png"), image.image)
        (self.root / f"{stem}.ocr.json").write_text(
            json.dumps(ocr_items, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return ocr_items

    def write_result(self, result: dict) -> str:
        """Persist one privacy-safe final result beside the frame evidence."""

        if not self.enabled:
            return ""
        self.root.mkdir(parents=True, exist_ok=True)
        destination = self.root / "FINAL_RESULT.json"
        temporary = destination.with_name(f"{destination.name}.tmp")
        temporary.write_text(
            json.dumps(result, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        temporary.replace(destination)
        return str(destination)


def _wait_for_text(
    expected: Iterable[str],
    timeout: float = 8.0,
) -> tuple[object, list[dict]]:
    expected_normalized = {_normalize_text(text) for text in expected}
    deadline = time.monotonic() + timeout
    latest = None
    latest_ocr: list[dict] = []
    while time.monotonic() < deadline:
        latest = screenshot()
        latest_ocr = latest.ocr()
        visible = {_normalize_text(item.get("text")) for item in latest_ocr}
        if expected_normalized.issubset(visible):
            return latest, latest_ocr
        time.sleep(0.6)
    raise BlockedBySafetyError(f"等待商店文本超时: {', '.join(expected)}")


def _dialog_quantity(ocr_items: Iterable[dict]) -> tuple[int, int] | None:
    for item in ocr_items:
        _, center_y = _center(item)
        quantity = parse_quantity_text(item.get("text"))
        if quantity and 320 <= center_y <= 420:
            return quantity
    return None


def _dialog_price(ocr_items: Iterable[dict]) -> int | None:
    data = list(ocr_items)
    candidates: list[tuple[int, float, float]] = []
    for item in data:
        center_x, center_y = _center(item)
        if center_x >= 600 and 420 <= center_y <= 490:
            value = _numeric_value(item.get("text"))
            if value is not None:
                candidates.append((value, center_x, center_y))
    if not candidates:
        return None

    label_bbox = _price_label_bbox(data)
    if label_bbox is not None:
        _, label_y1, label_x2, label_y2 = label_bbox
        label_center_y = (label_y1 + label_y2) / 2.0
        anchored = [
            candidate
            for candidate in candidates
            if label_x2 <= candidate[1] <= label_x2 + 220
            and abs(candidate[2] - label_center_y) <= 35
        ]
        if anchored:
            # The price is the first numeric text immediately to the right of
            # "售价".  V6 may also detect stray zero-like glyphs farther right;
            # OCR result order must not decide which number is authoritative.
            return min(anchored, key=lambda item: (item[1] - label_x2, abs(item[2] - label_center_y)))[0]

    # Compatibility for older/synthetic OCR without a visible price label.
    # Prefer the stable dialog price anchor instead of the last OCR token.
    return min(candidates, key=lambda item: abs(item[1] - 683) + abs(item[2] - 452))[0]


def _price_label_bbox(
    ocr_items: Iterable[dict],
) -> tuple[int, int, int, int] | None:
    """Return the bounding box of the "售价" label using normalized matching."""
    for raw in ocr_items:
        text = _normalize_text(raw.get("text"))
        if text == _normalize_text("售价"):
            pos = raw.get("position", [])
            if len(pos) >= 4:
                center_y = _center(raw)[1]
                if center_y >= 420:
                    x1 = min(int(pt[0]) for pt in pos)
                    y1 = min(int(pt[1]) for pt in pos)
                    x2 = max(int(pt[0]) for pt in pos)
                    y2 = max(int(pt[1]) for pt in pos)
                    return x1, y1, x2, y2
    return None


def _price_roi_ocr(
    frame_image,
    dialog_ocr: list[dict],
) -> int | None:
    """Crop, upscale and contrast-enhance the price digit region.

    When the general OCR model misses a small single digit next to a
    currency icon, a focused ROI crop with 4× upscaling and CLAHE contrast
    enhancement can recover it.  This is a pure analysis pass — it never
    guesses; if the enhanced crop still yields no number, it returns None.
    """
    label_bbox = _price_label_bbox(dialog_ocr)
    if label_bbox is None:
        return None
    label_x1, label_y1, label_x2, label_y2 = label_bbox
    matrix = frame_image.image if hasattr(frame_image, "image") else frame_image
    height, width = matrix.shape[:2]
    # Crop the region to the right of the price label, with modest padding.
    roi_x1 = max(0, label_x2)
    roi_y1 = max(0, label_y1 - 10)
    roi_x2 = min(width, label_x2 + 120)
    roi_y2 = min(height, label_y2 + 10)
    if roi_x2 <= roi_x1 or roi_y2 <= roi_y1:
        return None
    roi = matrix[roi_y1:roi_y2, roi_x1:roi_x2]
    if roi.size == 0:
        return None
    try:
        # 4× upscale to help the detection model find single small digits.
        upscaled = cv.resize(roi, None, fx=4, fy=4, interpolation=cv.INTER_CUBIC)
        # CLAHE contrast enhancement on the luminance channel.
        lab = cv.cvtColor(upscaled, cv.COLOR_BGR2LAB)
        l_channel, a_channel, b_channel = cv.split(lab)
        clahe = cv.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
        l_eq = clahe.apply(l_channel)
        enhanced = cv.cvtColor(
            cv.merge((l_eq, a_channel, b_channel)), cv.COLOR_LAB2BGR,
        )
    except cv.error:
        return None
    from core.image.ocr import predict

    try:
        ocr_result = predict(enhanced, cropped_pos1=(0, 0))
    except Exception:
        return None
    values = []
    for item in ocr_result:
        value = _numeric_value(item.get("text"))
        if value is not None:
            values.append(value)
    return values[-1] if values else None


def _dialog_has_item(ocr_items: Iterable[dict], item: ShopItem) -> bool:
    expected = _normalize_text(item.name)
    return any(_normalize_text(value.get("text")) == expected for value in ocr_items)


def _ocr_bbox(ocr_items: list[dict], text: str) -> tuple[int, int, int, int] | None:
    """Return the bounding box (x1, y1, x2, y2) of the first OCR item matching
    ``text``, or None."""
    for item in ocr_items:
        if str(item.get("text", "")) == text:
            pos = item.get("position", [])
            if len(pos) >= 4:
                x1 = min(int(pt[0]) for pt in pos)
                y1 = min(int(pt[1]) for pt in pos)
                x2 = max(int(pt[0]) for pt in pos)
                y2 = max(int(pt[1]) for pt in pos)
                return x1, y1, x2, y2
    return None


def _finalize_attempt(result: dict) -> dict:
    """Best-effort status update; the pre-confirm block must survive failures."""
    try:
        update_shop_attempt(str(result["id"]), str(result["status"]), result)
    except Exception:  # never erase at-most-once protection after confirmation
        logger.exception(f"无法更新商店执行账本: {result.get('id', '')}")
    return result


class HeadquartersBlackMoonAdapter:
    key = "headquarters_black_moon"

    def __init__(self, shop: ShopDefinition, recorder: ShopEvidenceRecorder):
        self.shop = shop
        self.recorder = recorder

    def open(self) -> None:
        initial = screenshot()
        initial_ocr = initial.ocr()
        initial_texts = {_normalize_text(item.get("text")) for item in initial_ocr}
        if _has_quantity_dialog(initial_ocr):
            input_tap(DIALOG_CANCEL_POS)
            time.sleep(0.8)
            initial = screenshot()
            initial_ocr = initial.ocr()
            initial_texts = {
                _normalize_text(item.get("text")) for item in initial_ocr
            }
        already_in_store = {"总部商店", "赴命商店"}.issubset(initial_texts)
        if already_in_store:
            self.recorder.capture("existing-shop", initial, initial_ocr)
        else:
            if not go_home():
                raise BlockedBySafetyError("无法返回主界面，未进入商店")
            self.recorder.capture("home")
            input_tap(SHOP_ENTRY_POS)
            image, ocr_items = _wait_for_text(
                ("总部商店", "黑月商店"), timeout=10
            )
            self.recorder.capture("shop-entry", image, ocr_items)
        input_tap(HEADQUARTERS_TAB_POS)
        image, ocr_items = _wait_for_text(("总部商店", "黑月商店", "确认购买"))
        self.recorder.capture("headquarters-top", image, ocr_items)
        if _batch_purchase_enabled(image):
            logger.info("检测到批量购买已开启，切换为逐件数量弹窗模式")
            input_tap(BATCH_TOGGLE_POS)
            time.sleep(0.8)
            image = screenshot()
            ocr_items = image.ocr()
            self.recorder.capture("batch-disabled", image, ocr_items)
            if _batch_purchase_enabled(image):
                raise BlockedBySafetyError("无法关闭批量购买，已停止自动购买")
        self._rewind_to_top()

    def _rewind_to_top(self) -> None:
        """The game remembers the last scroll position, so never assume page one."""
        previous = screenshot()
        stable = 0
        for index in range(14):
            input_swipe(
                PRODUCT_REWIND_START, PRODUCT_REWIND_END, swipe_time=650,
                intent=ActionIntent("shop_catalog_rewind", "shop_catalog_content", f"rewind-{index:02d}"),
            )
            time.sleep(0.9)
            current = screenshot()
            difference = _content_difference(previous.image, current.image)
            ocr_items = current.ocr()
            self.recorder.capture(f"rewind-{index:02d}", current, ocr_items)
            logger.debug(f"商店回顶第 {index + 1} 次差异: {difference:.3f}")
            stable = stable + 1 if difference <= 4.0 else 0
            previous = current
            if stable >= 2:
                return
        raise BlockedBySafetyError("商店回顶超过安全滑动次数，已停止继续")

    def _cancel_dialog(self, label: str) -> None:
        input_tap(DIALOG_CANCEL_POS)
        time.sleep(0.8)
        remaining_ocr = self.recorder.capture(label)
        if _has_quantity_dialog(remaining_ocr):
            raise BlockedBySafetyError("数量弹窗取消后仍未关闭，已停止继续操作")

    def _verify_shop_page(self) -> bool:
        """Return True when the current screen shows the headquarters shop."""
        frame = screenshot()
        ocr_items = frame.ocr()
        texts = {_normalize_text(item.get("text")) for item in ocr_items}
        return {"总部商店", "黑月商店"}.issubset(texts)

    def _recover_to_shop_page(self) -> None:
        """Verify we are still on the shop page after a per-item cancellation.

        The dialog was already dismissed by :meth:`_cancel_dialog` inside
        :meth:`inspect_dialog` before the exception that triggers this call.
        This method only verifies the shop page is present; it never emits
        input on an unverified page.  If the shop is not visible the entire
        batch halts safely.
        """
        if self._verify_shop_page():
            return
        raise BlockedBySafetyError(
            "单项失败后未返回商店页，已停止继续扫描"
        )

    def inspect_dialog(
        self,
        located: LocatedProduct,
        quantity_mode: str,
        price_observations: list[dict] | None = None,
    ) -> tuple[int, int]:
        input_tap(located.center)
        time.sleep(0.9)
        dialog = screenshot()
        dialog_ocr = dialog.ocr()
        self.recorder.capture(f"dialog-{located.item.id}", dialog, dialog_ocr)
        if not _has_complete_quantity_dialog(dialog, dialog_ocr):
            self._cancel_dialog(f"cancel-missing-controls-{located.item.id}")
            raise BlockedBySafetyError(
                f"未识别到完整数量弹窗，拒绝确认购买: {located.item.name}"
            )
        if not _dialog_has_item(dialog_ocr, located.item):
            self._cancel_dialog(f"cancel-unexpected-{located.item.id}")
            raise BlockedBySafetyError(f"商品弹窗名称校验失败: {located.item.name}")
        observed_price = _dialog_price(dialog_ocr)
        if observed_price is None:
            observed_price = _price_roi_ocr(dialog, dialog_ocr)
        expected_price = located.item.price_for_remaining(located.remaining)
        if expected_price is None or observed_price != expected_price:
            self._cancel_dialog(f"cancel-price-{located.item.id}")
            raise BlockedBySafetyError(
                f"商品价格校验失败: {located.item.name}，"
                f"目录档位 {expected_price}，实机 {observed_price}"
            )
        observed_total = observed_price
        quantity = _dialog_quantity(dialog_ocr)
        if quantity is None:
            if located.remaining != 1:
                self._cancel_dialog(f"cancel-quantity-{located.item.id}")
                raise BlockedBySafetyError(f"未识别数量控件: {located.item.name}")
            quantity = (1, 1)
        dialog_maximum = quantity[1]
        if not (1 <= quantity[0] <= dialog_maximum <= located.remaining):
            self._cancel_dialog(f"cancel-dialog-limit-{located.item.id}")
            raise BlockedBySafetyError(
                f"商品弹窗可选上限不可信: {located.item.name}，"
                f"本期剩余 {located.remaining}，弹窗 {quantity[0]}/{dialog_maximum}"
            )
        if price_observations is not None:
            if quantity[1] - quantity[0] > MAX_QUANTITY_PROBE_INCREMENTS:
                self._cancel_dialog(f"cancel-probe-limit-{located.item.id}")
                raise BlockedBySafetyError(
                    f"商品数量只读探测超过 {MAX_QUANTITY_PROBE_INCREMENTS} 次上限: "
                    f"{located.item.name}"
                )
            price_observations.clear()
            price_observations.append({
                "quantity": quantity[0],
                "marginal_cost": observed_total,
                "cumulative_cost": observed_total,
            })
            previous_total = observed_total
            previous_marginal = observed_total
            while quantity[0] < quantity[1]:
                requested_quantity = quantity[0] + 1
                dispatched = input_tap(
                    DIALOG_PLUS_POS,
                    random_offset=False,
                    intent=ActionIntent(
                        "shop_quantity_increment",
                        "shop_quantity_increment_button",
                        f"{located.item.id}:{requested_quantity}",
                    ),
                )
                if dispatched is False:
                    self._cancel_dialog(f"cancel-probe-denied-{located.item.id}")
                    raise BlockedBySafetyError(
                        f"商品数量只读探测点击被拒绝: {located.item.name}"
                    )
                time.sleep(0.45)
                dialog = screenshot()
                dialog_ocr = dialog.ocr()
                self.recorder.capture(
                    f"dialog-probe-{located.item.id}-{requested_quantity}",
                    dialog,
                    dialog_ocr,
                )
                next_quantity = _dialog_quantity(dialog_ocr)
                next_total = _dialog_price(dialog_ocr)
                marginal = (
                    next_total - previous_total
                    if next_total is not None
                    else None
                )
                if (
                    not _has_complete_quantity_dialog(dialog.image, dialog_ocr)
                    or not _dialog_has_item(dialog_ocr, located.item)
                    or next_quantity != (requested_quantity, quantity[1])
                    or next_total is None
                    or marginal is None
                    or marginal <= 0
                    or marginal < previous_marginal
                ):
                    self._cancel_dialog(
                        f"cancel-probe-mismatch-{located.item.id}-{requested_quantity}"
                    )
                    raise BlockedBySafetyError(
                        f"商品数量只读探测结果不可信: {located.item.name}，"
                        f"期望 {requested_quantity}/{quantity[1]} 且边际单调不减，"
                        f"实机 {next_quantity}、累计 {next_total}、边际 {marginal}"
                    )
                price_observations.append({
                    "quantity": requested_quantity,
                    "marginal_cost": marginal,
                    "cumulative_cost": next_total,
                })
                quantity = next_quantity
                observed_total = next_total
                previous_total = next_total
                previous_marginal = marginal
        elif quantity_mode == "max" and quantity[0] != quantity[1]:
            input_tap(DIALOG_MAX_POS)
            time.sleep(0.6)
            dialog = screenshot()
            dialog_ocr = dialog.ocr()
            self.recorder.capture(f"dialog-max-{located.item.id}", dialog, dialog_ocr)
            quantity = _dialog_quantity(dialog_ocr) or quantity
            observed_total = _dialog_price(dialog_ocr)
            if observed_total is None or observed_total < observed_price:
                self._cancel_dialog(f"cancel-total-{located.item.id}")
                raise BlockedBySafetyError(
                    f"未能安全识别上限模式实时总价: {located.item.name}"
                )
        expected = (
            dialog_maximum
            if price_observations is not None or quantity_mode == "max"
            else 1
        )
        if quantity != (expected, dialog_maximum):
            self._cancel_dialog(f"cancel-quantity-mismatch-{located.item.id}")
            raise BlockedBySafetyError(
                f"商品数量校验失败: {located.item.name}，"
                f"期望 {expected}/{dialog_maximum}，实机 {quantity[0]}/{quantity[1]}"
            )
        return quantity[0], observed_total

    def purchase(
        self,
        located: LocatedProduct,
        quantity_mode: str,
        dry_run: bool,
    ) -> dict:
        price_observations: list[dict] | None = (
            [] if dry_run and bool(located.item.price_tiers) else None
        )
        if price_observations is None:
            quantity, observed_total = self.inspect_dialog(located, quantity_mode)
        else:
            quantity, observed_total = self.inspect_dialog(
                located,
                quantity_mode,
                price_observations=price_observations,
            )
        if dry_run:
            self._cancel_dialog(f"dry-run-cancel-{located.item.id}")
            return {
                "id": located.item.id,
                "name": located.item.name,
                "status": "validated",
                "quantity": quantity,
                "cost": observed_total,
                "price_observations": price_observations or [],
                "dry_run": True,
                "final_action": "cancel",
            }
        # At-most-once boundary: persist the item-period lock before the ADB
        # confirmation tap.  A crash may skip one cycle, but can never repeat it.
        try:
            ledger_entry = record_shop_attempt(
                located.item,
                quantity_mode,
                quantity=quantity,
                cost=observed_total,
                status="prepared",
            )
        except ShopAttemptAlreadyActive as error:
            self._cancel_dialog(f"cancel-period-blocked-{located.item.id}")
            return {
                "id": located.item.id,
                "name": located.item.name,
                "status": "blocked_by_period",
                "blocked_until": error.entry.get("blocked_until", ""),
            }
        except Exception:
            # A failed write-ahead record must fail closed: close the dialog and
            # never send the irreversible confirmation tap.
            self._cancel_dialog(f"cancel-ledger-error-{located.item.id}")
            raise
        try:
            dispatch_result = _dispatch_shop_confirm(
                _shop_confirm_snapshot(
                    located.item, quantity_mode, quantity, observed_total, ledger_entry
                ),
                located.item,
            )
            if dispatch_result is False:
                self._cancel_dialog(f"cancel-policy-denied-{located.item.id}")
                raise BlockedBySafetyError("商店确认前置条件不再成立，未发送购买确认")
        except StopExecution:
            _finalize_attempt(
                {
                    "id": located.item.id,
                    "name": located.item.name,
                    "status": "submitted_unverified",
                    "quantity": quantity,
                    "cost": observed_total,
                    "remaining_before": located.remaining,
                    "remaining_after": None,
                    "verification_error": "确认点击阶段收到停止请求",
                }
            )
            raise
        except BlockedBySafetyError:
            # This is a pre-dispatch denial.  The dialog has been safely
            # cancelled and no write-ahead record may be re-used for a tap.
            raise
        except Exception as error:
            # The ADB command may have reached the emulator even if its caller
            # observed an error.  Preserve the period lock and never tap twice.
            return _finalize_attempt(
                {
                    "id": located.item.id,
                    "name": located.item.name,
                    "status": "submitted_unverified",
                    "quantity": quantity,
                    "cost": observed_total,
                    "remaining_before": located.remaining,
                    "remaining_after": None,
                    "verification_error": f"确认点击返回异常: {type(error).__name__}: {error}",
                }
            )
        time.sleep(1.5)
        try:
            result_image = screenshot()
            result_ocr = result_image.ocr()
            self.recorder.capture(
                f"purchase-result-{located.item.id}", result_image, result_ocr
            )
        except StopExecution:
            _finalize_attempt(
                {
                    "id": located.item.id,
                    "name": located.item.name,
                    "status": "submitted_unverified",
                    "quantity": quantity,
                    "cost": observed_total,
                    "remaining_before": located.remaining,
                    "remaining_after": None,
                    "verification_error": "确认后校验阶段收到停止请求",
                }
            )
            raise
        except Exception as error:  # confirmation is an irreversible boundary
            logger.exception(f"购买已提交但无法读取结果: {located.item.name}")
            return _finalize_attempt({
                "id": located.item.id,
                "name": located.item.name,
                "status": "submitted_unverified",
                "quantity": quantity,
                "cost": observed_total,
                "remaining_before": located.remaining,
                "remaining_after": None,
                "verification_error": f"{type(error).__name__}: {error}",
            })
        if any("不足" in str(item.get("text", "")) for item in result_ocr):
            input_tap((100, 650))
            time.sleep(0.5)
            self.recorder.capture(f"insufficient-{located.item.id}")
            return _finalize_attempt({
                "id": located.item.id,
                "name": located.item.name,
                "status": "insufficient_currency",
                "quantity": 0,
                "cost": 0,
            })
        if _has_quantity_dialog(result_ocr):
            # Never tap confirmation twice.  The server may still be processing,
            # so leave this as a submitted/unknown outcome and suppress retries.
            return _finalize_attempt({
                "id": located.item.id,
                "name": located.item.name,
                "status": "submitted_unverified",
                "quantity": quantity,
                "cost": observed_total,
                "remaining_before": located.remaining,
                "remaining_after": None,
                "verification_error": "确认后数量弹窗仍可见",
            })
        # If the purchase-result overlay ("获得物品" / "触碰空白区域退出")
        # is covering the shop card, dismiss it with one safe blank-area tap
        # to reveal the refreshed card underneath.  This is the button-free
        # game instruction, not a second confirmation.
        result_texts = {str(item.get("text", "")) for item in result_ocr}
        if "获得物品" in result_texts and "触碰空白区域退出" in result_texts:
            # Compute a safe blank-area point from the actual frame and OCR
            # bboxes.  The overlay is uniform dark, so the selector's Canny
            # edge pass finds zero edges; its result reduces to "any region
            # outside the excluded OCR bboxes that is large enough to tap".
            selector = AnnouncementSafeRegionSelector(
                minimum_region_area=1200,
                exclusion_margin=12,
            )
            frame_img = getattr(result_image, "image", result_image)
            height, width = frame_img.shape[:2]
            dialog_bbox = _ocr_bbox(result_ocr, "获得物品")
            if dialog_bbox is None:
                dialog_bbox = (0, 0, 1, 1)  # fallback: zero-area
            ocr_bboxes = tuple(
                _ocr_bbox(result_ocr, text)
                for text in result_texts
                if text != "获得物品"
            )
            ocr_bboxes = tuple(b for b in ocr_bboxes if b is not None)
            safety_map = selector.select(
                result_image,
                overlay_bbox=(0, 0, width, height),
                dialog_bbox=dialog_bbox,
                ocr_bboxes=ocr_bboxes,
            )
            safe_point = safety_map.candidates[0].point if safety_map.candidates else None
            if safe_point is not None:
                dismiss_intent = ActionIntent(
                    "dialog_cancel", "shop_result_overlay_dismiss", located.item.id
                )
                dismissed = input_tap(
                    safe_point, random_offset=False, intent=dismiss_intent
                )
                if dismissed is False:
                    # The dismiss tap was denied — hardware is unreachable
                    # or a policy blocked it.  Do not wait or screenshot.
                    return _finalize_attempt({
                        "id": located.item.id,
                        "name": located.item.name,
                        "status": "submitted_unverified",
                        "quantity": quantity,
                        "cost": observed_total,
                        "remaining_before": located.remaining,
                        "remaining_after": None,
                        "verification_error": "覆盖层退出点击被拒绝，无法读取购买后限购余量",
                    })
                time.sleep(0.8)
                try:
                    dismissed_image = screenshot()
                    dismissed_ocr = dismissed_image.ocr()
                except StopExecution:
                    _finalize_attempt({
                        "id": located.item.id,
                        "name": located.item.name,
                        "status": "submitted_unverified",
                        "quantity": quantity,
                        "cost": observed_total,
                        "remaining_before": located.remaining,
                        "remaining_after": None,
                        "verification_error": "覆盖层退出后截图阶段收到停止请求",
                    })
                    raise
                except Exception as error:
                    # Re-screenshot failed.  Do NOT record a "dismissed"
                    # evidence frame — the overlay may still be present.
                    # Return submitted_unverified with a precise reason.
                    logger.exception(
                        f"覆盖层退出后截图失败: {located.item.name}"
                    )
                    return _finalize_attempt({
                        "id": located.item.id,
                        "name": located.item.name,
                        "status": "submitted_unverified",
                        "quantity": quantity,
                        "cost": observed_total,
                        "remaining_before": located.remaining,
                        "remaining_after": None,
                        "verification_error": (
                            f"覆盖层退出后截图失败: "
                            f"{type(error).__name__}: {error}"
                        ),
                    })
                self.recorder.capture(
                    f"purchase-result-dismissed-{located.item.id}",
                    dismissed_image, dismissed_ocr,
                )
                result_image = dismissed_image
                result_ocr = dismissed_ocr
            else:
                # No safe blank region found on the overlay; conservative.
                logger.warning(
                    f"购买结果覆盖层无可安全点击区域: {located.item.name}"
                )
        refreshed = locate_product(result_ocr, located.item)
        expected_remaining = max(0, located.remaining - quantity)
        verified = refreshed is not None and refreshed.remaining == expected_remaining
        return _finalize_attempt({
            "id": located.item.id,
            "name": located.item.name,
            "status": "purchased" if verified else "submitted_unverified",
            "quantity": quantity,
            "cost": observed_total,
            "remaining_before": located.remaining,
            "remaining_after": refreshed.remaining if refreshed else None,
            "verification_error": "" if verified else "未能核对购买后的限购余量",
        })

    def scan(
        self,
        purchases: Iterable[ConfiguredPurchase] | None = None,
        dry_run: bool = True,
        max_pages: int = MAX_SCAN_PAGES,
    ) -> dict:
        if isinstance(max_pages, bool) or not isinstance(max_pages, int) or max_pages < 1:
            raise ValueError("商店扫描页数上限必须是正整数")
        catalog_probe = purchases is None
        requested = {
            purchase.item.id: purchase
            for purchase in (
                purchases
                if purchases is not None
                else [
                    ConfiguredPurchase(self.shop, item, "one")
                    for item in self.shop.items
                ]
            )
        }
        pending = dict(requested)
        results: list[dict] = []
        page_count = 0
        stable = 0
        previous_matrix = None
        reached_bottom = False
        stop_for_review = False
        while page_count < max_pages:
            page_index = page_count
            page_image = screenshot()
            page_ocr = page_image.ocr()
            self.recorder.capture(f"page-{page_index:02d}", page_image, page_ocr)
            page_matrix = page_image.image.copy()
            page_count += 1
            if previous_matrix is not None:
                difference = _content_difference(previous_matrix, page_matrix)
                stable = stable + 1 if difference <= 4.0 else 0
                logger.debug(f"商店第 {page_index} 页差异: {difference:.3f}")
            for item_id, purchase in list(pending.items()):
                located = locate_product(
                    page_ocr, purchase.item, page_image=page_image,
                )
                if not located:
                    continue
                if located.remaining <= 0:
                    result = {
                        "id": item_id,
                        "name": purchase.item.name,
                        "status": "sold_out",
                        "remaining": 0,
                    }
                    results.append(result)
                    if not catalog_probe and not dry_run:
                        record_shop_attempt(
                            purchase.item,
                            purchase.quantity,
                            status="sold_out",
                        )
                elif catalog_probe:
                    results.append(
                        {
                            "id": item_id,
                            "name": purchase.item.name,
                            "status": "found",
                            "remaining": located.remaining,
                            "limit": located.total,
                        }
                    )
                else:
                    try:
                        result = self.purchase(
                            located, purchase.quantity, dry_run,
                        )
                    except BlockedBySafetyError as error:
                        self._recover_to_shop_page()
                        results.append({
                            "id": item_id,
                            "name": purchase.item.name,
                            "status": "failed",
                            "remaining": located.remaining,
                            "error": str(error),
                        })
                        pending.pop(item_id, None)
                        page_matrix = screenshot().image.copy()
                        continue
                    results.append(result)
                    if result["status"] == "submitted_unverified":
                        stop_for_review = True
                    elif not dry_run:
                        page_image = screenshot()
                        page_ocr = page_image.ocr()
                        page_matrix = page_image.image.copy()
                pending.pop(item_id, None)
                if stop_for_review:
                    break
            # A configured purchase may stop as soon as every requested item
            # has been handled.  A catalog probe must still prove it reached
            # the actual bottom with two consecutive stable swipes, otherwise
            # newly added/unknown products below the known catalog are missed.
            reached_bottom = stable >= 2
            if reached_bottom or stop_for_review or (not catalog_probe and not pending):
                break
            previous_matrix = page_matrix
            if page_count >= max_pages:
                break
            input_swipe(
                PRODUCT_SCROLL_START, PRODUCT_SCROLL_END, swipe_time=650,
                intent=ActionIntent("shop_catalog_scroll", "shop_catalog_content", f"scan-page-{page_count:02d}"),
            )
            time.sleep(1.0)
        if catalog_probe and not reached_bottom:
            raise BlockedBySafetyError(
                f"商店扫描达到 {max_pages} 页安全上限，仍未确认触底"
            )
        page_limit_reached = (
            bool(pending) and page_count >= max_pages and not reached_bottom
        )
        missing = [
            {"id": purchase.item.id, "name": purchase.item.name}
            for purchase in pending.values()
        ]
        attention_statuses = {"failed", "insufficient_currency", "submitted_unverified"}
        requires_attention = bool(
            missing
            or stop_for_review
            or page_limit_reached
            or any(result.get("status") in attention_statuses for result in results)
        )
        return {
            "success": not requires_attention,
            "shop": self.shop.id,
            "pages": page_count,
            "scan_page_limit": max_pages,
            "reached_bottom": reached_bottom,
            "page_limit_reached": page_limit_reached,
            "requires_attention": requires_attention,
            "results": results,
            "missing": missing,
        }


def _is_bureau_shop_page(ocr_items: Iterable[dict]) -> bool:
    texts = tuple(_normalize_text(item.get("text")) for item in ocr_items)
    bureau_titles = sum(text == "赴命商店" for text in texts)
    return bureau_titles >= 2 or "EXCHANGESTATION" in texts


def _bureau_dialog_item_visible(
    ocr_items: Iterable[dict], item: ReadOnlyShopItem,
) -> bool:
    texts = tuple(
        _normalize_bureau_dialog_identity(value.get("text"))
        for value in ocr_items
    )
    return any(
        _normalize_bureau_dialog_identity(alias) in text
        for alias in item.ocr_aliases
        for text in texts
    )


def _bureau_dialog_cost_candidates(
    ocr_items: Iterable[dict],
) -> tuple[tuple[int, float], ...]:
    values: list[tuple[int, float]] = []
    for item in ocr_items:
        center_x, center_y = _center(item)
        if not (
            BUREAU_DIALOG_COST_REGION[0] <= center_x <= BUREAU_DIALOG_COST_REGION[2]
            and BUREAU_DIALOG_COST_REGION[1] <= center_y <= BUREAU_DIALOG_COST_REGION[3]
        ):
            continue
        amount = _numeric_value(item.get("text"))
        if amount is not None and amount > 0:
            values.append((amount, center_x))
    return tuple(sorted(values, key=lambda value: value[1]))


def _bureau_dialog_snapshot(
    frame: object,
    ocr_items: Iterable[dict],
    item: ReadOnlyShopItem,
    *,
    expected_quantity: int,
    expected_maximum: int | None = None,
    channel_order: tuple[str, ...] | None = None,
    channel_x_order: tuple[float, ...] = (),
) -> dict:
    """Read one verified quantity-dialog state without emitting input."""

    data = list(ocr_items)
    dialog_ok = (
        _validate_increment_session_frame(
            frame, data, item,
            expected_quantity=expected_quantity,
            expected_maximum=expected_maximum or 0,
            channel_order=channel_order or (),
            channel_x_order=channel_x_order,
        )
        if channel_order is not None
        else _has_complete_bureau_quantity_dialog(frame, data, item)
    )
    if not dialog_ok:
        raise BlockedBySafetyError(
            f"赴命商品未出现完整数量弹窗: {item.name}"
        )
    # In continuity mode the frame validator already checked item identity;
    # do not re-apply _bureau_dialog_item_visible (which would reject a
    # frame where V6 temporarily dropped the confirmation line).
    if channel_order is None and not _bureau_dialog_item_visible(data, item):
        raise BlockedBySafetyError(f"赴命商品弹窗身份不匹配: {item.name}")
    quantity = _dialog_quantity(data)
    if quantity is None or quantity[0] != expected_quantity:
        raise BlockedBySafetyError(
            f"赴命商品数量变化不符合预期: {item.name}，预期 {expected_quantity}，实机 {quantity}"
        )
    if expected_maximum is not None and quantity[1] != expected_maximum:
        raise BlockedBySafetyError(
            f"赴命商品弹窗上限发生变化: {item.name}"
        )
    candidates = _bureau_dialog_cost_candidates(data)
    if len(candidates) != len(item.costs):
        raise BlockedBySafetyError(
            f"赴命商品成本通道数量不稳定: {item.name}，"
            f"目录 {len(item.costs)}，实机 {len(candidates)}"
        )
    if channel_order is None:
        remaining = list(candidates)
        ordered: list[tuple[str, int, float]] = []
        for cost in item.costs:
            matches = [entry for entry in remaining if entry[0] == cost.amount]
            if len(matches) != 1:
                raise BlockedBySafetyError(
                    f"赴命商品首档成本无法唯一绑定: {item.name}:{cost.currency}"
                )
            match = matches[0]
            remaining.remove(match)
            ordered.append((cost.currency, match[0], match[1]))
        ordered.sort(key=lambda entry: entry[2])
        channel_order = tuple(entry[0] for entry in ordered)
        channel_x_order = tuple(entry[2] for entry in ordered)
        totals = {currency: amount for currency, amount, _x in ordered}
    else:
        if len(channel_order) != len(candidates):
            raise BlockedBySafetyError(f"赴命商品成本通道顺序丢失: {item.name}")
        totals = {
            currency: candidates[index][0]
            for index, currency in enumerate(channel_order)
        }
        channel_x_order = tuple(
            float(candidates[index][1])
            for index in range(len(channel_order))
        )
    return {
        "quantity": quantity[0],
        "maximum": quantity[1],
        "channel_order": channel_order,
        "channel_x_order": channel_x_order,
        "totals": totals,
    }


class BureauReadOnlyCatalogAdapter:
    """Read-only bureau catalog and quantity-price observer.

    The adapter is deliberately absent from ``ADAPTERS`` and has no exchange
    confirmation path.  Its bounded detail probe may only open a proven row,
    increment quantity, read cumulative costs, and cancel a still-proven
    quantity dialog.
    """

    key = "bureau_exchange_read_only"

    def __init__(
        self,
        shop: ShopDefinition,
        catalog: ReadOnlyShopCatalog,
        recorder: ShopEvidenceRecorder,
    ):
        self.shop = shop
        self.catalog = catalog
        self.recorder = recorder
        self.dialog_open_dispatches = 0
        self.quantity_increment_dispatches = 0
        self.dialog_cancel_dispatches = 0

    def _wait_for_bureau_page(self, timeout: float = 10.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            frame = screenshot()
            ocr_items = frame.ocr()
            if _is_bureau_shop_page(ocr_items):
                return frame, ocr_items
            time.sleep(0.35)
        raise BlockedBySafetyError("未能确认进入赴命商店只读页面")

    def _cancel_verified_quantity_dialog(self, item: ReadOnlyShopItem) -> None:
        dismissed = input_tap(
            DIALOG_CANCEL_POS,
            random_offset=False,
            intent=ActionIntent(
                "shop_quantity_cancel", "shop_quantity_cancel_button", item.id
            ),
        )
        if dismissed is False:
            raise BlockedBySafetyError(
                f"赴命商品数量弹窗取消点击被拒绝: {item.name}"
            )
        self.dialog_cancel_dispatches += 1
        time.sleep(0.8)
        frame = screenshot()
        ocr_items = frame.ocr()
        self.recorder.capture(
            f"bureau-price-cancel-{item.id}", frame, ocr_items
        )
        if not _is_bureau_shop_page(ocr_items):
            raise BlockedBySafetyError(
                f"赴命商品数量弹窗取消后未返回商店: {item.name}"
            )

    def _probe_price_schedule(self, match: dict, item: ReadOnlyShopItem) -> dict:
        """Open one quantity dialog, read every marginal, then cancel once.

        The right-side exchange control is used when the live row proves that
        more than one unit remains, or when the remaining count is unavailable
        but the row identity and exchange control were uniquely bound.  In the
        latter case the verified quantity dialog supplies the authoritative
        probe maximum.  A one-unit row already contains its complete marginal
        schedule, so it never needs an item-level click.
        """

        observed_limit = str(match.get("observed_limit") or "")
        remaining = _bureau_remaining(observed_limit)
        base = {
            "id": item.id,
            "name": item.name,
            "observed_limit": observed_limit or "未稳定识别",
            "source": "live_read_only_quantity_probe",
        }
        if remaining == 0:
            return {
                **base,
                "status": "price_probe_unavailable",
                "error": "商品已售罄，无法打开数量弹窗核验边际价格",
                "price_observations": [],
            }
        if remaining == 1:
            return {
                **base,
                "status": "price_schedule_validated",
                "price_observations": [{
                    "quantity": 1,
                    "costs": [
                        {
                            "currency": cost.currency,
                            "marginal_cost": cost.amount,
                            "cumulative_cost": cost.amount,
                        }
                        for cost in item.costs
                    ],
                }],
            }
        if remaining is None and str(match.get("id") or "") != item.id:
            return {
                **base,
                "status": "price_probe_unavailable",
                "error": "剩余次数未知且商品身份未唯一绑定，未打开商品",
                "price_observations": [],
            }
        point = match.get("exchange_point")
        if not (
            isinstance(point, tuple)
            and len(point) == 2
            and all(isinstance(value, int) for value in point)
        ):
            return {
                **base,
                "status": "price_probe_unavailable",
                "error": "未唯一识别本行兑换控件，未打开商品",
                "price_observations": [],
            }
        opened = input_tap(
            point,
            random_offset=False,
            intent=ActionIntent(
                "shop_bureau_quantity_open", "bureau_exchange_control", item.id
            ),
        )
        if opened is False:
            return {
                **base,
                "status": "price_probe_unavailable",
                "error": "数量弹窗打开点击被拒绝",
                "price_observations": [],
            }
        self.dialog_open_dispatches += 1
        time.sleep(0.8)
        frame = screenshot()
        ocr_items = frame.ocr()
        self.recorder.capture(f"bureau-price-01-{item.id}", frame, ocr_items)
        # No cancel is dispatched until the page is independently proven to be
        # a quantity dialog.  An unexpected post-page therefore terminates the
        # complete scan with zero additional input.
        if not _has_complete_bureau_quantity_dialog(frame, ocr_items, item):
            raise BlockedBySafetyError(
                f"赴命商品点击后未出现数量弹窗: {item.name}"
            )
        try:
            first = _bureau_dialog_snapshot(
                frame, ocr_items, item, expected_quantity=1
            )
        except BlockedBySafetyError as error:
            self._cancel_verified_quantity_dialog(item)
            return {
                **base,
                "status": "price_probe_failed",
                "error": str(error),
                "price_observations": [],
            }
        maximum = int(first["maximum"])
        increment_limit = _bureau_quantity_probe_increment_limit(item)
        exceeds_observed_remaining = (
            remaining is not None and maximum > remaining
        )
        if (
            exceeds_observed_remaining
            or maximum - 1 > increment_limit
        ):
            self._cancel_verified_quantity_dialog(item)
            remaining_label = (
                str(remaining) if remaining is not None else "UNKNOWN"
            )
            return {
                **base,
                "status": "price_probe_failed",
                "error": (
                    f"赴命商品弹窗上限超出只读探针预算: {item.name}，"
                    f"剩余 {remaining_label}，弹窗 {maximum}，预算 {increment_limit}"
                ),
                "price_observations": [],
            }
        observations: list[dict] = []
        previous_totals: dict[str, int] = {}
        previous_marginals: dict[str, int] = {}
        channel_order = tuple(first["channel_order"])
        channel_x_order = tuple(first["channel_x_order"])
        snapshot = first
        probe_error: BlockedBySafetyError | None = None
        dialog_still_verified = True
        try:
            for quantity in range(1, maximum + 1):
                if quantity > 1:
                    dispatched = input_tap(
                        DIALOG_PLUS_POS,
                        random_offset=False,
                        intent=ActionIntent(
                            "shop_quantity_increment",
                            "shop_quantity_increment_button",
                            item.id,
                        ),
                    )
                    if dispatched is False:
                        raise BlockedBySafetyError(
                            f"赴命商品数量增加点击被拒绝: {item.name}"
                        )
                    self.quantity_increment_dispatches += 1
                    # Once +1 was dispatched, no further coordinate input is
                    # safe until a fresh frame independently proves that the
                    # quantity dialog is still present.
                    dialog_still_verified = False
                    time.sleep(0.55)
                    frame = screenshot()
                    ocr_items = frame.ocr()
                    self.recorder.capture(
                        f"bureau-price-{quantity:02d}-{item.id}",
                        frame,
                        ocr_items,
                    )
                    if not _validate_increment_session_frame(
                        frame, ocr_items, item,
                        expected_quantity=quantity,
                        expected_maximum=maximum,
                        channel_order=channel_order,
                        channel_x_order=channel_x_order,
                    ):
                        raise BlockedBySafetyError(
                            f"赴命商品数量增加后页面身份不明: {item.name}"
                        )
                    dialog_still_verified = True
                    snapshot = _bureau_dialog_snapshot(
                        frame,
                        ocr_items,
                        item,
                        expected_quantity=quantity,
                        expected_maximum=maximum,
                        channel_order=channel_order,
                        channel_x_order=channel_x_order,
                    )
                costs: list[dict] = []
                for currency in channel_order:
                    total = int(snapshot["totals"][currency])
                    previous_total = previous_totals.get(currency, 0)
                    marginal = total - previous_total
                    if marginal <= 0 or marginal < previous_marginals.get(currency, 0):
                        raise BlockedBySafetyError(
                            f"赴命商品边际成本不满足单调递增合同: "
                            f"{item.name}:{currency}"
                        )
                    costs.append({
                        "currency": currency,
                        "marginal_cost": marginal,
                        "cumulative_cost": total,
                    })
                    previous_totals[currency] = total
                    previous_marginals[currency] = marginal
                observations.append({"quantity": quantity, "costs": costs})
        except BlockedBySafetyError as error:
            if not dialog_still_verified:
                # The post-dispatch page is unknown.  Do not guess that the
                # old cancel coordinate is still valid.
                raise
            probe_error = error
        except StopExecution:
            raise
        except Exception:
            raise
        self._cancel_verified_quantity_dialog(item)
        if probe_error is not None:
            return {
                **base,
                "status": "price_probe_failed",
                "error": str(probe_error),
                "price_observations": [],
            }
        return {
            **base,
            "status": "price_schedule_validated",
            "dialog_maximum": maximum,
            "price_observations": observations,
        }

    def open(self) -> None:
        initial = screenshot()
        initial_ocr = initial.ocr()
        if _is_bureau_shop_page(initial_ocr):
            self.recorder.capture("bureau-existing", initial, initial_ocr)
        else:
            initial_texts = {
                _normalize_text(item.get("text")) for item in initial_ocr
            }
            if not {"总部商店", "赴命商店"}.issubset(initial_texts):
                if not go_home():
                    raise BlockedBySafetyError("无法返回主界面，未进入赴命商店")
                self.recorder.capture("bureau-home")
                input_tap(
                    SHOP_ENTRY_POS,
                    random_offset=False,
                    intent=ActionIntent(
                        "shop_page_open", "shop_entry", "bureau_exchange"
                    ),
                )
                image, ocr_items = _wait_for_text(
                    ("总部商店", "赴命商店"), timeout=10
                )
                self.recorder.capture("bureau-selector", image, ocr_items)
            input_tap(
                BUREAU_TAB_POS,
                random_offset=False,
                intent=ActionIntent(
                    "shop_bureau_open", "bureau_shop_tab", "bureau_exchange"
                ),
            )
            image, ocr_items = self._wait_for_bureau_page()
            self.recorder.capture("bureau-open", image, ocr_items)
        self._rewind_to_top()

    def _rewind_to_top(self) -> None:
        previous = screenshot()
        stable = 0
        for index in range(14):
            input_swipe(
                PRODUCT_REWIND_START,
                PRODUCT_REWIND_END,
                swipe_time=650,
                intent=ActionIntent(
                    "shop_catalog_rewind",
                    "shop_catalog_content",
                    f"bureau-rewind-{index:02d}",
                ),
            )
            time.sleep(0.9)
            current = screenshot()
            ocr_items = current.ocr()
            if not _is_bureau_shop_page(ocr_items):
                raise BlockedBySafetyError("赴命商店回顶后页面身份丢失")
            difference = _content_difference(
                previous.image, current.image, BUREAU_PRODUCT_REGION
            )
            self.recorder.capture(
                f"bureau-rewind-{index:02d}", current, ocr_items
            )
            stable = stable + 1 if difference <= 4.0 else 0
            previous = current
            if stable >= 2:
                return
        raise BlockedBySafetyError("赴命商店回顶超过安全滑动次数")

    def scan(
        self,
        max_pages: int = MAX_SCAN_PAGES,
        *,
        probe_prices: bool = False,
    ) -> dict:
        if isinstance(max_pages, bool) or not isinstance(max_pages, int) or max_pages < 1:
            raise ValueError("赴命商店扫描页数上限必须是正整数")
        pending = {item.id: item for item in self.catalog.items}
        results: list[dict] = []
        page_count = 0
        stable = 0
        previous_matrix = None
        while page_count < max_pages:
            frame = screenshot()
            ocr_items = frame.ocr()
            if not _is_bureau_shop_page(ocr_items):
                raise BlockedBySafetyError("赴命商店扫描期间页面身份丢失")
            self.recorder.capture(
                f"bureau-page-{page_count:02d}", frame, ocr_items
            )
            matrix = frame.image.copy()
            page_count += 1
            if previous_matrix is not None:
                difference = _content_difference(
                    previous_matrix, matrix, BUREAU_PRODUCT_REGION
                )
                stable = stable + 1 if difference <= 4.0 else 0
            for item_id, item in list(pending.items()):
                match = locate_read_only_bureau_item(ocr_items, item)
                if match is None:
                    continue
                open_dispatches_before = self.dialog_open_dispatches
                results.append(
                    self._probe_price_schedule(match, item)
                    if probe_prices
                    else match
                )
                pending.pop(item_id, None)
                if (
                    probe_prices
                    and self.dialog_open_dispatches > open_dispatches_before
                ):
                    # Cancel has returned to the list, but every following row
                    # must be rebound to a fresh frame rather than reusing the
                    # coordinates captured before the dialog was opened.
                    frame = screenshot()
                    ocr_items = frame.ocr()
                    if not _is_bureau_shop_page(ocr_items):
                        raise BlockedBySafetyError(
                            "赴命商品价格探针返回后页面身份丢失"
                        )
                    self.recorder.capture(
                        f"bureau-price-list-refresh-{item.id}",
                        frame,
                        ocr_items,
                    )
                    matrix = frame.image.copy()
            reached_bottom = stable >= 2
            if reached_bottom:
                break
            previous_matrix = matrix
            if page_count >= max_pages:
                break
            input_swipe(
                PRODUCT_SCROLL_START,
                PRODUCT_SCROLL_END,
                swipe_time=650,
                intent=ActionIntent(
                    "shop_catalog_scroll",
                    "shop_catalog_content",
                    f"bureau-page-{page_count:02d}",
                ),
            )
            time.sleep(1.0)
        missing = [
            {"id": item.id, "name": item.name}
            for item in pending.values()
        ]
        reached_bottom = stable >= 2
        incomplete_price_statuses = {
            "price_probe_unavailable", "price_probe_failed"
        }
        requires_attention = bool(
            missing
            or not reached_bottom
            or any(
                result.get("status") in incomplete_price_statuses
                for result in results
            )
        )
        return {
            "success": not requires_attention,
            "shop": self.shop.id,
            "mode": "read_only_catalog",
            "pages": page_count,
            "scan_page_limit": max_pages,
            "reached_bottom": reached_bottom,
            "page_limit_reached": page_count >= max_pages and not reached_bottom,
            "requires_attention": requires_attention,
            "results": results,
            "missing": missing,
            "business_actions": 0,
            "exchange_actions": 0,
            "dialog_open_dispatches": self.dialog_open_dispatches,
            "quantity_increment_dispatches": self.quantity_increment_dispatches,
            "dialog_cancel_dispatches": self.dialog_cancel_dispatches,
            "confirm_dispatches": 0,
            "same_action_retries": 0,
        }


ADAPTERS: dict[
    str,
    Callable[[ShopDefinition, ShopEvidenceRecorder], HeadquartersBlackMoonAdapter],
] = {
    HeadquartersBlackMoonAdapter.key: HeadquartersBlackMoonAdapter,
}


def register_shop_adapter(key: str, factory: Callable) -> None:
    """Register an additional shop without changing the planner or scheduler."""
    ADAPTERS[str(key)] = factory


def _connected_run(callback: Callable[[], object]) -> object:
    transport = "adb"
    try:
        try:
            adb_connected = connect_adb()
        except Exception as error:  # MuMu may leave its TCP transport offline
            adb_connected = False
            logger.warning(
                "TCP ADB 连接异常，将尝试模拟器 IPC: "
                f"{type(error).__name__}: {error}"
            )
        if not adb_connected:
            if not connect():
                raise BlockedBySafetyError("无法通过 ADB 或模拟器 IPC 连接模拟器")
            transport = "nemu_ipc"
            logger.warning("本次商店任务使用 NEMUIPC 回退传输")
        result = callback()
        if isinstance(result, dict):
            result["transport"] = transport
        return result
    finally:
        try:
            kill()
        except Exception:
            # Cleanup must not hide the original connection/callback result.
            # The next run creates a fresh transport object either way.
            logger.exception("商店任务结束后释放模拟器连接失败")


def probe_shop_catalog(capture_evidence: bool = True) -> dict:
    """Read-only live catalog probe used by the persistent debug runtime."""
    catalog = load_shop_catalog()
    shop = catalog.shop("headquarters_black_moon")
    recorder = ShopEvidenceRecorder(capture_evidence, "probe")

    def run() -> dict:
        adapter = HeadquartersBlackMoonAdapter(shop, recorder)
        adapter.open()
        return adapter.scan(purchases=None)

    return _connected_run(run)  # type: ignore[return-value]


def probe_bureau_shop_catalog(capture_evidence: bool = True) -> dict:
    """Run the bureau's live read-only catalog observer.

    This entry point is intentionally separate from ``run_shop_purchase`` and
    from the executable adapter registry.  It cannot issue an exchange action.
    """

    catalog = load_shop_catalog()
    shop = catalog.shop("bureau_exchange")
    if not shop.read_only_catalog:
        raise BlockedBySafetyError("赴命商店缺少只读证据目录")
    observed = load_read_only_shop_catalog(
        shop.read_only_catalog, catalog.currencies
    )
    recorder = ShopEvidenceRecorder(capture_evidence, "bureau-read-only")

    def run() -> dict:
        adapter = BureauReadOnlyCatalogAdapter(shop, observed, recorder)
        adapter.open()
        shop_result = adapter.scan(probe_prices=True)
        return {
            "success": bool(shop_result.get("success")),
            "dry_run": True,
            "read_only": True,
            "requires_attention": bool(shop_result.get("requires_attention")),
            "shops": [shop_result],
        }

    result = _connected_run(run)
    if not isinstance(result, dict):
        raise TypeError("赴命商店只读扫描返回了无效结果")
    result["completed_at"] = datetime.now().astimezone().isoformat(
        timespec="seconds"
    )
    if bool(getattr(recorder, "enabled", False)):
        result["result_file"] = str(recorder.root / "FINAL_RESULT.json")
        try:
            recorder.write_result(result)
        except Exception:
            result["result_file"] = ""
            logger.exception("保存赴命商店只读扫描结果失败")
    else:
        result["result_file"] = ""
    return result


def probe_shop_quantity_dialog(
    item_id: str = "cactus_energy_weekly_iron",
    quantity: str = "max",
    capture_evidence: bool = True,
) -> dict:
    """Validate one complete item/dialog/quantity flow and always cancel it."""
    catalog = load_shop_catalog()
    item = catalog.item(item_id)
    shop = catalog.shop(item.shop_id)
    if quantity not in {"one", "max"}:
        raise ValueError(f"未知购买数量模式: {quantity}")
    recorder = ShopEvidenceRecorder(capture_evidence, "dialog-probe")
    purchase = ConfiguredPurchase(shop=shop, item=item, quantity=quantity)

    def run() -> dict:
        factory = ADAPTERS.get(shop.adapter)
        if not factory:
            raise RuntimeError(f"商店尚未注册自动化适配器: {shop.name}")
        adapter = factory(shop, recorder)
        adapter.open()
        return adapter.scan([purchase], dry_run=True)

    return _connected_run(run)  # type: ignore[return-value]


def run_shop_purchase(dry_run: bool = False) -> dict:
    catalog = load_shop_catalog()
    plan = load_shop_plan(catalog=catalog)
    purchases = configured_purchases(plan, catalog)
    if not purchases:
        return {"success": True, "skipped": "未启用任何自动购买商品", "shops": []}
    blocked_by_period = []
    if not dry_run:
        due_purchases = []
        for purchase in purchases:
            attempt = active_shop_attempt(purchase.item)
            if attempt:
                blocked_by_period.append(
                    {
                        "id": purchase.item.id,
                        "name": purchase.item.name,
                        "status": attempt.get("status", "attempted"),
                        "blocked_until": attempt.get("blocked_until", ""),
                    }
                )
            else:
                due_purchases.append(purchase)
        purchases = due_purchases
    if not purchases:
        return {
            "success": True,
            "skipped": "所有已选商品均已在当前刷新周期处理",
            "blocked_by_period": blocked_by_period,
            "shops": [],
        }
    grouped: dict[str, list[ConfiguredPurchase]] = {}
    for purchase in purchases:
        grouped.setdefault(purchase.shop.id, []).append(purchase)
    recorder = ShopEvidenceRecorder(bool(plan["capture_evidence"]), "dry" if dry_run else "run")

    def run() -> dict:
        shop_results = []
        for shop_id, shop_purchases in grouped.items():
            shop = catalog.shop(shop_id)
            factory = ADAPTERS.get(shop.adapter)
            if not factory:
                raise RuntimeError(f"商店尚未注册自动化适配器: {shop.name}")
            adapter = factory(shop, recorder)
            adapter.open()
            shop_results.append(adapter.scan(shop_purchases, dry_run=dry_run))
        requires_attention = any(
            bool(result.get("requires_attention")) for result in shop_results
        )
        return {
            "success": not requires_attention,
            "dry_run": dry_run,
            "requires_attention": requires_attention,
            "blocked_by_period": blocked_by_period,
            "shops": shop_results,
        }

    result = _connected_run(run)
    if not isinstance(result, dict):
        raise TypeError("商店任务返回了无效结果")
    result["completed_at"] = datetime.now().astimezone().isoformat(
        timespec="seconds"
    )
    if bool(getattr(recorder, "enabled", False)):
        result["result_file"] = str(recorder.root / "FINAL_RESULT.json")
        try:
            recorder.write_result(result)
        except Exception:
            result["result_file"] = ""
            logger.exception("保存商店任务最终结果失败")
    else:
        result["result_file"] = ""
    return result
