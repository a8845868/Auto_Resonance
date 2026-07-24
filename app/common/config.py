"""
Author: Night-stars-1 nujj1042633805@gmail.com
Date: 2024-04-02 19:13:20
LastEditTime: 2024-05-10 23:32:54
LastEditors: Night-stars-1 nujj1042633805@gmail.com
"""

import sys

from qfluentwidgets import ConfigItem, QConfig, Theme, qconfig, ConfigSerializer, OptionsValidator

from app.utils.config import CITYS
from core.control.adb_port import EmulatorInfo, EmulatorType
from version import __version__


class RunningBusinessConfig(QConfig):
    """Config of application"""

    BuyCount = ConfigItem("RunBuy", "BuyCount", 0, None)
    UseSilverBranch = ConfigItem("RunBuy", "UseSilverBranch", False, None)
    MaxIronSodaCost = ConfigItem("RunBuy", "MaxIronSodaCost", 500, None)
    UseNegotiationBook = ConfigItem("RunBuy", "UseNegotiationBook", False, None)
    OptimizerCargo = ConfigItem("WeeklyOptimizer", "Cargo", 1121, None)
    OptimizerBooks = ConfigItem("WeeklyOptimizer", "Books", 10, None)
    OptimizerFatigue = ConfigItem("WeeklyOptimizer", "Fatigue", 5292, None)
    OptimizerTradeLevel = ConfigItem("WeeklyOptimizer", "TradeLevel", 20, None)
    OptimizerBargainTries = ConfigItem("WeeklyOptimizer", "BargainTries", 5, None)
    OptimizerRaiseTries = ConfigItem("WeeklyOptimizer", "RaiseTries", 5, None)
    OptimizerBargainCountBonus = ConfigItem("WeeklyRole", "BargainCountBonus", 0, None)
    OptimizerRaiseCountBonus = ConfigItem("WeeklyRole", "RaiseCountBonus", 0, None)
    OptimizerBargainRateBonus = ConfigItem("WeeklyRole", "BargainRateBonus", 0, None)
    OptimizerRaiseRateBonus = ConfigItem("WeeklyRole", "RaiseRateBonus", 0, None)
    OptimizerBargainSuccessBonus = ConfigItem("WeeklyRole", "BargainSuccessBonus", 0, None)
    OptimizerRaiseSuccessBonus = ConfigItem("WeeklyRole", "RaiseSuccessBonus", 0, None)
    OptimizerFirstTrySuccessBonus = ConfigItem("WeeklyRole", "FirstTrySuccessBonus", 0, None)
    OptimizerAfterFailedSuccessBonus = ConfigItem("WeeklyRole", "AfterFailedSuccessBonus", 0, None)
    OptimizerFailedFatigueReduction = ConfigItem("WeeklyRole", "FailedFatigueReduction", 0, None)
    OptimizerTaxCutPercent = ConfigItem("WeeklyRole", "TaxCutPercent", 0, None)
    OptimizerExtraBuyPercent = ConfigItem("WeeklyRole", "ExtraBuyPercent", 0, None)
    OptimizerDriveFatigueReduction = ConfigItem("WeeklyRole", "DriveFatigueReduction", 0, None)
    PassengerSeats = ConfigItem("PassengerPlanner", "Seats", 64, None)
    PassengerTripsPerWeek = ConfigItem("PassengerPlanner", "TripsPerWeek", 7, None)
    PassengerReferenceCapacity = ConfigItem("PassengerPlanner", "ReferenceCapacity", 512, None)
    PassengerReferenceRevenueWan = ConfigItem("PassengerPlanner", "ReferenceRevenueWan", 589, None)
    PassengerOccupancy = ConfigItem("PassengerPlanner", "OccupancyPercent", 100, None)
    PassengerFatiguePerTrip = ConfigItem("PassengerPlanner", "FatiguePerTrip", 95, None)
    PassengerTargetCarriages = ConfigItem("PassengerBuild", "TargetCarriages", 7, None)
    PassengerBuiltExtraCarriages = ConfigItem("PassengerBuild", "BuiltExtraCarriages", 0, None)
    PassengerInstalledSeatGroups = ConfigItem("PassengerBuild", "InstalledSeatGroups", 0, None)
    PassengerCurrentIron = ConfigItem("PassengerBuild", "CurrentIron", 0, None)
    PassengerRouteObjective = ConfigItem("PassengerOperation", "RouteObjective", "当前利润优先", None)
    PassengerOrigin = ConfigItem("PassengerOperation", "Origin", "武林源", None)
    PassengerDestination = ConfigItem("PassengerOperation", "Destination", "岚心城", None)
    PassengerObservedRevenueWan = ConfigItem("PassengerOperation", "ObservedRevenueWan", 0, None)
    PassengerRouteFatigue = ConfigItem("PassengerOperation", "RouteFatigue", 95, None)
    for rating_key in ("Comfort", "Food", "Entertainment", "Pets", "Aquarium", "Plants", "Medical"):
        locals()[f"PassengerRating{rating_key}"] = ConfigItem("PassengerBuild", f"Rating{rating_key}", 0, None)
    InventoryBooks = ConfigItem("BookBudget", "CurrentInventory", 0, None)
    AutoReadInventoryBooks = ConfigItem("BookBudget", "AutoReadInventory", True, None)
    BookPlannerMigrated = ConfigItem("BookBudget", "PlannerV2Migrated", False, None)

    from core.services.book_budget import BOOK_SOURCES
    for source in BOOK_SOURCES:
        locals()[f"BookSource_{source.key}_Enabled"] = ConfigItem(
            "BookBudget", f"{source.key}.enabled", source.default_enabled, None
        )
        locals()[f"BookSource_{source.key}_Amount"] = ConfigItem(
            "BookBudget", f"{source.key}.amount", source.default_amount, None
        )

    for city in CITYS:
        # 特殊适配7号自由港
        locals()[f"{city}进货书"] = ConfigItem(
            "CityBook", city.replace("七号自由港", "7号自由港"), 0, None
        )
        locals()[f"{city}议价次数"] = ConfigItem(
            "CityHaggle", city.replace("七号自由港", "7号自由港"), 0, None
        )
        locals()[f"{city}声望等级"] = ConfigItem(
            "CityPrestige", city.replace("七号自由港", "7号自由港"), 20, None
        )


def isWin11():
    return sys.platform == "win32" and sys.getwindowsversion().build >= 22000

class EmulatorSerializer(ConfigSerializer):
    def serialize(self, value: EmulatorInfo) -> dict:
        return value.to_dict()

    def deserialize(self, data: dict) -> EmulatorInfo:
        return EmulatorInfo.from_dict(data)

class Config(RunningBusinessConfig):
    """Config of application"""

    emulatorType = ConfigItem("Global", "emulatorType", "Auto", None)
    device = ConfigItem(
        "Global",
        "device",
        EmulatorInfo(name="自定义端口", port=16384, path="", type=EmulatorType.CUSTOM, index=0),
        serializer=EmulatorSerializer(),
    )

    # Mirror酱
    mirrorCdk = ConfigItem("Global", "mirrorCdk", "", None)

    enableRewardCollection = ConfigItem("TaskQueue", "RewardCollection", True, None)
    enableResidentActivity = ConfigItem("TaskQueue", "ResidentActivity", True, None)
    enableResidentActivityOnce = ConfigItem("TaskQueue", "ResidentActivityOnce", False, None)
    enableRunBusiness = ConfigItem("TaskQueue", "RunBusiness", False, None)
    enablePassengerBuildMonitor = ConfigItem("TaskQueue", "PassengerBuildMonitor", False, None)
    enableFatiguePlanner = ConfigItem("TaskQueue", "FatiguePlanner", True, None)

    enableAutoGameLifecycle = ConfigItem(
        "EmulatorLifecycle", "Enabled", True, None
    )
    autoStartEmulator = ConfigItem(
        "EmulatorLifecycle", "AutoStartEmulator", True, None
    )
    closeGameWhenIdle = ConfigItem(
        "EmulatorLifecycle", "CloseGameWhenIdle", True, None
    )
    closeEmulatorWhenIdle = ConfigItem(
        "EmulatorLifecycle", "CloseEmulatorWhenIdle", False, None
    )
    autoConfirmResourceUpdate = ConfigItem(
        "PersonalAutomation", "AutoConfirmResourceUpdate", True, None
    )
    maximumResourceUpdateMb = ConfigItem(
        "PersonalAutomation", "MaximumResourceUpdateMb", 2048, None
    )
    enablePersonalStartupEpisode = ConfigItem(
        "PersonalAutomation", "PrepareGameAndEnterLanxinBeforeTasks", False, None
    )

    enableCodexSelfHealing = ConfigItem(
        "SelfHealing", "Enabled", False, None
    )
    allowCodexIsolatedRepair = ConfigItem(
        "SelfHealing", "AllowIsolatedRepair", False, None
    )

    residentActivityTask = ConfigItem(
        "ResidentActivity",
        "Task",
        "利刃行动",
        OptionsValidator(
            [
                "特殊订单",
                "利刃行动",
                "挑灯看剑",
                "武器材质分析",
                "骑士小说",
                "我思我在",
                "所知所闻",
                "大的！",
                "总体围剿",
            ]
        ),
    )

    autoCollectDailyActivity = ConfigItem(
        "RewardCollection", "DailyActivity", True, None
    )
    autoCollectTravelManual = ConfigItem(
        "RewardCollection", "TravelManual", True, None
    )
    rewardStrategy = ConfigItem(
        "RewardCollection",
        "Strategy",
        "maximize_progress",
        OptionsValidator(["maximize_progress", "claim_only"]),
    )
    residentActivityFullRealmReward = ConfigItem(
        "ResidentActivity",
        "FullRealmReward",
        "学会装备箱",
        OptionsValidator(["学会装备箱", "黑月装备箱", "帝国装备箱"]),
    )


YEAR = 2023
AUTHOR = "Night-stars-1"
VERSION = __version__
REPO_URL = "https://github.com/Night-stars-1/Auto_Resonance"


cfg = Config()
cfg.themeMode.value = Theme.AUTO
qconfig.load("config/app.json", cfg)
