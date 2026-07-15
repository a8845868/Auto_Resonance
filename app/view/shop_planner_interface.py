"""Shop-like GUI for selecting safe recurring purchases."""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLayout,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import CheckBox, ComboBox, ScrollArea

from app.common.style_sheet import StyleSheet
from app.components.task_schedule_card import TaskScheduleCard
from core.services.shop_catalog import (
    CurrencyDefinition,
    ShopDefinition,
    ShopItem,
    load_shop_catalog,
    load_shop_plan,
    next_shop_reset,
    save_shop_plan,
    shop_plan_enabled,
)


ROOT = Path(__file__).resolve().parents[2]
QUANTITY_OPTIONS = (
    ("one", "买 1 件"),
    ("max", "买到剩余上限（实时总价）"),
)


def _pixmap(relative_path: str, width: int, height: int) -> QPixmap:
    path = ROOT / Path(relative_path)
    pixmap = QPixmap(str(path))
    if pixmap.isNull():
        return pixmap
    return pixmap.scaled(
        width,
        height,
        Qt.AspectRatioMode.KeepAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    )


class ShopItemCard(QFrame):
    changed = Signal()

    def __init__(
        self,
        item: ShopItem,
        currency: CurrencyDefinition,
        rule: dict,
        parent=None,
    ):
        super().__init__(parent)
        self.item = item
        self.currency = currency
        self.setObjectName("shopItemCard")
        self.setMinimumHeight(142)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        root = QHBoxLayout(self)
        root.setContentsMargins(14, 12, 14, 12)
        root.setSpacing(13)

        icon = QLabel(self)
        icon.setFixedSize(82, 82)
        icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon.setPixmap(_pixmap(item.icon, 78, 78))
        root.addWidget(icon)

        details = QVBoxLayout()
        details.setSpacing(5)
        name = QLabel(item.name, self)
        name.setWordWrap(True)
        name.setStyleSheet("font-size: 15px; font-weight: 650;")
        limit = QLabel(f"{item.period_label}限购 {item.max_limit}", self)
        limit.setStyleSheet("color: #a3a3a3; font-size: 12px;")

        price_row = QHBoxLayout()
        price_row.setSpacing(5)
        currency_icon = QLabel(self)
        currency_icon.setFixedSize(24, 24)
        currency_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        currency_icon.setPixmap(_pixmap(currency.icon, 22, 22))
        price = QLabel(f"单件起价 {item.price:,}  {currency.name}", self)
        price.setStyleSheet("color: #e6bd61; font-size: 13px; font-weight: 600;")
        price_row.addWidget(currency_icon)
        price_row.addWidget(price)
        price_row.addStretch(1)
        details.addWidget(name)
        details.addWidget(limit)
        details.addLayout(price_row)
        details.addStretch(1)
        root.addLayout(details, 1)

        controls = QVBoxLayout()
        controls.setSpacing(8)
        self.enabledCheck = CheckBox("自动购买", self)
        self.enabledCheck.setChecked(bool(rule.get("enabled", False)))
        self.quantityCombo = ComboBox(self)
        self.quantityCombo.setMinimumWidth(158)
        for _, label in QUANTITY_OPTIONS:
            self.quantityCombo.addItem(label)
        selected_mode = str(rule.get("quantity", "max"))
        selected_index = next(
            (index for index, (key, _) in enumerate(QUANTITY_OPTIONS) if key == selected_mode),
            1,
        )
        self.quantityCombo.setCurrentIndex(selected_index)
        controls.addWidget(self.enabledCheck)
        controls.addWidget(self.quantityCombo)
        controls.addStretch(1)
        root.addLayout(controls)

        self.enabledCheck.checkStateChanged.connect(self._on_changed)
        self.quantityCombo.currentIndexChanged.connect(self._on_changed)
        self._refresh_style()

    def _on_changed(self, *_):
        self._refresh_style()
        self.changed.emit()

    def _refresh_style(self):
        if self.enabledCheck.isChecked():
            border = "rgba(224,174,76,0.92)"
            background = "rgba(181,121,29,0.16)"
        else:
            border = "rgba(150,150,150,0.25)"
            background = "rgba(255,255,255,0.045)"
        self.setStyleSheet(
            "QFrame#shopItemCard {"
            f"border: 1px solid {border}; background: {background}; border-radius: 10px;"
            "}"
            "QFrame#shopItemCard QLabel { border: none; background: transparent; }"
        )
        self.quantityCombo.setEnabled(self.enabledCheck.isChecked())

    def rule(self) -> dict:
        index = max(0, self.quantityCombo.currentIndex())
        return {
            "enabled": self.enabledCheck.isChecked(),
            "quantity": QUANTITY_OPTIONS[index][0],
        }


class ShopPlannerInterface(ScrollArea):
    """Configure recurring shop purchases without hard-coding future shops."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.catalog = load_shop_catalog()
        self.plan = load_shop_plan(catalog=self.catalog)
        self.currentShopId = self.catalog.shops[0].id
        self.itemCards: dict[str, ShopItemCard] = {}
        self.shopButtons: dict[str, QPushButton] = {}

        self.setObjectName("ShopPlannerInterface")
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setViewportMargins(0, 78, 0, 20)
        self.scrollWidget = QWidget(self)
        self.scrollWidget.setObjectName("scrollWidget")
        self.setWidget(self.scrollWidget)
        self.setWidgetResizable(True)
        StyleSheet.VIEW_INTERFACE.apply(self)

        title = QLabel("商店自动购买", self)
        title.setObjectName("titleLabel")
        title.move(36, 28)

        self.rootLayout = QVBoxLayout(self.scrollWidget)
        self.rootLayout.setSizeConstraint(QLayout.SizeConstraint.SetMinAndMaxSize)
        self.rootLayout.setContentsMargins(36, 6, 36, 24)
        self.rootLayout.setSpacing(13)

        safety = QFrame(self.scrollWidget)
        safety.setObjectName("shopSafetyPanel")
        safety.setStyleSheet(
            "QFrame#shopSafetyPanel { background: rgba(53,125,210,0.13); "
            "border: 1px solid rgba(74,154,240,0.48); border-radius: 11px; }"
            "QFrame#shopSafetyPanel QLabel { border: none; background: transparent; }"
        )
        safety_layout = QVBoxLayout(safety)
        safety_layout.setContentsMargins(17, 13, 17, 13)
        safety_title = QLabel("安全购买规则", safety)
        safety_title.setStyleSheet("font-size: 16px; font-weight: 700;")
        safety_text = QLabel(
            "默认全部不选。执行时关闭批量购买，逐件打开数量弹窗，并用 OCR 同时校验商品名、"
            "刷新周期/上限、起价、最终数量与弹窗实时总价；目录探测与干跑只点“取消”，"
            "不会确认购买。上限模式可能采用阶梯价格，不按起价乘数量估算。",
            safety,
        )
        safety_text.setWordWrap(True)
        safety_layout.addWidget(safety_title)
        safety_layout.addWidget(safety_text)
        self.rootLayout.addWidget(safety)

        options = QFrame(self.scrollWidget)
        options.setObjectName("shopOptionsPanel")
        options.setStyleSheet(
            "QFrame#shopOptionsPanel { background: rgba(255,255,255,0.045); "
            "border: 1px solid rgba(150,150,150,0.22); border-radius: 10px; }"
        )
        options_layout = QHBoxLayout(options)
        options_layout.setContentsMargins(17, 13, 17, 13)
        self.enabledCheck = CheckBox("启用商店自动购买", options)
        self.enabledCheck.setChecked(bool(self.plan["enabled"]))
        self.evidenceCheck = CheckBox("每一步保存截图与 OCR", options)
        self.evidenceCheck.setChecked(bool(self.plan["capture_evidence"]))
        self.summaryLabel = QLabel(options)
        self.summaryLabel.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.summaryLabel.setWordWrap(True)
        options_layout.addWidget(self.enabledCheck)
        options_layout.addWidget(self.evidenceCheck)
        options_layout.addStretch(1)
        options_layout.addWidget(self.summaryLabel, 2)
        self.rootLayout.addWidget(options)

        self.scheduleCard = TaskScheduleCard("shop_purchase", self.scrollWidget)
        self.rootLayout.addWidget(self.scheduleCard)

        tabs = QFrame(self.scrollWidget)
        tabs.setObjectName("shopTabs")
        tabs.setStyleSheet(
            "QFrame#shopTabs { background: rgba(16,16,16,0.50); border-radius: 9px; }"
        )
        tabs_layout = QHBoxLayout(tabs)
        tabs_layout.setContentsMargins(7, 7, 7, 7)
        tabs_layout.setSpacing(6)
        for shop in self.catalog.shops:
            button = QPushButton(shop.short_name, tabs)
            button.setMinimumHeight(36)
            button.clicked.connect(
                lambda _checked=False, shop_id=shop.id: self.showShop(shop_id)
            )
            tabs_layout.addWidget(button)
            self.shopButtons[shop.id] = button
        tabs_layout.addStretch(1)
        self.rootLayout.addWidget(tabs)

        self.shopHeader = QFrame(self.scrollWidget)
        self.shopHeader.setObjectName("shopHeader")
        self.shopHeaderLayout = QVBoxLayout(self.shopHeader)
        self.shopHeaderLayout.setContentsMargins(17, 13, 17, 13)
        self.shopTitle = QLabel(self.shopHeader)
        self.shopTitle.setStyleSheet("font-size: 20px; font-weight: 700;")
        self.shopDescription = QLabel(self.shopHeader)
        self.shopDescription.setWordWrap(True)
        self.shopDescription.setStyleSheet("color: #a7a7a7;")
        self.shopHeaderLayout.addWidget(self.shopTitle)
        self.shopHeaderLayout.addWidget(self.shopDescription)
        self.rootLayout.addWidget(self.shopHeader)

        self.productWidget = QWidget(self.scrollWidget)
        self.productGrid = QGridLayout(self.productWidget)
        self.productGrid.setContentsMargins(0, 0, 0, 0)
        self.productGrid.setHorizontalSpacing(12)
        self.productGrid.setVerticalSpacing(12)
        self.rootLayout.addWidget(self.productWidget)
        self.rootLayout.addStretch(1)

        self.enabledCheck.checkStateChanged.connect(self._plan_changed)
        self.evidenceCheck.checkStateChanged.connect(self._plan_changed)
        self.showShop(self.currentShopId)
        self._update_summary()

    def _clear_product_grid(self):
        self.itemCards.clear()
        while self.productGrid.count():
            item = self.productGrid.takeAt(0)
            widget = item.widget()
            if widget:
                widget.deleteLater()

    def showShop(self, shop_id: str):
        self.currentShopId = shop_id
        shop = self.catalog.shop(shop_id)
        self._clear_product_grid()
        self.shopTitle.setText(shop.name)
        self.shopDescription.setText(shop.description)
        self.shopHeader.setStyleSheet(
            "QFrame#shopHeader {"
            f"background: rgba(30,30,30,0.54); border-left: 5px solid {shop.accent}; "
            "border-radius: 8px; }"
            "QFrame#shopHeader QLabel { border: none; background: transparent; }"
        )
        for key, button in self.shopButtons.items():
            active = key == shop_id
            button.setStyleSheet(
                "QPushButton { border: none; border-radius: 7px; padding: 7px 15px; "
                f"background: {'rgba(224,174,76,0.32)' if active else 'transparent'}; "
                f"color: {'#f1c96e' if active else '#b7b7b7'}; "
                f"font-weight: {'700' if active else '500'}; }}"
                "QPushButton:hover { background: rgba(255,255,255,0.10); }"
            )

        if not shop.automation_supported:
            placeholder = QLabel(
                "该店铺已经进入统一目录与 GUI 框架。完成实机完整滚动采集、OCR 字段校验和"
                "专用购买适配器后，商品会直接出现在这里。",
                self.productWidget,
            )
            placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
            placeholder.setWordWrap(True)
            placeholder.setMinimumHeight(150)
            placeholder.setStyleSheet(
                "font-size: 15px; color: #a5a5a5; background: rgba(255,255,255,0.035); "
                "border: 1px dashed rgba(160,160,160,0.35); border-radius: 10px; padding: 24px;"
            )
            self.productGrid.addWidget(placeholder, 0, 0, 1, 2)
            return

        shop_plan = self.plan["shops"][shop.id]
        for index, item in enumerate(shop.items):
            card = ShopItemCard(
                item,
                self.catalog.currencies[item.currency],
                shop_plan["items"][item.id],
                self.productWidget,
            )
            card.changed.connect(self._plan_changed)
            self.itemCards[item.id] = card
            self.productGrid.addWidget(card, index // 2, index % 2)
        self.productGrid.setColumnStretch(0, 1)
        self.productGrid.setColumnStretch(1, 1)

    def _plan_changed(self, *_):
        self.plan["enabled"] = self.enabledCheck.isChecked()
        self.plan["capture_evidence"] = self.evidenceCheck.isChecked()
        shop_plan = self.plan["shops"][self.currentShopId]
        for item_id, card in self.itemCards.items():
            shop_plan["items"][item_id] = card.rule()
        self.plan = save_shop_plan(self.plan, catalog=self.catalog)
        self._update_summary()

    def _update_summary(self):
        selected = []
        fixed_costs = defaultdict(int)
        dynamic_total_count = 0
        for shop in self.catalog.shops:
            shop_plan = self.plan["shops"][shop.id]
            for item in shop.items:
                rule = shop_plan["items"][item.id]
                if not rule["enabled"]:
                    continue
                selected.append(item)
                if rule["quantity"] == "max":
                    dynamic_total_count += 1
                else:
                    fixed_costs[item.currency] += item.price
        costs = " + ".join(
            f"{amount:,} {self.catalog.currencies[key].name}"
            for key, amount in fixed_costs.items()
        )
        state = "自动执行已开启" if self.plan["enabled"] else "自动执行已关闭"
        estimates = []
        if costs:
            estimates.append(f"单件模式起价合计 {costs}")
        if dynamic_total_count:
            estimates.append(f"{dynamic_total_count} 项上限模式执行时读取实时总价")
        estimate = f"；{'；'.join(estimates)}" if estimates else ""
        self.summaryLabel.setText(f"{state} · 已选 {len(selected)} 项{estimate}")

    def buildQueuedTask(self):
        if not shop_plan_enabled(self.plan, self.catalog):
            return None
        from app.utils.task_queue import QueuedTask
        from auto.shop_purchase import run_shop_purchase

        return QueuedTask(
            "商店自动购买",
            run_shop_purchase,
            key="shop_purchase",
            next_run_factory=next_shop_reset,
        )

    def showEvent(self, event):
        self.plan = load_shop_plan(catalog=self.catalog)
        # A backend task may update the plan while this page is hidden.  Block
        # the two signals while refreshing so stale visible cards cannot write
        # over that newer file before the cards themselves are rebuilt.
        self.enabledCheck.blockSignals(True)
        self.evidenceCheck.blockSignals(True)
        try:
            self.enabledCheck.setChecked(bool(self.plan["enabled"]))
            self.evidenceCheck.setChecked(bool(self.plan["capture_evidence"]))
        finally:
            self.enabledCheck.blockSignals(False)
            self.evidenceCheck.blockSignals(False)
        self.showShop(self.currentShopId)
        self._update_summary()
        super().showEvent(event)
