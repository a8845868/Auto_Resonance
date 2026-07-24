from __future__ import annotations

from types import SimpleNamespace

from app.utils.task_queue import TaskExecutionContext
from core.services.personal_automation_entry import (
    build_personal_startup_queued_task,
    run_personal_automation_episode_from_config,
)
from tests.personal_runtime_fixtures import city_frame, home_frame


class FakeLifecycle:
    def __init__(self, index=0):
        self.device = SimpleNamespace(index=index, is_mumu=True, port=16384)

    def is_game_running(self):
        return True

    def start_game(self, _cancelled=None):
        raise AssertionError("prepared queue lifecycle must not relaunch the package")


def config():
    return SimpleNamespace(
        autoConfirmResourceUpdate=SimpleNamespace(value=True),
        maximumResourceUpdateMb=SimpleNamespace(value=2048),
    )


def test_gui_entry_reuses_existing_lifecycle_and_reaches_city_detail():
    inner = FakeLifecycle()
    queue_lifecycle = SimpleNamespace(lifecycle=inner)
    context = TaskExecutionContext(
        lifecycle=queue_lifecycle,
        cancelled=lambda: False,
        logger=SimpleNamespace(info=lambda _message: None),
    )
    frames = iter([home_frame(), city_frame()])
    clicks = []
    foregrounded = []

    result = run_personal_automation_episode_from_config(
        context,
        app_config=config(),
        frame_provider=frames.__next__,
        click=lambda point: clicks.append(point) or True,
        foreground_window=lambda lifecycle: foregrounded.append(lifecycle),
    )

    assert result["success"] is True
    assert result["final_state"] == "CITY_DETAIL"
    assert result["real_ui_actions"] == 1
    assert foregrounded == [inner]
    assert len(clicks) == 1


def test_gui_entry_accepts_explicitly_selected_nonzero_mumu_instance():
    inner = FakeLifecycle(index=7)
    context = TaskExecutionContext(
        lifecycle=SimpleNamespace(lifecycle=inner),
        cancelled=lambda: False,
        logger=SimpleNamespace(info=lambda _message: None),
    )
    frames = iter([home_frame(), city_frame()])

    result = run_personal_automation_episode_from_config(
        context,
        app_config=config(),
        frame_provider=frames.__next__,
        click=lambda _point: True,
        foreground_window=lambda _lifecycle: None,
    )

    assert result["success"] is True
    assert inner.device.index == 7


def test_gui_entry_cancellation_stops_before_capture_or_input():
    inner = FakeLifecycle()
    context = TaskExecutionContext(
        lifecycle=SimpleNamespace(lifecycle=inner),
        cancelled=lambda: True,
        logger=SimpleNamespace(info=lambda _message: None),
    )
    result = run_personal_automation_episode_from_config(
        context,
        app_config=config(),
        frame_provider=lambda: (_ for _ in ()).throw(AssertionError("capture forbidden")),
        click=lambda _point: (_ for _ in ()).throw(AssertionError("input forbidden")),
        foreground_window=lambda _lifecycle: None,
    )
    assert result["success"] is False
    assert result["reason"] == "episode_cancelled"
    assert result["real_ui_actions"] == 0


def test_startup_task_is_context_bound_and_halts_queue_on_failure():
    task = build_personal_startup_queued_task()
    assert task.run_with_context is run_personal_automation_episode_from_config
    assert task.halt_queue_on_failure is True
    assert task.recoverable_retries == 0
