"""Shared CLI/GUI product entry for one bounded personal startup episode."""

from __future__ import annotations

import ctypes
from dataclasses import dataclass
from typing import Callable

from core.services.personal_action_budget import EpisodeActionBudget
from core.services.personal_runtime_episode import (
    ActionExecutor,
    ActionPlanner,
    CoordinateTransform,
    EpisodePolicy,
    PersonalAutomationEpisode,
    ResourceUpdateHandler,
    ResourceUpdatePolicy,
    RunRecorder,
    StateDetector,
)


@dataclass(frozen=True)
class PersonalAutomationEntryConfig:
    auto_confirm_resource_update: bool = True
    minimum_resource_update_mb: float = 0.01
    maximum_resource_update_mb: float = 2048.0
    allowed_exact_resource_update_sizes_mb: tuple[float, ...] = ()
    episode_timeout_seconds: float = 120.0
    resource_update_timeout_seconds: float = 600.0
    resource_update_stall_timeout_seconds: float = 120.0
    observation_interval_seconds: float = 0.75

    def validate(self) -> None:
        ResourceUpdatePolicy(
            auto_confirm_enabled=self.auto_confirm_resource_update,
            minimum_size_mb=self.minimum_resource_update_mb,
            maximum_size_mb=self.maximum_resource_update_mb,
            allowed_exact_sizes_mb=self.allowed_exact_resource_update_sizes_mb,
            timeout_seconds=self.resource_update_timeout_seconds,
            stall_timeout_seconds=self.resource_update_stall_timeout_seconds,
        ).validate()
        EpisodePolicy(
            observation_interval_seconds=self.observation_interval_seconds,
            episode_timeout_seconds=self.episode_timeout_seconds,
            resource_update_timeout_seconds=self.resource_update_timeout_seconds,
            resource_update_stall_timeout_seconds=self.resource_update_stall_timeout_seconds,
        ).validate()


def run_personal_automation_episode(
    *,
    frame_provider: Callable[[], object],
    click: Callable[[tuple[int, int]], object],
    pre_dispatch_guard: Callable[[], bool],
    cancelled: Callable[[], bool] | None = None,
    budget: EpisodeActionBudget | None = None,
    config: PersonalAutomationEntryConfig = PersonalAutomationEntryConfig(),
    recorder: RunRecorder | None = None,
    resource_update_recover_package: Callable[[], None] | None = None,
    resource_update_package_running: Callable[[], bool] | None = None,
):
    """Run the shared episode orchestration used by CLI and GUI adapters."""

    config.validate()
    shared_budget = budget or EpisodeActionBudget()
    resource_policy = ResourceUpdatePolicy(
        auto_confirm_enabled=config.auto_confirm_resource_update,
        minimum_size_mb=config.minimum_resource_update_mb,
        maximum_size_mb=config.maximum_resource_update_mb,
        allowed_exact_sizes_mb=config.allowed_exact_resource_update_sizes_mb,
        timeout_seconds=config.resource_update_timeout_seconds,
        stall_timeout_seconds=config.resource_update_stall_timeout_seconds,
    )
    episode = PersonalAutomationEpisode(
        frame_provider=frame_provider,
        detector=StateDetector(),
        planner=ActionPlanner(ResourceUpdateHandler(resource_policy)),
        executor=ActionExecutor(click, pre_dispatch_guard=pre_dispatch_guard),
        transform_provider=lambda detected: CoordinateTransform(
            detected.frame_dimensions,
            (853, 480),
            detected.frame_dimensions,
            (0, 0),
        ),
        budget=shared_budget,
        recorder=recorder,
        policy=EpisodePolicy(
            minimum_action_interval_seconds=config.observation_interval_seconds,
            observation_interval_seconds=config.observation_interval_seconds,
            episode_timeout_seconds=config.episode_timeout_seconds,
            resource_update_timeout_seconds=config.resource_update_timeout_seconds,
            resource_update_stall_timeout_seconds=config.resource_update_stall_timeout_seconds,
        ),
        resource_update_recover_package=resource_update_recover_package,
        resource_update_package_running=resource_update_package_running,
        cancelled=cancelled,
    )
    return episode.run(), shared_budget


def _foreground_mumu_window(lifecycle: object) -> None:
    manager = getattr(lifecycle, "manager", None)
    if manager is None:
        raise RuntimeError("personal_entry_mumu_manager_required")
    info = manager.info()
    handles = {
        int(str(info.get(key) or "0"), 16)
        for key in ("main_wnd", "render_wnd")
        if str(info.get(key) or "0") != "0"
    }
    if len(handles) not in {1, 2}:
        raise RuntimeError("personal_entry_window_identity_incomplete")
    main_handle = int(str(info.get("main_wnd") or "0"), 16)
    if not main_handle:
        raise RuntimeError("personal_entry_window_identity_incomplete")
    user32 = ctypes.windll.user32
    user32.ShowWindow(main_handle, 9)
    if not user32.SetForegroundWindow(main_handle):
        raise RuntimeError("personal_entry_window_foreground_failed")


def run_personal_automation_episode_from_config(
    context,
    *,
    app_config=None,
    frame_provider: Callable[[], object] | None = None,
    click: Callable[[tuple[int, int]], object] | None = None,
    foreground_window: Callable[[object], None] | None = None,
):
    """Queue task adapter that reuses the already prepared queue lifecycle."""

    if context.lifecycle is None:
        raise RuntimeError("personal_entry_queue_lifecycle_required")
    lifecycle = getattr(context.lifecycle, "lifecycle", None)
    if lifecycle is None:
        raise RuntimeError("personal_entry_existing_lifecycle_required")
    device = getattr(lifecycle, "device", None)
    if not bool(getattr(device, "is_mumu", False)):
        raise RuntimeError("personal_entry_mumu_instance_required")
    selected_instance_index = int(getattr(device, "index", -1))

    if app_config is None:
        from app.common.config import cfg as app_config

    config = PersonalAutomationEntryConfig(
        auto_confirm_resource_update=bool(app_config.autoConfirmResourceUpdate.value),
        maximum_resource_update_mb=float(app_config.maximumResourceUpdateMb.value),
    )
    (foreground_window or _foreground_mumu_window)(lifecycle)

    if frame_provider is None or click is None:
        from core.control import control

        if not control.connect(getattr(device, "port", None)):
            raise RuntimeError("personal_entry_backend_connect_failed")
        frame_provider = frame_provider or control.screenshot
        click = click or (lambda point: control.input_tap(point, random_offset=False))

    budget = EpisodeActionBudget()

    def target_still_active() -> bool:
        return (
            not context.cancelled()
            and int(getattr(lifecycle.device, "index", -1)) == selected_instance_index
            and bool(lifecycle.is_game_running())
        )

    result, budget = run_personal_automation_episode(
        frame_provider=frame_provider,
        click=click,
        pre_dispatch_guard=target_still_active,
        cancelled=context.cancelled,
        budget=budget,
        config=config,
        resource_update_recover_package=lambda: lifecycle.start_game(context.cancelled),
        resource_update_package_running=lifecycle.is_game_running,
    )
    payload = {
        "success": result.status == "PASS",
        "status": result.status,
        "final_state": result.final_state.value,
        "reason": result.reason,
        "current_state": result.final_state.value,
        "last_action": next(
            (
                event.get("action")
                for event in reversed(result.events)
                if event.get("event") in {"execution", "plan"}
            ),
            "NONE",
        ),
        "total_action_budget": budget.snapshot(),
        "real_ui_actions": budget.total_actions,
        "observations": result.observation_count,
    }
    context.logger.info(
        "Personal startup result status={status} state={state} actions={actions} reason={reason}".format(
            status=payload["status"],
            state=payload["final_state"],
            actions=payload["real_ui_actions"],
            reason=payload["reason"],
        )
    )
    return payload


def build_personal_startup_queued_task():
    """Create the priority task without importing GUI modules or starting backends."""

    from app.utils.task_queue import QueuedTask

    return QueuedTask(
        "自动准备游戏并进入岚心城",
        lambda: False,
        key="",
        recoverable_retries=0,
        run_with_context=run_personal_automation_episode_from_config,
        halt_queue_on_failure=True,
    )


__all__ = [
    "PersonalAutomationEntryConfig",
    "build_personal_startup_queued_task",
    "run_personal_automation_episode",
    "run_personal_automation_episode_from_config",
]
