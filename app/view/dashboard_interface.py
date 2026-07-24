"""ALAS-inspired scheduler overview with integrated live log."""

from datetime import datetime, timedelta

from PySide6.QtCore import QTimer, Qt, Signal
from PySide6.QtWidgets import QFrame, QLabel, QSizePolicy, QSplitter, QVBoxLayout, QWidget
from qfluentwidgets import FluentIcon, PrimaryPushButton, ScrollArea

from app.common.config import cfg
from app.common.style_sheet import StyleSheet
from app.utils.task_queue import QueuedTask, TaskQueueWorker
from app.view.logger_interface import LoguruHandler, StructuredLogWidget
from auto.resident_activity import run_resident_activity
from auto.reward_collection import collect_scheduled_rewards
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
    task_result_next_run,
    task_timing,
)
from core.services.daily_capabilities import (
    DailyCapability,
    PRODUCTION_CAPABILITY_METADATA,
    plan_daily_reward_dependencies,
    resolve_daily_capability_prerequisites,
    select_daily_capabilities,
)
from core.services.daily_rewards import DailyProgressSnapshot, RewardStrategy
from core.services.server_calendar import SERVER_CLOCK
from core.services.fatigue_triggers import recover_pending_fatigue_schedules


def recover_startup_fatigue_schedules() -> bool:
    """Recover crash-pending schedule transactions without blocking startup."""

    try:
        return recover_pending_fatigue_schedules()
    except Exception as error:
        logger.warning(f"启动恢复疲劳调度失败，将在下次启动重试: {error}")
        return False


def select_reward_dependency_tasks(
    capabilities: list[DailyCapability],
    snapshot: DailyProgressSnapshot | None,
    strategy: RewardStrategy,
    *,
    satisfied_prerequisites: frozenset[str] = frozenset(),
    now: datetime | None = None,
    daily_activity_enabled: bool = True,
    travel_manual_enabled: bool = True,
) -> list[QueuedTask]:
    """Materialize only safe production tasks selected by the capability registry."""

    plan = plan_daily_reward_dependencies(
        capabilities,
        snapshot,
        strategy,
        now=now or (snapshot.observed_at if snapshot is not None else None),
        satisfied_prerequisites=satisfied_prerequisites,
        daily_activity_enabled=daily_activity_enabled,
        travel_manual_enabled=travel_manual_enabled,
    )
    return [
        capability.run_factory()
        for capability in plan.capabilities
    ]


def _daily_capability_registry(tasks: list[QueuedTask]) -> list[DailyCapability]:
    capabilities = []
    for task in tasks:
        metadata = PRODUCTION_CAPABILITY_METADATA.get(task.key)
        if not metadata:
            continue
        capabilities.append(
            DailyCapability(
                task_key=task.key,
                enabled=True,
                automation_available=callable(task.run),
                completed=(
                    task_timing(task.key).get("status") == "completed"
                    and not is_task_due(task.key)
                ),
                run_factory=lambda task=task: task,
                **metadata,
            )
        )
    return capabilities


def _last_reward_snapshot() -> DailyProgressSnapshot | None:
    result = task_timing("reward_collection").get("result")
    payload = result.get("snapshot_after") if isinstance(result, dict) else None
    if not isinstance(payload, dict):
        return None
    try:
        observed_at = payload.get("observed_at")
        if isinstance(observed_at, str):
            observed_at = datetime.fromisoformat(observed_at)
        snapshot = DailyProgressSnapshot(**{**payload, "observed_at": observed_at})
        now = SERVER_CLOCK.server_now()
        if snapshot.server_day_id != SERVER_CLOCK.server_day_id(now):
            return None
        if (
            snapshot.observed_at.tzinfo is None
            or snapshot.observed_at.utcoffset() is None
            or not timedelta(0) <= now - snapshot.observed_at <= timedelta(minutes=15)
        ):
            return None
        return snapshot
    except (TypeError, ValueError):
        return None

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
    result = collect_scheduled_rewards(
        daily,
        manual,
        strategy=str(cfg.rewardStrategy.value),
    )
    dispatch_collected = collect_dispatch_rewards()
    result["dispatch_collected"] = dispatch_collected
    result["progress_made"] = bool(result.get("progress_made") or dispatch_collected)
    logger.info(
        "奖励检查状态={status} 完成判定={complete} 下次={next_run} 原因={reason}".format(
            status=result.get("status"),
            complete=result.get("completion_predicate"),
            next_run=result.get("next_run_at"),
            reason=result.get("next_run_reason"),
        )
    )
    return result


def _history_status_label(status: str) -> str:
    return {
        "completed": "完成",
        "deferred": "等待复核",
    }.get(status, "失败/停止")


class DashboardInterface(ScrollArea):
    activityStateChanged = Signal(str, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.queueWorker = None
        self.businessTaskProvider = lambda: None
        self.priorityTaskProviders = []
        self.additionalTaskProviders = []
        recover_startup_fatigue_schedules()
        # Keep the scheduler listening from application startup so reaching a
        # configured next-run time does not require a manual button click.
        self.schedulerArmed = True
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
        self._setControlRunning(self.schedulerArmed)

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
        self.personalStartupStatusLabel = QLabel("个人启动：未运行", self.scrollWidget)
        self.personalStartupStatusLabel.setWordWrap(True)
        self.mainLayout.addWidget(self.personalStartupStatusLabel)

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
        business = self.businessTaskProvider()
        if business:
            tasks.append(business)
        for provider in self.additionalTaskProviders:
            provided_task = provider()
            if provided_task:
                tasks.append(provided_task)
        # Reward collection is deliberately the final ordinary task.  Existing
        # sweep, trade, passenger, and other enabled automations contribute
        # progress first; the collector then claims and evaluates the resulting
        # state instead of treating one early claim pass as completion.
        if bool(cfg.enableRewardCollection.value):
            daily = bool(cfg.autoCollectDailyActivity.value)
            manual = bool(cfg.autoCollectTravelManual.value)
            if daily or manual:
                tasks.append(QueuedTask(
                    "领取任务奖励",
                    lambda: _collect_scheduled_rewards(daily, manual),
                    key="reward_collection",
                ))
        return tasks

    def _enabledTasks(self):
        tasks = self._allEnabledTasks()
        due = [task for task in tasks if not task.key or is_task_due(task.key)]
        reward_task = next(
            (task for task in due if task.key == "reward_collection"), None
        )
        if reward_task is not None:
            try:
                strategy = RewardStrategy(str(cfg.rewardStrategy.value))
            except ValueError:
                strategy = RewardStrategy.MAXIMIZE_PROGRESS
            prerequisite_resolution = resolve_daily_capability_prerequisites()
            for prerequisite in prerequisite_resolution.evidence.values():
                logger.info(
                    "Daily prerequisite {name}: status={status} source={source} "
                    "observed_at={observed} reason={reason}".format(
                        name=prerequisite.name,
                        status=prerequisite.status,
                        source=prerequisite.source,
                        observed=prerequisite.observed_at,
                        reason=prerequisite.block_reason,
                    )
                )
            dependencies = select_reward_dependency_tasks(
                _daily_capability_registry(tasks),
                _last_reward_snapshot(),
                strategy,
                satisfied_prerequisites=prerequisite_resolution.satisfied,
                now=SERVER_CLOCK.server_now(),
                daily_activity_enabled=bool(cfg.autoCollectDailyActivity.value),
                travel_manual_enabled=bool(cfg.autoCollectTravelManual.value),
            )
            existing = {task.key for task in due}
            insertion = due.index(reward_task)
            for dependency in dependencies:
                if dependency.key in existing:
                    continue
                due.insert(insertion, dependency)
                insertion += 1
                existing.add(dependency.key)
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
            f"{_history_status_label(item.get('status', ''))}  "
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
        if bool(cfg.enableAutoGameLifecycle.value) or bool(
            cfg.enablePersonalStartupEpisode.value
        ):
            lifecycle = EmulatorQueueLifecycle(
                cfg.device.value,
                options=LifecycleOptions(
                    auto_start_emulator=bool(cfg.autoStartEmulator.value),
                    close_game_when_idle=bool(cfg.closeGameWhenIdle.value),
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
        from core.services.fatigue_triggers import cancel_deferred_fatigue_actions

        cancel_deferred_fatigue_actions()

    def _runDueTasks(self):
        """Wake scheduled tasks without keeping the queue worker blocked."""
        # Retry durable FAILED_RETRYABLE handoffs on every bounded scheduler
        # tick, so a transient schedule write failure does not require restart.
        recover_startup_fatigue_schedules()
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
        elif name == "自动准备游戏并进入岚心城":
            self.personalStartupStatusLabel.setText(
                "个人启动：运行中 · 正在观察状态 · 最近动作 NONE"
            )

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
        if task.name == "自动准备游戏并进入岚心城":
            details = result if isinstance(result, dict) else {}
            state = details.get("current_state", "UNKNOWN")
            action = details.get("last_action", "NONE")
            actions = details.get("real_ui_actions", 0)
            reason = details.get("reason", "no_result")
            outcome = "PASS" if succeeded else "FAIL"
            self.personalStartupStatusLabel.setText(
                f"个人启动：{outcome} · 状态 {state} · 最近动作 {action} · "
                f"动作 {actions}/10 · {reason}"
            )
        if not task.key:
            return
        deferred = bool(succeeded and task_result_deferred(result))
        explicit_next_run = task_result_next_run(result)
        record_task_execution(
            task.key,
            task.name,
            succeeded,
            explicit_next_run or task.next_run_after(succeeded and not deferred),
            result,
            deferred=deferred,
        )
        if succeeded and task.key in {
            "resident_activity",
            "run_business",
            "passenger_build",
        }:
            progress_made = result is True or (
                isinstance(result, dict)
                and (
                    result.get("progress_made") is True
                    or any(isinstance(value, int) and value > 0 for value in result.values())
                )
            )
            if progress_made:
                from core.services.daily_rewards import schedule_debounced_reward_recheck

                schedule_debounced_reward_recheck()
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
