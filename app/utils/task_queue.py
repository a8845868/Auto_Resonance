"""Single-threaded task scheduler used by the home page."""

from dataclasses import dataclass
from typing import Callable

from loguru import logger
from PySide6.QtCore import QThread, Signal

from core.control.control import reset_stop, stop
from core.exception.exceptions import StopExecution


@dataclass(frozen=True)
class QueuedTask:
    name: str
    run: Callable[[], object]
    stop: Callable[[], None] = stop


class TaskQueueWorker(QThread):
    """Execute a snapshot of enabled tasks strictly one after another."""

    queueChanged = Signal(list)
    taskStarted = Signal(str, int, int)
    taskFinished = Signal(str, bool)
    taskResult = Signal(str, object)
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
                if self._stop_requested:
                    succeeded = False
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
