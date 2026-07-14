"""Single-threaded task scheduler used by the home page."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable

from loguru import logger
from PySide6.QtCore import QThread, Signal

from core.control.control import reset_stop, stop
from core.exception.exceptions import StopExecution
from core.services.emulator_lifecycle import LifecycleCancelled, QueueLifecycle
from core.services.task_schedule_state import next_daily_reset, task_result_succeeded


@dataclass(frozen=True)
class QueuedTask:
    name: str
    run: Callable[[], object]
    stop: Callable[[], None] = stop
    key: str = ""
    next_run_factory: Callable[[datetime], datetime] = next_daily_reset
    failure_retry_seconds: int = 600

    def next_run_after(self, succeeded: bool, now: datetime | None = None) -> datetime:
        now = now or datetime.now()
        if succeeded:
            return self.next_run_factory(now)
        return now + timedelta(seconds=self.failure_retry_seconds)


class TaskQueueWorker(QThread):
    """Execute a snapshot of enabled tasks strictly one after another."""

    queueChanged = Signal(list)
    taskStarted = Signal(str, int, int)
    taskFinished = Signal(str, bool)
    taskResult = Signal(str, object)
    taskCompleted = Signal(object, bool, object)
    error = Signal(str)

    def __init__(
        self,
        tasks: list[QueuedTask],
        parent=None,
        *,
        lifecycle: QueueLifecycle | None = None,
    ):
        super().__init__(parent)
        self.tasks = tasks
        self.lifecycle = lifecycle
        self._stop_requested = False
        self._current: QueuedTask | None = None

    def run(self):
        reset_stop()
        pending = [task.name for task in self.tasks]
        self.queueChanged.emit(pending)
        lifecycle_entered = False
        try:
            if self.tasks and self.lifecycle is not None:
                lifecycle_entered = True
                try:
                    self.lifecycle.prepare(lambda: self._stop_requested)
                except LifecycleCancelled:
                    self._stop_requested = True
                    return
                except Exception:
                    if self._stop_requested:
                        return
                    logger.exception("任务队列启动模拟器或游戏失败")
                    self.error.emit("模拟器或游戏启动失败，本批任务已进入失败重试")
                    self._mark_tasks_failed_before_start()
                    return

            for index, task in enumerate(self.tasks, start=1):
                if self._stop_requested:
                    break
                self._current = task
                self.taskStarted.emit(task.name, index, len(self.tasks))
                self.queueChanged.emit([item.name for item in self.tasks[index:]])
                succeeded = True
                result = None
                try:
                    result = task.run()
                    if self._stop_requested or not task_result_succeeded(result):
                        succeeded = False
                        if not self._stop_requested:
                            logger.warning(f"任务未返回明确成功结果，按失败处理: {task.name}")
                except StopExecution:
                    succeeded = False
                    self._stop_requested = True
                except Exception:
                    succeeded = False
                    logger.exception(f"任务执行失败: {task.name}")
                    self.error.emit(f"{task.name}执行失败，已跳过并继续后续任务")
                self.taskFinished.emit(task.name, succeeded)
                if succeeded:
                    self.taskResult.emit(task.name, result)
                self.taskCompleted.emit(task, succeeded, result)
        finally:
            self._current = None
            if lifecycle_entered and self.lifecycle is not None:
                try:
                    self.lifecycle.cleanup()
                except Exception:
                    # Cleanup is resource hygiene, not the business result.  A
                    # successful daily task must not run twice because closing
                    # the idle game process happened to fail.
                    logger.exception("任务队列结束后的游戏资源清理失败")
                    self.error.emit("任务已结束，但关闭游戏或模拟器失败，请查看日志")
            self.queueChanged.emit([])

    def _mark_tasks_failed_before_start(self) -> None:
        """Record startup failure once so the scheduler uses its retry delay."""

        for index, task in enumerate(self.tasks, start=1):
            if self._stop_requested:
                break
            self.taskStarted.emit(task.name, index, len(self.tasks))
            self.queueChanged.emit([item.name for item in self.tasks[index:]])
            self.taskFinished.emit(task.name, False)
            self.taskCompleted.emit(task, False, None)

    def stop(self):
        self._stop_requested = True
        stop()
        if self._current:
            self._current.stop()

    @property
    def stop_requested(self) -> bool:
        return self._stop_requested
