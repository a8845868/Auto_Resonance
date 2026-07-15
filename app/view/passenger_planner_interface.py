from __future__ import annotations

from copy import deepcopy
from datetime import datetime

from PySide6.QtCore import QThread, QTimer, Qt, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QAbstractItemView, QCheckBox, QDialog, QFrame, QGridLayout, QHeaderView, QHBoxLayout, QLabel, QScrollArea, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget
from qfluentwidgets import ComboBox, FluentIcon, InfoBar, InfoBarPosition, PrimaryPushButton, ScrollArea, SpinBox, qconfig

from app.common.config import cfg
from app.common.signal_bus import signalBus
from app.common.style_sheet import StyleSheet
from app.components.passenger_layout_editor import PassengerLayoutEditor
from app.utils.config import CITYS
from app.utils.constants import ROOT_PATH
from core.services.passenger_build_planner import (
    build_monitor_summary,
    calculate_passenger_build_plan,
    create_build_monitor_plan,
    load_build_monitor_plan,
)
from core.services.passenger_layout import (
    active_layout,
    auto_place_owned,
    calculate_layout_summary,
    clone_as_custom,
    load_furniture_catalog,
    load_layout_state,
    save_layout_state,
    toggle_slot,
)
from core.services.passenger_planner import PassengerPlanConfig, estimate_passenger_plan, route_reference


class FurnitureInventoryWorker(QThread):
    succeeded = Signal(dict)
    failed = Signal(str)

    def run(self):
        try:
            from auto.furniture_inventory import scan_furniture_inventory

            self.succeeded.emit(scan_furniture_inventory())
        except Exception as exc:
            self.failed.emit(str(exc))


class PassengerBuildInventoryWorker(QThread):
    succeeded = Signal(dict)
    failed = Signal(str)

    def run(self):
        try:
            from auto.passenger_carriage_build import scan_passenger_build_inventory

            self.succeeded.emit(scan_passenger_build_inventory())
        except Exception as exc:
            self.failed.emit(str(exc))


class PassengerPlannerInterface(ScrollArea):
    """Passenger construction, readiness and operation planner."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.furnitureCatalog = load_furniture_catalog()
        self.layoutState = load_layout_state()
        self._refreshingFurnitureTable = False
        self.lastSelectedFurnitureSlot = ""
        self.furnitureWorker = None
        self.passengerInventoryWorker = None
        self.furnitureRefreshTimer = QTimer(self)
        self.furnitureRefreshTimer.setSingleShot(True)
        self.furnitureRefreshTimer.timeout.connect(self._refreshFurnitureLayout)
        self.buildMonitorTimer = QTimer(self)
        self.buildMonitorTimer.timeout.connect(self._refreshBuildMonitorStatus)
        self.buildMonitorTimer.start(1000)
        self.setObjectName("PassengerPlannerInterface")
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setViewportMargins(0, 80, 0, 20)
        self.scrollWidget = QWidget(self)
        self.scrollWidget.setObjectName("scrollWidget")
        self.setWidget(self.scrollWidget)
        self.setWidgetResizable(True)
        StyleSheet.VIEW_INTERFACE.apply(self)
        title = QLabel("客运规划", self)
        title.setObjectName("titleLabel")
        title.move(36, 30)
        root = QVBoxLayout(self.scrollWidget)
        root.setContentsMargins(36, 8, 36, 24)
        root.setSpacing(14)
        intro = QLabel("把客运拆成建设、评分达标和每日运营三阶段；先补齐车厢与资源，再计算可执行收益。", self.scrollWidget)
        intro.setWordWrap(True)
        root.addWidget(intro)

        self.inputs = {}
        consist = self._panel("一、目标编组与建设进度", root)
        grid = consist.layout()
        fields = (
            ("target", "目标客厢总数", cfg.PassengerTargetCarriages, 1, 8),
            ("built", "已建额外客厢", cfg.PassengerBuiltExtraCarriages, 0, 7),
            ("seats", "已安装四座椅组", cfg.PassengerInstalledSeatGroups, 0, 128),
            ("iron", "当前铁盟币", cfg.PassengerCurrentIron, 0, 2_000_000_000),
        )
        for index, (key, label, item, low, high) in enumerate(fields):
            spin = SpinBox(consist)
            spin.setRange(low, high)
            spin.setValue(int(item.value))
            spin.valueChanged.connect(lambda value, config_item=item, build_key=key: self._buildInputChanged(config_item, build_key, value))
            grid.addWidget(QLabel(label, consist), 1, index)
            grid.addWidget(spin, 2, index)
            self.inputs[key] = spin
        self.consistResult = QLabel(consist)
        self.consistResult.setWordWrap(True)
        self.consistResult.setStyleSheet("font-size:16px; color:#35d7e8; padding-top:8px;")
        grid.addWidget(self.consistResult, 3, 0, 1, 4)

        monitor = self._panel("客厢连续建造监控（每节 6 小时）", root)
        monitor_grid = monitor.layout()
        self.buildMonitorEnabled = QCheckBox("加入首页自动任务队列", monitor)
        self.buildMonitorEnabled.setChecked(bool(cfg.enablePassengerBuildMonitor.value))
        self.buildMonitorEnabled.toggled.connect(
            lambda checked: qconfig.set(cfg.enablePassengerBuildMonitor, checked)
        )
        self.createBuildPlanButton = PrimaryPushButton(
            FluentIcon.CALENDAR, "按当前数量创建/重置计划", monitor
        )
        self.createBuildPlanButton.clicked.connect(self._createBuildMonitorPlan)
        self.syncBuildInventoryButton = PrimaryPushButton(
            FluentIcon.SYNC, "从游戏同步实际数量", monitor
        )
        self.syncBuildInventoryButton.clicked.connect(self._startBuildInventorySync)
        self.buildMonitorStatus = QLabel(monitor)
        self.buildMonitorStatus.setWordWrap(True)
        self.buildMonitorStatus.setStyleSheet("font-size:16px; color:#35d7e8; padding-top:8px;")
        monitor_grid.addWidget(self.buildMonitorEnabled, 1, 0)
        monitor_grid.addWidget(self.syncBuildInventoryButton, 1, 1)
        monitor_grid.addWidget(self.createBuildPlanButton, 1, 2, 1, 2)
        monitor_grid.addWidget(self.buildMonitorStatus, 2, 0, 1, 4)
        from app.components.task_schedule_card import TaskScheduleCard
        self.scheduleCard = TaskScheduleCard("passenger_build_monitor", monitor)
        monitor_grid.addWidget(self.scheduleCard, 3, 0, 1, 4)
        self._refreshBuildMonitorStatus()

        ratings = self._panel("二、客运评分达标", root)
        rating_grid = ratings.layout()
        rating_fields = (
            ("comfort", "舒适", cfg.PassengerRatingComfort, 50_000),
            ("food", "美味", cfg.PassengerRatingFood, 10_000),
            ("entertainment", "娱乐", cfg.PassengerRatingEntertainment, 10_000),
            ("pets", "宠物", cfg.PassengerRatingPets, 10_000),
            ("aquarium", "水族", cfg.PassengerRatingAquarium, 10_000),
            ("plants", "绿植", cfg.PassengerRatingPlants, 10_000),
            ("medical", "医疗", cfg.PassengerRatingMedical, 10_000),
        )
        for index, (key, label, item, high) in enumerate(rating_fields):
            row, column = 1 + (index // 4) * 2, index % 4
            spin = SpinBox(ratings)
            spin.setRange(0, high)
            spin.setValue(int(item.value))
            spin.valueChanged.connect(lambda value, config_item=item, rating_key=key: self._ratingInputChanged(config_item, rating_key, value))
            rating_grid.addWidget(QLabel(label, ratings), row, column)
            rating_grid.addWidget(spin, row + 1, column)
            self.inputs[key] = spin
        self.ratingResult = QLabel(ratings)
        self.ratingResult.setWordWrap(True)
        rating_grid.addWidget(self.ratingResult, 5, 0, 1, 4)

        furniture = self._panel("三、家具购买与摆放指引", root)
        furniture_grid = furniture.layout()
        guides = (
            ("舒适 4.2万", "各主城商会家具店", "浴缸、餐吧、现代餐椅及现有高舒适家具", "优先均摊到8节客厢；不足再补车长室/护卫厢墙面挂钟。"),
            ("美味 7000", "各主城商会家具店", "餐吧×8、现代蒸烤套组×7、现代餐椅约90", "每节客厢放餐吧和蒸烤套组，餐椅分散填满；先保证座椅升5级。"),
            ("娱乐 7000", "7号自由港→炸鸡店→娃娃机", "露蕾蒂站/坐姿、闪耀系列、电话亭娃娃机", "兑换7套，按每节客厢一套分散；大富翁收益家具集中改造2节即可。"),
            ("绿植 7000", "各主城商会家具店", "绿梅约344盆（毕业表参考）", "用剩余地面格补绿梅，评分到7000即停，溢出收益很低。"),
            ("水族 7000", "海角城、修格里城每日买鱼", "护卫厢2000大缸＋客厢24格小缸；鱼长期累积", "全景/大鱼缸优先放护卫厢；小缸分散客厢，鱼不必挑品质，评分够即可。"),
            ("医疗 7000", "商会家具店及任务赠送", "治疗台约13、救护病床、设备柜、药品柜", "治疗台分散空余地面，赠送医疗家具全部保留；达到7000后停止堆叠。"),
            ("宠物 7000", "修格里城每周购买宠物", "宠物别墅＋长期购买的宠物", "宠物别墅只放默认车厢，避免拆卸客厢时宠物被撤下；不必追求单只最高评分。"),
        )
        for row, (target, location, items, placement) in enumerate(guides, start=1):
            furniture_grid.addWidget(QLabel(target, furniture), row, 0)
            furniture_grid.addWidget(QLabel(location, furniture), row, 1)
            item_label = QLabel(items, furniture)
            item_label.setWordWrap(True)
            furniture_grid.addWidget(item_label, row, 2)
            placement_label = QLabel(placement, furniture)
            placement_label.setWordWrap(True)
            furniture_grid.addWidget(placement_label, row, 3)

        layout_panel = self._panel("四、家具库存与可交互结构图", root)
        layout_grid = layout_panel.layout()
        self.layoutMode = ComboBox(layout_panel)
        self.layoutMode.addItems(["成本表毕业推荐结构（默认）", "我的自定义结构"])
        self.layoutMode.setCurrentIndex(1 if self.layoutState.get("active_layout") == "custom" else 0)
        self.layoutZoom = SpinBox(layout_panel)
        self.layoutZoom.setRange(40, 140)
        self.layoutZoom.setValue(75)
        layout_grid.addWidget(QLabel("方案", layout_panel), 1, 0)
        layout_grid.addWidget(self.layoutMode, 1, 1)
        layout_grid.addWidget(QLabel("缩放 %", layout_panel), 1, 2)
        layout_grid.addWidget(self.layoutZoom, 1, 3)

        toolbar = QHBoxLayout()
        self.autoPlaceButton = PrimaryPushButton(FluentIcon.ACCEPT, "按仓库自动点亮", layout_panel)
        self.customizeButton = PrimaryPushButton(FluentIcon.EDIT, "复制为自定义", layout_panel)
        self.saveLayoutButton = PrimaryPushButton(FluentIcon.SAVE, "保存结构图", layout_panel)
        self.scanFurnitureButton = PrimaryPushButton(FluentIcon.SEARCH, "扫描私人仓库", layout_panel)
        self.referenceAButton = PrimaryPushButton(FluentIcon.PHOTO, "彩映原图", layout_panel)
        self.referenceBButton = PrimaryPushButton(FluentIcon.PHOTO, "联围最原图", layout_panel)
        toolbar.addWidget(self.autoPlaceButton)
        toolbar.addWidget(self.customizeButton)
        toolbar.addWidget(self.saveLayoutButton)
        toolbar.addWidget(self.scanFurnitureButton)
        toolbar.addWidget(self.referenceAButton)
        toolbar.addWidget(self.referenceBButton)
        toolbar.addStretch(1)
        layout_grid.addLayout(toolbar, 2, 0, 1, 4)

        visual_row = QHBoxLayout()
        self.layoutEditor = PassengerLayoutEditor(layout_panel)
        visual_row.addWidget(self.layoutEditor, 1)
        scoreSide = QFrame(layout_panel)
        scoreSide.setMinimumWidth(330)
        scoreSide.setMaximumWidth(390)
        scoreSide.setStyleSheet("QFrame { background:rgba(0,0,0,0.15); border-radius:8px; }")
        scoreBox = QVBoxLayout(scoreSide)
        scoreBox.addWidget(QLabel("当前结构图评分（按客运表满图鉴满振口径估算）", scoreSide))
        self.layoutScoreLabel = QLabel(layout_panel)
        self.layoutScoreLabel.setWordWrap(True)
        self.layoutScoreLabel.setStyleSheet("font-size:16px; color:#35d7e8; padding:6px 0;")
        scoreBox.addWidget(self.layoutScoreLabel)
        self.layoutMissingLabel = QLabel(scoreSide)
        self.layoutMissingLabel.setWordWrap(True)
        scoreBox.addWidget(self.layoutMissingLabel)
        scoreBox.addStretch(1)
        visual_row.addWidget(scoreSide)
        layout_grid.addLayout(visual_row, 3, 0, 1, 4)

        custom_row = QHBoxLayout()
        self.addFurnitureSelector = ComboBox(layout_panel)
        for key, item in sorted(self.furnitureCatalog.items(), key=lambda pair: pair[1]["name"]):
            self.addFurnitureSelector.addItem(item["name"], userData=key)
        self.addFurnitureCount = SpinBox(layout_panel)
        self.addFurnitureCount.setRange(1, 999)
        self.addFurnitureCount.setValue(1)
        self.addFurnitureButton = PrimaryPushButton(FluentIcon.ADD, "添加家具块", layout_panel)
        self.deleteFurnitureButton = PrimaryPushButton(FluentIcon.DELETE, "删除选中块", layout_panel)
        custom_row.addWidget(QLabel("自定义编辑", layout_panel))
        custom_row.addWidget(self.addFurnitureSelector, 1)
        custom_row.addWidget(self.addFurnitureCount)
        custom_row.addWidget(self.addFurnitureButton)
        custom_row.addWidget(self.deleteFurnitureButton)
        layout_grid.addLayout(custom_row, 4, 0, 1, 4)

        self.furnitureTable = QTableWidget(0, 7, layout_panel)
        self.furnitureTable.setHorizontalHeaderLabels(["家具", "方案需要", "仓库现有", "已摆入", "持有合计", "还缺", "获取地点"])
        self.furnitureTable.verticalHeader().hide()
        self.furnitureTable.setMinimumHeight(430)
        self.furnitureTable.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.furnitureTable.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        header = self.furnitureTable.horizontalHeader()
        for column in range(1, 6):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(6, QHeaderView.ResizeMode.Stretch)
        layout_grid.addWidget(self.furnitureTable, 5, 0, 1, 4)
        layoutNote = QLabel(
            "颜色：绿色=已摆满，黄色=部分摆入，蓝色=仓库有但未摆，灰色=缺少。单击家具块可点亮/置灰；"
            "自定义方案可拖动、添加或删除家具块。仓库数量与已摆入数量分开保存。"
            "交互图按成本表清单汇总计分，不冒充逐格复刻；两位作者原图可用上方按钮查看。",
            layout_panel,
        )
        layoutNote.setWordWrap(True)
        layout_grid.addWidget(layoutNote, 6, 0, 1, 4)
        self.layoutMode.currentIndexChanged.connect(self._switchLayoutMode)
        self.layoutZoom.valueChanged.connect(lambda value: self.layoutEditor.zoom(value / 100))
        self.autoPlaceButton.clicked.connect(self._autoPlaceFurniture)
        self.customizeButton.clicked.connect(self._cloneCustomLayout)
        self.saveLayoutButton.clicked.connect(self._saveFurnitureState)
        self.scanFurnitureButton.clicked.connect(self._startFurnitureScan)
        self.referenceAButton.clicked.connect(lambda: self._showReferenceLayout("caijing-full.png", "彩映参考摆放图"))
        self.referenceBButton.clicked.connect(lambda: self._showReferenceLayout("lianweizui-full.png", "联围最参考摆放图"))
        self.addFurnitureButton.clicked.connect(self._addFurnitureBlock)
        self.deleteFurnitureButton.clicked.connect(self._deleteSelectedFurnitureBlock)
        self.layoutEditor.blockClicked.connect(self._toggleFurnitureBlock)
        self.layoutEditor.blockMoved.connect(self._furnitureBlockMoved)
        self.layoutEditor.selectionChanged.connect(self._rememberSelectedFurnitureSlot)
        self._syncSeatGroupsFromBuild()
        self._refreshFurnitureLayout()

        operations = self._panel("五、路线目标、运营与收益", root)
        operation_grid = operations.layout()
        self.routeObjective = ComboBox(operations)
        self.routeObjective.addItems(["当前利润优先", "指定站点声望", "自定义路线"])
        self.routeObjective.setCurrentText(str(cfg.PassengerRouteObjective.value))
        self.routeOrigin = ComboBox(operations)
        self.routeDestination = ComboBox(operations)
        self.routeOrigin.addItems(CITYS)
        self.routeDestination.addItems(CITYS)
        self.routeOrigin.setCurrentText(str(cfg.PassengerOrigin.value))
        self.routeDestination.setCurrentText(str(cfg.PassengerDestination.value))
        self.observedRevenue = SpinBox(operations)
        self.observedRevenue.setRange(0, 5000)
        self.observedRevenue.setValue(int(cfg.PassengerObservedRevenueWan.value))
        self.routeFatigue = SpinBox(operations)
        self.routeFatigue.setRange(0, 1000)
        self.routeFatigue.setValue(int(cfg.PassengerRouteFatigue.value))
        for column, (label, widget) in enumerate((
            ("规划目标", self.routeObjective), ("起点", self.routeOrigin), ("终点/声望站", self.routeDestination),
            ("实测满载收益(万，0=表内)", self.observedRevenue),
        )):
            operation_grid.addWidget(QLabel(label, operations), 1, column)
            operation_grid.addWidget(widget, 2, column)
        operation_grid.addWidget(QLabel("单程疲劳", operations), 3, 0)
        operation_grid.addWidget(self.routeFatigue, 3, 1)
        self.routeHint = QLabel(operations)
        self.routeHint.setWordWrap(True)
        operation_grid.addWidget(self.routeHint, 3, 2, 1, 2)
        self.routeObjective.currentTextChanged.connect(self._routeChanged)
        self.routeOrigin.currentTextChanged.connect(self._routeChanged)
        self.routeDestination.currentTextChanged.connect(self._routeChanged)
        self.observedRevenue.valueChanged.connect(self._routeChanged)
        self.routeFatigue.valueChanged.connect(self._routeChanged)
        self.operationResult = QLabel(operations)
        self.operationResult.setWordWrap(True)
        self.operationResult.setStyleSheet("font-size:16px; color:#35d7e8;")
        operation_grid.addWidget(self.operationResult, 4, 0, 1, 4)
        buttons = QHBoxLayout()
        currency = PrimaryPushButton(FluentIcon.ALBUM, "打开货币规划", operations)
        currency.clicked.connect(lambda: signalBus.switchToCard.emit("InventoryInterface"))
        trade = PrimaryPushButton(FluentIcon.TRAIN, "打开端点跑商", operations)
        trade.clicked.connect(lambda: signalBus.switchToCard.emit("TwoCityRunnBusinessInterface"))
        buttons.addWidget(currency)
        buttons.addWidget(trade)
        buttons.addStretch(1)
        operation_grid.addLayout(buttons, 5, 0, 1, 4)
        checklist = QLabel(
            "建设顺序：额外客厢 → 座椅升满 → 舒适4.2万/其余各7000 → 家具收益加成 → 广告与传单储备 → 清洁后发车。\n"
            "材料获取与精确家具清单后续接背包扫描；当前先用铁盟币总预算和评分缺口控制是否进入运营阶段。",
            operations,
        )
        checklist.setWordWrap(True)
        operation_grid.addWidget(checklist, 6, 0, 1, 4)
        root.addStretch(1)
        self.recalculate()

    def _switchLayoutMode(self, index: int):
        if index == 1 and not self.layoutState.get("custom_layout"):
            clone_as_custom(self.layoutState)
        self.layoutState["active_layout"] = "custom" if index == 1 else "default"
        save_layout_state(self.layoutState)
        self._refreshFurnitureLayout()

    def _startFurnitureScan(self):
        if self.furnitureWorker and self.furnitureWorker.isRunning():
            return
        self.scanFurnitureButton.setEnabled(False)
        self.scanFurnitureButton.setText("正在扫描…")
        self.furnitureWorker = FurnitureInventoryWorker(self)
        self.furnitureWorker.succeeded.connect(self._furnitureScanSucceeded)
        self.furnitureWorker.failed.connect(self._furnitureScanFailed)
        self.furnitureWorker.finished.connect(self._furnitureScanFinished)
        self.furnitureWorker.start()

    def _furnitureScanSucceeded(self, result: dict):
        warehouse = self.layoutState.setdefault("warehouse", {})
        for key, detail in result.get("items", {}).items():
            warehouse[key] = max(0, int(detail.get("warehouse_count", 0)))
        self.layoutState["last_scan"] = {
            "time": datetime.now().isoformat(timespec="seconds"),
            "complete": bool(result.get("complete")),
            "recognized": len(result.get("items", {})),
            "unknown": len(result.get("unknown", [])),
        }
        save_layout_state(self.layoutState)
        self._refreshFurnitureLayout()
        InfoBar.success(
            "私人仓库扫描完成",
            f"确认 {len(result.get('items', {}))} 类家具；未确认 {len(result.get('unknown', []))} 个卡片。未识别项保留原值。",
            position=InfoBarPosition.TOP,
            duration=5000,
            parent=self,
        )

    def _furnitureScanFailed(self, message: str):
        InfoBar.error("私人仓库扫描失败", message, position=InfoBarPosition.TOP, duration=6000, parent=self)

    def _furnitureScanFinished(self):
        self.scanFurnitureButton.setEnabled(True)
        self.scanFurnitureButton.setText("扫描私人仓库")
        if self.furnitureWorker:
            self.furnitureWorker.deleteLater()
        self.furnitureWorker = None

    def _showReferenceLayout(self, file_name: str, title: str):
        dialog = QDialog(self)
        dialog.setWindowTitle(title)
        dialog.resize(1500, 900)
        box = QVBoxLayout(dialog)
        note = QLabel("原表参考图与成本表计分清单不是逐项一一对应，仅用于参考空间摆法。", dialog)
        note.setWordWrap(True)
        box.addWidget(note)
        scroll = QScrollArea(dialog)
        image = QLabel()
        pixmap = QPixmap(str(ROOT_PATH / "resources" / "passenger" / "layouts" / file_name))
        if pixmap.isNull():
            image.setText("参考图资源缺失")
        else:
            image.setPixmap(pixmap)
            image.resize(pixmap.size())
        scroll.setWidget(image)
        scroll.setWidgetResizable(False)
        box.addWidget(scroll, 1)
        dialog.exec()

    def _refreshFurnitureLayout(self):
        diagram = active_layout(self.layoutState)
        self.layoutEditor.set_layout(
            diagram,
            self.furnitureCatalog,
            self.layoutState.get("warehouse", {}),
            self.layoutState.get("placements", {}),
        )
        self.layoutEditor.zoom(self.layoutZoom.value() / 100)
        summary = calculate_layout_summary(self.layoutState, diagram, self.furnitureCatalog)
        pet_score = max(summary["scores"]["pets"]["value"], self.inputs["pets"].value())
        summary["scores"]["pets"]["value"] = pet_score
        summary["scores"]["pets"]["ready"] = pet_score >= summary["scores"]["pets"]["target"]
        score_parts = []
        for key, row in summary["scores"].items():
            if key == "pets" and row["value"] <= 0:
                score_parts.append(f"宠物 未录入/{row['target']:,}（上方手填）")
            else:
                score_parts.append(f"{row['label']} {row['value']:,.0f}/{row['target']:,}{'✓' if row['ready'] else ''}")
        score_text = "；".join(score_parts)
        self.layoutScoreLabel.setText(
            f"已摆 {summary['placed_total']}/{summary['required_total']} 件；仍缺 {summary['missing_total']} 件。\n{score_text}"
        )
        missing_rows = [row for row in summary["rows"] if row["missing"]]
        self.layoutMissingLabel.setText(
            "优先补齐：\n" + "\n".join(f"• {row['name']} ×{row['missing']}（{row['source']}）" for row in missing_rows[:9])
            if missing_rows else "家具数量已满足当前结构。"
        )
        self._populateFurnitureTable(summary)
        custom = self.layoutState.get("active_layout") == "custom"
        self.addFurnitureSelector.setEnabled(custom)
        self.addFurnitureCount.setEnabled(custom)
        self.addFurnitureButton.setEnabled(custom)
        self.deleteFurnitureButton.setEnabled(custom)

    def _populateFurnitureTable(self, summary: dict):
        self._refreshingFurnitureTable = True
        self.furnitureTable.setRowCount(len(summary["rows"]))
        for row_index, row in enumerate(summary["rows"]):
            self.furnitureTable.setItem(row_index, 0, QTableWidgetItem(row["name"]))
            self.furnitureTable.setItem(row_index, 1, QTableWidgetItem(str(row["required"])))
            warehouse = SpinBox(self.furnitureTable)
            warehouse.setRange(0, 9999)
            warehouse.setValue(int(row["warehouse"]))
            warehouse.valueChanged.connect(lambda value, key=row["key"]: self._warehouseCountChanged(key, value))
            self.furnitureTable.setCellWidget(row_index, 2, warehouse)
            self.furnitureTable.setItem(row_index, 3, QTableWidgetItem(str(row["placed"])))
            self.furnitureTable.setItem(row_index, 4, QTableWidgetItem(str(row["owned"])))
            missing = QTableWidgetItem(str(row["missing"]))
            if row["missing"]:
                missing.setForeground(Qt.GlobalColor.red)
            self.furnitureTable.setItem(row_index, 5, missing)
            self.furnitureTable.setItem(row_index, 6, QTableWidgetItem(row["source"]))
        self._refreshingFurnitureTable = False

    def _warehouseCountChanged(self, key: str, value: int):
        if self._refreshingFurnitureTable:
            return
        self.layoutState.setdefault("warehouse", {})[key] = max(0, int(value))
        save_layout_state(self.layoutState)
        self.furnitureRefreshTimer.start(350)

    def _toggleFurnitureBlock(self, slot_id: str):
        toggle_slot(self.layoutState, slot_id)
        self._saveFurnitureState(refresh=True)

    def _autoPlaceFurniture(self):
        auto_place_owned(self.layoutState)
        self._saveFurnitureState(refresh=True)

    def _cloneCustomLayout(self):
        clone_as_custom(self.layoutState)
        self.layoutMode.blockSignals(True)
        self.layoutMode.setCurrentIndex(1)
        self.layoutMode.blockSignals(False)
        self._saveFurnitureState(refresh=True)

    def _saveFurnitureState(self, *_ , refresh: bool = False):
        save_layout_state(self.layoutState)
        if refresh:
            self.furnitureRefreshTimer.start(0)

    def _furnitureBlockMoved(self, slot_id: str, x: float, y: float):
        if self.layoutState.get("active_layout") != "custom" or not self.layoutState.get("custom_layout"):
            return
        slot = next((item for item in self.layoutState["custom_layout"].get("slots", []) if item["id"] == slot_id), None)
        if slot:
            canvas = self.layoutState["custom_layout"].get("canvas", {"width": 1600, "height": 820})
            x = min(max(0, x), float(canvas["width"]) - float(slot.get("w", 180)))
            y = min(max(0, y), float(canvas["height"]) - float(slot.get("h", 70)))
            slot["x"], slot["y"] = round(x / 10) * 10, round(y / 10) * 10
            save_layout_state(self.layoutState)
            self.furnitureRefreshTimer.start(0)

    def _rememberSelectedFurnitureSlot(self, slot_id: str):
        if slot_id:
            self.lastSelectedFurnitureSlot = slot_id

    def _addFurnitureBlock(self):
        if self.layoutState.get("active_layout") != "custom":
            return
        layout = self.layoutState.get("custom_layout")
        if not layout:
            return
        key = self.addFurnitureSelector.currentData()
        slot_id = f"custom-{key}-{datetime.now().strftime('%H%M%S%f')}"
        offset = len(layout.get("slots", [])) % 12
        layout.setdefault("slots", []).append({
            "id": slot_id, "item": key, "count": self.addFurnitureCount.value(),
            "x": 60 + offset * 110, "y": 710, "w": 180, "h": 70,
        })
        self._saveFurnitureState(refresh=True)

    def _deleteSelectedFurnitureBlock(self):
        if self.layoutState.get("active_layout") != "custom" or not self.layoutState.get("custom_layout"):
            return
        slot_id = self.layoutEditor.selected_slot_id() or self.lastSelectedFurnitureSlot
        if not slot_id:
            return
        slots = self.layoutState["custom_layout"].get("slots", [])
        slot = next((item for item in slots if item["id"] == slot_id), None)
        if slot:
            placed = int(self.layoutState.setdefault("placements", {}).pop(slot_id, 0))
            key = slot["item"]
            self.layoutState.setdefault("warehouse", {})[key] = int(self.layoutState["warehouse"].get(key, 0)) + placed
            slots.remove(slot)
        self._saveFurnitureState(refresh=True)

    def _routeChanged(self, *_):
        qconfig.set(cfg.PassengerRouteObjective, self.routeObjective.currentText())
        qconfig.set(cfg.PassengerOrigin, self.routeOrigin.currentText())
        qconfig.set(cfg.PassengerDestination, self.routeDestination.currentText())
        qconfig.set(cfg.PassengerObservedRevenueWan, self.observedRevenue.value())
        qconfig.set(cfg.PassengerRouteFatigue, self.routeFatigue.value())
        reference = route_reference(self.routeOrigin.currentText(), self.routeDestination.currentText())
        if self.observedRevenue.value() == 0 and reference:
            self.routeFatigue.blockSignals(True)
            self.routeFatigue.setValue(int(reference["fatigue"]))
            self.routeFatigue.blockSignals(False)
            qconfig.set(cfg.PassengerRouteFatigue, self.routeFatigue.value())
        self.recalculate()

    def _ratingInputChanged(self, config_item, rating_key: str, value: int):
        qconfig.set(config_item, value)
        self.recalculate()
        if rating_key == "pets":
            self.furnitureRefreshTimer.start(0)

    def _buildInputChanged(self, config_item, build_key: str, value: int):
        qconfig.set(config_item, value)
        if build_key == "seats":
            self._syncSeatGroupsFromBuild()
            save_layout_state(self.layoutState)
            self.furnitureRefreshTimer.start(0)
        self.recalculate()

    def _createBuildMonitorPlan(self):
        current = 1 + int(self.inputs["built"].value())
        target = int(self.inputs["target"].value())
        state = create_build_monitor_plan(
            target_carriages=target, current_carriages=current
        )
        qconfig.set(cfg.enablePassengerBuildMonitor, state["status"] != "completed")
        self.buildMonitorEnabled.setChecked(state["status"] != "completed")
        self._refreshBuildMonitorStatus()
        InfoBar.success(
            title="连续建造计划已保存",
            content=f"当前 {current} 节，目标 {target} 节；请到首页启动自动任务。",
            orient=Qt.Orientation.Horizontal,
            isClosable=True,
            position=InfoBarPosition.TOP_RIGHT,
            duration=5000,
            parent=self,
        )

    def _startBuildInventorySync(self):
        if self.passengerInventoryWorker and self.passengerInventoryWorker.isRunning():
            return
        self.syncBuildInventoryButton.setEnabled(False)
        self.syncBuildInventoryButton.setText("正在核对…")
        self.passengerInventoryWorker = PassengerBuildInventoryWorker(self)
        self.passengerInventoryWorker.succeeded.connect(self._buildInventorySynced)
        self.passengerInventoryWorker.failed.connect(self._buildInventorySyncFailed)
        self.passengerInventoryWorker.finished.connect(
            lambda: self.syncBuildInventoryButton.setEnabled(True)
        )
        self.passengerInventoryWorker.finished.connect(
            lambda: self.syncBuildInventoryButton.setText("从游戏同步实际数量")
        )
        self.passengerInventoryWorker.start()

    def _buildInventorySynced(self, result: dict):
        built = int(result["built_extra_passenger_carriages"])
        seats = int(result["installed_seat_groups"])
        for key, value, config_item in (
            ("built", built, cfg.PassengerBuiltExtraCarriages),
            ("seats", seats, cfg.PassengerInstalledSeatGroups),
        ):
            widget = self.inputs[key]
            widget.blockSignals(True)
            widget.setValue(value)
            widget.blockSignals(False)
            qconfig.set(config_item, value)
        self._syncSeatGroupsFromBuild()
        save_layout_state(self.layoutState)
        self._refreshFurnitureLayout()
        self.recalculate()
        InfoBar.success(
            title="已按游戏实况同步",
            content=(
                f"1 节初始客厢 + {built} 节额外标准客厢；"
                f"按每节 16 组同步四座椅组为 {seats}。"
            ),
            orient=Qt.Orientation.Horizontal,
            isClosable=True,
            position=InfoBarPosition.TOP_RIGHT,
            duration=5000,
            parent=self,
        )

    def _buildInventorySyncFailed(self, message: str):
        InfoBar.error(
            title="实况同步未通过核对",
            content=message,
            orient=Qt.Orientation.Horizontal,
            isClosable=True,
            position=InfoBarPosition.TOP_RIGHT,
            duration=6000,
            parent=self,
        )

    def _refreshBuildMonitorStatus(self):
        if not hasattr(self, "buildMonitorStatus"):
            return
        summary = build_monitor_summary(load_build_monitor_plan())
        suffix = "；监控已加入队列" if bool(cfg.enablePassengerBuildMonitor.value) and summary.get("active") else ""
        self.buildMonitorStatus.setText(summary["message"] + suffix)

    def buildQueuedTask(self):
        from app.utils.task_queue import QueuedTask
        from auto.passenger_carriage_build import run_build_monitor, stop
        from core.services.task_schedule_state import is_force_verify_requested

        state = load_build_monitor_plan()
        summary = build_monitor_summary(state)
        if not bool(cfg.enablePassengerBuildMonitor.value) or not summary.get("active"):
            return None
        from datetime import timedelta

        def next_build_check(now):
            latest = load_build_monitor_plan() or {}
            due = latest.get("active_due_at")
            if due:
                try:
                    return datetime.fromisoformat(due).replace(tzinfo=None)
                except ValueError:
                    pass
            return now + timedelta(minutes=5)

        return QueuedTask(
            "客厢连续建造监控",
            lambda: run_build_monitor(
                force_verify=is_force_verify_requested("passenger_build_monitor")
            ),
            stop,
            key="passenger_build_monitor",
            next_run_factory=next_build_check,
        )

    def _syncSeatGroupsFromBuild(self):
        target = max(0, int(self.inputs["seats"].value()))
        placements = self.layoutState.setdefault("placements", {})
        warehouse = self.layoutState.setdefault("warehouse", {})
        current = max(0, int(placements.get("seat", 0)))
        if target < current:
            warehouse["seat_group"] = max(0, int(warehouse.get("seat_group", 0))) + current - target
        elif target > current:
            warehouse["seat_group"] = max(0, int(warehouse.get("seat_group", 0)) - (target - current))
        placements["seat"] = target

    @staticmethod
    def _panel(title: str, root: QVBoxLayout) -> QFrame:
        panel = QFrame()
        panel.setStyleSheet("QFrame { background:rgba(255,255,255,0.05); border-radius:9px; }")
        layout = QGridLayout(panel)
        layout.setContentsMargins(18, 14, 18, 14)
        layout.setHorizontalSpacing(18)
        heading = QLabel(title, panel)
        heading.setStyleSheet("font-size:18px; font-weight:600;")
        layout.addWidget(heading, 0, 0, 1, 4)
        root.addWidget(panel)
        return panel

    def recalculate(self, *_):
        result = calculate_passenger_build_plan(
            target_passenger_carriages=self.inputs["target"].value(),
            built_extra_passenger_carriages=self.inputs["built"].value(),
            installed_seat_groups=self.inputs["seats"].value(),
            current_iron=self.inputs["iron"].value(),
            comfort=self.inputs["comfort"].value(),
            food=self.inputs["food"].value(),
            entertainment=self.inputs["entertainment"].value(),
            pets=self.inputs["pets"].value(),
            aquarium=self.inputs["aquarium"].value(),
            plants=self.inputs["plants"].value(),
            medical=self.inputs["medical"].value(),
        )
        self.consistResult.setText(
            f"11节编组：{result['passenger_carriages']}客 + {result['freight_carriages']}货 + 2功能厢；"
            f"共 {result['seats']} 客位。尚缺额外客厢 {result['missing_extra_passenger_carriages']} 节、"
            f"四座椅组 {result['missing_seat_groups']} 组；铁盟币预算缺口约 {result['iron_shortfall'] / 10_000:.0f} 万。"
        )
        self.ratingResult.setText(
            "评分已达运营门槛。" if result["rating_ready"] else "未达标：" + "、".join(result["missing_ratings"])
        )
        reference = route_reference(self.routeOrigin.currentText(), self.routeDestination.currentText())
        observed = self.observedRevenue.value() * 10_000
        reference_revenue = observed or (int(reference["revenue"]) if reference else int(cfg.PassengerReferenceRevenueWan.value) * 10_000)
        passenger = estimate_passenger_plan(
            PassengerPlanConfig(
                seats=result["seats"],
                trips_per_week=int(cfg.PassengerTripsPerWeek.value),
                reference_trip_revenue=reference_revenue,
                fatigue_per_trip=self.routeFatigue.value(),
            )
        )
        state = "可进入运营" if result["build_ready"] and result["rating_ready"] else "先完成建设与评分"
        source = "用户实测" if observed else ("2026-05-25客运表" if reference else "通用基准估算")
        objective_note = "终点声望优先" if self.routeObjective.currentText() == "指定站点声望" else self.routeObjective.currentText()
        self.routeHint.setText(
            f"{source}；{objective_note}。未知路线建议先跑1次，把结算填入“实测满载收益”，以后版本变化也只需改这里。"
        )
        self.operationResult.setText(
            f"{state}；{self.routeOrigin.currentText()} → {self.routeDestination.currentText()}，"
            f"预计客运 {passenger['weekly_revenue'] / 10_000:.0f} 万/周，"
            f"占用疲劳约 {passenger['weekly_fatigue']}。"
        )
