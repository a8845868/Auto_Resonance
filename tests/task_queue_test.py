from app.utils.task_queue import QueuedTask, TaskQueueWorker


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
