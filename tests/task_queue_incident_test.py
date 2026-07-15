from app.utils.task_queue import QueuedTask, TaskQueueWorker


class _Lifecycle:
    def __init__(self, events):
        self.events = events

    def prepare(self, _cancelled=None):
        self.events.append("prepare")

    def cleanup(self):
        self.events.append("cleanup")


def test_incident_is_dispatched_only_after_resource_cleanup():
    events = []
    incidents = []

    def fail():
        events.append("task")
        raise RuntimeError("screen never changed")

    def report(incident):
        events.append("report")
        incidents.append(incident)

    worker = TaskQueueWorker(
        [QueuedTask("失败任务", fail, key="failure")],
        lifecycle=_Lifecycle(events),
        incident_reporter=report,
        halt_on_failure=True,
    )

    worker.run()

    assert events == ["prepare", "task", "cleanup", "report"]
    assert worker.halted_for_repair is True
    assert incidents[0]["task_key"] == "failure"
    assert incidents[0]["failure_kind"] == "exception"
    assert "RuntimeError: screen never changed" in incidents[0]["traceback"]


def test_self_healing_halt_prevents_cascading_tasks():
    completed = []
    worker = TaskQueueWorker(
        [
            QueuedTask("未确认", lambda: False, key="unconfirmed"),
            QueuedTask("不应继续", lambda: completed.append("continued") or True),
        ],
        incident_reporter=lambda _incident: None,
        halt_on_failure=True,
    )

    worker.run()

    assert completed == []
    assert worker.stop_requested is False
    assert worker.halted_for_repair is True


def test_deferred_reward_result_continues_queue_without_reporting_incident():
    completed = []
    incidents = []
    worker = TaskQueueWorker(
        [
            QueuedTask(
                "领取任务奖励",
                lambda: {"success": True, "deferred": True},
                key="reward_collection",
            ),
            QueuedTask("后续任务", lambda: completed.append("continued") or True),
        ],
        incident_reporter=incidents.append,
        halt_on_failure=True,
    )

    worker.run()

    assert completed == ["continued"]
    assert incidents == []
    assert worker.halted_for_repair is False


def test_unexpected_result_keeps_observed_value_in_incident():
    incidents = []
    worker = TaskQueueWorker(
        [QueuedTask("结果异常", lambda: {"success": False}, key="result")],
        incident_reporter=incidents.append,
    )

    worker.run()

    assert len(incidents) == 1
    incident = incidents[0]
    assert incident["source"] == "task_queue"
    assert incident["task_key"] == "result"
    assert incident["task_name"] == "结果异常"
    assert incident["failure_kind"] == "unexpected_result"
    assert incident["observed"] == "{'success': False}"
    assert incident["context"]["task_index"] == 1
    assert incident["context"]["task_total"] == 1
    assert incident["context"]["dispatch_allowed"] is True
    assert incident["context"]["batch_id"]


def test_cleanup_failure_makes_entire_batch_diagnostic_only():
    incidents = []

    class CleanupFailure(_Lifecycle):
        def cleanup(self):
            self.events.append("cleanup")
            raise RuntimeError("cleanup failed")

    worker = TaskQueueWorker(
        [QueuedTask("失败任务", lambda: False, key="failure")],
        lifecycle=CleanupFailure([]),
        incident_reporter=incidents.append,
        halt_on_failure=True,
    )

    worker.run()

    assert [item["failure_kind"] for item in incidents] == [
        "unexpected_result",
        "resource_cleanup_error",
    ]
    assert [item["context"]["dispatch_allowed"] for item in incidents] == [
        False,
        False,
    ]
