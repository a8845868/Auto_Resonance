from datetime import datetime, timedelta

from core.services.task_schedule_state import (
    completed_history,
    is_force_verify_requested,
    is_task_due,
    record_task_execution,
    request_immediate_run,
    set_next_run,
    task_timing,
    task_result_deferred,
)


def test_empty_next_run_is_due_immediately(tmp_path):
    path = tmp_path / "schedule.json"
    now = datetime(2026, 7, 12, 12, 0, 0)
    set_next_run("reward", now + timedelta(hours=1), path)
    assert not is_task_due("reward", now, path)
    set_next_run("reward", None, path)
    assert is_task_due("reward", now, path)


def test_explicit_immediate_run_requests_one_shot_live_verification(tmp_path):
    path = tmp_path / "schedule.json"
    request_immediate_run("passenger_build_monitor", path)

    assert is_task_due("passenger_build_monitor", path=path)
    assert is_force_verify_requested("passenger_build_monitor", path)

    record_task_execution(
        "passenger_build_monitor",
        "客厢连续建造监控",
        True,
        datetime(2026, 7, 15, 5, 0, 0),
        True,
        datetime(2026, 7, 15, 0, 0, 0),
        path,
    )

    assert not is_force_verify_requested("passenger_build_monitor", path)


def test_saving_a_future_run_cancels_explicit_live_verification(tmp_path):
    path = tmp_path / "schedule.json"
    request_immediate_run("passenger_build_monitor", path)

    set_next_run(
        "passenger_build_monitor",
        datetime(2026, 7, 15, 5, 10, 26),
        path,
    )

    assert not is_force_verify_requested("passenger_build_monitor", path)


def test_execution_persists_last_next_and_completed_history(tmp_path):
    path = tmp_path / "schedule.json"
    now = datetime(2026, 7, 12, 12, 0, 0)
    next_run = datetime(2026, 7, 13, 5, 0, 0)
    record_task_execution("sweep", "扫荡", True, next_run, {"利刃": 2}, now, path)
    timing = task_timing("sweep", path)
    assert timing["last_run"] == "2026-07-12T12:00:00"
    assert timing["next_run"] == "2026-07-13T05:00:00"
    assert completed_history(path)[0]["name"] == "扫荡"


def test_deferred_execution_preserves_completion_and_uses_retry_schedule(tmp_path):
    path = tmp_path / "schedule.json"
    completed_at = datetime(2026, 7, 12, 11, 0, 0)
    record_task_execution(
        "fatigue",
        "疲劳规划",
        True,
        datetime(2026, 7, 12, 12, 0, 0),
        {"success": True},
        completed_at,
        path,
    )
    deferred_at = datetime(2026, 7, 12, 12, 0, 0)
    retry_at = deferred_at + timedelta(minutes=10)
    result = {"success": True, "deferred": True, "reason": "not_ready"}

    assert task_result_deferred(result) is True
    record_task_execution(
        "fatigue",
        "疲劳规划",
        True,
        retry_at,
        result,
        deferred_at,
        path,
        deferred=True,
    )

    timing = task_timing("fatigue", path)
    assert timing["status"] == "deferred"
    assert timing["completed_at"] == "2026-07-12T11:00:00"
    assert timing["last_attempt"] == "2026-07-12T12:00:00"
    assert timing["next_run"] == "2026-07-12T12:10:00"
    assert completed_history(path)[0]["completed_at"] == ""
