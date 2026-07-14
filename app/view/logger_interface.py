"""
Author: Night-stars-1 nujj1042633805@gmail.com
Date: 2024-04-06 23:52:14
LastEditTime: 2024-04-14 14:20:47
LastEditors: Night-stars-1 nujj1042633805@gmail.com
"""

import re

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QKeySequence
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QHeaderView,
    QLabel,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import PlainTextEdit, ScrollArea

from core.logger import logger

from ..common.style_sheet import StyleSheet


class LoguruHandler(QObject):
    new_log_signal = Signal(str)

    def __init__(self, widget: PlainTextEdit):
        super().__init__()
        self.widget = widget
        if hasattr(self.widget, "setReadOnly"):
            self.widget.setReadOnly(True)
        
        receiver = (
            self.widget.appendLog
            if hasattr(self.widget, "appendLog")
            else self.widget.appendPlainText
        )
        self.new_log_signal.connect(receiver)

    def write(self, message):
        message = message.rstrip("\r\n")
        if message:
            self.new_log_signal.emit(message)


class StructuredLogWidget(QTableWidget):
    """Read-only, colour-coded log view with stable level/time columns."""

    _HISTORY_PATTERN = re.compile(
        r"^(?P<time>\d{2}:\d{2}:\d{2}(?:\.\d+)?)\s*-\s*"
        r"(?P<level>[A-Z]+)\s*\|\s*"
        r"(?:[^|]*?\s+-\s+)?(?P<message>.*)$"
    )
    _LEVEL_COLOURS = {
        "TRACE": QColor("#8b949e"),
        "DEBUG": QColor("#8b949e"),
        "INFO": QColor("#3b8eea"),
        "SUCCESS": QColor("#55b86a"),
        "WARNING": QColor("#e5a445"),
        "ERROR": QColor("#ef6461"),
        "CRITICAL": QColor("#ff4d4f"),
    }

    def __init__(self, parent=None, maximum_rows=1000):
        super().__init__(0, 3, parent)
        self.maximumRows = maximum_rows
        self._scrollTimer = QTimer(self)
        self._scrollTimer.setSingleShot(True)
        self._scrollTimer.setInterval(0)
        self._scrollTimer.timeout.connect(self.scrollToBottom)
        self.setHorizontalHeaderLabels(["级别", "时间", "消息"])
        self.verticalHeader().hide()
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setShowGrid(False)
        self.setWordWrap(True)
        self.setTextElideMode(Qt.TextElideMode.ElideNone)
        self.setAlternatingRowColors(True)
        self.setColumnWidth(0, 82)
        self.setColumnWidth(1, 112)
        header = self.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.verticalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.ResizeToContents
        )
        self.setStyleSheet(
            "QTableWidget { border: 1px solid rgba(128,128,128,0.28); "
            "border-radius: 6px; background: rgba(20,20,20,0.12); }"
            "QTableWidget::item { padding: 2px 7px; border: 0; }"
            "QHeaderView::section { padding: 6px 7px; border: 0; "
            "border-bottom: 1px solid rgba(128,128,128,0.28); "
            "font-weight: 600; background: rgba(128,128,128,0.10); }"
        )

    @classmethod
    def parseLine(cls, line):
        if "\x1f" in line:
            parts = line.split("\x1f", 2)
            if len(parts) == 3:
                return tuple(part.strip() for part in parts)
        match = cls._HISTORY_PATTERN.match(line.strip())
        if match:
            return (
                match.group("level"),
                match.group("time"),
                match.group("message").strip(),
            )
        return "INFO", "--:--:--", line.strip()

    def appendLog(self, line):
        level, timestamp, message = self.parseLine(line)
        if self.rowCount() >= self.maximumRows:
            self.removeRow(0)
        row = self.rowCount()
        self.insertRow(row)
        values = (level, timestamp, message)
        colour = self._LEVEL_COLOURS.get(level, self._LEVEL_COLOURS["INFO"])
        for column, value in enumerate(values):
            item = QTableWidgetItem(value)
            item.setToolTip(value)
            if column == 0:
                item.setForeground(colour)
                font = item.font()
                font.setBold(True)
                item.setFont(font)
            elif column == 1:
                item.setForeground(QColor("#22b8cf"))
            self.setItem(row, column, item)
        self.resizeRowToContents(row)
        # Restarting one owned timer coalesces history and live bursts into a
        # single scroll operation.  Qt also stops it automatically on destroy.
        self._scrollTimer.start()

    def copySelection(self):
        rows = sorted({index.row() for index in self.selectedIndexes()})
        if not rows:
            return
        lines = []
        for row in rows:
            lines.append(
                "\t".join(
                    self.item(row, column).text() if self.item(row, column) else ""
                    for column in range(self.columnCount())
                )
            )
        QApplication.clipboard().setText("\n".join(lines))

    def keyPressEvent(self, event):
        if event.matches(QKeySequence.StandardKey.Copy):
            self.copySelection()
            event.accept()
            return
        super().keyPressEvent(event)


class LoggerInterface(ScrollArea):
    """Home interface"""

    def __init__(self, parent=None):
        super().__init__(parent=parent)

        self.scrollWidget = QWidget(self)
        self.vBoxLayout = QVBoxLayout(self)

        self.loggerLabel = QLabel("日志", self)
        self.log_widget = PlainTextEdit(self)

        self.initWidget()
        self.initLayout()
        self.loadLogger()

    def initWidget(self):
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setWidget(self.scrollWidget)
        self.setWidgetResizable(True)
        self.setObjectName("LoggerInterface")

        # initialize style sheet
        self.scrollWidget.setObjectName("scrollWidget")
        self.loggerLabel.setObjectName("settingLabel")
        StyleSheet.SETTING_INTERFACE.apply(self)


    def initLayout(self):
        self.loggerLabel.move(36, 30)

        self.vBoxLayout.setContentsMargins(36, 80, 36, 10)
        self.vBoxLayout.setSpacing(28)
        self.vBoxLayout.addWidget(self.log_widget)

    def loadLogger(self):
        logger.add(
            LoguruHandler(self.log_widget),
            level="INFO",
            format="{message}",
        )
