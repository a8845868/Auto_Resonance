from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True)
class Asset:
    name: str
    count: int
    category: str


CURRENCY_NAMES = {
    "金币", "桦石", "里程点", "铁安币", "铁盟币", "黑月采购券", "采购券", "拉普拉斯协议",
    "尘鸣坚骨", "赴命奖章", "绝命奖章", "绝命奖章·金",
}
CATEGORY_KEYWORDS = {
    "货币": tuple(CURRENCY_NAMES) + ("币", "点券", "奖章"),
    "补给与票券": ("券", "票", "书", "凭证", "许可", "钥匙", "箱"),
    "角色养成": ("经验", "胶卷", "晶簇", "磁流", "线圈", "弦", "骨", "核"),
    "装备材料": ("武器", "装备", "改造", "零件", "材料", "碎片"),
    "可使用道具": ("药", "饮料", "食品", "便当", "礼物", "背包"),
}
IGNORED_TEXT = {"背包", "道具", "材料", "任务", "全部", "筛选", "排序", "返回", "使用", "出售", "批量", "详情", "获得途径", "货币", "仓库"}
_NUMBER_RE = re.compile(r"(?<!\d)(?:[x×*]\s*)?([\d,]+(?:\.\d+)?)\s*([万亿]?)", re.I)
_INLINE_RE = re.compile(r"^\s*(.+?)\s*[x×*]\s*([\d,]+(?:\.\d+)?[万亿]?)\s*$", re.I)


def parse_amount(text: str) -> int | None:
    match = _NUMBER_RE.search(text.replace("，", ","))
    if not match:
        return None
    value = float(match.group(1).replace(",", ""))
    return int(value * {"": 1, "万": 10_000, "亿": 100_000_000}[match.group(2)])


def classify_asset(name: str) -> str:
    for category, keywords in CATEGORY_KEYWORDS.items():
        if any(keyword in name for keyword in keywords):
            return category
    return "其他"


def _center(item: dict) -> tuple[float, float]:
    position = item.get("position") or ((0, 0), (0, 0), (0, 0), (0, 0))
    return ((position[0][0] + position[2][0]) / 2, (position[0][1] + position[2][1]) / 2)


def parse_ocr_assets(items: Iterable[dict]) -> list[Asset]:
    """Pair OCR item names with nearby counts and return a de-duplicated snapshot."""
    tokens = [item for item in items if str(item.get("text", "")).strip()]
    numbers: list[tuple[float, float, int]] = []
    names: list[tuple[str, float, float]] = []
    found: dict[str, int] = {}
    for item in tokens:
        text = str(item["text"]).strip()
        inline = _INLINE_RE.match(text)
        if inline:
            name, raw = inline.groups()
            amount = parse_amount(raw)
            if name not in IGNORED_TEXT and amount is not None:
                found[name] = max(found.get(name, 0), amount)
            continue
        x, y = _center(item)
        if re.fullmatch(r"\s*(?:[x×*]\s*)?[\d,]+(?:\.\d+)?\s*[万亿]?\s*", text, re.I):
            amount = parse_amount(text)
            if amount is not None:
                numbers.append((x, y, amount))
        elif text not in IGNORED_TEXT and len(text) <= 24 and not text.isascii():
            names.append((text, x, y))
    for name, x, y in names:
        candidates = [(abs(nx - x) + abs(ny - y) * 0.7, amount) for nx, ny, amount in numbers if abs(nx - x) <= 150 and -35 <= ny - y <= 170]
        if candidates:
            amount = min(candidates)[1]
            found[name] = max(found.get(name, 0), amount)
    return [Asset(name, count, classify_asset(name)) for name, count in sorted(found.items())]


def merge_assets(*snapshots: Iterable[Asset]) -> list[Asset]:
    merged: dict[str, Asset] = {}
    for snapshot in snapshots:
        for asset in snapshot:
            if asset.name not in merged or asset.count > merged[asset.name].count:
                merged[asset.name] = asset
    return sorted(merged.values(), key=lambda asset: (asset.category, asset.name))
