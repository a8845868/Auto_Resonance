"""Shop-like GUI for selecting safe recurring purchases."""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path

from PySide6.QtCore import QThread, Qt, Signal
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
from qfluentwidgets import (
    CheckBox,
    ComboBox,
    FluentIcon,
    InfoBar,
    InfoBarPosition,
    PrimaryPushButton,
    ScrollArea,
)

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


def _quantity_options(
    item: ShopItem,
    currency: CurrencyDefinition,
) -> tuple[tuple[str, str], ...]:
    if not item.price_tiers:
        return QUANTITY_OPTIONS
    return (
        ("one", "从当前状态仅买 1 件（按当前档位）"),
        ("max", "买完当前剩余（弹窗实时总价）"),
    )


def _price_breakdown_text(item: ShopItem, currency: CurrencyDefinition) -> str:
    rows = []
    for number, _remaining, marginal, cumulative in item.price_breakdown():
        if marginal is None or cumulative is None:
            rows.append(f"第 {number} 件：价格未采集（不可选为累计目标）")
        else:
            rows.append(
                f"第 {number} 件：边际 {marginal:,}，累计 {cumulative:,} {currency.name}"
            )
    return "  ·  ".join(rows)


def _observed_price_breakdown_text(
    observations: object,
    currency: CurrencyDefinition,
) -> str:
    if not isinstance(observations, list) or not observations:
        return ""
    rows = []
    for observation in observations:
        if not isinstance(observation, dict):
            return ""
        try:
            quantity = int(observation["quantity"])
            marginal = int(observation["marginal_cost"])
            cumulative = int(observation["cumulative_cost"])
        except (KeyError, TypeError, ValueError):
            return ""
        if quantity < 1 or marginal <= 0 or cumulative <= 0:
            return ""
        rows.append(
            f"{quantity} 件：边际 {marginal:,}，累计 {cumulative:,} {currency.name}"
        )
    return "实机只读观察 · " + "  ·  ".join(rows)


def _run_shop_dry_run() -> dict:
    """Run the existing shop scanner without enabling purchase confirmation."""

    from auto.shop_purchase import run_shop_purchase

    result = run_shop_purchase(dry_run=True)
    if not isinstance(result, dict):
        raise TypeError("商店干跑返回了无效结果")
    return result


def _shop_dry_run_summary(result: dict) -> str:
    skipped = str(result.get("skipped") or "").strip()
    if skipped:
        return f"扫描完成：{skipped}"
    shops = result.get("shops")
    if not isinstance(shops, list):
        shops = []
    page_count = sum(
        int(shop.get("pages", 0) or 0)
        for shop in shops
        if isinstance(shop, dict)
    )
    found_count = sum(
        len(shop.get("results") or [])
        for shop in shops
        if isinstance(shop, dict) and isinstance(shop.get("results"), list)
    )
    missing_count = sum(
        len(shop.get("missing") or [])
        for shop in shops
        if isinstance(shop, dict) and isinstance(shop.get("missing"), list)
    )
    failed_count = sum(
        1
        for shop in shops
        if isinstance(shop, dict)
        for result in (shop.get("results") or [])
        if isinstance(result, dict) and result.get("status") == "failed"
    )
    parts = [f"干跑完成：扫描 {page_count} 页，处理 {found_count} 项"]
    if failed_count:
        parts.append(f"{failed_count} 项失败")
    if missing_count:
        parts.append(f"未定位 {missing_count} 项")
    if bool(result.get("requires_attention")):
        parts.append("需要人工复核")
    return "，".join(parts)


class ShopDryRunWorker(QThread):
    succeeded = Signal(dict)
    failed = Signal(str)

    def run(self):
        try:
            self.succeeded.emit(_run_shop_dry_run())
        except Exception as error:  # noqa: BLE001 - report worker failure to GUI
            self.failed.emit(f"{type(error).__name__}: {error}")


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
        self.quantityOptions = _quantity_options(item, currency)
        self.setObjectName("shopItemCard")
        self.setMinimumHeight(218 if item.price_tiers else 142)
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
        price_label = "固定单价" if not item.price_tiers else "首档边际"
        price = QLabel(f"{price_label} {item.price:,}  {currency.name}", self)
        price.setStyleSheet("color: #e6bd61; font-size: 13px; font-weight: 600;")
        price_row.addWidget(currency_icon)
        price_row.addWidget(price)
        price_row.addStretch(1)
        details.addWidget(name)
        details.addWidget(limit)
        details.addLayout(price_row)
        if item.price_tiers:
            breakdown = QLabel(_price_breakdown_text(item, currency), self)
            breakdown.setWordWrap(True)
            breakdown.setStyleSheet("color: #c7c7c7; font-size: 12px;")
            details.addWidget(breakdown)
            self.observedPriceLabel = QLabel(self)
            self.observedPriceLabel.setWordWrap(True)
            self.observedPriceLabel.setStyleSheet(
                "color: #7fd8a8; font-size: 12px; font-weight: 600;"
            )
            self.observedPriceLabel.hide()
            details.addWidget(self.observedPriceLabel)
        else:
            self.observedPriceLabel = None
        self.failureLabel = QLabel(self)
        self.failureLabel.setWordWrap(True)
        self.failureLabel.setStyleSheet(
            "color: #e05555; font-size: 12px; font-weight: 600;"
        )
        self.failureLabel.hide()
        details.addWidget(self.failureLabel)
        details.addStretch(1)
        root.addLayout(details, 1)

        controls = QVBoxLayout()
        controls.setSpacing(8)
        self.enabledCheck = CheckBox("自动购买", self)
        self.enabledCheck.setChecked(bool(rule.get("enabled", False)))
        self.quantityCombo = ComboBox(self)
        self.quantityCombo.setMinimumWidth(158)
        for _, label in self.quantityOptions:
            self.quantityCombo.addItem(label)
        selected_mode = str(rule.get("quantity", "max"))
        selected_index = next(
            (index for index, (key, _) in enumerate(self.quantityOptions) if key == selected_mode),
            0,
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
            "quantity": self.quantityOptions[index][0],
        }

    def setObservedPriceSchedule(self, observations: object) -> None:
        if self.observedPriceLabel is None:
            return
        text = _observed_price_breakdown_text(observations, self.currency)
        self.observedPriceLabel.setText(text)
        self.observedPriceLabel.setVisible(bool(text))
        if text:
            self.failureLabel.hide()

    def setFailureReason(self, reason: str) -> None:
        self.failureLabel.setText(f"干跑失败：{reason}")
        self.failureLabel.show()
        if self.observedPriceLabel is not None:
            self.observedPriceLabel.hide()


class ShopPlannerInterface(ScrollArea):
    """Configure recurring shop purchases without hard-coding future shops."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.catalog = load_shop_catalog()
        self.plan = load_shop_plan(catalog=self.catalog)
        self.currentShopId = self.catalog.shops[0].id
        self.itemCards: dict[str, ShopItemCard] = {}
        self.observedPriceSchedules: dict[str, list[dict]] = {}
        self.shopButtons: dict[str, QPushButton] = {}
        self.dryRunWorker: ShopDryRunWorker | None = None

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

        dry_run_row = QHBoxLayout()
        self.dryRunButton = PrimaryPushButton(
            FluentIcon.SEARCH,
            "仅扫描商店（不购买）",
            self.scrollWidget,
        )
        self.dryRunButton.setToolTip(
            "独立执行商店干跑：允许翻页并打开商品弹窗核验，但只点取消，不确认购买"
        )
        self.dryRunStatus = QLabel(
            "不会进入主任务队列，也不会绕过购买确认门禁",
            self.scrollWidget,
        )
        self.dryRunStatus.setWordWrap(True)
        self.dryRunButton.clicked.connect(self.startDryRun)
        dry_run_row.addWidget(self.dryRunButton)
        dry_run_row.addWidget(self.dryRunStatus, 1)
        self.rootLayout.addLayout(dry_run_row)

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
            card.setObservedPriceSchedule(
                self.observedPriceSchedules.get(item.id, [])
            )
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
                quantity_mode = str(rule["quantity"])
                if quantity_mode == "max" or (
                    quantity_mode == "one" and bool(item.price_tiers)
                ):
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
            estimates.append(f"当前可确定金额合计 {costs}")
        if dynamic_total_count:
            estimates.append(f"{dynamic_total_count} 项执行时按当前档位读取实时总价")
        estimate = f"；{'；'.join(estimates)}" if estimates else ""
        self.summaryLabel.setText(f"{state} · 已选 {len(selected)} 项{estimate}")

    def startDryRun(self):
        if self.dryRunWorker and self.dryRunWorker.isRunning():
            return
        self.observedPriceSchedules.clear()
        for card in self.itemCards.values():
            card.setObservedPriceSchedule([])
            card.failureLabel.hide()
        self.dryRunButton.setEnabled(False)
        self.dryRunButton.setText("正在扫描商店…")
        self.dryRunStatus.setText(
            "正在独立执行干跑；只允许翻页、打开商品弹窗并取消"
        )
        self.dryRunWorker = ShopDryRunWorker(self)
        self.dryRunWorker.succeeded.connect(self._dryRunSucceeded)
        self.dryRunWorker.failed.connect(self._dryRunFailed)
        self.dryRunWorker.finished.connect(self._dryRunFinished)
        self.dryRunWorker.start()

    def _dryRunSucceeded(self, result: dict):
        for shop_result in result.get("shops") or []:
            if not isinstance(shop_result, dict):
                continue
            for item_result in shop_result.get("results") or []:
                if not isinstance(item_result, dict):
                    continue
                card = self.itemCards.get(str(item_result.get("id") or ""))
                item_id = str(item_result.get("id") or "")
                status = str(item_result.get("status") or "")
                observations = item_result.get("price_observations")
                if isinstance(observations, list) and observations:
                    self.observedPriceSchedules[item_id] = observations
                if card is not None:
                    if status == "failed":
                        card.setFailureReason(
                            str(item_result.get("error", "未知错误"))
                        )
                    else:
                        card.setObservedPriceSchedule(observations)
        summary = _shop_dry_run_summary(result)
        self.dryRunStatus.setText(summary)
        info = InfoBar.warning if bool(result.get("requires_attention")) else InfoBar.success
        info(
            "商店干跑完成",
            summary,
            position=InfoBarPosition.TOP,
            duration=5000,
            parent=self,
        )

    def _dryRunFailed(self, message: str):
        self.dryRunStatus.setText("商店干跑失败；未进入购买确认")
        InfoBar.error(
            "商店干跑失败",
            message,
            position=InfoBarPosition.TOP,
            duration=5000,
            parent=self,
        )

    def _dryRunFinished(self):
        self.dryRunButton.setEnabled(True)
        self.dryRunButton.setText("仅扫描商店（不购买）")
        if self.dryRunWorker is not None:
            self.dryRunWorker.deleteLater()
        self.dryRunWorker = None

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
