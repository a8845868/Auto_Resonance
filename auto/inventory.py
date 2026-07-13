from __future__ import annotations

import re
import time
import json

import cv2 as cv
import numpy as np

from loguru import logger

from core.control.control import connect, input_swipe, input_tap, screenshot
from core.preset import go_home
from core.preset.control import blurry_ocr_click
from core.utils.utils import RESOURCES_PATH
from core.services.inventory_assets import Asset, merge_assets, parse_amount, parse_ocr_assets


def _center(item: dict) -> tuple[float, float]:
    position = item["position"]
    return ((position[0][0] + position[2][0]) / 2, (position[0][1] + position[2][1]) / 2)


def _is_train_in_transit(items: list[dict]) -> bool:
    """Detect the driving HUD, where station-only menus cannot be opened."""
    texts = [str(item.get("text", "")).replace(" ", "") for item in items]
    if any(marker in text for marker in ("自动巡航", "剩余行程") for text in texts):
        return True
    has_destination = any("目的地" in text for text in texts)
    has_carriage = any("车厢内" in text or "副官室" in text for text in texts)
    return has_destination and has_carriage


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


def _read_unicode_image(path) -> cv.typing.MatLike | None:
    try:
        return cv.imdecode(np.fromfile(str(path), dtype=np.uint8), cv.IMREAD_COLOR)
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


def _find_assets_entry(image) -> tuple[tuple[int, int] | None, float]:
    """Find the icon-only Assets entry across emulator/UI scale differences."""
    template = cv.imread(str(RESOURCES_PATH / "inventory" / "assets_entry.png"))
    if template is None:
        return None, 0.0
    # The icon lives in the top-right toolbar. Cropping avoids visually similar
    # white hexagons elsewhere on the home screen and makes matching faster.
    height, width = image.shape[:2]
    x1, y1 = int(width * 0.58), 0
    roi = image[y1 : int(height * 0.18), x1:width]
    best_score = 0.0
    best_loc = None
    for scale_percent in range(45, 91, 5):
        scale = scale_percent / 100
        resized = cv.resize(template, None, fx=scale, fy=scale, interpolation=cv.INTER_AREA)
        th, tw = resized.shape[:2]
        if th >= roi.shape[0] or tw >= roi.shape[1]:
            continue
        result = cv.matchTemplate(roi, resized, cv.TM_CCOEFF_NORMED)
        _, score, _, loc = cv.minMaxLoc(result)
        if score > best_score:
            best_score = score
            best_loc = (x1 + loc[0] + tw // 2, y1 + loc[1] + th // 2)
    return (best_loc if best_score >= 0.66 else None), best_score


def _open_assets_entry() -> bool:
    image = screenshot()
    location, score = _find_assets_entry(image.image)
    if location:
        logger.info(f"识别到资产入口图标：{location}（匹配度 {score:.3f}）")
        input_tap(location)
        return True
    logger.warning(f"未识别到资产入口图标（最高匹配度 {score:.3f}），尝试 OCR 兼容入口")
    return blurry_ocr_click("背包", score=0.55, trynum=2, log=False)


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


def read_restock_book_count() -> int | None:
    """Best-effort inventory read; return None instead of blocking trading."""
    try:
        if not connect():
            logger.warning("ADB 连接失败，无法读取背包进货书")
            return None
        go_home()
        if not _open_assets_entry():
            logger.warning("未找到背包入口，将使用界面填写的进货书库存")
            return None
        time.sleep(1.2)
        items = screenshot().ocr()
        book = next((item for item in items if "进货" in item["text"] and "书" in item["text"]), None)
        if not book:
            logger.warning("背包中未识别到进货采买书")
            return None
        position = book["position"]
        x1, y1 = position[0]
        x2, y2 = position[2]
        input_tap(((x1 + x2) // 2, (y1 + y2) // 2))
        time.sleep(0.6)
        nearby = screenshot().ocr()
        candidates = []
        for item in nearby:
            count = _parse_count(item["text"])
            if count is None:
                continue
            item_position = item["position"]
            ix1, iy1 = item_position[0]
            ix2, iy2 = item_position[2]
            distance = abs((ix1 + ix2) / 2 - (x1 + x2) / 2) + abs((iy1 + iy2) / 2 - (y1 + y2) / 2)
            candidates.append((distance, count, item["text"]))
        if not candidates:
            logger.warning("已找到进货采买书，但数量 OCR 失败")
            return None
        _, count, raw = min(candidates)
        logger.info(f"背包进货书数量: {count}（OCR: {raw}）")
        return count
    except Exception:
        logger.exception("读取背包进货书失败，将使用界面填写值")
        return None
    finally:
        try:
            go_home()
        except Exception:
            pass
