"""
Author: Night-stars-1 nujj1042633805@gmail.com
Date: 2024-04-02 19:13:20
LastEditTime: 2024-05-10 23:32:54
LastEditors: Night-stars-1 nujj1042633805@gmail.com
"""

import json
import os
from pathlib import Path
import sys
import uuid

from loguru import logger

from qfluentwidgets import (
    ConfigItem,
    ConfigSerializer,
    OptionsConfigItem,
    OptionsValidator,
    QConfig,
    Theme,
    qconfig,
)

from app.utils.config import CITYS
from core.control.adb_port import EmulatorInfo, EmulatorType
from version import __version__


PERSONAL_STARTUP_CONFIG_PATH = Path("config/app.json")


def apply_ocr_runtime_environment(config, environ=None) -> dict[str, str]:
    """Expose persisted OCR choices before any OCR consumer is imported.

    Explicit process environment variables remain authoritative, which keeps
    diagnostic and test launches reproducible without rewriting GUI config.
    """

    target = os.environ if environ is None else environ
    values = {
        "AUTO_RESONANCE_OCR_BACKEND": str(config.ocrBackend.value),
        "AUTO_RESONANCE_OCR_PROVIDER": str(config.ocrProvider.value),
    }
    for key, value in values.items():
        target.setdefault(key, value)
    return {key: str(target[key]) for key in values}


def migrate_personal_startup_config(path: Path = PERSONAL_STARTUP_CONFIG_PATH) -> bool:
    """Migrate only the legacy enable flag, never its fixed-city meaning."""

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    section = payload.get("PersonalAutomation")
    if not isinstance(section, dict):
        return False
    legacy_key = "PrepareGameAndEnterLanxinBeforeTasks"
    generic_key = "PrepareGameBeforeTasks"
    if legacy_key not in section:
        return False
    if generic_key not in section:
        section[generic_key] = section.get(legacy_key) is True
    section.pop(legacy_key, None)
    temporary = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=4),
            encoding="utf-8",
        )
        os.replace(temporary, path)
    except OSError as error:
        logger.warning(f"旧启动准备配置迁移暂未写入，将在下次启动重试: {error}")
        return False
    logger.info("已迁移旧启动准备开关；未迁移任何固定目标城市语义")
    return True


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

    ocrBackend = OptionsConfigItem(
        "OCR",
        "Backend",
        "ppocr-v4",
        OptionsValidator(["ppocr-v4", "ppocr-v6-medium"]),
        restart=True,
    )
    ocrProvider = OptionsConfigItem(
        "OCR",
        "Provider",
        "auto",
        OptionsValidator(["auto", "cpu", "cuda"]),
        restart=True,
    )

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
        "PersonalAutomation", "PrepareGameBeforeTasks", False, None
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
migrate_personal_startup_config()
qconfig.load("config/app.json", cfg)
