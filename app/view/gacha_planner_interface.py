from __future__ import annotations

import json

from PySide6.QtCore import QThread, Qt, Signal
from PySide6.QtWidgets import QAbstractSpinBox, QFrame, QGridLayout, QHeaderView, QHBoxLayout, QLabel, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget
from qfluentwidgets import CheckBox, FluentIcon, InfoBar, InfoBarPosition, PrimaryPushButton, ScrollArea, SpinBox

from app.common.style_sheet import StyleSheet
from app.utils.constants import ROOT_PATH
from auto.gacha_resources import scan_gacha_resources
from core.services.gacha_planner import GACHA_SOURCES, GACHA_SOURCE_CATALOG_VERSION, PAID_GACHA_PACKS, STONE_PER_PULL, calculate_gacha_plan, expected_source_total, pulls_to_guarantee


class GachaScanWorker(QThread):
    succeeded = Signal(list)
    failed = Signal(str)

    def run(self):
        try:
            self.succeeded.emit([scan_gacha_resources()])
        except Exception as exc:
            self.failed.emit(str(exc))


class GachaPlannerInterface(ScrollArea):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.plan_path = ROOT_PATH / "config" / "gacha_plan.json"
        self.saved = self._load()
        self.worker = None
        self.source_inputs = {}
        self.paid_checks = {}
        self.setObjectName("GachaPlannerInterface")
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setViewportMargins(0, 80, 0, 20)
        self.scrollWidget = QWidget(self)
        self.setWidget(self.scrollWidget)
        self.setWidgetResizable(True)
        StyleSheet.VIEW_INTERFACE.apply(self)
        title = QLabel("抽卡资源规划", self)
        title.setObjectName("titleLabel")
        title.move(36, 30)
        root = QVBoxLayout(self.scrollWidget)
        root.setContentsMargins(36, 8, 36, 24)
        root.setSpacing(14)
        intro = QLabel("统计拉普拉斯协议与桦石，选择目标抽数或保底进度，自动生成资源消耗、缺口和获取清单。", self.scrollWidget)
        intro.setWordWrap(True)
        root.addWidget(intro)

        scan_row = QHBoxLayout()
        self.scanButton = PrimaryPushButton(FluentIcon.SEARCH, "从游戏读取资源", self.scrollWidget)
        self.scanButton.clicked.connect(self.startScan)
        self.scanStatus = QLabel("也可直接在下方填写截图中的数量", self.scrollWidget)
        scan_row.addWidget(self.scanButton)
        scan_row.addWidget(self.scanStatus, 1)
        root.addLayout(scan_row)

        stock = QFrame(self.scrollWidget)
        stock.setStyleSheet("QFrame { background: rgba(53,215,232,0.07); border: 1px solid rgba(53,215,232,0.20); border-radius: 10px; }")
        grid = QGridLayout(stock)
        grid.setContentsMargins(18, 14, 18, 14)
        self.tickets = self._spin(0, 99999, self.saved.get("tickets", 0))
        self.stones = self._spin(0, 99999999, self.saved.get("stones", 0))
        self.target = self._spin(0, 9999, self.saved.get("target", 80))
        self.pity = self._spin(0, 999, self.saved.get("pity", 0))
        self.guarantee = self._spin(1, 999, self.saved.get("guarantee", 80))
        fields = (("拉普拉斯协议", self.tickets), ("桦石", self.stones), ("计划抽数", self.target), ("当前保底计数", self.pity), ("保底所需总抽数", self.guarantee))
        for index, (name, widget) in enumerate(fields):
            grid.addWidget(QLabel(name, stock), 0, index)
            grid.addWidget(widget, 1, index)
            widget.valueChanged.connect(self._changed)
        root.addWidget(stock)

        summary_grid = QGridLayout()
        self.summaryCards = []
        for index, label in enumerate(("当前可抽", "计划期可抽", "距目标缺口", "预计消耗")):
            card = QFrame(self.scrollWidget)
            card.setStyleSheet("QFrame { background: rgba(255,255,255,0.05); border-radius: 9px; } QLabel { border: none; }")
            box = QVBoxLayout(card)
            caption = QLabel(label, card)
            caption.setStyleSheet("color:#999;")
            value = QLabel("0", card)
            value.setStyleSheet("font-size:22px; font-weight:600;")
            box.addWidget(caption)
            box.addWidget(value)
            summary_grid.addWidget(card, 0, index)
            self.summaryCards.append(value)
        root.addLayout(summary_grid)

        source_title = QLabel("本计划期准备获取的资源", self.scrollWidget)
        source_title.setStyleSheet("font-size:18px; font-weight:600;")
        root.addWidget(source_title)
        self.sourceTable = QTableWidget(len(GACHA_SOURCES), 6, self.scrollWidget)
        self.sourceTable.setHorizontalHeaderLabels(["获取途径", "周期 / 上限", "协议", "桦石", "计入计划", "预计合计"])
        self.sourceTable.verticalHeader().hide()
        self.sourceTable.verticalHeader().setDefaultSectionSize(52)
        self.sourceTable.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.sourceTable.setMinimumHeight(465)
        self.sourceTable.setWordWrap(False)
        self.sourceTable.setStyleSheet(
            "QTableWidget { gridline-color: rgba(255,255,255,0.08); }"
            "QTableWidget::item { padding-left: 10px; padding-right: 8px; }"
            "QScrollBar:horizontal { height: 12px; background: rgba(255,255,255,0.06); margin: 1px; }"
            "QScrollBar::handle:horizontal { min-width: 80px; background: rgba(53,215,232,0.55); border-radius: 5px; }"
        )
        header = self.sourceTable.horizontalHeader()
        header.setMinimumSectionSize(90)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        self.sourceTable.setColumnWidth(0, 280)
        self.sourceTable.setColumnWidth(1, 120)
        self.sourceTable.setColumnWidth(2, 140)
        self.sourceTable.setColumnWidth(3, 140)
        self.sourceTable.setColumnWidth(4, 140)
        # Catalog-owned fixed rewards always use the current evidence-backed
        # constants below. User-entered variable sources and opt-in choices are
        # safe to carry forward when the catalog itself is refreshed.
        source_saved = self.saved.get("sources", {})
        if not isinstance(source_saved, dict):
            source_saved = {}
        for row, source in enumerate(GACHA_SOURCES):
            self.sourceTable.setItem(row, 0, QTableWidgetItem(source.name))
            self.sourceTable.setItem(row, 1, QTableWidgetItem(source.cycle))
            values = source_saved.get(source.key, {})
            fixed_reward = bool(source.default_tickets or source.default_stones)
            if fixed_reward:
                ticket = source.default_tickets
                stone = source.default_stones
                self.sourceTable.setItem(row, 2, QTableWidgetItem(f"{ticket:,}"))
                self.sourceTable.setItem(row, 3, QTableWidgetItem(f"{stone:,}"))
            else:
                ticket = self._tableSpin(0, 99999, values.get("tickets", 0), "本期可获得的拉普拉斯协议")
                stone = self._tableSpin(0, 9999999, values.get("stones", 0), "本期可获得的桦石")
                for column, widget in ((2, ticket), (3, stone)):
                    self.sourceTable.setCellWidget(row, column, widget)
                    widget.valueChanged.connect(self._changed)
            if source.max_times == 1:
                times = CheckBox("计入", self.sourceTable)
                times.setChecked(bool(values.get("times", source.default_times)))
                times.stateChanged.connect(self._changed)
            else:
                times = self._tableSpin(0, source.max_times, values.get("times", source.default_times), "本计划期预计完成次数")
                times.valueChanged.connect(self._changed)
            for column, widget in ((4, times),):
                self.sourceTable.setCellWidget(row, column, widget)
            self.sourceTable.setItem(row, 5, QTableWidgetItem("0"))
            self.source_inputs[source.key] = (ticket, stone, times)
            self.sourceTable.item(row, 0).setToolTip(source.note)
        root.addWidget(self.sourceTable)

        paid_title = QLabel("可选付费来源（默认不计入）", self.scrollWidget)
        paid_title.setStyleSheet("font-size:18px; font-weight:600; margin-top:8px;")
        root.addWidget(paid_title)
        self.paidTable = QTableWidget(len(PAID_GACHA_PACKS), 6, self.scrollWidget)
        self.paidTable.setHorizontalHeaderLabels(["选择", "礼包", "周期 / 上限", "内容", "价格", "计入结果"])
        self.paidTable.verticalHeader().hide()
        self.paidTable.verticalHeader().setDefaultSectionSize(48)
        self.paidTable.setMinimumHeight(390)
        paid_header = self.paidTable.horizontalHeader()
        for column, width in ((0, 70), (1, 240), (2, 130), (4, 80)):
            paid_header.setSectionResizeMode(column, QHeaderView.ResizeMode.Fixed)
            self.paidTable.setColumnWidth(column, width)
        paid_header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        paid_header.setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        paid_saved = self.saved.get("paid", {})
        if not isinstance(paid_saved, dict):
            paid_saved = {}
        restored_exclusive_groups = set()
        for row, pack in enumerate(PAID_GACHA_PACKS):
            check = CheckBox("", self.paidTable)
            checked = bool(paid_saved.get(pack.key, False))
            if checked and pack.exclusive_group:
                if pack.exclusive_group in restored_exclusive_groups:
                    checked = False
                else:
                    restored_exclusive_groups.add(pack.exclusive_group)
            check.setChecked(checked)
            self.paidTable.setCellWidget(row, 0, check)
            self.paidTable.setItem(row, 1, QTableWidgetItem(pack.name))
            self.paidTable.setItem(row, 2, QTableWidgetItem(pack.cycle))
            content = []
            if pack.tickets:
                content.append(f"协议×{pack.tickets}")
            if pack.stones:
                content.append(f"桦石×{pack.stones:,}")
            if pack.special_pulls:
                content.append(f"限定秘钥×{pack.special_pulls}")
            self.paidTable.setItem(row, 3, QTableWidgetItem(" + ".join(content)))
            self.paidTable.item(row, 3).setToolTip(pack.note)
            self.paidTable.setItem(row, 4, QTableWidgetItem(f"¥{pack.price_yuan}"))
            self.paidTable.setItem(row, 5, QTableWidgetItem("未选择"))
            self.paid_checks[pack.key] = check
            check.stateChanged.connect(lambda state, key=pack.key: self._paidChanged(key, state))
        root.addWidget(self.paidTable)

        self.action = QLabel(self.scrollWidget)
        self.action.setWordWrap(True)
        self.action.setTextFormat(Qt.TextFormat.RichText)
        self.action.setStyleSheet("font-size:16px; padding:16px; background:rgba(53,215,232,0.10); border-radius:9px;")
        root.addWidget(self.action)
        note = QLabel("换算按 160 桦石/抽；活动、签到和补偿会随版本变化，表格默认不填虚构数量，请按当期游戏奖励录入。保底阈值可按当前卡池说明修改。", self.scrollWidget)
        note.setWordWrap(True)
        note.setStyleSheet("color:#999;")
        root.addWidget(note)
        if self.saved.get("sourceCatalogVersion") != GACHA_SOURCE_CATALOG_VERSION:
            self._save()
        self._recalculate()

    def startScan(self):
        if self.worker and self.worker.isRunning():
            return
        self.scanButton.setEnabled(False)
        self.scanButton.setText("正在扫描…")
        self.scanStatus.setText("正在读取主界面与背包")
        self.worker = GachaScanWorker(self)
        self.worker.succeeded.connect(self._scanSucceeded)
        self.worker.failed.connect(self._scanFailed)
        self.worker.finished.connect(self._scanFinished)
        self.worker.start()

    def _scanSucceeded(self, results):
        values = results[0] if results else {}
        ticket = values.get("tickets")
        stone = values.get("stones")
        if ticket is not None:
            self.tickets.setValue(ticket)
        if stone is not None:
            self.stones.setValue(stone)
        names = []
        if ticket is not None:
            names.append(f"拉普拉斯协议 {ticket}")
        if stone is not None:
            names.append(f"桦石 {stone:,}")
        self.scanStatus.setText("已读取：" + "，".join(names) if names else "扫描完成，但未识别到抽卡资源；可手动填写")

    def _scanFailed(self, message):
        self.scanStatus.setText("扫描失败，可继续手动填写")
        InfoBar.error("抽卡资源扫描失败", message, position=InfoBarPosition.TOP, duration=5000, parent=self)

    def _scanFinished(self):
        self.scanButton.setEnabled(True)
        self.scanButton.setText("重新读取")
        self.worker.deleteLater()
        self.worker = None

    @staticmethod
    def _spin(minimum, maximum, value):
        widget = SpinBox()
        widget.setRange(minimum, maximum)
        widget.setValue(int(value))
        return widget

    @staticmethod
    def _tableSpin(minimum, maximum, value, tooltip):
        widget = SpinBox()
        widget.setRange(minimum, maximum)
        widget.setValue(int(value))
        widget.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        widget.setAlignment(Qt.AlignmentFlag.AlignCenter)
        widget.setToolTip(tooltip + "；点击后可直接输入数字")
        widget.setMinimumWidth(118)
        widget.setFixedHeight(38)
        return widget

    def _changed(self, *_):
        self._save()
        self._recalculate()

    def _paidChanged(self, key, state):
        if state:
            selected_pack = next(pack for pack in PAID_GACHA_PACKS if pack.key == key)
            if selected_pack.exclusive_group:
                for pack in PAID_GACHA_PACKS:
                    if pack.key != key and pack.exclusive_group == selected_pack.exclusive_group:
                        other = self.paid_checks.get(pack.key)
                        if other and other.isChecked():
                            other.blockSignals(True)
                            other.setChecked(False)
                            other.blockSignals(False)
        self._changed()

    def _source_totals(self):
        tickets = stones = 0
        for row, source in enumerate(GACHA_SOURCES):
            ticket, stone, times = self.source_inputs[source.key]
            ticket_value = ticket if isinstance(ticket, int) else ticket.value()
            stone_value = stone if isinstance(stone, int) else stone.value()
            times_value = int(times.isChecked()) if isinstance(times, CheckBox) else times.value()
            ticket_total = expected_source_total(ticket_value, times_value)
            stone_total = expected_source_total(stone_value, times_value)
            tickets += ticket_total
            stones += stone_total
            pulls = ticket_total + stone_total // STONE_PER_PULL
            if times_value > 0:
                text = f"{ticket_total} 协议 + {stone_total:,} 桦石（约 {pulls} 抽）"
            elif ticket_value or stone_value:
                text = f"上限：{ticket_value} 协议 + {stone_value:,} 桦石；勾选后计入"
            else:
                text = "有免费奖励，数量随账号进度或当期版本变化，暂不计入"
            self.sourceTable.item(row, 5).setText(text)
        return tickets, stones

    def _recalculate(self):
        gain_tickets, gain_stones = self._source_totals()
        paid_tickets = paid_stones = paid_special = paid_cost = 0
        for row, pack in enumerate(PAID_GACHA_PACKS):
            selected = self.paid_checks[pack.key].isChecked()
            self.paidTable.item(row, 5).setText("已计入" if selected else "未选择")
            if selected:
                paid_tickets += pack.tickets
                paid_stones += pack.stones
                paid_special += pack.special_pulls
                paid_cost += pack.price_yuan
        gain_tickets += paid_tickets
        gain_stones += paid_stones
        current = calculate_gacha_plan(self.tickets.value(), self.stones.value(), 0)
        plan = calculate_gacha_plan(self.tickets.value(), self.stones.value(), self.target.value(), gain_tickets, gain_stones)
        self.summaryCards[0].setText(f"{current['available_pulls']} 抽")
        self.summaryCards[1].setText(f"{plan['available_pulls']} 抽")
        self.summaryCards[2].setText(f"{plan['missing_pulls']} 抽")
        self.summaryCards[3].setText(f"{plan['tickets_used']} 协议 + {plan['stones_used']:,} 桦石")
        pity_need = pulls_to_guarantee(self.pity.value(), self.guarantee.value())
        if plan["missing_pulls"]:
            shortage = f"仍缺 <b>{plan['missing_pulls']} 抽</b>，等价于 {plan['missing_stones']:,} 桦石，或相同数量的拉普拉斯协议。"
        else:
            shortage = f"目标可完成；抽取后预计剩余 {plan['tickets_left']} 协议和 {plan['stones_left']:,} 桦石。"
        self.action.setText(
            f"<b>行动方案</b><br>当前资源可抽 {current['available_pulls']} 次；计划来源预计增加 {gain_tickets} 协议和 {gain_stones:,} 桦石，"
            f"合计可抽 {plan['available_pulls']} 次（{plan['ten_pulls']} 个十连 + {plan['single_pulls']} 单抽）。<br>"
            f"距当前设置的保底还需 {pity_need} 抽。{shortage}"
            + (f"<br>已选付费礼包：¥{paid_cost}，其中通用资源 {paid_tickets} 协议 + {paid_stones:,} 桦石。" if paid_cost else "")
            + (f" 限定池秘钥另计 {paid_special} 抽，不混入通用抽数。" if paid_special else "")
        )

    def _load(self):
        try:
            return json.loads(self.plan_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return {}

    def _save(self):
        try:
            self.plan_path.parent.mkdir(parents=True, exist_ok=True)
            def value(widget):
                if isinstance(widget, int):
                    return widget
                if isinstance(widget, CheckBox):
                    return int(widget.isChecked())
                return widget.value()
            sources = {key: {"tickets": value(a), "stones": value(b), "times": value(c)} for key, (a, b, c) in self.source_inputs.items()}
            paid = {key: check.isChecked() for key, check in self.paid_checks.items()}
            data = {"tickets": self.tickets.value(), "stones": self.stones.value(), "target": self.target.value(), "pity": self.pity.value(), "guarantee": self.guarantee.value(), "sourceCatalogVersion": GACHA_SOURCE_CATALOG_VERSION, "sources": sources, "paid": paid}
            self.plan_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            pass
