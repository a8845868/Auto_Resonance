from __future__ import annotations

import re
import time
import json
from collections import Counter

import cv2 as cv
import numpy as np

from loguru import logger

from core.control.control import connect, input_swipe, input_tap, screenshot
from core.exception.exceptions import StopExecution
from core.preset import go_home
from core.preset.control import blurry_ocr_click
from core.services.screen_state import (
    is_inventory_item_detail,
    is_inventory_screen,
    is_train_in_transit as _is_train_in_transit,
)
from core.utils.utils import RESOURCES_PATH
from core.services.inventory_assets import Asset, merge_assets, parse_amount, parse_ocr_assets


def _center(item: dict) -> tuple[float, float]:
    position = item["position"]
    return ((position[0][0] + position[2][0]) / 2, (position[0][1] + position[2][1]) / 2)


STATION_PRIMARY_CURRENCIES = {
    "武林源": "交子",
}


def _home_primary_currency(items: list[dict]) -> list[Asset]:
    """Map the home-screen `资产` balance to the current station's currency."""
    label = next((item for item in items if item["text"].strip() == "资产"), None)
    if not label:
        return []
    screen_text = " ".join(str(item.get("text", "")).replace(" ", "") for item in items)
    currency_name = next(
        (
            currency
            for station, currency in STATION_PRIMARY_CURRENCIES.items()
            if station in screen_text
        ),
        "铁盟币",
    )
    x, y = _center(label)
    candidates = []
    for item in items:
        amount = parse_amount(item["text"])
        if amount is None or not item["text"].replace(",", "").isdigit():
            continue
        nx, ny = _center(item)
        if 0 <= nx - x <= 180 and abs(ny - y) <= 35:
            candidates.append((abs(nx - x) + abs(ny - y), amount))
    return [Asset(currency_name, min(candidates)[1], "货币")] if candidates else []


def _home_iron_currency(items: list[dict]) -> list[Asset]:
    """Backward-compatible wrapper for callers/tests using the old name."""
    return _home_primary_currency(items)


def _read_unicode_image(path, flags=cv.IMREAD_COLOR) -> cv.typing.MatLike | None:
    try:
        return cv.imdecode(np.fromfile(str(path), dtype=np.uint8), flags)
    except OSError:
        return None


def _parse_currency_icons(image, ocr_items: list[dict]) -> list[Asset]:
    """Identify icon-only currencies on the Assets screen and pair their lower labels."""
    icon_root = RESOURCES_PATH / "currency"
    try:
        manifest = json.loads((icon_root / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    numbers = []
    for item in ocr_items:
        raw = item["text"].replace(",", "").strip()
        if not raw.isdigit():
            continue
        amount = parse_amount(raw)
        if amount is not None:
            numbers.append((*_center(item), amount))
    height, width = image.shape[:2]
    x1, x2 = int(width * 0.30), int(width * 0.85)
    y1, y2 = int(height * 0.10), int(height * 0.92)
    roi = image[y1:y2, x1:x2]
    found = []
    # These two can appear deeper in the item list. Core currencies use the
    # stable first-page grid below because Wiki thumbnails include a different background.
    for name in ("里程点数", "黑月采购券"):
        filename = manifest.get(name)
        template = _read_unicode_image(icon_root / filename) if filename else None
        if template is None:
            continue
        best_score, best_center = 0.0, None
        for scale_percent in range(55, 126, 5):
            resized = cv.resize(template, None, fx=scale_percent / 100, fy=scale_percent / 100, interpolation=cv.INTER_AREA)
            th, tw = resized.shape[:2]
            if th >= roi.shape[0] or tw >= roi.shape[1]:
                continue
            _, score, _, location = cv.minMaxLoc(cv.matchTemplate(roi, resized, cv.TM_CCOEFF_NORMED))
            if score > best_score:
                best_score = score
                best_center = (x1 + location[0] + tw / 2, y1 + location[1] + th / 2)
        if best_center is None or best_score < 0.76:
            logger.debug(f"资产图标未匹配：{name}（{best_score:.3f}）")
            continue
        ix, iy = best_center
        nearby = [
            (abs(nx - ix) + (ny - iy) * 0.4, amount)
            for nx, ny, amount in numbers
            if abs(nx - ix) <= 75 and 15 <= ny - iy <= 105
        ]
        if nearby:
            amount = min(nearby)[1]
            logger.info(f"资产图标识别：{name} {amount}（匹配度 {best_score:.3f}）")
            found.append(Asset(name, amount, "货币"))
    return found


def _parse_primary_currency_grid(image, ocr_items: list[dict]) -> list[Asset]:
    """Read the stable currency slots on the first Assets page."""
    height, width = image.shape[:2]
    numbers = []
    for item in ocr_items:
        raw = item["text"].replace(",", "").strip()
        if raw.isdigit():
            numbers.append((*_center(item), int(raw)))
    # Count label centers observed on the normalized 1280x720 game canvas.
    slots = {
        "桦石": (0.495, 0.290),
        "交子": (0.570, 0.290),
        "铁盟币": (0.684, 0.290),
        "绝命奖章": (0.804, 0.290),
        "赴命奖章": (0.390, 0.480),
    }
    found = []
    for name, (x_ratio, y_ratio) in slots.items():
        tx, ty = width * x_ratio, height * y_ratio
        candidates = [
            (abs(x - tx) + abs(y - ty), amount)
            for x, y, amount in numbers
            if abs(x - tx) <= width * 0.045 and abs(y - ty) <= height * 0.035
        ]
        if candidates:
            amount = min(candidates)[1]
            logger.info(f"资产槽位识别：{name} {amount}")
            found.append(Asset(name, amount, "货币"))
    return found


def _white_icon_mask(image: cv.typing.MatLike) -> cv.typing.MatLike:
    """Keep the bright low-saturation glyph and discard its colored background."""
    hsv = cv.cvtColor(image, cv.COLOR_BGR2HSV)
    mask = cv.inRange(hsv, np.array([0, 0, 170]), np.array([180, 90, 255]))
    return cv.morphologyEx(mask, cv.MORPH_OPEN, np.ones((2, 2), dtype=np.uint8))


def _find_assets_entry(image) -> tuple[tuple[int, int] | None, float]:
    """Find the Assets entry from its white glyph, ignoring transparent/blue pixels."""
    template = cv.imread(str(RESOURCES_PATH / "inventory" / "assets_entry.png"))
    if template is None:
        return None, 0.0
    # The stored screenshot contains old scenery around the icon. Only the
    # central white six-part glyph is stable across themes and game versions.
    th, tw = template.shape[:2]
    template = template[int(th * 0.16) : int(th * 0.87), int(tw * 0.16) : int(tw * 0.87)]
    template_mask = _white_icon_mask(template)
    ys, xs = np.where(template_mask > 0)
    if not len(xs):
        return None, 0.0
    template_mask = template_mask[ys.min() : ys.max() + 1, xs.min() : xs.max() + 1]
    # The icon lives in the top-right toolbar. Cropping avoids visually similar
    # white hexagons elsewhere on the home screen and makes matching faster.
    height, width = image.shape[:2]
    x1, y1 = int(width * 0.58), 0
    roi = image[y1 : int(height * 0.18), x1:width]
    roi_mask = _white_icon_mask(roi)
    best_score = 0.0
    best_loc = None
    for scale_percent in range(60, 141, 5):
        scale = scale_percent / 100
        resized = cv.resize(template_mask, None, fx=scale, fy=scale, interpolation=cv.INTER_NEAREST)
        glyph_h, glyph_w = resized.shape[:2]
        if glyph_h >= roi_mask.shape[0] or glyph_w >= roi_mask.shape[1]:
            continue
        result = cv.matchTemplate(roi_mask, resized, cv.TM_CCOEFF_NORMED)
        _, score, _, loc = cv.minMaxLoc(result)
        if score > best_score:
            best_score = score
            best_loc = (x1 + loc[0] + glyph_w // 2, y1 + loc[1] + glyph_h // 2)
    return (best_loc if best_score >= 0.70 else None), best_score


def _find_assets_text_entry(items: list[dict], width: int, height: int):
    """Find the bottom-left balance label only as a station-home guard.

    The label itself is not an entry.  Older code clicked it and then scanned
    the unchanged station screen as though it were the backpack, producing
    false icon matches.
    """
    for item in items:
        if str(item.get("text", "")).replace(" ", "").strip() != "资产":
            continue
        x, y = _center(item)
        if x <= width * 0.35 and y >= height * 0.78:
            return int(x), int(y)
    return None


def _is_assets_inventory_screen(items: list[dict]) -> bool:
    """Require the backpack's right-hand category rail before scanning it."""
    return is_inventory_screen(items)


def _wait_for_assets_inventory(timeout: float = 6.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _is_assets_inventory_screen(screenshot().ocr()):
            return True
        time.sleep(0.4)
    return False


def _open_assets_entry() -> bool:
    image = screenshot()
    height, width = image.image.shape[:2]
    items = image.ocr()
    if _is_assets_inventory_screen(items):
        return True
    # `资产` at bottom-left proves this is the station home, but is only a
    # balance display.  The actual backpack entry is the white cube in the
    # top-right toolbar.
    if not _find_assets_text_entry(items, width, height):
        logger.warning("当前画面未识别到站点主界面的资产余额，拒绝盲点背包入口")
        return False
    location, score = _find_assets_entry(image.image)
    candidates = []
    if location and location[0] >= width * 0.75 and location[1] <= height * 0.2:
        candidates.append((location, f"模板匹配度 {score:.3f}"))
    # Current UI: white cube icon at about 86% width / 9.5% height.  This
    # normalized fallback is used only after the station-home guard above.
    normalized_cube = (int(width * 0.858), int(height * 0.095))
    if not candidates or abs(candidates[0][0][0] - normalized_cube[0]) > width * 0.06:
        candidates.append((normalized_cube, "主界面归一化坐标"))
    for candidate, source in candidates:
        logger.info(f"点击右上角资产魔方图标：{candidate}（{source}）")
        input_tap(candidate)
        if _wait_for_assets_inventory():
            logger.info("已确认进入资产背包（识别到右侧道具/材料分类栏）")
            return True
    logger.error("点击资产魔方后仍未识别到背包分类栏，停止背包扫描")
    return False


def scan_inventory_assets(max_pages: int = 6) -> list[Asset]:
    """Scan visible currencies and backpack pages; duplicates keep the largest count."""
    if not connect():
        raise RuntimeError("ADB 连接失败，请先在“ADB信息”中确认模拟器连接")
    initial_items = screenshot().ocr()
    if _is_train_in_transit(initial_items):
        raise RuntimeError("列车正在行驶，当前无法打开资产页面；请到站后重新扫描")
    should_restore_home = False
    try:
        if not go_home():
            raise RuntimeError("无法返回站点主画面；请确认列车已到站后重试")
        # The home screen contains many unrelated numbers; only retain known currency-like rows.
        home_items = screenshot().ocr()
        currencies = _home_primary_currency(home_items)
        currencies.extend(asset for asset in parse_ocr_assets(home_items) if asset.category == "货币")
        snapshots = [currencies]
        if not _open_assets_entry():
            raise RuntimeError("未找到游戏主界面的资产入口图标；请停留在主界面后重试")
        should_restore_home = True
        time.sleep(1.2)
        first_frame = screenshot()
        first_ocr = first_frame.ocr()
        snapshots.append(_parse_primary_currency_grid(first_frame.image, first_ocr))
        snapshots.append(_parse_currency_icons(first_frame.image, first_ocr))
        previous_names: set[str] = set()
        for page in range(max_pages):
            page_frame = first_frame if page == 0 else screenshot()
            page_ocr = first_ocr if page == 0 else page_frame.ocr()
            if page > 0:
                snapshots.append(_parse_currency_icons(page_frame.image, page_ocr))
            current = parse_ocr_assets(page_ocr)
            snapshots.append(current)
            names = {asset.name for asset in current}
            if page > 0 and names == previous_names:
                break
            previous_names = names
            if page < max_pages - 1:
                input_swipe((930, 640), (930, 285), swipe_time=600)
                time.sleep(0.8)
        assets = merge_assets(*snapshots)
        logger.info(f"资产扫描完成：识别到 {len(assets)} 类物品")
        return assets
    finally:
        if should_restore_home:
            try:
                go_home()
            except Exception:
                logger.warning("资产扫描结束后返回主界面失败")


def _parse_count(text: str) -> int | None:
    match = re.search(r"(?:x|×)?\s*(\d{1,5})", text.replace(",", ""), re.IGNORECASE)
    return int(match.group(1)) if match else None


def _is_restock_book_name(text: str) -> bool:
    compact = str(text).replace(" ", "")
    return "进货" in compact and "书" in compact and any(
        marker in compact for marker in ("采买", "采购")
    )


def _restock_book_item(items: list[dict]) -> dict | None:
    return next(
        (item for item in items if _is_restock_book_name(item.get("text", ""))),
        None,
    )


def _find_restock_book_icon(image) -> tuple[tuple[int, int] | None, float]:
    """Locate the restock-book artwork in the item grid.

    Item names are hidden until an icon is opened, so OCR-only scanning can
    never discover the book from the grid.  The shipped transparent icon is
    matched with its alpha mask across the visible item area instead.
    """
    if not isinstance(image, np.ndarray) or image.ndim != 3:
        return None, 0.0
    template = _read_unicode_image(
        RESOURCES_PATH / "currency" / "进货采买书.png", cv.IMREAD_UNCHANGED
    )
    if template is None or template.ndim != 3 or template.shape[2] != 4:
        return None, 0.0

    alpha = template[:, :, 3]
    ys, xs = np.where(alpha >= 32)
    if not len(xs):
        return None, 0.0
    template_bgr = template[ys.min() : ys.max() + 1, xs.min() : xs.max() + 1, :3]
    template_mask = alpha[ys.min() : ys.max() + 1, xs.min() : xs.max() + 1]

    height, width = image.shape[:2]
    x1, x2 = int(width * 0.29), int(width * 0.85)
    y1, y2 = int(height * 0.10), int(height * 0.99)
    roi = image[y1:y2, x1:x2]
    best_score = 0.0
    best_center = None
    for scale_percent in range(55, 131, 5):
        scale = scale_percent / 100
        resized = cv.resize(template_bgr, None, fx=scale, fy=scale, interpolation=cv.INTER_AREA)
        mask = cv.resize(template_mask, (resized.shape[1], resized.shape[0]), interpolation=cv.INTER_NEAREST)
        th, tw = resized.shape[:2]
        if th >= roi.shape[0] or tw >= roi.shape[1]:
            continue
        scores = cv.matchTemplate(roi, resized, cv.TM_CCORR_NORMED, mask=mask)
        scores = np.nan_to_num(scores, nan=0.0, posinf=0.0, neginf=0.0)
        _, score, _, location = cv.minMaxLoc(scores)
        if score > best_score:
            best_score = score
            best_center = (x1 + location[0] + tw // 2, y1 + location[1] + th // 2)
    return (best_center if best_score >= 0.78 else None), best_score


def _restock_book_count_near_icon(
    items: list[dict], location: tuple[int, int]
) -> tuple[int, str] | None:
    """Pair an icon only with the count directly beneath its own grid cell."""
    x, y = location
    candidates = []
    for item in items:
        raw = str(item.get("text", "")).strip()
        count = _parse_count(raw)
        if count is None or not re.fullmatch(r"(?:x|×)?\s*\d{1,5}", raw, re.IGNORECASE):
            continue
        nx, ny = _center(item)
        if abs(nx - x) <= 70 and 15 <= ny - y <= 105:
            candidates.append((abs(nx - x) + abs(ny - y) * 0.5, count, raw))
    if not candidates:
        return None
    _, count, raw = min(candidates)
    return count, raw


def _confirm_restock_book_icon_count(initial_items, location, frames=3):
    readings = []
    raw_values = []
    items = initial_items
    for frame in range(frames):
        result = _restock_book_count_near_icon(items, location)
        if result:
            count, raw = result
            readings.append(count)
            raw_values.append(str(raw))
        if frame + 1 < frames:
            time.sleep(0.25)
            items = screenshot().ocr()
    if not readings:
        return None
    count, confirmations = Counter(readings).most_common(1)[0]
    if confirmations < 2:
        logger.warning(f"进货书图标下方数量多帧 OCR 不一致: {readings}")
        return None
    return count, ", ".join(raw_values)


def _restock_book_count_from_items(items: list[dict]) -> tuple[int, str] | None:
    """Pair the restock-book label only with a nearby/inline quantity."""
    for asset in parse_ocr_assets(items):
        if _is_restock_book_name(asset.name):
            return asset.count, asset.name

    book = _restock_book_item(items)
    if not book:
        return None
    # The opened detail card renders the canonical name on the left and
    # `拥有：N` on the upper-right, much farther away than a normal grid pair.
    for item in items:
        raw = str(item.get("text", ""))
        if "拥有" in raw and (count := _parse_count(raw)) is not None:
            return count, raw
    x, y = _center(book)
    candidates = []
    for item in items:
        count = _parse_count(str(item.get("text", "")))
        if count is None:
            continue
        nx, ny = _center(item)
        if abs(nx - x) <= 150 and -35 <= ny - y <= 170:
            candidates.append((abs(nx - x) + abs(ny - y) * 0.7, count, item["text"]))
    if not candidates:
        return None
    _, count, raw = min(candidates)
    return count, raw


def _confirm_restock_book_count(initial_items: list[dict], frames=3):
    readings = []
    raw_values = []
    items = initial_items
    for frame in range(frames):
        result = _restock_book_count_from_items(items)
        if result:
            count, raw = result
            readings.append(count)
            raw_values.append(str(raw))
        if frame + 1 < frames:
            time.sleep(0.25)
            page_frame = screenshot()
            items = page_frame.ocr()
    if not readings:
        return None
    count, confirmations = Counter(readings).most_common(1)[0]
    if confirmations < 2:
        logger.warning(f"进货书数量多帧 OCR 不一致: {readings}")
        return None
    return count, ", ".join(raw_values)


def _restock_book_detail_count(items: list[dict]) -> tuple[int, str] | None:
    """Read only the canonical restock-book name and its owned count from a detail card."""
    if not is_inventory_item_detail(items) or not _restock_book_item(items):
        return None
    for item in items:
        raw = str(item.get("text", ""))
        if "拥有" in raw and (count := _parse_count(raw)) is not None:
            return count, raw
    return None


def _confirm_restock_book_detail_count(initial_items: list[dict], frames=3):
    """Require the exact detail identity and owned count in at least two frames."""
    readings = []
    raw_values = []
    detail_seen = False
    items = initial_items
    for frame in range(frames):
        detail_seen = detail_seen or is_inventory_item_detail(items)
        result = _restock_book_detail_count(items)
        if result:
            count, raw = result
            readings.append(count)
            raw_values.append(str(raw))
        if frame + 1 < frames:
            time.sleep(0.25)
            items = screenshot().ocr()
    if not readings:
        return None, detail_seen
    count, confirmations = Counter(readings).most_common(1)[0]
    if confirmations < 2:
        logger.warning(f"进货书详情数量多帧 OCR 不一致: {readings}")
        return None, detail_seen
    return (count, ", ".join(raw_values)), detail_seen


def _inventory_page_signature(items: list[dict]) -> tuple[str, ...]:
    """Stable text signature used to detect the bottom of the scroll list."""
    return tuple(sorted(str(item.get("text", "")).replace(" ", "") for item in items))


def read_restock_book_count(max_pages: int = 10) -> int | None:
    """Best-effort inventory read; return None instead of blocking trading."""
    stopped = False
    should_restore_home = False
    try:
        if not connect():
            logger.warning("ADB 连接失败，无法读取背包进货书")
            return None
        if _is_train_in_transit(screenshot().ocr()):
            logger.info("列车正在行驶，跳过进货书背包扫描，交给跑商恢复流程等待到站")
            return None
        if not go_home():
            return None
        should_restore_home = True
        if not _open_assets_entry():
            logger.warning("未找到背包入口，将使用界面填写的进货书库存")
            return None
        time.sleep(1.2)
        previous_signature = None
        unchanged_pages = 0
        for page in range(1, max_pages + 1):
            page_frame = screenshot()
            items = page_frame.ocr()
            signature = _inventory_page_signature(items)
            logger.info(f"扫描背包第 {page}/{max_pages} 页")

            book = _restock_book_item(items)
            if book:
                logger.info(f"在背包第 {page} 页识别到进货采买书，开始多帧核对数量")
                confirmed = _confirm_restock_book_count(items)
                if not confirmed:
                    x, y = _center(book)
                    input_tap((int(x), int(y)))
                    time.sleep(0.6)
                    confirmed = _confirm_restock_book_count(screenshot().ocr())
                if confirmed:
                    count, raw = confirmed
                    logger.info(f"背包进货书数量: {count}（多帧 OCR: {raw}）")
                    return count
                logger.warning("已找到进货采买书，但多帧数量核对失败，继续扫描")
            else:
                icon, score = _find_restock_book_icon(getattr(page_frame, "image", None))
                if icon:
                    logger.info(
                        f"在背包第 {page} 页识别到进货采买书图标 {icon}"
                        f"（匹配度 {score:.3f}），开始多帧核对数量"
                    )
                    grid_confirmed = _confirm_restock_book_icon_count(items, icon)
                    # A high-confidence icon may still have no OCR-readable grid
                    # count. Open its detail card so the exact item name and
                    # `拥有：N` can provide an independent multi-frame result.
                    if grid_confirmed or score >= 0.90:
                        input_tap(icon)
                        time.sleep(0.6)
                        detail_items = screenshot().ocr()
                        detail, detail_seen = _confirm_restock_book_detail_count(detail_items)
                        if detail and (
                            grid_confirmed is None or detail[0] == grid_confirmed[0]
                        ):
                            count, detail_raw = detail
                            grid_raw = grid_confirmed[1] if grid_confirmed else "未识别"
                            logger.info(
                                f"背包进货书数量: {count}（格位多帧 OCR: {grid_raw}；"
                                f"详情多帧 OCR: {detail_raw}）"
                            )
                            return count
                        logger.warning(
                            "疑似进货书图标的详情名称/拥有数量未通过多帧确认，继续扫描"
                        )
                        if detail_seen:
                            input_tap((640, 600))
                            time.sleep(0.4)
                    else:
                        logger.warning(
                            f"疑似进货书图标匹配度 {score:.3f}，但下方数量未通过多帧核对"
                        )

            if signature and signature == previous_signature:
                unchanged_pages += 1
            else:
                unchanged_pages = 0
            previous_signature = signature
            if unchanged_pages >= 2:
                logger.info("背包内容连续三次未变化，已到列表末页")
                break
            if page < max_pages:
                input_swipe((930, 640), (930, 285), swipe_time=600)
                time.sleep(0.9)

        logger.warning(f"已翻查背包 {min(page, max_pages)} 页，仍未确认进货采买书数量")
        return None
    except StopExecution:
        stopped = True
        raise
    except Exception:
        logger.exception("读取背包进货书失败，将使用界面填写值")
        return None
    finally:
        if should_restore_home and not stopped:
            try:
                go_home()
            except StopExecution:
                raise
            except Exception:
                pass
