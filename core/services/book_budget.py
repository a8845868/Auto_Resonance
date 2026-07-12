from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True)
class BookSource:
    key: str
    label: str
    period: str
    default_amount: int
    description: str
    location: str = ""
    editable: bool = False
    default_enabled: bool = True


BOOK_SOURCES = (
    BookSource("weekly_black_moon", "黑月总部周商店", "weekly", 6, "每周做满可购买 6 本", "任一主城 → 休息区 → 黑月总部商店"),
    BookSource("weekly_mileage", "科伦巴里程周兑换", "weekly", 5, "每周限购 5 本，消耗里程点", "任一主城 → 商会 → 里程点数兑换"),
    BookSource("weekly_iron", "铁盟赴命周兑换", "weekly", 5, "每周限购 5 本，消耗赴命奖章", "任一设有铁安局的站点 → 赴命奖章兑换"),
    BookSource("monthly_mileage", "科伦巴里程月兑换", "monthly", 10, "每月限购 10 本，消耗里程点", "任一主城 → 商会 → 里程点数兑换"),
    BookSource("monthly_iron", "铁盟赴命月兑换", "monthly", 10, "每月限购 10 本，消耗赴命奖章", "任一设有铁安局的站点 → 赴命奖章兑换"),
    BookSource("weekly_pack", "每周商会支援礼包", "weekly", 0, "礼包档位可能调整，请填本周实际数量", "商城", True, False),
    BookSource("monthly_pack", "月度商会支援礼包", "monthly", 0, "礼包档位可能调整，请填整月数量", "商城", True, False),
    BookSource("monthly_card", "付费月卡", "monthly", 0, "如当前月卡包含进货书，请填整月数量", "商城", True, False),
    BookSource("battle_pass", "付费大月卡/环游手册", "monthly", 0, "填写本期付费档可领取总数", "环游手册", True, False),
    BookSource("one_off", "投资/成就/活动/其他", "once", 0, "只计入本周预计能拿到的数量", "对应活动或城市投资", True, False),
)


def weekly_equivalent(period: str, amount: int) -> float:
    amount = max(0, int(amount))
    if period == "daily":
        return amount * 7.0
    if period == "monthly":
        return amount * 12.0 / 52.0
    return float(amount)


def calculate_book_budget(rows: Iterable[dict], current_inventory: int = 0) -> dict:
    enabled = [row for row in rows if row.get("enabled")]
    daily = sum(int(row["amount"]) for row in enabled if row["period"] == "daily")
    weekly = sum(int(row["amount"]) for row in enabled if row["period"] == "weekly")
    monthly = sum(int(row["amount"]) for row in enabled if row["period"] == "monthly")
    once = sum(int(row["amount"]) for row in enabled if row["period"] == "once")
    weekly_income = sum(weekly_equivalent(row["period"], row["amount"]) for row in enabled)
    return {
        "daily": daily,
        "weekly": weekly,
        "monthly": monthly,
        "once": once,
        "weekly_income": weekly_income,
        "available_this_week": max(0, int(current_inventory)) + int(weekly_income),
    }
