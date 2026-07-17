"""
Author: Night-stars-1 nujj1042633805@gmail.com
Date: 2024-04-10 22:54:08
LastEditTime: 2025-02-10 23:25:35
LastEditors: Night-stars-1 nujj1042633805@gmail.com
"""

from datetime import date, timedelta
from typing import Dict, Optional

from loguru import logger
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QGridLayout, QHBoxLayout, QLabel, QVBoxLayout, QWidget
from qfluentwidgets import ComboBox, ExpandLayout, ExpandSettingCard, PushSettingCard, SpinBox, SwitchSettingCard, qconfig
from qfluentwidgets import FluentIcon as FIF
from qfluentwidgets import ScrollArea, InfoBar

from app.common.config import cfg
from app.common.signal_bus import signalBus
from app.common.style_sheet import StyleSheet
from app.utils.worker import Worker
from app.components.primary_push_load_card import PrimaryPushLoadCard
from app.components.task_schedule_card import TaskScheduleCard
from app.components.settings.spin_box_setting_card import SpinBoxSettingCard
from app.utils.config import CITYS, CITY_GOODS, CITY_POSITIONS
from core.model.config import config


class TwoRunBusinessInterface(ScrollArea):
    """每日任务 interface"""

    def __init__(self, parent=None):
        super().__init__(parent=parent)
        self.skillCardData: Dict[str, SpinBoxSettingCard] = {}  # 角色技能卡片集合
        self.workers: Optional[Worker] = None
        self.optimizerWorker: Optional[Worker] = None
        self.optimizationResult: Optional[dict] = None
        self.appliedWeeklyPlan: Optional[dict] = None
        self.scrollWidget = QWidget(self)
        self.expandLayout = ExpandLayout(self.scrollWidget)

        # label
        self.titleLabel = QLabel("跑商配置", self)

        self.__initWidget()
        self.progressTimer = QTimer(self)
        self.progressTimer.timeout.connect(self.refreshWeeklyProgress)
        self.progressTimer.start(2000)
        self.refreshWeeklyProgress()

    def __initWidget(self):
        self.resize(1000, 800)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setViewportMargins(0, 80, 0, 20)
        self.setWidget(self.scrollWidget)
        self.setWidgetResizable(True)
        self.setObjectName("TwoCityRunnBusinessInterface")

        # initialize style sheet
        self.scrollWidget.setObjectName("scrollWidget")
        self.titleLabel.setObjectName("titleLabel")
        StyleSheet.VIEW_INTERFACE.apply(self)

        # initialize layout
        self.loadSamples()
        self.__initLayout()
        self.connectSignalToSlot()

    def loadSamples(self):
        """load samples"""
        self.enableRunBusinessCard = SwitchSettingCard(
            FIF.TRAIN,
            "加入任务序列",
            "端点跑商",
            cfg.enableRunBusiness,
            self.scrollWidget,
        )
        self.scheduleCard = TaskScheduleCard("run_business", self.scrollWidget)
        self.isSpeedCard = SwitchSettingCard(
            FIF.MARKET,
            "是否自动加速",
            "是否自动使用加速弹丸",
            parent=self.scrollWidget,
        )
        self.isAutoPickCard = SwitchSettingCard(
            FIF.TILES,
            "是否自动拾取",
            "是否自动拾取掉落物",
            parent=self.scrollWidget,
        )
        self.isSpeedCard.setValue(config.global_config.is_speed)
        self.isAutoPickCard.setValue(config.global_config.is_auto_pick)
        self.isSpeedCard.switchButton.checkedChanged.connect(self.saveAutomationOptions)
        self.isAutoPickCard.switchButton.checkedChanged.connect(self.saveAutomationOptions)
        self.bookBudgetWidget = QWidget(self.scrollWidget)
        self.bookBudgetWidget.setObjectName("bookBudgetWidget")
        self.bookBudgetWidget.setStyleSheet(
            "#bookBudgetWidget { background: rgba(255,255,255,0.05);"
            "border: 1px solid rgba(255,255,255,0.08); border-radius: 8px; }"
        )
        budgetLayout = QHBoxLayout(self.bookBudgetWidget)
        budgetLayout.setContentsMargins(16, 12, 16, 12)
        self.bookBudgetSummaryLabel = QLabel(self.bookBudgetWidget)
        self.bookBudgetSummaryLabel.setWordWrap(True)
        budgetLayout.addWidget(self.bookBudgetSummaryLabel, 1)
        self.openBookPlannerButton = PushSettingCard(
            "打开规划器", FIF.CALENDAR, "进货书规划", "查看固定来源、站点和付费项目", self.bookBudgetWidget
        )
        self.openBookPlannerButton.setFixedWidth(360)
        budgetLayout.addWidget(self.openBookPlannerButton)
        self.bookBudgetWidget.setFixedHeight(108)
        self.liveOptimizeCard = PrimaryPushLoadCard(
            "实时计算",
            FIF.SYNC,
            "科伦巴实时周计划优化",
            "读取实时价格，按科伦巴期望公式计算一周总利润最高的双城路线",
            self.scrollWidget,
        )
        self.applyOptimizeCard = PushSettingCard(
            "套用路线",
            FIF.ACCEPT,
            "应用计算结果",
            "自动选择起点、终点并设置两地进货书数量",
            self.scrollWidget,
        )
        self.applyOptimizeCard.button.setEnabled(False)
        self.optimizerWidget = QWidget(self.scrollWidget)
        self.optimizerWidget.setFixedHeight(275)
        optimizerLayout = QGridLayout(self.optimizerWidget)
        optimizerLayout.setContentsMargins(16, 8, 16, 8)

        def add_optimizer_spin(row, column, label, config_item, minimum, maximum):
            optimizerLayout.addWidget(QLabel(label, self.optimizerWidget), row, column * 2)
            spin = SpinBox(self.optimizerWidget)
            spin.setRange(minimum, maximum)
            spin.setValue(int(config_item.value))
            spin.valueChanged.connect(lambda value, item=config_item: qconfig.set(item, value))
            optimizerLayout.addWidget(spin, row, column * 2 + 1)
            return spin

        self.optimizerCargoSpinBox = add_optimizer_spin(0, 0, "货舱", cfg.OptimizerCargo, 1, 5000)
        self.optimizerBooksSpinBox = add_optimizer_spin(0, 1, "每周进货书", cfg.OptimizerBooks, 0, 999)
        self.optimizerFatigueSpinBox = add_optimizer_spin(0, 2, "每周疲劳", cfg.OptimizerFatigue, 1, 20000)
        self.optimizerTradeLevelSpinBox = add_optimizer_spin(1, 0, "贸易等级", cfg.OptimizerTradeLevel, 0, 100)
        self.optimizerBargainSpinBox = add_optimizer_spin(1, 1, "砍价次数上限", cfg.OptimizerBargainTries, 0, 10)
        self.optimizerRaiseSpinBox = add_optimizer_spin(1, 2, "抬价次数上限", cfg.OptimizerRaiseTries, 0, 10)
        self.passengerSeatsSpinBox = add_optimizer_spin(2, 0, "固定客位", cfg.PassengerSeats, 0, 1024)
        self.passengerTripsSpinBox = add_optimizer_spin(2, 1, "每周客运次数", cfg.PassengerTripsPerWeek, 0, 100)
        self.passengerRevenueSpinBox = add_optimizer_spin(2, 2, "满编单次收益(万)", cfg.PassengerReferenceRevenueWan, 0, 5000)
        self.optimizerResultLabel = QLabel("尚未计算新的周计划", self.optimizerWidget)
        self.optimizerResultLabel.setWordWrap(True)
        optimizerLayout.addWidget(self.optimizerResultLabel, 3, 0, 1, 6)
        self.weeklyProgressLabel = QLabel("本周尚未套用计划", self.optimizerWidget)
        self.weeklyProgressLabel.setWordWrap(True)
        self.weeklyProgressLabel.setStyleSheet("padding-top: 6px; color: #35d7e8;")
        optimizerLayout.addWidget(self.weeklyProgressLabel, 4, 0, 1, 6)
        self.buyCountCard = SpinBoxSettingCard(
            cfg.BuyCount,
            FIF.ACCEPT,
            "运行次数",
            spin_box_max=20,
            parent=self.scrollWidget,
        )
        self.useNegotiationBookCard = SwitchSettingCard(
            FIF.BOOK_SHELF,
            "议价次数不足时使用议价书",
            "卖货抬价次数耗尽后，消耗议价书重置次数并继续抬价到上限",
            configItem=cfg.UseNegotiationBook,
            parent=self.scrollWidget,
        )
        self.routeSelectionWidget = QWidget(self.scrollWidget)
        self.routeSelectionWidget.setObjectName("routeSelectionWidget")
        # ExpandLayout cannot reliably infer the size hint of a plain QWidget
        # that contains nested layouts, so reserve explicit space for it.
        self.routeSelectionWidget.setFixedHeight(104)
        self.routeSelectionWidget.setStyleSheet(
            "#routeSelectionWidget {"
            "background: rgba(255, 255, 255, 0.05);"
            "border: 1px solid rgba(255, 255, 255, 0.08);"
            "border-radius: 8px;"
            "}"
        )
        routeLayout = QVBoxLayout(self.routeSelectionWidget)
        routeLayout.setContentsMargins(16, 12, 16, 12)
        routeHeaderLayout = QHBoxLayout()
        routeHeaderLayout.addWidget(QLabel("起点", self.routeSelectionWidget))
        self.buyCityComboBox = ComboBox(self.routeSelectionWidget)
        self.buyCityComboBox.addItems(CITYS)
        routeHeaderLayout.addWidget(self.buyCityComboBox, 1)
        routeHeaderLayout.addSpacing(16)
        routeHeaderLayout.addWidget(QLabel("终点", self.routeSelectionWidget))
        self.sellCityComboBox = ComboBox(self.routeSelectionWidget)
        self.sellCityComboBox.addItems(CITYS)
        if len(CITYS) > 1:
            self.sellCityComboBox.setCurrentIndex(1)
        # Resume the active weekly route after the app is restarted.
        from core.services import load_weekly_plan

        saved_plan = load_weekly_plan(include_expired=True)
        if saved_plan and len(saved_plan.get("cycle", [])) == 2:
            self.buyCityComboBox.setCurrentText(saved_plan["cycle"][0])
            self.sellCityComboBox.setCurrentText(saved_plan["cycle"][1])
        routeHeaderLayout.addWidget(self.sellCityComboBox, 1)
        routeLayout.addLayout(routeHeaderLayout)
        self.routeInfoLabel = QLabel(self.routeSelectionWidget)
        self.routeInfoLabel.setWordWrap(True)
        routeLayout.addWidget(self.routeInfoLabel)
        self.buyCityComboBox.currentTextChanged.connect(self.updateRouteInfo)
        self.sellCityComboBox.currentTextChanged.connect(self.updateRouteInfo)
        self.updateRouteInfo()

        # Only the two cities in the active route need frequent editing. The
        # previous all-city expanders became extremely tall as stations were
        # added, so expose both settings in one compact two-row editor.
        self.routeTradeSettingsWidget = QWidget(self.scrollWidget)
        self.routeTradeSettingsWidget.setObjectName("routeTradeSettingsWidget")
        self.routeTradeSettingsWidget.setFixedHeight(142)
        self.routeTradeSettingsWidget.setStyleSheet(
            "#routeTradeSettingsWidget {"
            "background: rgba(255, 255, 255, 0.05);"
            "border: 1px solid rgba(255, 255, 255, 0.08);"
            "border-radius: 8px;"
            "}"
        )
        tradeLayout = QGridLayout(self.routeTradeSettingsWidget)
        tradeLayout.setContentsMargins(16, 10, 16, 10)
        tradeLayout.setHorizontalSpacing(18)
        tradeLayout.addWidget(QLabel("当前路线交易设置", self.routeTradeSettingsWidget), 0, 0)
        tradeLayout.addWidget(QLabel("每次进货书", self.routeTradeSettingsWidget), 0, 1)
        tradeLayout.addWidget(QLabel("成功议价次数", self.routeTradeSettingsWidget), 0, 2)
        self.routeCityLabels = []
        self.routeBookSpinBoxes = []
        self.routeHaggleSpinBoxes = []
        for row in range(2):
            cityLabel = QLabel(self.routeTradeSettingsWidget)
            bookSpin = SpinBox(self.routeTradeSettingsWidget)
            bookSpin.setRange(0, 20)
            haggleSpin = SpinBox(self.routeTradeSettingsWidget)
            haggleSpin.setRange(0, 10)
            tradeLayout.addWidget(cityLabel, row + 1, 0)
            tradeLayout.addWidget(bookSpin, row + 1, 1)
            tradeLayout.addWidget(haggleSpin, row + 1, 2)
            self.routeCityLabels.append(cityLabel)
            self.routeBookSpinBoxes.append(bookSpin)
            self.routeHaggleSpinBoxes.append(haggleSpin)
            bookSpin.valueChanged.connect(
                lambda value, index=row: self.saveRouteTradeSetting(index, "book", value)
            )
            haggleSpin.valueChanged.connect(
                lambda value, index=row: self.saveRouteTradeSetting(index, "haggle", value)
            )
        self.updateRouteTradeSettings()

        self.prestigeGroup = ExpandSettingCard(
            FIF.CERTIFICATE, "城市声望等级（用于税率、库存和议价概率）", parent=self.scrollWidget
        )
        self.roleGroup = ExpandSettingCard(
            FIF.PEOPLE, "角色与活动综合修正", parent=self.scrollWidget
        )
        for city in CITYS:
            prestigeCard = SpinBoxSettingCard(
                getattr(cfg, f"{city}声望等级"),
                FIF.CERTIFICATE,
                city,
                f"{city}声望等级",
                spin_box_max=20,
                parent=self.prestigeGroup,
            )
            prestigeCard.spinBox.setMinimum(1)
            self.prestigeGroup.viewLayout.addWidget(prestigeCard)

        role_fields = [
            (cfg.OptimizerBargainCountBonus, "额外砍价次数", 10),
            (cfg.OptimizerRaiseCountBonus, "额外抬价次数", 10),
            (cfg.OptimizerBargainRateBonus, "单次砍价率加成（百分点）", 20),
            (cfg.OptimizerRaiseRateBonus, "单次抬价率加成（百分点）", 20),
            (cfg.OptimizerBargainSuccessBonus, "砍价成功率加成（百分点）", 100),
            (cfg.OptimizerRaiseSuccessBonus, "抬价成功率加成（百分点）", 100),
            (cfg.OptimizerFirstTrySuccessBonus, "首次成功率加成（百分点）", 100),
            (cfg.OptimizerAfterFailedSuccessBonus, "失败后成功率加成（百分点）", 100),
            (cfg.OptimizerFailedFatigueReduction, "失败时疲劳减免", 8),
            (cfg.OptimizerTaxCutPercent, "交易税减免（百分点）", 10),
            (cfg.OptimizerExtraBuyPercent, "额外库存加成（百分点）", 200),
            (cfg.OptimizerDriveFatigueReduction, "每程驾驶疲劳减免", 20),
        ]
        for item, label, maximum in role_fields:
            card = SpinBoxSettingCard(item, FIF.PEOPLE, label, label, spin_box_max=maximum, parent=self.roleGroup)
            self.roleGroup.viewLayout.addWidget(card)
        self.refreshBookBudget()

    def __initLayout(self):
        self.titleLabel.move(36, 30)

        self.prestigeGroup._adjustViewSize()
        self.roleGroup._adjustViewSize()

        self.expandLayout.setContentsMargins(36, 0, 36, 0)

        self.expandLayout.addWidget(self.enableRunBusinessCard)
        self.expandLayout.addWidget(self.scheduleCard)
        self.expandLayout.addWidget(self.isSpeedCard)
        self.expandLayout.addWidget(self.isAutoPickCard)
        self.expandLayout.addWidget(self.routeSelectionWidget)
        self.expandLayout.addWidget(self.routeTradeSettingsWidget)
        self.expandLayout.addWidget(self.bookBudgetWidget)
        self.expandLayout.addWidget(self.liveOptimizeCard)
        self.expandLayout.addWidget(self.optimizerWidget)
        self.expandLayout.addWidget(self.applyOptimizeCard)
        self.expandLayout.addWidget(self.prestigeGroup)
        self.expandLayout.addWidget(self.roleGroup)
        self.expandLayout.addWidget(self.buyCountCard)
        self.expandLayout.addWidget(self.useNegotiationBookCard)

    def connectSignalToSlot(self):
        self.liveOptimizeCard.clicked.connect(self.calculateLiveRoute)
        self.applyOptimizeCard.clicked.connect(self.applyOptimizedRoute)
        self.openBookPlannerButton.clicked.connect(lambda: signalBus.switchToCard.emit("BookPlannerInterface"))
        signalBus.bookBudgetChanged.connect(self.refreshBookBudget)

    def saveAutomationOptions(self):
        config.global_config.is_speed = self.isSpeedCard.isChecked()
        config.global_config.is_auto_pick = self.isAutoPickCard.isChecked()
        config.save_config()

    def refreshBookBudget(self, *_):
        planned = int(cfg.OptimizerBooks.value)
        self.optimizerBooksSpinBox.blockSignals(True)
        self.optimizerBooksSpinBox.setValue(planned)
        self.optimizerBooksSpinBox.blockSignals(False)
        self.bookBudgetSummaryLabel.setText(
            f"进货书规划器已同步：本周按 {planned} 本计算；当前背包兜底值 {int(cfg.InventoryBooks.value)} 本。\n"
            "固定免费来源、购买站点和浮动付费项目请在独立规划器中维护。"
        )

    def calculateLiveRoute(self):
        from core.services import OptimizationConfig, optimize_live_routes

        if self.optimizerWorker:
            return
        self.liveOptimizeCard.loading(True)
        self.applyOptimizeCard.button.setEnabled(False)
        self.optimizerResultLabel.setText("正在读取科伦巴实时价格并计算……")
        config = OptimizationConfig(
            cargo=self.optimizerCargoSpinBox.value(),
            books=self.optimizerBooksSpinBox.value(),
            weekly_fatigue=self.optimizerFatigueSpinBox.value(),
            trade_level=self.optimizerTradeLevelSpinBox.value(),
            prestige={city: int(getattr(cfg, f"{city}声望等级").value) for city in CITYS},
            max_bargain_tries=self.optimizerBargainSpinBox.value(),
            max_raise_tries=self.optimizerRaiseSpinBox.value(),
            max_cycle_length=2,
            bargain_count_bonus=int(cfg.OptimizerBargainCountBonus.value),
            raise_count_bonus=int(cfg.OptimizerRaiseCountBonus.value),
            bargain_rate_bonus=float(cfg.OptimizerBargainRateBonus.value),
            raise_rate_bonus=float(cfg.OptimizerRaiseRateBonus.value),
            bargain_success_bonus=float(cfg.OptimizerBargainSuccessBonus.value),
            raise_success_bonus=float(cfg.OptimizerRaiseSuccessBonus.value),
            first_try_success_bonus=float(cfg.OptimizerFirstTrySuccessBonus.value),
            after_failed_success_bonus=float(cfg.OptimizerAfterFailedSuccessBonus.value),
            failed_fatigue_reduction=float(cfg.OptimizerFailedFatigueReduction.value),
            tax_cut_percent=float(cfg.OptimizerTaxCutPercent.value),
            extra_buy_percent=float(cfg.OptimizerExtraBuyPercent.value),
            drive_fatigue_reduction=int(cfg.OptimizerDriveFatigueReduction.value),
            passenger_seats=self.passengerSeatsSpinBox.value(),
            passenger_trips_per_week=self.passengerTripsSpinBox.value(),
            passenger_reference_capacity=int(cfg.PassengerReferenceCapacity.value),
            passenger_reference_trip_revenue=self.passengerRevenueSpinBox.value() * 10_000,
            passenger_occupancy_percent=int(cfg.PassengerOccupancy.value),
            passenger_fatigue_per_trip=int(cfg.PassengerFatiguePerTrip.value),
        )
        self.optimizerWorker = Worker(optimize_live_routes, config=config)
        self.optimizerWorker.result.connect(self.onOptimizationFinished)
        self.optimizerWorker.error.connect(self.onOptimizationError)
        self.optimizerWorker.finished.connect(self.onOptimizationWorkerFinished)
        self.optimizerWorker.start()

    def onOptimizationFinished(self, result: dict):
        self.optimizationResult = result
        self.appliedWeeklyPlan = None
        cycle = result["cycle"]
        batches = result["execution_batches"]
        batch_text = "；".join(self._format_batch(batch) for batch in batches)
        self.optimizerResultLabel.setText(
            f"新计划（尚未套用）：{cycle[0]} → {cycle[1]} → {cycle[0]}\n"
            f"本周任务：完整往返 {result['repeats']} 次（去一趟再回来，算1次往返）\n"
            f"进货书安排：{batch_text}，合计使用 {result['books_used']} 本\n"
            f"货运 {result['cargo_profit']:,} + 固定客运 {result['passenger_profit']:,}"
            f" = 周总利润 {result['combined_profit']:,}；预计总疲劳 {round(result['fatigue'])}；价格更新 {result['price_time']}"
        )
        self.applyOptimizeCard.button.setEnabled(True)

    def onOptimizationError(self, message: str):
        self.optimizationResult = None
        self.optimizerResultLabel.setText("实时计算失败，请检查网络或查看日志")
        InfoBar.error(
            title="实时计算失败",
            content=message,
            orient=Qt.Orientation.Horizontal,
            isClosable=True,
            parent=self,
        )

    def onOptimizationWorkerFinished(self):
        self.liveOptimizeCard.loading(False)
        if self.optimizerWorker:
            self.optimizerWorker.deleteLater()
        self.optimizerWorker = None

    def applyOptimizedRoute(self):
        from core.services import remaining_batches, save_weekly_plan

        if not self.optimizationResult:
            return
        cycle = self.optimizationResult["cycle"]
        state = save_weekly_plan(self.optimizationResult)
        batches = remaining_batches(state)
        first_batch = batches[0] if batches else {"runs": 0, "books": {}}
        self.buyCityComboBox.setCurrentText(cycle[0])
        self.sellCityComboBox.setCurrentText(cycle[1])
        for city in CITYS:
            qconfig.set(getattr(cfg, f"{city}进货书"), int(first_batch["books"].get(city, 0)))
        qconfig.set(cfg.BuyCount, int(first_batch["runs"]))
        self.updateRouteTradeSettings()
        self.appliedWeeklyPlan = self.optimizationResult
        self.updateRouteInfo()
        self.refreshWeeklyProgress()
        InfoBar.success(
            title="已套用周计划第一批",
            content=f"{cycle[0]} → {cycle[1]} → {cycle[0]}，点击开始后将自动完成全部 {self.optimizationResult['repeats']} 次往返并切换进货书",
            orient=Qt.Orientation.Horizontal,
            isClosable=False,
            parent=self,
        )

    @staticmethod
    def _format_books(books: dict) -> str:
        using = [f"每次到{city}进货时用{count}本" for city, count in books.items() if int(count) > 0]
        return "、".join(using) if using else "不用进货书"

    def _format_batch(self, batch: dict) -> str:
        return f"{batch['runs']}次往返{self._format_books(batch.get('books', {}))}"

    def refreshWeeklyProgress(self):
        from core.services import progress_summary

        summary = progress_summary()
        if not summary:
            self.weeklyProgressLabel.setText("本周尚未套用计划。请先实时计算，再点击“套用路线”。")
            return
        cycle = summary["cycle"]
        confirmed = summary.get("confirmed_round_trips")
        fact_text = (
            "未知 / 待同步"
            if confirmed is None
            else f"{confirmed} 次已确认完整往返"
        )
        source = summary.get("progress_source", "UNKNOWN")
        partial = summary.get("current_partial_cycle")
        partial_text = (
            f"当前部分周期：{partial.get('confirmed_legs', 0)} 程已确认，"
            f"停在 {partial.get('last_destination') or '未知站点'}"
            if partial
            else "当前部分周期：无账本确认"
        )
        if summary["finished"]:
            self.weeklyProgressLabel.setText(
                f"本周事实（{source}）：{fact_text}；{partial_text}\n"
                f"本周计划已完成：{cycle[0]} → {cycle[1]} → {cycle[0]}；"
                f"计划执行记录 {summary['completed_runs']}/{summary['total_runs']} 次。"
            )
            return
        current = summary["current_batch"]
        later_batches = summary["remaining_batches"][1:]
        later = "；完成后再跑 " + "；".join(self._format_batch(batch) for batch in later_batches) if later_batches else ""
        from core.services.server_calendar import SERVER_CLOCK

        today = SERVER_CLOCK.server_day_date()
        days_left = max(1, 7 - today.weekday())
        suggested_today = (summary["remaining_runs"] + days_left - 1) // days_left
        self.weeklyProgressLabel.setText(
            f"本周事实（{source}）：{fact_text}；{partial_text}；"
            f"已确认进货书 {summary.get('confirmed_books_used', 0)} 本\n"
            f"本周计划：计划记录完成 {summary['completed_runs']} 次，"
            f"剩余计划 {summary['planned_round_trips_remaining']} 次；"
            f"按疲劳最多 {summary['feasible_round_trips_by_fatigue']} 次；"
            f"预计净利润 {summary['expected_total_net_profit']}，"
            f"预计疲劳 {summary['expected_total_fatigue']}，"
            f"净利润/疲劳 {summary['expected_profit_per_fatigue']}\n"
            f"下一步动作：{self._format_batch(current)}{later}\n"
            f"今日建议：{suggested_today} 次完整往返；"
            f"预计剩余计划疲劳约 {round(summary['remaining_fatigue'])}"
        )

    def routeCities(self):
        return [self.buyCityComboBox.currentText(), self.sellCityComboBox.currentText()]

    def updateRouteTradeSettings(self):
        if not hasattr(self, "routeCityLabels"):
            return
        for index, city in enumerate(self.routeCities()):
            self.routeCityLabels[index].setText(("起点  " if index == 0 else "终点  ") + city)
            bookSpin = self.routeBookSpinBoxes[index]
            haggleSpin = self.routeHaggleSpinBoxes[index]
            bookSpin.blockSignals(True)
            haggleSpin.blockSignals(True)
            bookSpin.setValue(int(getattr(cfg, f"{city}进货书").value))
            haggleSpin.setValue(int(getattr(cfg, f"{city}议价次数").value))
            bookSpin.blockSignals(False)
            haggleSpin.blockSignals(False)

    def saveRouteTradeSetting(self, index: int, kind: str, value: int):
        city = self.routeCities()[index]
        suffix = "进货书" if kind == "book" else "议价次数"
        qconfig.set(getattr(cfg, f"{city}{suffix}"), int(value))

    def updateRouteInfo(self):
        buy_city = self.buyCityComboBox.currentText()
        sell_city = self.sellCityComboBox.currentText()
        buy_count = len(CITY_GOODS.get(buy_city, {}))
        sell_count = len(CITY_GOODS.get(sell_city, {}))
        buy_ready = buy_city in CITY_POSITIONS
        sell_ready = sell_city in CITY_POSITIONS
        navigation = "导航资源完整" if buy_ready and sell_ready else "缺少地图坐标"
        self.routeInfoLabel.setText(
            f"{buy_city}（{buy_count} 种商品） → "
            f"{sell_city}（{sell_count} 种商品） · {navigation}"
        )
        self.updateRouteTradeSettings()

    def buildQueuedTask(self):
        """Return the configured run-business task for the global scheduler."""
        from app.utils.task_queue import QueuedTask
        from auto.run_business import stop

        if not bool(cfg.enableRunBusiness.value):
            return None
        buy_city_name = self.buyCityComboBox.currentText()
        sell_city_name = self.sellCityComboBox.currentText()
        if buy_city_name == sell_city_name or cfg.BuyCount.value <= 0:
            return None
        return QueuedTask(
            "端点跑商",
            lambda: self._runBusinessTask(buy_city_name, sell_city_name),
            stop,
            key="run_business",
            next_run_factory=lambda now: self._nextBusinessRun(
                buy_city_name, sell_city_name, now
            ),
        )

    @staticmethod
    def _nextBusinessRun(buy_city_name: str, sell_city_name: str, now):
        """Resume an unfinished weekly plan soon after yielding the task queue."""
        from datetime import timedelta

        from core.services import load_weekly_plan, progress_summary
        from core.services.task_schedule_state import next_daily_reset

        state = load_weekly_plan()
        summary = progress_summary(state)
        if (
            state
            and state.get("cycle") == [buy_city_name, sell_city_name]
            and summary
            and not summary["finished"]
        ):
            return now + timedelta(seconds=5)
        return next_daily_reset(now)

    def _runBusinessTask(self, buy_city_name: str, sell_city_name: str):
        from auto.run_business import adaptive_weekly_run, two_city_run
        from core.services import load_weekly_plan, progress_summary
        saved_plan = load_weekly_plan()
        route_plan = saved_plan or load_weekly_plan(include_expired=True)
        weekly_plan_matches = route_plan and route_plan["cycle"] == [buy_city_name, sell_city_name]
        if weekly_plan_matches:
            summary = progress_summary(saved_plan) if saved_plan else None
            if summary and summary["finished"]:
                logger.info("本周跑商计划已经完成，跳过端点跑商")
                return True
            return adaptive_weekly_run()
        return two_city_run(
            buy_city_name=buy_city_name,
            sell_city_name=sell_city_name,
        )
