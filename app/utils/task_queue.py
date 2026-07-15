"""Single-threaded task scheduler used by the home page."""

from dataclasses import dataclass
from datetime import datetime, timedelta
import traceback
from typing import Callable
import uuid

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
        incident_reporter: Callable[[dict], None] | None = None,
        halt_on_failure: bool = False,
    ):
        super().__init__(parent)
        self.tasks = tasks
        self.lifecycle = lifecycle
        self.incident_reporter = incident_reporter
        self.halt_on_failure = bool(halt_on_failure)
        self._stop_requested = False
        self._halted_for_repair = False
        self._current: QueuedTask | None = None
        self._pending_incidents: list[dict] = []
        self._incident_batch_id = uuid.uuid4().hex

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
                except Exception as error:
                    if self._stop_requested:
                        return
                    if self.halt_on_failure:
                        self._halted_for_repair = True
                    self._queue_incident(
                        failure_kind="resource_startup_error",
                        message=f"{type(error).__name__}: {error}",
                        expected="模拟器与游戏资源准备成功",
                        observed="资源准备抛出异常",
                        traceback_text=traceback.format_exc(),
                    )
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
                            self._queue_incident(
                                task=task,
                                failure_kind="unexpected_result",
                                message="任务未返回明确成功结果",
                                expected="task_result_succeeded(result) == True",
                                observed=self._safe_observed(result),
                                context={"task_index": index, "task_total": len(self.tasks)},
                            )
                            logger.warning(f"任务未返回明确成功结果，按失败处理: {task.name}")
                except StopExecution:
                    succeeded = False
                    self._stop_requested = True
                except Exception as error:
                    succeeded = False
                    self._queue_incident(
                        task=task,
                        failure_kind="exception",
                        message=f"{type(error).__name__}: {error}",
                        expected="任务无异常完成并返回成功结果",
                        observed="任务抛出异常",
                        traceback_text=traceback.format_exc(),
                        context={"task_index": index, "task_total": len(self.tasks)},
                    )
                    logger.exception(f"任务执行失败: {task.name}")
                    action = "已暂停后续任务并保存现场" if self.halt_on_failure else "已跳过并继续后续任务"
                    self.error.emit(f"{task.name}执行失败，{action}")
                self.taskFinished.emit(task.name, succeeded)
                if succeeded:
                    self.taskResult.emit(task.name, result)
                self.taskCompleted.emit(task, succeeded, result)
                if not succeeded and not self._stop_requested and self.halt_on_failure:
                    self._halted_for_repair = True
                    logger.warning(
                        f"已暂停本批后续任务，等待 Codex 对 {task.name} 的失败现场进行隔离诊断"
                    )
                    break
        finally:
            self._current = None
            if lifecycle_entered and self.lifecycle is not None:
                try:
                    self.lifecycle.cleanup()
                except Exception as error:
                    # Cleanup is resource hygiene, not the business result.  A
                    # successful daily task must not run twice because closing
                    # the idle game process happened to fail.
                    logger.exception("任务队列结束后的游戏资源清理失败")
                    self._queue_incident(
                        failure_kind="resource_cleanup_error",
                        message=f"{type(error).__name__}: {error}",
                        expected="任务结束后释放游戏与模拟器资源",
                        observed="资源清理抛出异常",
                        traceback_text=traceback.format_exc(),
                    )
                    self.error.emit("任务已结束，但关闭游戏或模拟器失败，请查看日志")
            self.queueChanged.emit([])
            self._flush_incidents()

    def _queue_incident(
        self,
        *,
        failure_kind: str,
        message: str,
        expected: str,
        observed: str,
        task: QueuedTask | None = None,
        traceback_text: str = "",
        context: dict | None = None,
    ) -> None:
        self._pending_incidents.append(
            {
                "source": "task_queue",
                "task_key": task.key if task else "task_queue",
                "task_name": task.name if task else "任务队列",
                "failure_kind": failure_kind,
                "message": message,
                "expected": expected,
                "observed": observed,
                "traceback": traceback_text,
                "context": {
                    "batch_id": self._incident_batch_id,
                    **(context or {}),
                },
            }
        )

    def _flush_incidents(self) -> None:
        pending, self._pending_incidents = self._pending_incidents, []
        if self.incident_reporter is None:
            return
        cleanup_failed = any(
            incident["failure_kind"] == "resource_cleanup_error"
            for incident in pending
        )
        dispatch_claimed = False
        for incident in pending:
            dispatch_allowed = (
                not cleanup_failed
                and not dispatch_claimed
                and incident["failure_kind"] != "resource_cleanup_error"
            )
            incident["context"]["dispatch_allowed"] = dispatch_allowed
            dispatch_claimed = dispatch_claimed or dispatch_allowed
            try:
                self.incident_reporter(incident)
            except Exception as error:  # noqa: BLE001 - reporting must not crash the queue
                logger.warning(f"记录 Codex 自愈事故失败: {type(error).__name__}: {error}")

    @staticmethod
    def _safe_observed(value: object, limit: int = 2000) -> str:
        try:
            text = repr(value)
        except Exception as error:  # noqa: BLE001 - failure evidence is best effort
            text = f"<repr failed: {type(error).__name__}: {error}>"
        if len(text) <= limit:
            return text
        return f"{text[:limit]}... <truncated {len(text) - limit} chars>"

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

    @property
    def halted_for_repair(self) -> bool:
        return self._halted_for_repair
