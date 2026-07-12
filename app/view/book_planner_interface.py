from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QGridLayout, QLabel, QVBoxLayout, QWidget
from qfluentwidgets import CheckBox, ScrollArea, SpinBox, qconfig

from app.common.config import cfg
from app.common.signal_bus import signalBus
from app.common.style_sheet import StyleSheet
from core.services import BOOK_SOURCES, calculate_book_budget


class BookPlannerInterface(ScrollArea):
    """Restock-book income planner shared with the weekly route optimizer."""

    def __init__(self, parent=None):
        super().__init__(parent)
        if not bool(cfg.BookPlannerMigrated.value):
            for source in BOOK_SOURCES:
                qconfig.set(getattr(cfg, f"BookSource_{source.key}_Enabled"), source.default_enabled)
                if not source.editable:
                    qconfig.set(getattr(cfg, f"BookSource_{source.key}_Amount"), source.default_amount)
            qconfig.set(cfg.BookPlannerMigrated, True)
        self.setObjectName("BookPlannerInterface")
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setViewportMargins(0, 80, 0, 20)
        self.scrollWidget = QWidget(self)
        self.scrollWidget.setObjectName("scrollWidget")
        self.setWidget(self.scrollWidget)
        self.setWidgetResizable(True)
        StyleSheet.VIEW_INTERFACE.apply(self)

        self.titleLabel = QLabel("进货书规划器", self)
        self.titleLabel.setObjectName("titleLabel")
        self.titleLabel.move(36, 30)
        root = QVBoxLayout(self.scrollWidget)
        root.setContentsMargins(36, 8, 36, 24)
        root.setSpacing(12)

        intro = QLabel(
            "固定渠道只需勾选“本周期会拿满”，数量已按商店上限锁定；只有付费、活动等浮动来源需要手填。",
            self.scrollWidget,
        )
        intro.setWordWrap(True)
        root.addWidget(intro)

        self.rows = []
        fixed = self._panel("固定免费来源", root)
        fixed_layout = fixed.layout()
        fixed_layout.addWidget(QLabel("计入", fixed), 1, 0)
        fixed_layout.addWidget(QLabel("来源", fixed), 1, 1)
        fixed_layout.addWidget(QLabel("做满上限", fixed), 1, 2)
        fixed_layout.addWidget(QLabel("去哪里获取", fixed), 1, 3)
        fixed_row = 2
        for source in (item for item in BOOK_SOURCES if not item.editable):
            checkbox = CheckBox(fixed)
            checkbox.setChecked(bool(getattr(cfg, f"BookSource_{source.key}_Enabled").value))
            amount = QLabel(f"{source.default_amount} 本/{'周' if source.period == 'weekly' else '月'}", fixed)
            description = QLabel(source.location, fixed)
            description.setWordWrap(True)
            fixed_layout.addWidget(checkbox, fixed_row, 0)
            fixed_layout.addWidget(QLabel(source.label, fixed), fixed_row, 1)
            fixed_layout.addWidget(amount, fixed_row, 2)
            fixed_layout.addWidget(description, fixed_row, 3)
            self.rows.append((source, checkbox, None))
            checkbox.checkStateChanged.connect(self.recalculate)
            fixed_row += 1
        note = QLabel(
            "日常任务本身没有稳定的进货书直领奖励；做运输订单的价值是获得里程点。"
            "黑月本地商店位于各主城休息区，每周随机刷新；按你的使用习惯默认不计入。",
            fixed,
        )
        note.setWordWrap(True)
        fixed_layout.addWidget(note, fixed_row, 0, 1, 4)

        variable = self._panel("浮动与付费来源", root)
        variable_layout = variable.layout()
        variable_layout.addWidget(QLabel("计入", variable), 1, 0)
        variable_layout.addWidget(QLabel("来源", variable), 1, 1)
        variable_layout.addWidget(QLabel("周期", variable), 1, 2)
        variable_layout.addWidget(QLabel("实际数量", variable), 1, 3)
        for row, source in enumerate((item for item in BOOK_SOURCES if item.editable), start=2):
            checkbox = CheckBox(variable)
            checkbox.setChecked(bool(getattr(cfg, f"BookSource_{source.key}_Enabled").value))
            amount = SpinBox(variable)
            amount.setRange(0, 999)
            amount.setValue(int(getattr(cfg, f"BookSource_{source.key}_Amount").value))
            variable_layout.addWidget(checkbox, row, 0)
            variable_layout.addWidget(QLabel(source.label, variable), row, 1)
            variable_layout.addWidget(QLabel({"weekly": "每周", "monthly": "每月", "once": "本周一次"}[source.period], variable), row, 2)
            variable_layout.addWidget(amount, row, 3)
            self.rows.append((source, checkbox, amount))
            checkbox.checkStateChanged.connect(self.recalculate)
            amount.valueChanged.connect(self.recalculate)

        inventory = self._panel("库存与同步结果", root)
        inventory_layout = inventory.layout()
        self.autoRead = CheckBox("执行前自动读取背包；识别失败时使用右侧库存", inventory)
        self.autoRead.setChecked(bool(cfg.AutoReadInventoryBooks.value))
        self.inventory = SpinBox(inventory)
        self.inventory.setRange(0, 9999)
        self.inventory.setValue(int(cfg.InventoryBooks.value))
        inventory_layout.addWidget(self.autoRead, 1, 0, 1, 3)
        inventory_layout.addWidget(self.inventory, 1, 3)
        self.resultLabel = QLabel(inventory)
        self.resultLabel.setWordWrap(True)
        self.resultLabel.setStyleSheet("font-size: 16px; color: #35d7e8; padding: 8px 0;")
        inventory_layout.addWidget(self.resultLabel, 2, 0, 1, 4)
        self.syncLabel = QLabel("该结果会自动同步到“端点跑商 → 每周进货书”。", inventory)
        inventory_layout.addWidget(self.syncLabel, 3, 0, 1, 4)
        self.autoRead.checkStateChanged.connect(self.recalculate)
        self.inventory.valueChanged.connect(self.recalculate)
        root.addStretch(1)
        self.recalculate()

    @staticmethod
    def _panel(title: str, root: QVBoxLayout) -> QWidget:
        panel = QWidget()
        panel.setStyleSheet("background: rgba(255,255,255,0.05); border-radius: 8px;")
        layout = QGridLayout(panel)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setHorizontalSpacing(20)
        heading = QLabel(title, panel)
        heading.setStyleSheet("font-size: 18px;")
        layout.addWidget(heading, 0, 0, 1, 4)
        root.addWidget(panel)
        return panel

    def recalculate(self, *_):
        rows = []
        for source, checkbox, amount_widget in self.rows:
            amount = source.default_amount if amount_widget is None else amount_widget.value()
            qconfig.set(getattr(cfg, f"BookSource_{source.key}_Enabled"), checkbox.isChecked())
            qconfig.set(getattr(cfg, f"BookSource_{source.key}_Amount"), amount)
            rows.append({"enabled": checkbox.isChecked(), "period": source.period, "amount": amount})
        qconfig.set(cfg.InventoryBooks, self.inventory.value())
        qconfig.set(cfg.AutoReadInventoryBooks, self.autoRead.isChecked())
        budget = calculate_book_budget(rows, self.inventory.value())
        planned = budget["available_this_week"]
        qconfig.set(cfg.OptimizerBooks, planned)
        self.resultLabel.setText(
            f"每周固定/礼包 {budget['weekly']} 本；每月来源 {budget['monthly']} 本；"
            f"本周一次性 {budget['once']} 本；折算每周新增约 {budget['weekly_income']:.1f} 本。\n"
            f"当前库存 {self.inventory.value()} 本，本周可规划 {planned} 本。"
        )
        signalBus.bookBudgetChanged.emit()
