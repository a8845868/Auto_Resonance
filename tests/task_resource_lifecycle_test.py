from app.utils.task_queue import QueuedTask, TaskQueueWorker
from core.services.emulator_lifecycle import LifecycleCancelled


class FakeLifecycle:
    def __init__(self, events, *, prepare_error=None, cleanup_error=None):
        self.events = events
        self.prepare_error = prepare_error
        self.cleanup_error = cleanup_error
        self.prepare_calls = 0
        self.cleanup_calls = 0

    def prepare(self, cancelled=None):
        self.prepare_calls += 1
        self.events.append("prepare")
        if self.prepare_error:
            raise self.prepare_error

    def cleanup(self):
        self.cleanup_calls += 1
        self.events.append("cleanup")
        if self.cleanup_error:
            raise self.cleanup_error


def test_queue_prepares_once_and_cleans_once_for_multiple_tasks():
    events = []
    lifecycle = FakeLifecycle(events)
    worker = TaskQueueWorker(
        [
            QueuedTask("一", lambda: events.append("task-1") or True),
            QueuedTask("二", lambda: events.append("task-2") or True),
        ],
        lifecycle=lifecycle,
    )

    worker.run()

    assert events == ["prepare", "task-1", "task-2", "cleanup"]
    assert lifecycle.prepare_calls == 1
    assert lifecycle.cleanup_calls == 1


def test_empty_queue_never_starts_or_closes_resources():
    events = []
    lifecycle = FakeLifecycle(events)

    TaskQueueWorker([], lifecycle=lifecycle).run()

    assert events == []


def test_task_failure_still_cleans_and_keeps_following_task_behavior():
    events = []
    lifecycle = FakeLifecycle(events)
    outcomes = []

    def fail():
        events.append("fail")
        raise RuntimeError("boom")

    worker = TaskQueueWorker(
        [
            QueuedTask("失败", fail),
            QueuedTask("继续", lambda: events.append("continue") or True),
        ],
        lifecycle=lifecycle,
    )
    worker.taskCompleted.connect(
        lambda task, succeeded, _result: outcomes.append((task.name, succeeded))
    )

    worker.run()

    assert events == ["prepare", "fail", "continue", "cleanup"]
    assert outcomes == [("失败", False), ("继续", True)]


def test_manual_stop_during_task_cleans_without_starting_remaining_tasks():
    events = []
    lifecycle = FakeLifecycle(events)
    worker = None

    def first():
        events.append("task-1")
        worker.stop()
        return True

    worker = TaskQueueWorker(
        [
            QueuedTask("一", first, lambda: events.append("stop-current")),
            QueuedTask("二", lambda: events.append("task-2") or True),
        ],
        lifecycle=lifecycle,
    )

    worker.run()

    assert events == ["prepare", "task-1", "stop-current", "cleanup"]


def test_cancel_during_prepare_cleans_and_does_not_record_startup_failure():
    events = []
    lifecycle = FakeLifecycle(events, prepare_error=LifecycleCancelled("stop"))
    outcomes = []
    worker = TaskQueueWorker(
        [QueuedTask("一", lambda: events.append("task") or True)],
        lifecycle=lifecycle,
    )
    worker.taskCompleted.connect(
        lambda task, succeeded, _result: outcomes.append((task.name, succeeded))
    )

    worker.run()

    assert events == ["prepare", "cleanup"]
    assert outcomes == []
    assert worker.stop_requested is True


def test_prepare_failure_marks_every_task_failed_for_retry_and_cleans():
    events = []
    lifecycle = FakeLifecycle(events, prepare_error=RuntimeError("offline"))
    outcomes = []
    worker = TaskQueueWorker(
        [
            QueuedTask("一", lambda: events.append("task-1") or True),
            QueuedTask("二", lambda: events.append("task-2") or True),
        ],
        lifecycle=lifecycle,
    )
    worker.taskCompleted.connect(
        lambda task, succeeded, _result: outcomes.append((task.name, succeeded))
    )

    worker.run()

    assert events == ["prepare", "cleanup"]
    assert outcomes == [("一", False), ("二", False)]


def test_cleanup_failure_does_not_change_successful_task_result():
    events = []
    lifecycle = FakeLifecycle(events, cleanup_error=RuntimeError("close failed"))
    outcomes = []
    errors = []
    worker = TaskQueueWorker(
        [QueuedTask("成功", lambda: events.append("task") or True)],
        lifecycle=lifecycle,
    )
    worker.taskCompleted.connect(
        lambda task, succeeded, _result: outcomes.append((task.name, succeeded))
    )
    worker.error.connect(errors.append)

    worker.run()

    assert events == ["prepare", "task", "cleanup"]
    assert outcomes == [("成功", True)]
    assert errors == ["任务已结束，但关闭游戏或模拟器失败，请查看日志"]
