"""ALAS-inspired scheduler overview with integrated live log."""

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QSplitter, QVBoxLayout, QWidget
from qfluentwidgets import FluentIcon, PlainTextEdit, PrimaryPushButton, PushButton, ScrollArea

from app.common.config import cfg
from app.common.style_sheet import StyleSheet
from app.utils.task_queue import QueuedTask, TaskQueueWorker
from app.view.logger_interface import LoguruHandler
from auto.resident_activity import run_resident_activity
from auto.reward_collection import collect_rewards
from core.logger import logger


class StatusPanel(QFrame):
    def __init__(self, title, parent=None):
        super().__init__(parent)
        self.setObjectName("schedulerPanel")
        self.setStyleSheet(
            "QFrame#schedulerPanel { border: 1px solid rgba(128,128,128,0.28); "
            "border-radius: 8px; background: rgba(128,128,128,0.06); }"
        )
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)
        title_label = QLabel(title, self)
        title_label.setStyleSheet("font-size: 17px; font-weight: 600;")
        self.content = QLabel("无任务", self)
        self.content.setWordWrap(True)
        self.content.setStyleSheet("color: #888; padding-top: 8px;")
        layout.addWidget(title_label)
        layout.addWidget(self.content, 1)

    def setTasks(self, tasks):
        self.content.setText("\n".join(tasks) if tasks else "无任务")


class DashboardInterface(ScrollArea):
    activityStateChanged = Signal(str, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.queueWorker = None
        self.businessTaskProvider = lambda: None
        self.currentTask = None
        self.scrollWidget = QWidget(self)
        self.mainLayout = QVBoxLayout(self.scrollWidget)
        self.setObjectName("HomeInterface")
        self.scrollWidget.setObjectName("view")
        self.setWidget(self.scrollWidget)
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.mainLayout.setContentsMargins(28, 24, 28, 24)
        self.mainLayout.setSpacing(14)
        StyleSheet.HOME_INTERFACE.apply(self)
        self._buildUi()

    def _buildUi(self):
        title = QLabel("自动任务", self.scrollWidget)
        title.setStyleSheet("font-size: 24px; font-weight: 700;")
        self.mainLayout.addWidget(title)

        controls = QHBoxLayout()
        self.startButton = PrimaryPushButton(FluentIcon.PLAY, "开始全部任务", self.scrollWidget)
        self.stopButton = PushButton(FluentIcon.CANCEL, "停止", self.scrollWidget)
        self.startButton.setMinimumHeight(48)
        self.stopButton.setMinimumHeight(48)
        self.stopButton.setEnabled(False)
        self.startButton.clicked.connect(self.startTaskQueue)
        self.stopButton.clicked.connect(self.stopTaskQueue)
        controls.addWidget(self.startButton, 1)
        controls.addWidget(self.stopButton, 1)
        self.mainLayout.addLayout(controls)

        splitter = QSplitter(Qt.Orientation.Horizontal, self.scrollWidget)
        scheduler = QWidget(splitter)
        scheduler_layout = QVBoxLayout(scheduler)
        scheduler_layout.setContentsMargins(0, 0, 4, 0)
        scheduler_title = QLabel("任务序列", scheduler)
        scheduler_title.setStyleSheet("font-size: 20px; font-weight: 600;")
        scheduler_layout.addWidget(scheduler_title)
        self.runningPanel = StatusPanel("运行中", scheduler)
        self.pendingPanel = StatusPanel("队列中", scheduler)
        self.finishedPanel = StatusPanel("本轮已完成", scheduler)
        scheduler_layout.addWidget(self.runningPanel)
        scheduler_layout.addWidget(self.pendingPanel)
        scheduler_layout.addWidget(self.finishedPanel)

        logs = QWidget(splitter)
        logs_layout = QVBoxLayout(logs)
        logs_layout.setContentsMargins(4, 0, 0, 0)
        log_title = QLabel("运行日志", logs)
        log_title.setStyleSheet("font-size: 20px; font-weight: 600;")
        self.logWidget = PlainTextEdit(logs)
        self.logWidget.setReadOnly(True)
        self.logHandler = LoguruHandler(self.logWidget)
        self.logSink = logger.add(
            self.logHandler,
            level="INFO",
            format="{time:HH:mm:ss} | {level} | {message}",
        )
        self._loadRecentLog()
        logs_layout.addWidget(log_title)
        logs_layout.addWidget(self.logWidget, 1)

        splitter.addWidget(scheduler)
        splitter.addWidget(logs)
        splitter.setSizes([340, 620])
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 2)
        self.mainLayout.addWidget(splitter, 1)

    def _loadRecentLog(self):
        """Show recent history immediately, then continue with live log events."""
        try:
            with open("logs/debug.log", "r", encoding="utf-8", errors="replace") as stream:
                recent = stream.readlines()[-200:]
            self.logWidget.setPlainText("".join(recent).rstrip())
            cursor = self.logWidget.textCursor()
            cursor.movePosition(cursor.MoveOperation.End)
            self.logWidget.setTextCursor(cursor)
        except OSError:
            pass

    def setBusinessTaskProvider(self, provider):
        self.businessTaskProvider = provider

    def _enabledTasks(self):
        tasks = []
        if bool(cfg.enableResidentActivity.value):
            task = cfg.residentActivityTask.value
            reward = cfg.residentActivityFullRealmReward.value
            tasks.append(QueuedTask("扫荡与全域整备", lambda: run_resident_activity(task, reward)))
        if bool(cfg.enableRewardCollection.value):
            daily = bool(cfg.autoCollectDailyActivity.value)
            manual = bool(cfg.autoCollectTravelManual.value)
            if daily or manual:
                tasks.append(QueuedTask("领取任务奖励", lambda: collect_rewards(daily, manual)))
        business = self.businessTaskProvider()
        if business:
            tasks.append(business)
        return tasks

    def startTaskQueue(self):
        if self.queueWorker and self.queueWorker.isRunning():
            return
        tasks = self._enabledTasks()
        if not tasks:
            self.pendingPanel.setTasks(["未启用可执行任务，请在左侧功能页开启"])
            return
        self.finishedPanel.setTasks([])
        self._completed = []
        self.queueWorker = TaskQueueWorker(tasks, self)
        self.queueWorker.taskStarted.connect(self._taskStarted)
        self.queueWorker.taskFinished.connect(self._taskFinished)
        self.queueWorker.taskResult.connect(self._taskResult)
        self.queueWorker.queueChanged.connect(self.pendingPanel.setTasks)
        self.queueWorker.error.connect(lambda message: logger.error(message))
        self.queueWorker.finished.connect(self._queueFinished)
        self.startButton.setEnabled(False)
        self.stopButton.setEnabled(True)
        self.queueWorker.start()

    def stopTaskQueue(self):
        if self.queueWorker and self.queueWorker.isRunning():
            self.queueWorker.stop()
            self.stopButton.setEnabled(False)
            if self.currentTask == "扫荡与全域整备":
                self.activityStateChanged.emit("■  已请求停止", "#f0a44b")

    def _taskStarted(self, name, index, total):
        self.currentTask = name
        self.runningPanel.setTasks([f"{index}/{total}  {name}"])
        if name == "扫荡与全域整备":
            self.activityStateChanged.emit("●  运行中：正在执行全域整备", "#43a5ff")

    def _taskFinished(self, name, succeeded):
        self._completed.append(f"{'完成' if succeeded else '停止/失败'}  {name}")
        self.finishedPanel.setTasks(self._completed)
        if name == "扫荡与全域整备":
            if succeeded:
                self.activityStateChanged.emit("✓  扫荡方案执行完成", "#65c466")
            elif not self.queueWorker.stop_requested:
                self.activityStateChanged.emit("✕  扫荡方案执行失败", "#ff6b6b")

    def _taskResult(self, name, result):
        if name != "扫荡与全域整备" or not isinstance(result, dict):
            return
        details = "，".join(f"{task} {count} 次" for task, count in result.items())
        self.activityStateChanged.emit(f"✓  已完成：{details}", "#65c466")

    def _queueFinished(self):
        self.runningPanel.setTasks([])
        self.pendingPanel.setTasks([])
        self.startButton.setEnabled(True)
        self.stopButton.setEnabled(False)
        self.queueWorker.deleteLater()
        self.queueWorker = None

    def shutdown(self):
        if self.queueWorker and self.queueWorker.isRunning():
            self.queueWorker.stop()
            self.queueWorker.wait(3000)
        if self.logSink is not None:
            logger.remove(self.logSink)
            self.logSink = None
