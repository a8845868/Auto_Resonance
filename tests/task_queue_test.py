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
        ("malformed-success-string", False),
        ("malformed-success-number", False),
    ]
