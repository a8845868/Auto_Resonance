"""
Author: Night-stars-1 nujj1042633805@gmail.com
Date: 2024-04-06 23:52:14
LastEditTime: 2024-04-14 14:20:47
LastEditors: Night-stars-1 nujj1042633805@gmail.com
"""

import re

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import (
    QColor,
    QFontDatabase,
    QTextCharFormat,
    QTextCursor,
)
from PySide6.QtWidgets import (
    QLabel,
    QTextEdit,
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


class StructuredLogWidget(QTextEdit):
    """Colour-coded text log with stable columns and native text selection."""

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
        super().__init__(parent)
        self.maximumRows = maximum_rows
        self._scrollTimer = QTimer(self)
        self._scrollTimer.setSingleShot(True)
        self._scrollTimer.setInterval(0)
        self._scrollTimer.timeout.connect(self._scrollToBottom)
        self.setReadOnly(True)
        self.setLineWrapMode(QTextEdit.LineWrapMode.WidgetWidth)
        self.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        self.document().setMaximumBlockCount(maximum_rows)
        self.setStyleSheet(
            "QTextEdit { border: 1px solid rgba(128,128,128,0.28); "
            "border-radius: 6px; background: rgba(20,20,20,0.12); }"
        )

    def _scrollToBottom(self):
        scroll_bar = self.verticalScrollBar()
        scroll_bar.setValue(scroll_bar.maximum())

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
        cursor = QTextCursor(self.document())
        cursor.movePosition(QTextCursor.MoveOperation.End)
        if not self.document().isEmpty():
            cursor.insertBlock()

        level_format = QTextCharFormat()
        level_format.setForeground(
            self._LEVEL_COLOURS.get(level, self._LEVEL_COLOURS["INFO"])
        )
        level_format.setFontWeight(700)
        time_format = QTextCharFormat()
        time_format.setForeground(QColor("#22b8cf"))
        message_format = QTextCharFormat()

        cursor.insertText(f"{level:<8} ", level_format)
        cursor.insertText(f"{timestamp:<12}", time_format)
        cursor.insertText(" │ ", message_format)
        cursor.insertText(message, message_format)
        # Restarting one owned timer coalesces history and live bursts into a
        # single scroll operation.  Qt also stops it automatically on destroy.
        self._scrollTimer.start()


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
