"""Reusable last-run/next-run editor for task pages."""

from datetime import datetime

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from core.services.task_schedule_state import set_next_run, task_timing


class TaskScheduleCard(QWidget):
    scheduleChanged = Signal()

    def __init__(self, task_key: str, parent=None):
        super().__init__(parent)
        self.taskKey = task_key
        # ExpandLayout otherwise compresses a plain QWidget to roughly one
        # text line, hiding the next-run editor and buttons.
        self.setMinimumHeight(96)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self.setObjectName("taskScheduleCard")
        self.setStyleSheet(
            "QWidget#taskScheduleCard { border: 1px solid rgba(128,128,128,.28); "
            "border-radius: 8px; background: rgba(128,128,128,.06); }"
        )
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 12, 16, 12)
        self.lastRunLabel = QLabel(self)
        row = QHBoxLayout()
        row.addWidget(QLabel("下一次执行", self))
        self.nextRunEdit = QLineEdit(self)
        self.nextRunEdit.setPlaceholderText("留空表示立即执行，例如 2026-07-13 05:00:00")
        save = QPushButton("保存", self)
        clear = QPushButton("清空并立即执行", self)
        save.clicked.connect(self.save)
        clear.clicked.connect(self.clearNextRun)
        row.addWidget(self.nextRunEdit, 1)
        row.addWidget(save)
        row.addWidget(clear)
        root.addWidget(self.lastRunLabel)
        root.addLayout(row)
        self.refresh()

    def refresh(self):
        timing = task_timing(self.taskKey)
        self.lastRunLabel.setText(f"上次执行：{timing.get('last_run') or '从未执行'}")
        self.nextRunEdit.setText((timing.get("next_run") or "").replace("T", " "))

    def save(self):
        text = self.nextRunEdit.text().strip()
        if text:
            try:
                parsed = datetime.fromisoformat(text)
            except ValueError:
                self.nextRunEdit.setStyleSheet("border: 1px solid #ff5f57;")
                return
            set_next_run(self.taskKey, parsed)
        else:
            set_next_run(self.taskKey, None)
        self.nextRunEdit.setStyleSheet("")
        self.refresh()
        self.scheduleChanged.emit()

    def clearNextRun(self):
        self.nextRunEdit.clear()
        set_next_run(self.taskKey, None)
        self.refresh()
        self.scheduleChanged.emit()

    def showEvent(self, event):
        self.refresh()
        super().showEvent(event)
