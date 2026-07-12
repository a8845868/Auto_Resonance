from __future__ import annotations

from dataclasses import dataclass
import math


STONE_PER_PULL = 160
GACHA_SOURCE_CATALOG_VERSION = 5


@dataclass(frozen=True)
class GachaSource:
    key: str
    name: str
    cycle: str
    note: str
    default_tickets: int = 0
    default_stones: int = 0
    default_times: int = 0
    max_times: int = 99


@dataclass(frozen=True)
class PaidGachaPack:
    key: str
    name: str
    cycle: str
    price_yuan: int
    tickets: int = 0
    stones: int = 0
    special_pulls: int = 0
    note: str = ""
    exclusive_group: str = ""


GACHA_SOURCES = (
    GachaSource("black_moon", "黑月总部商店", "每月上限", "三档各限购2张，共6张；合计消耗1,200,000铁盟币", 6, 0, 1, 1),
    GachaSource(
        "travel_manual",
        "环游手册·标准手册",
        "每期",
        "当前“甜蜜之旅”标准轨等级10、20、30、40、50各奖励拉普拉斯协议×1，共5协议；等级60及61–80无额外抽卡资源",
        5,
        0,
        1,
        1,
    ),
    GachaSource("heterogeneous_branch", "行车沿线·异构厄枝", "刷新周期", "可获得桦石，实际数量按目标等级和游戏结算界面填写"),
    GachaSource("version_event", "版本活动 / 活动商店", "每期", "通常可兑换协议并从任务获得桦石；数量随当期活动变化"),
    GachaSource("login", "签到活动", "每期", "仅录入当前仍在进行、尚未领取的签到奖励"),
    GachaSource("mail_compensation", "邮件 / 维护补偿", "不定期", "只录入官方已公布且尚未领取的补偿"),
    # One-time sources deliberately come last and are opt-in.
    GachaSource("freeport_expulsion", "7号自由港驱逐任务", "一次性", "完成3组驱逐任务总计；每个账号仅一次", 10, 300, 0, 1),
    GachaSource("main_side", "主线及支线任务", "一次性", "任务奖励不统一；只录入本计划期尚未完成且奖励明确的任务", 0, 0, 0, 1),
    GachaSource("other_expulsion", "其他城市铁安局驱逐进度", "一次性", "不同城市与系列奖励不同，完成前在进度奖励界面核对", 0, 0, 0, 1),
    GachaSource("construction", "城市建设进度奖励", "一次性", "荒原站、淘金乐园、能源研究所、铁盟哨站、战备工厂、云岫桥、汇流塔、远星大桥、栖羽站等", 0, 0, 0, 1),
    GachaSource("achievements", "成就奖励", "一次性", "桦石来源；只录入本计划期确定能够完成的成就", 0, 0, 0, 1),
)


PAID_GACHA_PACKS = (
    PaidGachaPack(
        "travel_development",
        "环游手册·开拓手册",
        "每期二选一",
        68,
        tickets=10,
        stones=680,
        note="相对免费档的增量：购买立即5协议+680桦石；付费轨等级20、30、40、50、60各1协议，共10协议+680桦石；等级61–80无额外抽卡资源",
        exclusive_group="travel_manual",
    ),
    PaidGachaPack(
        "travel_boundless",
        "环游手册·无垠手册",
        "每期二选一",
        128,
        tickets=10,
        stones=680,
        note="包含开拓手册全部奖励，抽卡资源同为付费增量10协议+680桦石；额外提供等级+10和其他附赠，等级61–80无额外抽卡资源",
        exclusive_group="travel_manual",
    ),
    PaidGachaPack("stone_month_pass", "桦石树养护套组", "30天", 30, stones=3000, note="立即300桦石，之后每日90桦石×30天"),
    PaidGachaPack("weekly_recruit", "每周招募礼包", "每周限购1", 6, tickets=1, note="另含战斗记忆体（8钛）×1、自观测胶卷×1"),
    PaidGachaPack("monthly_recruit", "月度乘员招募礼包", "每月限购1", 168, tickets=10, stones=1680),
    PaidGachaPack("event_memorial", "球场小将纪念礼包", "限时限购1", 6, special_pulls=3, note="万象秘钥·球场小将，仅限对应活动招募"),
    PaidGachaPack("event_starter", "球场小将开启礼包", "限时限购2", 18, special_pulls=10, note="每份10把限定秘钥"),
    PaidGachaPack("event_selected", "球场小将精选礼包", "限时限购2", 30, special_pulls=20, note="每份20把限定秘钥"),
    PaidGachaPack("event_premium", "球场小将至臻礼包", "限时限购2", 68, special_pulls=50, note="每份50把限定秘钥"),
)


def calculate_gacha_plan(
    tickets: int,
    stones: int,
    target_pulls: int,
    planned_ticket_gain: int = 0,
    planned_stone_gain: int = 0,
    stone_per_pull: int = STONE_PER_PULL,
) -> dict:
    tickets = max(0, int(tickets))
    stones = max(0, int(stones))
    target = max(0, int(target_pulls))
    ticket_gain = max(0, int(planned_ticket_gain))
    stone_gain = max(0, int(planned_stone_gain))
    cost = max(1, int(stone_per_pull))
    total_tickets = tickets + ticket_gain
    total_stones = stones + stone_gain
    stone_pulls = total_stones // cost
    available = total_tickets + stone_pulls
    missing = max(0, target - available)
    tickets_used = min(total_tickets, target)
    stone_pulls_used = min(stone_pulls, max(0, target - tickets_used))
    return {
        "available_pulls": available,
        "ticket_pulls": total_tickets,
        "stone_pulls": stone_pulls,
        "missing_pulls": missing,
        "missing_stones": missing * cost,
        "tickets_used": tickets_used,
        "stones_used": stone_pulls_used * cost,
        "tickets_left": total_tickets - tickets_used,
        "stones_left": total_stones - stone_pulls_used * cost,
        "ten_pulls": available // 10,
        "single_pulls": available % 10,
    }


def pulls_to_guarantee(pity_count: int, guarantee_at: int) -> int:
    return max(0, int(guarantee_at) - max(0, int(pity_count)))


def expected_source_total(amount_each: int, occurrences: int) -> int:
    return max(0, int(amount_each)) * max(0, int(occurrences))
