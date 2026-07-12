from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ExchangeItem:
    name: str
    limit: str
    costs: dict[str, int]
    recommended: bool = False


@dataclass(frozen=True)
class ActivityYield:
    name: str
    rewards: dict[str, tuple[int, int]]
    note: str = ""


@dataclass(frozen=True)
class CurrencyInfo:
    key: str
    name: str
    aliases: tuple[str, ...]
    kind: str
    description: str
    sources: tuple[str, ...]
    sinks: tuple[str, ...]
    source_url: str
    exchanges: tuple[ExchangeItem, ...] = ()
    activities: tuple[ActivityYield, ...] = ()


MEDAL_EXCHANGES = (
    ExchangeItem("星云物质（4钛）", "每日6次", {"fu_ming": 100}),
    ExchangeItem("自观测胶卷", "每日2次", {"jue_ming": 42}),
    ExchangeItem("一般武装改造凭证", "每周15次", {"fu_ming": 50}, True),
    ExchangeItem("一般武装改造凭证", "每周15次", {"jue_ming": 10}, True),
    ExchangeItem("特殊武装改造特许", "每周5次", {"fu_ming": 150}, True),
    ExchangeItem("特殊武装改造特许", "每周10次", {"jue_ming": 30}, True),
    ExchangeItem("进货采买书", "每周5次", {"fu_ming": 150}, True),
    ExchangeItem("广告投放券", "每周3次", {"fu_ming": 150}, True),
    ExchangeItem("银枝薄荷糖", "每周3次", {"fu_ming": 50}),
    ExchangeItem("“一元二次”", "每周2次", {"fu_ming": 100}),
    ExchangeItem("“豆蔻姜百合”", "每周1次", {"fu_ming": 150}),
    ExchangeItem("再交涉请求书", "每周2次", {"fu_ming": 100}),
    ExchangeItem("诱饵气球", "每周10次", {"fu_ming": 20}),
    ExchangeItem("追加注资申请书", "每月1次", {"fu_ming": 500}, True),
    ExchangeItem("进货采买书", "每月10次", {"fu_ming": 150}),
    ExchangeItem("广告投放券", "每月6次", {"fu_ming": 150}),
    ExchangeItem("胡尔顿气球", "永久1次", {"jue_ming": 500, "fu_ming": 2500}, True),
    ExchangeItem("世界团结", "永久1次", {"jue_ming": 500, "fu_ming": 2500}),
    ExchangeItem("清醒梦纤维", "永久1次", {"jue_ming": 800, "fu_ming": 4000}),
    ExchangeItem("错峰出行", "永久1次", {"jue_ming": 800, "fu_ming": 4000}),
)

MEDAL_ACTIVITIES = (
    ActivityYield("混响浮标 Lv1", {"jue_ming": (10, 10)}, "推荐等级28；另有其他材料"),
    ActivityYield("混响浮标 Lv2", {"jue_ming": (11, 11)}, "推荐等级30；另有其他材料"),
    ActivityYield("混响浮标 Lv3", {"fu_ming": (60, 60), "jue_ming": (12, 12)}, "社区实测记录"),
    ActivityYield("混响浮标 Lv4", {"jue_ming": (13, 13)}, "推荐等级34；另有其他材料"),
    ActivityYield("铁安局强敌：私贩追缴Ⅱ", {"fu_ming": (120, 120)}, "同时掉落城市声望与养成材料"),
    ActivityYield("铁安局强敌：私贩追缴Ⅲ", {"fu_ming": (140, 140)}, "同时掉落城市声望与养成材料"),
)


# 铁盟币的主要收入随实时价格、路线和玩家进度变化。固定商城价格可以直接
# 规划；浮动收入则由界面接入跑商优化器的本周预计利润，或按 10 万为单位补录。
IRON_EXCHANGES = (
    ExchangeItem("拉普拉斯协议（10万档）", "每月2次", {"tie_meng_bi": 100_000}, True),
    ExchangeItem("拉普拉斯协议（20万档）", "每月2次", {"tie_meng_bi": 200_000}),
    ExchangeItem("拉普拉斯协议（30万档）", "每月2次", {"tie_meng_bi": 300_000}),
    ExchangeItem("自观测胶卷（黑月本地商店）", "每周随机，最多99件", {"tie_meng_bi": 75_000}),
    ExchangeItem("独石碎片（黑月本地商店）", "每周随机，最多99件", {"tie_meng_bi": 50_000}),
    ExchangeItem("仓库扩建许可证", "永久1次", {"tie_meng_bi": 1_000_000, "jue_ming": 300, "fu_ming": 1_500}, True),
    ExchangeItem("列车基础性能改装（Lv1～20总计）", "永久1次", {"tie_meng_bi": 2_100_000}, True),
    ExchangeItem("高配客运列车建设预算（参考）", "项目1次", {"tie_meng_bi": 185_000_000}),
    ExchangeItem("自定义项目预算（每10万）", "最多999份", {"tie_meng_bi": 100_000}),
)

IRON_ACTIVITIES = (
    ActivityYield("其他预计收入（每10万）", {"tie_meng_bi": (100_000, 100_000)}, "订单、客运、回收、任务等浮动收入手动合并录入"),
)


CURRENCIES = (
    CurrencyInfo(
        "tie_meng_bi", "铁盟币", ("金币", "铁安币"), "通用流通货币",
        "能源本位的主要流通货币，贸易、建设和列车养成都大量消耗。",
        (
            "主线任务与支线任务", "交易所贸易（低买高卖）", "科伦巴商会订单",
            "铁安局任务", "城际自由客运", "各城素材回收", "荒原站垃圾回收",
            "商城：启动资金礼包", "商城：豪华启动资金礼包", "商城：电力升级礼包",
            "商城：列车长上任培养礼包", "商城：私人仓库扩建礼包",
        ),
        (
            "交易所购买跑商货物", "城市投资及建设计划", "列车电力、核心和拖车升级",
            "装备、材料、配方及兑换计划", "商会列车家具商店与花鸟市场宠物",
            "部分桦石、进货书及限购物资", "制造设施升级与加工成本",
        ),
        "https://wiki.biligame.com/resonance/铁盟币",
        IRON_EXCHANGES, IRON_ACTIVITIES,
    ),
    CurrencyInfo(
        "hua_shi", "桦石", (), "高级货币",
        "稀缺高级货币，优先用于招募与高价值限购，避免无计划刷新或兑换。",
        (
            "商城充值", "成就奖励", "主线任务与支线任务", "黑月商店",
            "铁安局驱逐任务进度奖励", "行车沿线：异构厄枝", "环游手册",
            "荒原站建设进度奖励", "淘金乐园建设进度奖励", "阿妮塔能源研究所建设进度奖励",
            "铁盟哨站建设进度奖励", "阿妮塔战备工厂建设进度奖励", "云岫桥基地建设进度奖励",
            "汇流塔建设进度奖励", "远星大桥建设进度奖励", "栖羽站建设进度奖励",
        ),
        ("购买拉普拉斯协议进行乘员招募", "商城礼包、资源与限购物资", "部分商店刷新或补充次数"),
        "https://wiki.biligame.com/resonance/桦石",
    ),
    CurrencyInfo(
        "mileage", "里程点数", ("里程点",), "周常兑换货币",
        "商会订单专属回报，建议先覆盖每周进货采买书与广告投放券。",
        ("科伦巴商会运输订单", "科伦巴商会物资运输订单", "活动或版本追加的商会订单奖励"),
        ("商会里程点商店：进货采买书", "商会里程点商店：广告投放券", "商会商店其他周常、月常物资", "部分城市兑换计划"),
        "https://wiki.biligame.com/resonance/里程点数",
    ),
    CurrencyInfo(
        "fu_ming", "赴命奖章", (), "战斗兑换货币",
        "沿线战斗与强敌悬赏产出，用于铁盟奖章兑换。",
        ("铁安局悬赏任务：强敌", "行车沿线：桦树生物", "行车沿线：桦树浮标", "行车沿线：混响浮标", "行车沿线：异构厄枝"),
        ("一般/特殊武装改造凭证", "进货采买书", "广告投放券", "追加注资申请书", "诱饵爆炸气球及其他铁盟兑换物资"),
        "https://wiki.biligame.com/resonance/赴命奖章",
        MEDAL_EXCHANGES, MEDAL_ACTIVITIES,
    ),
    CurrencyInfo(
        "jue_ming", "绝命奖章", ("绝命奖章·金",), "高危战斗货币",
        "清剿混响浮标获得的高危行动奖章，适合为高价值装备或兑换物资预留。",
        ("行车沿线：混响浮标",),
        ("铁盟奖章兑换处的装备与高阶物资", "胡尔顿气球等高价值兑换", "与赴命奖章组合支付的兑换项目"),
        "https://wiki.biligame.com/resonance/绝命奖章",
        MEDAL_EXCHANGES, MEDAL_ACTIVITIES,
    ),
    CurrencyInfo(
        "black_moon_voucher", "黑月采购券", ("采购券",), "活动/商店兑换货币",
        "版本活动或专项奖励发放的黑月兑换资源，具体可兑换内容随版本变化。",
        ("版本活动奖励", "全服建设或专项任务奖励", "官方邮件及运营活动"),
        ("黑月商店当期兑换物资",),
        "https://soli-reso.com/news/?news_id=68&type=notice",
    ),
)


def currency_for_asset(name: str) -> CurrencyInfo | None:
    normalized = name.strip()
    return next((item for item in CURRENCIES if normalized == item.name or normalized in item.aliases), None)


def calculate_currency_plan(current: float, planned_spend: int, reserve_target: int) -> dict:
    available_after_spend = max(0.0, float(current) - max(0, int(planned_spend)))
    target = max(0, int(reserve_target))
    return {
        "available_after_spend": available_after_spend,
        "shortfall": max(0, target - available_after_spend),
        "surplus": max(0, available_after_spend - target),
    }


def calculate_acquisition(minimum: int, maximum: int, attempts: int, probability_percent: float = 100) -> dict:
    """Return possible range and expectation for an acquisition source."""
    low = max(0, int(minimum))
    high = max(low, int(maximum))
    times = max(0, int(attempts))
    probability = min(100.0, max(0.0, float(probability_percent))) / 100
    return {
        "possible_min": low * times if probability >= 1 else 0,
        "possible_max": high * times,
        "expected": ((low + high) / 2) * times * probability,
    }
