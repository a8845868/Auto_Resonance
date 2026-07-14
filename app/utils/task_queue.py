"""Single-threaded task scheduler used by the home page."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable

from loguru import logger
from PySide6.QtCore import QThread, Signal

from core.control.control import reset_stop, stop
from core.exception.exceptions import StopExecution
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

    def __init__(self, tasks: list[QueuedTask], parent=None):
        super().__init__(parent)
        self.tasks = tasks
        self._stop_requested = False
        self._current: QueuedTask | None = None

    def run(self):
        reset_stop()
        pending = [task.name for task in self.tasks]
        self.queueChanged.emit(pending)
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
        self._current = None
        self.queueChanged.emit([])

    def stop(self):
        self._stop_requested = True
        stop()
        if self._current:
            self._current.stop()

    @property
    def stop_requested(self) -> bool:
        return self._stop_requested
