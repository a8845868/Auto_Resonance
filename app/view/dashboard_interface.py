"""ALAS-inspired scheduler overview with integrated live log."""

from PySide6.QtCore import QTimer, Qt, Signal
from PySide6.QtWidgets import QFrame, QLabel, QSizePolicy, QSplitter, QVBoxLayout, QWidget
from qfluentwidgets import FluentIcon, PrimaryPushButton, ScrollArea

from app.common.config import cfg
from app.common.style_sheet import StyleSheet
from app.utils.task_queue import QueuedTask, TaskQueueWorker
from app.view.logger_interface import LoguruHandler, StructuredLogWidget
from auto.resident_activity import run_resident_activity
from auto.reward_collection import collect_rewards
from auto.module.dispatch import collect_dispatch_rewards
from core.logger import logger
from core.services.emulator_lifecycle import (
    EmulatorQueueLifecycle,
    LifecycleOptions,
)
from core.services.self_healing import (
    discover_log_incidents,
    submit_incident,
)
from core.services.task_schedule_state import (
    completed_history,
    is_task_due,
    record_task_execution,
    task_result_deferred,
    task_timing,
)

class StatusPanel(QFrame):
    def __init__(self, title, parent=None):
        super().__init__(parent)
        self.setObjectName("schedulerPanel")
        self.setStyleSheet(
            "QFrame#schedulerPanel { border: 1px solid rgba(128,128,128,0.28); "
            "border-radius: 8px; background: rgba(128,128,128,0.06); }"
        )
        self.setMinimumHeight(118)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
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


def _collect_scheduled_rewards(daily: bool, manual: bool):
    """Keep all reward side work inside the reward queue task."""
    result = collect_rewards(daily, manual)
    dispatch_collected = collect_dispatch_rewards()
    payload = {"task_rewards": result, "dispatch_collected": dispatch_collected}
    if not any(result.values()) and not dispatch_collected:
        logger.warning("奖励任务没有确认领取任何奖励，本次不记完成，稍后重试")
        return {}
    return payload


class DashboardInterface(ScrollArea):
    activityStateChanged = Signal(str, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.queueWorker = None
        self.businessTaskProvider = lambda: None
        self.priorityTaskProviders = []
        self.additionalTaskProviders = []
        self.schedulerArmed = False
        self.scheduleTimer = QTimer(self)
        self.scheduleTimer.setInterval(30_000)
        self.scheduleTimer.timeout.connect(self._runDueTasks)
        self.scheduleTimer.start()
        self.currentTask = None
        self.shutdownRequested = False
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

        self.controlButton = PrimaryPushButton(
            FluentIcon.PLAY, "开始全部任务", self.scrollWidget
        )
        self.controlButton.setMinimumHeight(48)
        self.controlButton.clicked.connect(self._toggleTaskQueue)
        self.mainLayout.addWidget(self.controlButton)

        splitter = QSplitter(Qt.Orientation.Horizontal, self.scrollWidget)
        scheduler = QWidget(splitter)
        scheduler_layout = QVBoxLayout(scheduler)
        scheduler_layout.setContentsMargins(0, 0, 4, 0)
        scheduler_title = QLabel("任务序列", scheduler)
        scheduler_title.setStyleSheet("font-size: 20px; font-weight: 600;")
        scheduler_layout.addWidget(scheduler_title)
        self.runningPanel = StatusPanel("运行中", scheduler)
        self.pendingPanel = StatusPanel("队列中", scheduler)
        self.finishedPanel = StatusPanel("已完成", scheduler)
        self.waitingPanel = StatusPanel("等待中", scheduler)
        scheduler_layout.addWidget(self.runningPanel, 1)
        scheduler_layout.addWidget(self.pendingPanel, 1)
        scheduler_layout.addWidget(self.waitingPanel, 1)
        scheduler_layout.addWidget(self.finishedPanel)

        logs = QWidget(splitter)
        logs_layout = QVBoxLayout(logs)
        logs_layout.setContentsMargins(4, 0, 0, 0)
        log_title = QLabel("运行日志", logs)
        log_title.setStyleSheet("font-size: 20px; font-weight: 600;")
        self.logWidget = StructuredLogWidget(logs)
        self.logHandler = LoguruHandler(self.logWidget)
        self.logSink = logger.add(
            self.logHandler,
            level="INFO",
            format="{level.name}\x1f{time:HH:mm:ss.SSS}\x1f{message}",
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
        self.refreshScheduleOverview()

    def _loadRecentLog(self):
        """Show recent history immediately, then continue with live log events."""
        try:
            with open("logs/debug.log", "r", encoding="utf-8", errors="replace") as stream:
                recent = stream.readlines()[-200:]
            for line in recent:
                self.logWidget.appendLog(line.rstrip("\r\n"))
        except OSError:
            pass

    def setBusinessTaskProvider(self, provider):
        self.businessTaskProvider = provider

    def addTaskProvider(self, provider):
        self.additionalTaskProviders.append(provider)

    def addPriorityTaskProvider(self, provider):
        """Register a task that must run before all ordinary daily tasks."""
        self.priorityTaskProviders.append(provider)

    def _allEnabledTasks(self):
        tasks = []
        for provider in self.priorityTaskProviders:
            provided_task = provider()
            if provided_task:
                tasks.append(provided_task)
        if bool(cfg.enableResidentActivity.value):
            activity_task = cfg.residentActivityTask.value
            reward = cfg.residentActivityFullRealmReward.value
            tasks.append(QueuedTask(
                "扫荡与全域整备",
                lambda task=activity_task, full_reward=reward: run_resident_activity(task, full_reward),
                key="resident_activity",
            ))
        if bool(cfg.enableRewardCollection.value):
            daily = bool(cfg.autoCollectDailyActivity.value)
            manual = bool(cfg.autoCollectTravelManual.value)
            if daily or manual:
                tasks.append(QueuedTask(
                    "领取任务奖励",
                    lambda: _collect_scheduled_rewards(daily, manual),
                    key="reward_collection",
                ))
        business = self.businessTaskProvider()
        if business:
            tasks.append(business)
        for provider in self.additionalTaskProviders:
            provided_task = provider()
            if provided_task:
                tasks.append(provided_task)
        return tasks

    def _enabledTasks(self):
        tasks = self._allEnabledTasks()
        due = [task for task in tasks if not task.key or is_task_due(task.key)]
        waiting = []
        for task in tasks:
            if task in due:
                continue
            next_run = task_timing(task.key).get("next_run", "").replace("T", " ")
            waiting.append(f"{task.name}  ·  {next_run}")
        self.waitingPanel.setTasks(waiting)
        return due

    def refreshScheduleOverview(self):
        history = completed_history()
        self._completed = [
            f"{'完成' if item.get('status') == 'completed' else '失败/停止'}  "
            f"{item.get('name', item.get('key', '任务'))}  ·  {item.get('last_run', '').replace('T', ' ')}"
            for item in history[:12]
        ]
        self.finishedPanel.setTasks(self._completed)
        waiting = []
        for task in self._allEnabledTasks():
            if task.key and not is_task_due(task.key):
                next_run = task_timing(task.key).get("next_run", "").replace("T", " ")
                waiting.append(f"{task.name}  ·  {next_run}")
        self.waitingPanel.setTasks(waiting)

    def startTaskQueue(self):
        self.schedulerArmed = True
        self._setControlRunning(True)
        # Keep the finished worker reserved until its queued finished slot has
        # run.  Otherwise an old slot can accidentally delete a new worker.
        if self.queueWorker is not None:
            return
        tasks = self._enabledTasks()
        if not tasks:
            if self.waitingPanel.content.text() != "无任务":
                self.pendingPanel.setTasks(["当前没有到期任务；可清空某项的下次执行时间以立即运行"])
            else:
                self.pendingPanel.setTasks(["未启用可执行任务，请在左侧功能页开启"])
            return
        self_healing_enabled = bool(cfg.enableCodexSelfHealing.value)
        isolated_repair_allowed = bool(cfg.allowCodexIsolatedRepair.value)

        def report_incident(incident):
            # Keep dispatch policy consistent with the worker's halt policy for
            # the entire batch, even if a setting is toggled mid-run.
            submit_incident(
                incident,
                dispatch=self_healing_enabled,
                allow_repair=isolated_repair_allowed,
            )

        lifecycle = None
        if bool(cfg.enableAutoGameLifecycle.value):
            lifecycle = EmulatorQueueLifecycle(
                cfg.device.value,
                options=LifecycleOptions(
                    auto_start_emulator=bool(cfg.autoStartEmulator.value),
                    close_game_when_idle=True,
                    close_emulator_when_idle=bool(
                        cfg.closeEmulatorWhenIdle.value
                    ),
                ),
            )
        self.queueWorker = TaskQueueWorker(
            tasks,
            self,
            lifecycle=lifecycle,
            incident_reporter=report_incident,
            halt_on_failure=self_healing_enabled,
        )
        self.queueWorker.taskStarted.connect(self._taskStarted)
        self.queueWorker.taskFinished.connect(self._taskFinished)
        self.queueWorker.taskResult.connect(self._taskResult)
        self.queueWorker.taskCompleted.connect(self._taskCompleted)
        self.queueWorker.queueChanged.connect(self.pendingPanel.setTasks)
        self.queueWorker.error.connect(lambda message: logger.error(message))
        worker = self.queueWorker
        worker.finished.connect(lambda: self._queueFinished(worker))
        self.queueWorker.start()

    def _toggleTaskQueue(self):
        if self.schedulerArmed:
            self.stopTaskQueue()
        else:
            self.startTaskQueue()

    def _setControlRunning(self, running):
        self.controlButton.setIcon(FluentIcon.CANCEL if running else FluentIcon.PLAY)
        self.controlButton.setText("停止全部任务" if running else "开始全部任务")
        self.controlButton.setEnabled(True)

    def stopTaskQueue(self):
        self.schedulerArmed = False
        self._setControlRunning(False)
        if self.queueWorker and self.queueWorker.isRunning():
            self.queueWorker.stop()
            if self.currentTask == "扫荡与全域整备":
                self.activityStateChanged.emit("■  已请求停止", "#f0a44b")

    def _runDueTasks(self):
        """Wake scheduled tasks without keeping the queue worker blocked."""
        if self.queueWorker is not None:
            return
        if bool(cfg.enableCodexSelfHealing.value):
            discover_log_incidents(
                dispatch=True,
                allow_repair=bool(cfg.allowCodexIsolatedRepair.value),
            )
        if not self.schedulerArmed:
            return
        if any(not task.key or is_task_due(task.key) for task in self._allEnabledTasks()):
            self.startTaskQueue()

    def _taskStarted(self, name, index, total):
        self.currentTask = name
        self.runningPanel.setTasks([f"{index}/{total}  {name}"])
        if name == "扫荡与全域整备":
            self.activityStateChanged.emit("●  运行中：正在执行全域整备", "#43a5ff")

    def _taskFinished(self, name, succeeded):
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

    def _taskCompleted(self, task, succeeded, result):
        if not task.key:
            return
        deferred = bool(succeeded and task_result_deferred(result))
        record_task_execution(
            task.key,
            task.name,
            succeeded,
            task.next_run_after(succeeded and not deferred),
            result,
            deferred=deferred,
        )
        self.refreshScheduleOverview()

    def _queueFinished(self, worker):
        if worker is not self.queueWorker:
            worker.deleteLater()
            return
        halted_for_repair = worker.halted_for_repair
        if halted_for_repair:
            self.schedulerArmed = False
        self.runningPanel.setTasks([])
        if halted_for_repair:
            self.pendingPanel.setTasks(
                ["检测到异常，自动调度已暂停；请前往“调试”查看 Codex 诊断或候选修复"]
            )
            logger.warning("检测到任务异常，自动调度已熔断并等待人工审阅")
        else:
            self.pendingPanel.setTasks([])
        self._setControlRunning(self.schedulerArmed)
        self.refreshScheduleOverview()
        worker.deleteLater()
        self.queueWorker = None

    def shutdown(self) -> bool:
        """Request shutdown without blocking the Qt main thread."""

        self.scheduleTimer.stop()
        if self.queueWorker is not None:
            if self.queueWorker.isRunning() and not self.shutdownRequested:
                self.shutdownRequested = True
                self.queueWorker.stop()
            return False
        if self.logSink is not None:
            logger.remove(self.logSink)
            self.logSink = None
        return True
