from __future__ import annotations

import importlib
from unittest.mock import patch

from app.utils.task_queue import QueuedTask, TaskQueueWorker
from auto.module import strength


def test_recoverable_error_retries_with_bounded_backoff_then_succeeds():
    runtime_errors = importlib.import_module("core.services.runtime_errors")
    attempts: list[int] = []
    backoffs: list[float] = []

    def transient_task():
        attempts.append(len(attempts) + 1)
        if len(attempts) < 3:
            raise runtime_errors.OcrUnknownError("OCR page unknown")
        return True

    worker = TaskQueueWorker(
        [
            QueuedTask(
                "transient OCR",
                transient_task,
                recoverable_retries=2,
                retry_backoff_seconds=0.25,
            )
        ],
        retry_sleep=backoffs.append,
    )

    worker.run()

    assert attempts == [1, 2, 3]
    assert backoffs == [0.25, 0.5]
    assert worker.fatal_error is None


def test_exhausted_recoverable_error_records_retryable_failure():
    runtime_errors = importlib.import_module("core.services.runtime_errors")
    incidents: list[dict] = []
    following: list[str] = []
    worker = TaskQueueWorker(
        [
            QueuedTask(
                "ADB observation",
                lambda: (_ for _ in ()).throw(
                    runtime_errors.AdbTimeoutError("ADB timed out")
                ),
                recoverable_retries=1,
                retry_backoff_seconds=0,
            ),
            QueuedTask("following", lambda: following.append("ran") or True),
        ],
        incident_reporter=incidents.append,
        retry_sleep=lambda _seconds: None,
    )

    worker.run()

    assert following == ["ran"]
    assert incidents[0]["failure_kind"] == "recoverable_automation_error"
    assert incidents[0]["context"]["attempts"] == 2


def test_unexpected_exception_converts_to_fatal_and_stops_queue():
    runtime_errors = importlib.import_module("core.services.runtime_errors")
    following: list[str] = []
    worker = TaskQueueWorker(
        [
            QueuedTask(
                "unexpected",
                lambda: (_ for _ in ()).throw(RuntimeError("schema drift")),
            ),
            QueuedTask("following", lambda: following.append("ran") or True),
        ]
    )

    worker.run()

    assert following == []
    assert isinstance(worker.fatal_error, runtime_errors.FatalAutomationError)
    assert worker.fatal_error.original_type == "RuntimeError"


def test_runtime_error_taxonomy_has_required_recoverable_subtypes():
    runtime_errors = importlib.import_module("core.services.runtime_errors")

    for subtype in (
        runtime_errors.AdbTimeoutError,
        runtime_errors.OcrUnknownError,
        runtime_errors.ScreenshotTimeoutError,
        runtime_errors.TransientConnectionError,
    ):
        assert issubclass(subtype, runtime_errors.RecoverableAutomationError)


def test_production_soda_execution_verifies_an_increase_without_overflow():
    with patch.object(
        strength, "read_strength", side_effect=[(44, 816), (94, 816)]
    ), patch.object(
        strength, "_open_fatigue_panel", return_value=True
    ), patch.object(
        strength,
        "_use_free_rest_area",
        return_value=strength.RestAreaRecovery(94, "used", 1),
    ) as use_soda, patch.object(strength, "go_home", return_value=True):
        result = strength.execute_planned_recovery_action(
            "DRINK_SODA", station_name="instance-0-station", count=1
        )

    assert result == {
        "success": True,
        "kind": "DRINK_SODA",
        "bubble_water_uses": 1,
        "before": 44,
        "after": 94,
    }
    use_soda.assert_called_once_with(
        44,
        0,
        "instance-0-station",
        maximum_fatigue=816,
        max_drinks=1,
    )


def test_headroom_trigger_fires_only_at_or_below_safe_maximum(tmp_path):
    from core.services.fatigue_triggers import (
        notify_fatigue_event,
        register_deferred_fatigue_actions,
    )

    path = tmp_path / "fatigue-headroom.json"
    register_deferred_fatigue_actions(
        [
            {
                "trigger_type": "FATIGUE_HEADROOM",
                "kind": "REPLAN",
                "fatigue_threshold": 766,
            }
        ],
        path=path,
    )
    scheduled: list[str] = []

    assert not notify_fatigue_event(
        "fatigue_threshold",
        fatigue_used=780,
        path=path,
        schedule=lambda: scheduled.append("ran"),
    )
    assert notify_fatigue_event(
        "fatigue_threshold",
        fatigue_used=766,
        path=path,
        schedule=lambda: scheduled.append("ran"),
    )
    assert scheduled == ["ran"]
