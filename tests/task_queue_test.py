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
