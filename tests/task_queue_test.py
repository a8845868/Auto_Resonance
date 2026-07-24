from app.utils.task_queue import QueuedTask, TaskQueueWorker
from core.services.emulator_lifecycle import LifecycleCancelled


def test_tasks_run_in_declared_order():
    completed = []
    worker = TaskQueueWorker([
        QueuedTask("一", lambda: completed.append("一")),
        QueuedTask("二", lambda: completed.append("二")),
        QueuedTask("三", lambda: completed.append("三")),
    ])

    worker.run()

    assert completed == ["一", "二", "三"]


def test_stop_clears_remaining_queue():
    completed = []
    worker = None

    def first():
        completed.append("一")
        worker.stop()

    worker = TaskQueueWorker([
        QueuedTask("一", first, lambda: None),
        QueuedTask("二", lambda: completed.append("二")),
    ])

    worker.run()

    assert completed == ["一"]
    assert worker.stop_requested


def test_only_explicit_nonempty_results_are_completed():
    outcomes = []
    worker = TaskQueueWorker([
        QueuedTask("false", lambda: False),
        QueuedTask("none", lambda: None),
        QueuedTask("zero", lambda: 0),
        QueuedTask("true", lambda: True),
        QueuedTask("details", lambda: {"completed": 0}),
        QueuedTask("explicit-failure", lambda: {"success": False, "details": 1}),
        QueuedTask("explicit-success", lambda: {"success": True}),
        QueuedTask(
            "explicit-deferral",
            lambda: {"success": True, "deferred": True},
        ),
        QueuedTask("malformed-success-string", lambda: {"success": "false"}),
        QueuedTask("malformed-success-number", lambda: {"success": 1}),
    ])
    worker.taskCompleted.connect(
        lambda task, succeeded, result: outcomes.append(
            (task.name, succeeded, result)
        )
    )

    worker.run()

    assert [(name, succeeded) for name, succeeded, _ in outcomes] == [
        ("false", False),
        ("none", False),
        ("zero", False),
        ("true", True),
        ("details", True),
        ("explicit-failure", False),
        ("explicit-success", True),
        ("explicit-deferral", True),
        ("malformed-success-string", False),
        ("malformed-success-number", False),
    ]


def test_context_task_reuses_queue_lifecycle_and_success_continues():
    class Lifecycle:
        def prepare(self, _cancelled):
            pass

        def cleanup(self):
            pass

    lifecycle = Lifecycle()
    events = []
    worker = TaskQueueWorker(
        [
            QueuedTask(
                "startup",
                lambda: False,
                run_with_context=lambda context: events.append(
                    ("startup", context.lifecycle, context.cancelled())
                )
                or {"success": True},
                halt_queue_on_failure=True,
            ),
            QueuedTask("ordinary", lambda: events.append(("ordinary",)) or True),
        ],
        lifecycle=lifecycle,
    )
    worker.run()

    assert events == [("startup", lifecycle, False), ("ordinary",)]


def test_failed_priority_context_task_stops_ordinary_tasks():
    events = []
    worker = TaskQueueWorker(
        [
            QueuedTask(
                "startup",
                lambda: False,
                run_with_context=lambda _context: {"success": False},
                halt_queue_on_failure=True,
            ),
            QueuedTask("ordinary", lambda: events.append("ordinary") or True),
        ]
    )

    worker.run()

    assert events == []


def test_lifecycle_cancellation_is_reported_without_running_tasks():
    events = []

    class CancelledLifecycle:
        def prepare(self, _cancelled):
            raise LifecycleCancelled("cancelled")

        def cleanup(self):
            events.append("cleanup")

    worker = TaskQueueWorker(
        [QueuedTask("task", lambda: events.append("task"))],
        lifecycle=CancelledLifecycle(),
    )
    worker.error.connect(events.append)

    worker.run()

    assert "task" not in events
    assert "personal_startup_cancelled" in events
    assert "cleanup" in events


def test_lifecycle_cancellation_marks_one_shot_terminal_for_same_run():
    class CancelledLifecycle:
        def prepare(self, _cancelled):
            raise LifecycleCancelled("cancelled")

        def cleanup(self):
            pass

    task = QueuedTask(
        "startup",
        lambda: {"success": True, "terminal": True},
        one_shot_key="startup",
        requires_terminal_result=True,
    )
    worker = TaskQueueWorker(
        [task],
        lifecycle=CancelledLifecycle(),
        queue_run_id="lifecycle-cancelled",
    )

    worker.run()

    assert worker.queue_state == "CANCELLED"
    assert TaskQueueWorker.one_shot_state_for(
        "lifecycle-cancelled", "startup"
    ) == "CANCELLED"


def test_terminal_one_shot_is_consumed_before_ordinary_task_runs():
    events = []
    worker = TaskQueueWorker(
        [
            QueuedTask(
                "startup",
                lambda: False,
                run_with_context=lambda _context: events.append("startup")
                or {"success": True, "terminal": True},
                halt_queue_on_failure=True,
                one_shot_key="startup",
                requires_terminal_result=True,
            ),
            QueuedTask("ordinary", lambda: events.append("ordinary") or True),
        ],
        queue_run_id="terminal-before-handoff",
    )

    worker.run()

    assert events == ["startup", "ordinary"]
    assert worker.queue_state == "COMPLETED"
    assert TaskQueueWorker.one_shot_state_for(
        "terminal-before-handoff", "startup"
    ) == "CONSUMED"


def test_nonterminal_startup_result_fails_and_halts_queue():
    events = []
    worker = TaskQueueWorker(
        [
            QueuedTask(
                "startup",
                lambda: False,
                run_with_context=lambda _context: {"success": True, "terminal": False},
                halt_queue_on_failure=True,
                one_shot_key="startup",
                requires_terminal_result=True,
            ),
            QueuedTask("ordinary", lambda: events.append("ordinary") or True),
        ],
        queue_run_id="nonterminal-halts",
    )

    worker.run()

    assert events == []
    assert worker.queue_state == "FAILED"
    assert TaskQueueWorker.one_shot_state_for(
        "nonterminal-halts", "startup"
    ) == "FAILED"


def test_reconstructed_worker_cannot_repeat_consumed_one_shot_in_same_run():
    events = []
    task = QueuedTask(
        "startup",
        lambda: events.append("startup") or {"success": True, "terminal": True},
        one_shot_key="startup",
        requires_terminal_result=True,
    )
    TaskQueueWorker([task], queue_run_id="same-run-consumed").run()
    second = TaskQueueWorker([task], queue_run_id="same-run-consumed")

    second.run()

    assert events == ["startup"]
    assert second.queue_state == "COMPLETED"


def test_new_queue_run_can_execute_one_shot_again():
    events = []
    task = QueuedTask(
        "startup",
        lambda: events.append("startup") or {"success": True, "terminal": True},
        one_shot_key="startup",
        requires_terminal_result=True,
    )

    TaskQueueWorker([task], queue_run_id="new-run-a").run()
    TaskQueueWorker([task], queue_run_id="new-run-b").run()

    assert events == ["startup", "startup"]


def test_failed_one_shot_cannot_be_reentered_in_same_queue_run():
    events = []
    task = QueuedTask(
        "startup",
        lambda: events.append("startup") or {"success": False, "terminal": False},
        one_shot_key="startup",
        requires_terminal_result=True,
    )
    first = TaskQueueWorker([task], queue_run_id="same-run-failed")
    first.run()
    second = TaskQueueWorker([task], queue_run_id="same-run-failed")

    second.run()

    assert events == ["startup"]
    assert first.queue_state == second.queue_state == "FAILED"


def test_cancelled_one_shot_is_terminal_for_same_queue_run():
    events = []
    worker = None

    def cancel_during_task():
        events.append("startup")
        worker.stop()
        return {"success": False, "terminal": False}

    task = QueuedTask(
        "startup",
        cancel_during_task,
        one_shot_key="startup",
        requires_terminal_result=True,
    )
    worker = TaskQueueWorker([task], queue_run_id="same-run-cancelled")
    worker.run()
    second = TaskQueueWorker([task], queue_run_id="same-run-cancelled")

    second.run()

    assert events == ["startup"]
    assert worker.queue_state == second.queue_state == "CANCELLED"
