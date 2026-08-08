from __future__ import annotations

from difflib import SequenceMatcher
import re
import time
import unicodedata

from loguru import logger

from auto.inventory import _open_assets_entry
from core.control.control import connect, input_swipe, input_tap, screenshot
from core.preset import go_home
from core.preset.control import blurry_ocr_click
from core.services.inventory_assets import parse_amount
from core.services.passenger_layout import load_furniture_catalog
from core.services.runtime_errors import BlockedBySafetyError


def _normal(value: str) -> str:
    value = unicodedata.normalize("NFKC", str(value)).lower()
    return re.sub(r"[\s·•:：,，。()（）\[\]【】\"'“”]", "", value)


def _center(item: dict) -> tuple[int, int]:
    position = item.get("position") or ((0, 0), (0, 0), (0, 0), (0, 0))
    return ((position[0][0] + position[2][0]) // 2, (position[0][1] + position[2][1]) // 2)


def _match_catalog(items: list[dict], catalog: dict[str, dict]) -> tuple[str, str, float] | None:
    aliases = [(key, row["name"], _normal(row["name"])) for key, row in catalog.items()]
    candidates = []
    for token in items:
        raw = str(token.get("text", "")).strip()
        normalized = _normal(raw)
        if len(normalized) < 2:
            continue
        for key, name, alias in aliases:
            if normalized == alias or (len(alias) >= 4 and (alias in normalized or normalized in alias)):
                return key, raw, 1.0
            if len(alias) >= 4 and len(normalized) >= 4:
                score = SequenceMatcher(None, normalized, alias).ratio()
                if score >= 0.84:
                    candidates.append((score, key, raw, name))
    if not candidates:
        return None
    candidates.sort(reverse=True)
    best = candidates[0]
    second = candidates[1][0] if len(candidates) > 1 else 0
    if best[0] - second < 0.08:
        return None
    return best[1], best[2], best[0]


def _visible_card_counts(items: list[dict], width: int, height: int) -> list[tuple[int, int, int, str]]:
    cards = []
    for item in items:
        raw = str(item.get("text", "")).strip()
        if not re.fullmatch(r"\s*(?:[x×*]\s*)?[\d,]+\s*", raw, re.I):
            continue
        count = parse_amount(raw)
        if count is None or count < 0:
            continue
        x, y = _center(item)
        if not (int(width * 0.04) <= x <= int(width * 0.76) and int(height * 0.16) <= y <= int(height * 0.88)):
            continue
        cards.append((x, y, count, raw))
    cards.sort(key=lambda row: (row[1], row[0]))
    deduplicated = []
    for row in cards:
        if any(abs(row[0] - old[0]) < 25 and abs(row[1] - old[1]) < 20 for old in deduplicated):
            continue
        deduplicated.append(row)
    return deduplicated[:25]


def scan_furniture_inventory(max_pages: int = 6, max_items: int = 120) -> dict:
    """Conservatively scan Private Warehouse cards by confirming names in the detail pane.

    Only recognized catalog items are returned. Callers must merge these keys into
    prior/manual warehouse values and must not clear keys omitted by this scan.
    """
    if not connect():
        raise BlockedBySafetyError("ADB连接失败，请先在“ADB信息”确认模拟器连接")
    catalog = load_furniture_catalog()
    found: dict[str, dict] = {}
    unknown = []
    complete = True
    try:
        go_home()
        if not _open_assets_entry():
            raise BlockedBySafetyError("未找到资产/背包入口")
        time.sleep(1.0)
        if not blurry_ocr_click("私人仓库", score=0.62, trynum=3, log=False):
            raise BlockedBySafetyError(
                "未找到“私人仓库”分页；请停留在游戏主界面后重试"
            )
        time.sleep(1.0)
        seen_pages = set()
        scanned = 0
        for page in range(max_pages):
            frame = screenshot()
            items = frame.ocr()
            height, width = frame.image.shape[:2]
            cards = _visible_card_counts(items, width, height)
            fingerprint = tuple((round(x / 20), round(y / 20), count) for x, y, count, _ in cards)
            if not cards or fingerprint in seen_pages:
                break
            seen_pages.add(fingerprint)
            for x, y, count, raw_count in cards:
                if scanned >= max_items:
                    complete = False
                    break
                input_tap((x, y))
                time.sleep(0.28)
                detail = screenshot().ocr()
                matched = _match_catalog(detail, catalog)
                if matched:
                    key, raw_name, confidence = matched
                    previous = found.get(key)
                    if not previous or count > previous["warehouse_count"]:
                        found[key] = {
                            "warehouse_count": count,
                            "raw_name": raw_name,
                            "raw_count": raw_count,
                            "confidence": round(confidence, 3),
                        }
                else:
                    unknown.append({"page": page + 1, "position": [x, y], "raw_count": raw_count})
                scanned += 1
            if scanned >= max_items:
                break
            input_swipe((int(width * 0.67), int(height * 0.78)), (int(width * 0.67), int(height * 0.28)), swipe_time=650)
            time.sleep(0.8)
        else:
            complete = False
        logger.info(f"私人仓库家具扫描：确认 {len(found)} 类，未确认 {len(unknown)} 个卡片")
        if not found:
            raise BlockedBySafetyError(
                "已进入私人仓库，但未能从详情面板确认家具名称；请手动填写仓库数量"
            )
        return {"items": found, "unknown": unknown, "complete": complete, "scanned_cards": scanned}
    finally:
        try:
            go_home()
        except Exception:
            logger.warning("家具扫描结束后返回主界面失败")
