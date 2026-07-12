from __future__ import annotations

from collections import Counter
from html import escape
import json
import math

from PySide6.QtCore import QSize, QThread, Qt, QTimer, Signal
from PySide6.QtWidgets import QSizePolicy
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QFrame, QGridLayout, QHeaderView, QHBoxLayout, QLabel, QScrollArea, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget
from qfluentwidgets import FluentIcon, InfoBar, InfoBarPosition, PrimaryPushButton, ScrollArea, SpinBox

from app.common.style_sheet import StyleSheet
from app.utils.constants import ROOT_PATH
from auto.inventory import scan_inventory_assets
from core.services.currency_planner import ActivityYield, CURRENCIES, calculate_acquisition, currency_for_asset
from core.services.weekly_plan_state import load_weekly_plan


class InventoryScanWorker(QThread):
    succeeded = Signal(list)
    failed = Signal(str)

    def run(self):
        try:
            self.succeeded.emit(scan_inventory_assets())
        except Exception as exc:
            self.failed.emit(str(exc))


def _exchange_id(item) -> str:
    costs = "+".join(f"{key}:{value}" for key, value in sorted(item.costs.items()))
    return f"{item.name}|{item.limit}|{costs}"


class InventoryInterface(ScrollArea):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.worker = None
        self.current_amounts = {currency.key: 0 for currency in CURRENCIES}
        self.plan_path = ROOT_PATH / "config" / "currency_action_plan.json"
        saved = self._loadPlan()
        self.exchange_quantities = saved.get("exchange", {})
        self.activity_times = saved.get("activities", {})
        self.current_detail_row = 0
        self._building_detail = False
        self.exchange_cards = {}
        self.shop_cards = []

        self.setObjectName("InventoryInterface")
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setViewportMargins(0, 80, 0, 20)
        self.scrollWidget = QWidget(self)
        self.setWidget(self.scrollWidget)
        self.setWidgetResizable(True)
        StyleSheet.VIEW_INTERFACE.apply(self)
        title = QLabel("游戏货币规划器", self)
        title.setObjectName("titleLabel")
        title.move(36, 30)
        layout = QVBoxLayout(self.scrollWidget)
        layout.setContentsMargins(36, 8, 36, 24)
        layout.setSpacing(14)
        intro = QLabel("选择想兑换的物品和数量，程序自动计算货币需求；填写计划参加的活动次数，自动计算预计产出与缺口。", self.scrollWidget)
        intro.setWordWrap(True)
        layout.addWidget(intro)

        controls = QHBoxLayout()
        self.scanButton = PrimaryPushButton(FluentIcon.SEARCH, "扫描货币与背包", self.scrollWidget)
        self.scanButton.clicked.connect(self.startScan)
        self.summary = QLabel("尚未扫描；兑换与活动计划会自动保存", self.scrollWidget)
        controls.addWidget(self.scanButton)
        controls.addWidget(self.summary, 1)
        layout.addLayout(controls)

        self.currencyTable = QTableWidget(len(CURRENCIES), 5, self.scrollWidget)
        self.currencyTable.setHorizontalHeaderLabels(["货币", "当前持有", "兑换需要", "活动期望获取", "规划结果"])
        self.currencyTable.verticalHeader().hide()
        self.currencyTable.verticalHeader().setDefaultSectionSize(38)
        self.currencyTable.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.currencyTable.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.currencyTable.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.currencyTable.currentCellChanged.connect(lambda row, *_: self._showCurrency(row))
        header = self.currencyTable.horizontalHeader()
        for column in range(4):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        self.currencyTable.setMinimumHeight(260)
        self.currencyTable.setIconSize(QSize(28, 28))
        for row, currency in enumerate(CURRENCIES):
            name_item = QTableWidgetItem(self._itemIcon(currency.name), currency.name)
            self.currencyTable.setItem(row, 0, name_item)
            self.currencyTable.setItem(row, 1, QTableWidgetItem("未识别"))
            for column in range(2, 5):
                self.currencyTable.setItem(row, column, QTableWidgetItem("0"))
        layout.addWidget(self.currencyTable)

        self.detailTitle = QLabel(self.scrollWidget)
        self.detailTitle.setStyleSheet("font-size: 20px; font-weight: 600; margin-top: 6px;")
        self.detailDescription = QLabel(self.scrollWidget)
        self.detailDescription.setWordWrap(True)
        layout.addWidget(self.detailTitle)
        layout.addWidget(self.detailDescription)

        exchange_title = QLabel("① 像逛商店一样选择兑换物", self.scrollWidget)
        exchange_title.setStyleSheet("font-size: 18px; font-weight: 600;")
        layout.addWidget(exchange_title)
        shop_workspace = QHBoxLayout()
        shop_workspace.setSpacing(14)
        self.shopScroll = QScrollArea(self.scrollWidget)
        self.shopScroll.setWidgetResizable(True)
        self.shopScroll.setFrameShape(QFrame.Shape.NoFrame)
        self.shopScroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.shopScroll.setMinimumHeight(390)
        self.shopWidget = QWidget(self.shopScroll)
        self.shopGrid = QGridLayout(self.shopWidget)
        self.shopGrid.setContentsMargins(0, 0, 6, 0)
        self.shopGrid.setHorizontalSpacing(10)
        self.shopGrid.setVerticalSpacing(10)
        self.shopGrid.setColumnStretch(0, 1)
        self.shopGrid.setColumnStretch(1, 1)
        self.shopScroll.setWidget(self.shopWidget)
        shop_workspace.addWidget(self.shopScroll, 1)

        self.cartPanel = QFrame(self.scrollWidget)
        self.cartPanel.setObjectName("cartPanel")
        self.cartPanel.setMinimumWidth(330)
        self.cartPanel.setMaximumWidth(370)
        self.cartPanel.setStyleSheet(
            "QFrame#cartPanel { background: rgba(53,215,232,0.08); "
            "border: 1px solid rgba(53,215,232,0.22); border-radius: 10px; }"
        )
        cart_layout = QVBoxLayout(self.cartPanel)
        cart_layout.setContentsMargins(16, 14, 16, 14)
        cart_title = QLabel("本周兑换清单", self.cartPanel)
        cart_title.setStyleSheet("font-size: 18px; font-weight: 600;")
        self.cartList = QLabel("尚未选择兑换物", self.cartPanel)
        self.cartList.setWordWrap(True)
        self.cartList.setTextFormat(Qt.TextFormat.RichText)
        self.cartTotals = QLabel(self.cartPanel)
        self.cartTotals.setWordWrap(True)
        self.cartTotals.setStyleSheet("font-size: 16px; font-weight: 600; color: #35d7e8;")
        cart_layout.addWidget(cart_title)
        cart_layout.addWidget(self.cartList, 1)
        cart_layout.addWidget(self.cartTotals)
        shop_workspace.addWidget(self.cartPanel)
        layout.addLayout(shop_workspace)

        activity_title = QLabel("② 选择准备参加的活动 / 副本", self.scrollWidget)
        activity_title.setStyleSheet("font-size: 18px; font-weight: 600;")
        layout.addWidget(activity_title)
        self.activityTable = QTableWidget(0, 5, self.scrollWidget)
        self.activityTable.setHorizontalHeaderLabels(["活动 / 副本", "每次掉落", "计划次数", "可能获得", "期望获得"])
        self.activityTable.verticalHeader().hide()
        self.activityTable.verticalHeader().setDefaultSectionSize(40)
        self.activityTable.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.activityTable.setWordWrap(True)
        self.activityTable.setMinimumHeight(245)
        self.activityTable.setIconSize(QSize(30, 30))
        activity_header = self.activityTable.horizontalHeader()
        activity_header.setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        activity_header.resizeSection(0, 380)
        for column in range(1, 4):
            activity_header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        activity_header.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.activityTable)

        self.advice = QLabel(self.scrollWidget)
        self.advice.setWordWrap(True)
        self.advice.setStyleSheet("font-size: 16px; padding: 14px; background: rgba(53,215,232,0.10); border-radius: 8px;")
        layout.addWidget(self.advice)
        self.sourceLabel = QLabel(self.scrollWidget)
        self.sourceLabel.setWordWrap(True)
        self.sourceLabel.setTextFormat(Qt.TextFormat.RichText)
        self.sourceLabel.setOpenExternalLinks(True)
        layout.addWidget(self.sourceLabel)

        inventory_title = QLabel("背包物品明细", self.scrollWidget)
        inventory_title.setStyleSheet("font-size: 19px; font-weight: 600; margin-top: 8px;")
        layout.addWidget(inventory_title)
        self.inventoryTable = QTableWidget(0, 3, self.scrollWidget)
        self.inventoryTable.setHorizontalHeaderLabels(["分类", "物品 / 货币", "数量"])
        self.inventoryTable.verticalHeader().hide()
        self.inventoryTable.verticalHeader().setDefaultSectionSize(38)
        self.inventoryTable.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.inventoryTable.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        inventory_header = self.inventoryTable.horizontalHeader()
        inventory_header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        inventory_header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        inventory_header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.inventoryTable.setMinimumHeight(300)
        self.inventoryTable.setIconSize(QSize(28, 28))
        layout.addWidget(self.inventoryTable)

        self._recalculate()
        self.currencyTable.selectRow(3)
        self._showCurrency(3)

    def _showCurrency(self, row):
        if not 0 <= row < len(CURRENCIES):
            return
        self._building_detail = True
        self.current_detail_row = row
        currency = CURRENCIES[row]
        self.detailTitle.setText(f"{currency.name} · {currency.kind}")
        self.detailDescription.setText(currency.description)
        exchanges = []
        seen = set()
        for item in currency.exchanges:
            item_id = _exchange_id(item)
            if currency.key in item.costs and item_id not in seen:
                exchanges.append(item)
                seen.add(item_id)
        self.visible_exchanges = exchanges
        self._clearShopGrid()
        self.exchange_cards = {}
        for card_index, item in enumerate(exchanges):
            card = self._createExchangeCard(item)
            self.shop_cards.append(card)
        self._reflowShopCards()
        activities = self._activitiesForCurrency(currency)
        self.visible_activities = activities
        self.activityTable.clearSpans()
        self.activityTable.setRowCount(len(activities))
        for table_row, activity in enumerate(activities):
            low, high = activity.rewards[currency.key]
            self.activityTable.setItem(table_row, 0, QTableWidgetItem(self._itemIcon(currency.name), activity.name))
            self.activityTable.setItem(table_row, 1, QTableWidgetItem(f"{low}" if low == high else f"{low}～{high}"))
            times = SpinBox(self.activityTable)
            times.setRange(0, 1 if activity.name == "已套用的本周跑商计划" else 999)
            times.setValue(int(self.activity_times.get(activity.name, 0)))
            times.valueChanged.connect(lambda value, name=activity.name: self._activityChanged(name, value))
            self.activityTable.setCellWidget(table_row, 2, times)
            result = calculate_acquisition(low, high, times.value(), 100)
            self.activityTable.setItem(table_row, 3, QTableWidgetItem(f"{result['possible_min']:,}～{result['possible_max']:,}"))
            self.activityTable.setItem(table_row, 4, QTableWidgetItem(f"{result['expected']:,.1f}"))
        if not exchanges:
            empty = QLabel("暂无经过核实、可计算价格的结构化兑换表", self.shopWidget)
            empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.shopGrid.addWidget(empty, 0, 0, 1, 2)
        if not activities:
            self.activityTable.setRowCount(1)
            self.activityTable.setItem(0, 0, QTableWidgetItem("来源存在，但公开资料没有稳定的固定产出数字，暂不计算虚假期望"))
            self.activityTable.setSpan(0, 0, 1, 5)
        sources = "<br>".join(f"• {escape(item)}" for item in currency.sources)
        self.sourceLabel.setText(f'<b>其他获取渠道：</b><br>{sources}<br><a href="{escape(currency.source_url)}">核对资料来源</a>')
        self._building_detail = False
        self._recalculate()

    def _exchangeChanged(self, key, value):
        self.exchange_quantities[key] = value
        self._planChanged()

    def _activityChanged(self, name, value):
        self.activity_times[name] = value
        self._planChanged()

    def _planChanged(self):
        if self._building_detail:
            return
        self._savePlan()
        currency = CURRENCIES[self.current_detail_row]
        for item in self.visible_exchanges:
            item_id = _exchange_id(item)
            quantity = int(self.exchange_quantities.get(item_id, 0))
            if item_id in self.exchange_cards:
                self.exchange_cards[item_id]["subtotal"].setText(self._costText(item, quantity))
        for row, activity in enumerate(self.visible_activities):
            low, high = activity.rewards[currency.key]
            times = int(self.activity_times.get(activity.name, 0))
            result = calculate_acquisition(low, high, times, 100)
            self.activityTable.item(row, 3).setText(f"{result['possible_min']:,}～{result['possible_max']:,}")
            self.activityTable.item(row, 4).setText(f"{result['expected']:,.1f}")
        self._recalculate()

    def _recalculate(self):
        required = {currency.key: 0 for currency in CURRENCIES}
        expected = {currency.key: 0.0 for currency in CURRENCIES}
        unique_exchanges = {_exchange_id(item): item for currency in CURRENCIES for item in currency.exchanges}
        for item_id, quantity in self.exchange_quantities.items():
            item = unique_exchanges.get(item_id)
            if not item:
                continue
            for key, price in item.costs.items():
                required[key] = required.get(key, 0) + price * int(quantity)
        unique_activities = {
            item.name: item
            for currency in CURRENCIES
            for item in self._activitiesForCurrency(currency)
        }
        for name, times in self.activity_times.items():
            activity = unique_activities.get(name)
            if not activity:
                continue
            for key, (low, high) in activity.rewards.items():
                expected[key] = expected.get(key, 0) + calculate_acquisition(low, high, times, 100)["expected"]
        for row, currency in enumerate(CURRENCIES):
            need = required[currency.key]
            gain = expected[currency.key]
            balance = self.current_amounts[currency.key] + gain - need
            self.currencyTable.item(row, 2).setText(f"{need:,}")
            self.currencyTable.item(row, 3).setText(f"{gain:,.1f}")
            self.currencyTable.item(row, 4).setText(f"预计剩余 {balance:,.1f}" if balance >= 0 else f"预计还缺 {-balance:,.1f}")
        currency = CURRENCIES[self.current_detail_row]
        need = required[currency.key]
        gain = expected[currency.key]
        deficit = need - self.current_amounts[currency.key] - gain
        if need <= 0:
            message = "先在上方选择想兑换的物品和数量，系统会自动生成获取建议。标 ★ 的是攻略推荐兑换项。"
        elif deficit <= 0:
            message = f"所选兑换共需 {need:,} {currency.name}；按当前库存与活动计划，预计可以完成，兑换后约剩 {-deficit:,.1f}。"
        else:
            best = self._bestActivity(currency.key)
            if best:
                activity, per_run = best
                runs = math.ceil(deficit / per_run)
                message = f"所选兑换共需 {need:,} {currency.name}，预计还缺 {deficit:,.1f}。建议再完成“{activity.name}”约 {runs} 次（按每次期望 {per_run:,.1f} 计算）。"
            else:
                message = f"所选兑换共需 {need:,} {currency.name}，预计还缺 {deficit:,.1f}；公开资料没有稳定单次产出，需按游戏内实际奖励补录。"
        self.advice.setText(message)
        selected_lines = []
        totals = []
        for item_id, quantity in self.exchange_quantities.items():
            if not int(quantity):
                continue
            item = unique_exchanges.get(item_id)
            if not item:
                continue
            selected_lines.append(f"• {escape(item.name)} × {int(quantity)}<br><span style='color:#999'>{escape(self._costText(item, int(quantity)))}</span>")
        for item in CURRENCIES:
            if required[item.key]:
                totals.append(f"{required[item.key]:,} {item.name}")
        self.cartList.setText("<br><br>".join(selected_lines) if selected_lines else "尚未选择兑换物")
        self.cartTotals.setText("合计：" + " + ".join(totals) if totals else "合计：0")

    def _bestActivity(self, currency_key):
        choices = []
        for currency in CURRENCIES:
            for activity in self._activitiesForCurrency(currency):
                if currency_key in activity.rewards:
                    low, high = activity.rewards[currency_key]
                    choices.append(((low + high) / 2, activity))
        if not choices:
            return None
        amount, activity = max(choices, key=lambda item: item[0])
        return activity, amount

    @staticmethod
    def _activitiesForCurrency(currency):
        activities = [item for item in currency.activities if currency.key in item.rewards]
        if currency.key != "tie_meng_bi":
            return activities
        weekly_plan = load_weekly_plan()
        if not weekly_plan:
            return activities
        expected_profit = max(0, int(weekly_plan.get("expected_profit", 0)))
        if not expected_profit:
            return activities
        cycle = " → ".join(weekly_plan.get("cycle", []))
        activities.insert(0, ActivityYield(
            "已套用的本周跑商计划",
            {"tie_meng_bi": (expected_profit, expected_profit)},
            f"{cycle}；来自跑商页当前周计划",
        ))
        return activities

    @staticmethod
    def _limitCount(limit):
        digits = "".join(character for character in limit if character.isdigit())
        return int(digits) if digits else 99

    @staticmethod
    def _currencyName(key):
        return next((currency.name for currency in CURRENCIES if currency.key == key), key)

    def _costText(self, item, quantity):
        return " + ".join(f"{amount * quantity:,} {self._currencyName(key)}" for key, amount in item.costs.items())

    def _clearShopGrid(self):
        self.shop_cards = []
        while self.shopGrid.count():
            layout_item = self.shopGrid.takeAt(0)
            widget = layout_item.widget()
            if widget:
                widget.deleteLater()

    def _reflowShopCards(self):
        if not hasattr(self, "shopGrid") or not self.shop_cards:
            return
        for card in self.shop_cards:
            self.shopGrid.removeWidget(card)
        # Two cards need enough room for icon, text and quantity controls.
        columns = self._shopColumnCount(self.shopScroll.viewport().width())
        for index, card in enumerate(self.shop_cards):
            self.shopGrid.addWidget(card, index // columns, index % columns)
        self.shopGrid.setColumnStretch(0, 1)
        self.shopGrid.setColumnStretch(1, 1 if columns == 2 else 0)

    @staticmethod
    def _shopColumnCount(viewport_width):
        return 2 if viewport_width >= 980 else 1

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "shopScroll"):
            QTimer.singleShot(0, self._reflowShopCards)

    def _createExchangeCard(self, item):
        item_id = _exchange_id(item)
        card = QFrame(self.shopWidget)
        card.setObjectName("exchangeCard")
        card.setMinimumHeight(112)
        card.setMinimumWidth(0)
        card.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        card.setStyleSheet(
            "QFrame#exchangeCard { background: rgba(255,255,255,0.055); "
            "border: 1px solid rgba(255,255,255,0.09); border-radius: 9px; }"
            "QFrame#exchangeCard:hover { border-color: rgba(53,215,232,0.55); "
            "background: rgba(53,215,232,0.06); }"
        )
        root = QHBoxLayout(card)
        root.setContentsMargins(12, 10, 12, 10)
        root.setSpacing(12)
        icon = QLabel(card)
        icon.setFixedSize(58, 58)
        icon.setPixmap(self._itemIcon(item.name).pixmap(54, 54))
        icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        root.addWidget(icon)
        details = QVBoxLayout()
        name = QLabel(("★ " if item.recommended else "") + item.name, card)
        name.setStyleSheet("font-size: 15px; font-weight: 600;")
        name.setWordWrap(True)
        name.setMinimumWidth(0)
        name.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        price = QLabel(" + ".join(f"{amount:,} {self._currencyName(key)}" for key, amount in item.costs.items()), card)
        price.setStyleSheet("color: #35d7e8;")
        price.setWordWrap(True)
        price.setMinimumWidth(0)
        price.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        limit = QLabel(item.limit, card)
        limit.setStyleSheet("color: #999;")
        details.addWidget(name)
        details.addWidget(price)
        details.addWidget(limit)
        root.addLayout(details, 1)
        controls = QVBoxLayout()
        quantity = SpinBox(card)
        quantity.setRange(0, self._limitCount(item.limit))
        quantity.setFixedWidth(112)
        quantity.setValue(int(self.exchange_quantities.get(item_id, 0)))
        quantity.valueChanged.connect(lambda value, key=item_id: self._exchangeChanged(key, value))
        subtotal = QLabel(self._costText(item, quantity.value()), card)
        subtotal.setAlignment(Qt.AlignmentFlag.AlignRight)
        subtotal.setStyleSheet("color: #aaa; font-size: 12px;")
        subtotal.setWordWrap(True)
        subtotal.setMaximumWidth(130)
        controls.addWidget(quantity)
        controls.addWidget(subtotal)
        root.addLayout(controls)
        self.exchange_cards[item_id] = {"quantity": quantity, "subtotal": subtotal}
        return card

    @staticmethod
    def _itemIcon(name):
        icon_path = ROOT_PATH / "resources" / "currency" / f"{name}.png"
        if icon_path.exists():
            return QIcon(str(icon_path))
        # Unknown/new backpack entries still receive a consistent visual placeholder.
        pixmap = QPixmap(64, 64)
        pixmap.fill(Qt.GlobalColor.transparent)
        hue = sum(ord(character) for character in name) % 360
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setBrush(QColor.fromHsv(hue, 105, 205))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawRoundedRect(3, 3, 58, 58, 14, 14)
        painter.setPen(QColor("white"))
        font = painter.font()
        font.setBold(True)
        font.setPixelSize(27)
        painter.setFont(font)
        painter.drawText(pixmap.rect(), Qt.AlignmentFlag.AlignCenter, name[:1] or "?")
        painter.end()
        return QIcon(pixmap)

    def startScan(self):
        if self.worker and self.worker.isRunning():
            return
        self.scanButton.setEnabled(False)
        self.scanButton.setText("正在扫描…")
        self.summary.setText("正在连接模拟器并翻阅背包")
        self.worker = InventoryScanWorker(self)
        self.worker.succeeded.connect(self._showAssets)
        self.worker.failed.connect(self._showError)
        self.worker.finished.connect(self._scanFinished)
        self.worker.start()

    def _showAssets(self, assets):
        self.current_amounts = {currency.key: 0 for currency in CURRENCIES}
        for asset in assets:
            currency = currency_for_asset(asset.name)
            if currency:
                self.current_amounts[currency.key] = max(self.current_amounts[currency.key], asset.count)
        for row, currency in enumerate(CURRENCIES):
            amount = self.current_amounts[currency.key]
            self.currencyTable.item(row, 1).setText(f"{amount:,}" if amount else "未识别")
        self.inventoryTable.setRowCount(len(assets))
        category_counts = Counter(asset.category for asset in assets)
        for row, asset in enumerate(assets):
            self.inventoryTable.setItem(row, 0, QTableWidgetItem(asset.category))
            self.inventoryTable.setItem(row, 1, QTableWidgetItem(self._itemIcon(asset.name), asset.name))
            self.inventoryTable.setItem(row, 2, QTableWidgetItem(f"{asset.count:,}"))
        details = "，".join(f"{name} {count} 类" for name, count in category_counts.items())
        self.summary.setText(f"共识别 {len(assets)} 类：{details}" if assets else "未识别到带数量的物品")
        self._recalculate()

    def _showError(self, message):
        self.summary.setText("扫描失败")
        InfoBar.error("资产扫描失败", message, position=InfoBarPosition.TOP, duration=5000, parent=self)

    def _scanFinished(self):
        self.scanButton.setEnabled(True)
        self.scanButton.setText("重新扫描")
        self.worker.deleteLater()
        self.worker = None

    def _loadPlan(self):
        try:
            return json.loads(self.plan_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return {}

    def _savePlan(self):
        try:
            self.plan_path.parent.mkdir(parents=True, exist_ok=True)
            data = {"exchange": self.exchange_quantities, "activities": self.activity_times}
            self.plan_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            pass
