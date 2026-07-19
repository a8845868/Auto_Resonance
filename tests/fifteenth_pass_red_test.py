from __future__ import annotations

from datetime import datetime
import importlib

import pytest

from app.utils.task_queue import QueuedTask, TaskQueueWorker
from core.services.fatigue_planner import (
    FatigueSnapshot,
    SodaPriceTier,
    plan_fatigue_recovery,
)
from core.services.server_calendar import SERVER_CLOCK


def _snapshot(
    current: int,
    *,
    maximum: int = 816,
    soda: int = 0,
    bento: int = 0,
) -> FatigueSnapshot:
    return FatigueSnapshot(
        server_day_id="2026-07-20",
        observed_at=datetime(2026, 7, 20, 8, 0, tzinfo=SERVER_CLOCK.timezone),
        fatigue_used=current,
        fatigue_cap=maximum,
        current_city_id="instance-0-city",
        current_station_id="instance-0-station",
        current_amenities=frozenset({"REST_AREA"}),
        soda_uses_used=0,
        soda_uses_remaining=1 if soda else 0,
        soda_reduction_per_use=soda,
        soda_price_tiers=(SodaPriceTier(1, "FREE", 0, True),) if soda else (),
        bento_batches_available=1 if bento else 0,
        bento_total_reduction_available=bento,
        next_bento_release_at=None,
        natural_recovery_at=None,
        source_confidence="HIGH",
    )


def test_resident_driver_imports_every_production_screen_state_dependency():
    from auto import resident_activity
    from core.services import screen_state

    expected = (
        "clarity_replenish_cancel_position",
        "startup_screen_action",
        "RESOURCE_DOWNLOAD_CONFIRM_TAP",
        "RESOURCE_DOWNLOAD_WAIT_ATTEMPTS",
    )
    for name in expected:
        assert getattr(resident_activity, name) is getattr(screen_state, name)


def test_44_of_816_can_use_soda_and_bento_without_overflow():
    plan = plan_fatigue_recovery(_snapshot(44, soda=50, bento=72))

    assert [action.kind for action in plan.immediate_actions] == [
        "DRINK_SODA",
        "USE_ALL_BENTOS",
    ]


def test_780_of_816_rejects_50_point_soda_overflow():
    plan = plan_fatigue_recovery(_snapshot(780, soda=50))

    assert all(action.kind != "DRINK_SODA" for action in plan.immediate_actions)


def test_760_of_816_rejects_72_point_bento_overflow():
    plan = plan_fatigue_recovery(_snapshot(760, bento=72))

    assert all(
        action.kind != "USE_ALL_BENTOS" for action in plan.immediate_actions
    )


def test_multiple_resources_choose_highest_zero_waste_benefit_first():
    plan = plan_fatigue_recovery(_snapshot(700, soda=50, bento=72))

    assert [action.kind for action in plan.immediate_actions] == ["USE_ALL_BENTOS"]


def test_name_error_is_fatal_and_does_not_continue_task_queue():
    runtime_errors = importlib.import_module("core.services.runtime_errors")
    completed: list[str] = []
    incidents: list[dict] = []
    worker = TaskQueueWorker(
        [
            QueuedTask(
                "resident activity",
                lambda: (_ for _ in ()).throw(NameError("missing symbol")),
                key="resident_activity",
            ),
            QueuedTask("must not run", lambda: completed.append("continued") or True),
        ],
        incident_reporter=incidents.append,
    )

    worker.run()

    assert completed == []
    assert worker.halted_for_repair is True
    assert isinstance(worker.fatal_error, runtime_errors.FatalAutomationError)
    assert incidents[0]["failure_kind"] == "fatal_automation_error"


@pytest.mark.parametrize(
    "error_type",
    [ImportError, AttributeError, AssertionError, TypeError],
)
def test_builtin_programming_errors_classify_as_fatal(error_type):
    runtime_errors = importlib.import_module("core.services.runtime_errors")

    classified = runtime_errors.classify_runtime_error(error_type("broken"))

    assert isinstance(classified, runtime_errors.FatalAutomationError)
