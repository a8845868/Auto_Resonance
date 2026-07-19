"""Single-threaded task scheduler used by the home page."""

from dataclasses import dataclass
from datetime import datetime, timedelta
import time
import traceback
from typing import Callable
import uuid

from loguru import logger
from PySide6.QtCore import QThread, Signal

from core.control.control import reset_stop, stop
from core.exception.exceptions import StopExecution
from core.services.emulator_lifecycle import LifecycleCancelled, QueueLifecycle
from core.services.runtime_errors import (
    FatalAutomationError,
    RecoverableAutomationError,
    classify_runtime_error,
)
from core.services.task_schedule_state import next_daily_reset, task_result_succeeded


@dataclass(frozen=True)
class QueuedTask:
    name: str
    run: Callable[[], object]
    stop: Callable[[], None] = stop
    key: str = ""
    next_run_factory: Callable[[datetime], datetime] = next_daily_reset
    failure_retry_seconds: int = 600
    recoverable_retries: int = 2
    retry_backoff_seconds: float = 1.0

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
        retry_sleep: Callable[[float], None] = time.sleep,
    ):
        super().__init__(parent)
        self.tasks = tasks
        self.lifecycle = lifecycle
        self.incident_reporter = incident_reporter
        self.halt_on_failure = bool(halt_on_failure)
        self.retry_sleep = retry_sleep
        self._stop_requested = False
        self._halted_for_repair = False
        self._fatal_error: FatalAutomationError | None = None
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
                attempt = 0
                while True:
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
                        break
                    except StopExecution:
                        succeeded = False
                        self._stop_requested = True
                        break
                    except Exception as error:
                        classified = classify_runtime_error(error)
                        traceback_text = traceback.format_exc()
                        if isinstance(classified, RecoverableAutomationError):
                            if attempt < max(0, int(task.recoverable_retries)):
                                delay = max(0.0, float(task.retry_backoff_seconds)) * (
                                    2 ** attempt
                                )
                                attempt += 1
                                logger.warning(
                                    "任务暂时失败，将进行有界重试 "
                                    f"{attempt}/{task.recoverable_retries}: {task.name}; "
                                    f"{type(classified).__name__}: {classified}"
                                )
                                self.error.emit(
                                    f"{task.name}暂时失败，将在 {delay:g} 秒后重试"
                                )
                                self.retry_sleep(delay)
                                continue
                            succeeded = False
                            self._queue_incident(
                                task=task,
                                failure_kind="recoverable_automation_error",
                                message=f"{type(classified).__name__}: {classified}",
                                expected="temporary runtime failure recovers within retry budget",
                                observed="recoverable retry budget exhausted",
                                traceback_text=traceback_text,
                                context={
                                    "task_index": index,
                                    "task_total": len(self.tasks),
                                    "attempts": attempt + 1,
                                },
                            )
                            logger.exception(f"任务暂时失败且重试已耗尽: {task.name}")
                            self.error.emit(
                                f"{task.name}暂时失败且重试已耗尽，本任务等待调度重试"
                            )
                            break

                        fatal = (
                            classified
                            if isinstance(classified, FatalAutomationError)
                            else FatalAutomationError(str(classified), original=error)
                        )
                        succeeded = False
                        self._fatal_error = fatal
                        self._halted_for_repair = True
                        self._queue_incident(
                            task=task,
                            failure_kind="fatal_automation_error",
                            message=f"{fatal.original_type}: {fatal.original_message}",
                            expected="production task has valid imports, schema and call signatures",
                            observed="fatal programming/runtime defect",
                            traceback_text=traceback_text,
                            context={
                                "task_index": index,
                                "task_total": len(self.tasks),
                                "original_type": fatal.original_type,
                            },
                        )
                        logger.exception(f"任务发生致命程序错误，停止后续队列: {task.name}")
                        self.error.emit(
                            f"{task.name}发生致命程序错误，已停止后续任务并记录现场"
                        )
                        break
                self.taskFinished.emit(task.name, succeeded)
                if succeeded:
                    self.taskResult.emit(task.name, result)
                self.taskCompleted.emit(task, succeeded, result)
                if self._fatal_error is not None:
                    break
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
        from core.services.fatigue_triggers import cancel_deferred_fatigue_actions

        cancel_deferred_fatigue_actions()
        stop()
        if self._current:
            self._current.stop()

    @property
    def stop_requested(self) -> bool:
        return self._stop_requested

    @property
    def halted_for_repair(self) -> bool:
        return self._halted_for_repair

    @property
    def fatal_error(self) -> FatalAutomationError | None:
        return self._fatal_error
