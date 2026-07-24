from __future__ import annotations

from types import SimpleNamespace

import pytest

from core.control.adb_port import EmulatorInfo, EmulatorType
from core.services.emulator_lifecycle import (
    EmulatorInstanceState,
    EmulatorLifecycle,
    LifecycleError,
    LifecycleOptions,
)
from core.services.personal_action_budget import EpisodeActionBudget
from core.services.personal_runtime_episode import (
    ActionExecutor,
    ActionPlanner,
    CoordinateTransform,
    EpisodePolicy,
    PersonalAutomationEpisode,
    ResourceUpdateHandler,
    ResourceUpdatePolicy,
    RuntimeAction,
    RuntimeState,
    StateDetector,
)


def test_resource_update_policy_accepts_range_and_optional_exact_allowlist():
    ranged = ResourceUpdatePolicy(minimum_size_mb=10, maximum_size_mb=30)
    assert ranged.allows(25.25)
    assert not ranged.allows(9.99)
    assert not ranged.allows(30.01)
    exact = ResourceUpdatePolicy(allowed_exact_sizes_mb=(25.25,))
    assert exact.allows(25.25)
    assert not exact.allows(25.5)


def test_resource_update_policy_disabled_is_observation_only():
    detected = StateDetector().detect(resource_update_frame(size="48.5MB"))
    plan = ResourceUpdateHandler(
        ResourceUpdatePolicy(auto_confirm_enabled=False)
    ).plan(detected, EpisodeActionBudget())
    assert plan.action is RuntimeAction.OBSERVE_ONLY
    assert plan.reason == "resource_update_auto_confirm_disabled"


def test_resource_update_policy_rejects_invalid_configuration():
    with pytest.raises(ValueError, match="resource_update_policy_invalid"):
        ResourceUpdateHandler(ResourceUpdatePolicy(minimum_size_mb=30, maximum_size_mb=10))


def test_default_episode_observation_limit_covers_full_resource_timeout():
    policy = EpisodePolicy(
        observation_interval_seconds=0.75,
        resource_update_timeout_seconds=600,
    )
    assert policy.observation_limit() * policy.observation_interval_seconds >= 600


def test_nonpositive_observation_interval_is_rejected():
    with pytest.raises(ValueError, match="episode_observation_policy_invalid"):
        EpisodePolicy(observation_interval_seconds=0).validate()
from tests.personal_runtime_fixtures import (
    Frame,
    city_frame,
    home_frame,
    item,
    resource_update_frame,
    unknown_frame,
)


class Clock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class BootAdb:
    def factory(self, _host, *, port, **_kwargs):
        class Device:
            def connect(self, **_kwargs):
                return True

            def shell(self, command, **_kwargs):
                return "1" if command == "getprop sys.boot_completed" else ""

            def close(self):
                pass

        return Device()


class SequenceManager:
    def __init__(self, states):
        self.states = list(states)
        self.current = self.states[0]
        self.launches = 0
        self.device = None

    def info(self):
        if self.states:
            self.current = self.states.pop(0)
        return dict(self.current)

    def launch_emulator(self):
        self.launches += 1


def lifecycle_for(states, *, clock=None, **options):
    clock = clock or Clock()
    device = EmulatorInfo(
        name="雷索纳斯",
        port=16384,
        path=r"C:\Program Files\NetEase\MuMu",
        type=EmulatorType.MUMUV5,
        index=0,
    )
    manager = SequenceManager(states)
    manager.device = device
    lifecycle = EmulatorLifecycle(
        device,
        manager=manager,
        adb_factory=BootAdb().factory,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
        options=LifecycleOptions(
            poll_interval=0.1,
            stopping_settle_timeout=options.get("stopping_settle_timeout", 1.0),
            android_boot_timeout=options.get("android_boot_timeout", 1.0),
        ),
    )
    return lifecycle, manager


def state(player_state, *, process=True, android=False):
    return {
        "index": "0",
        "name": "雷索纳斯",
        "player_state": player_state,
        "is_process_started": process,
        "is_android_started": android,
        "adb_port": 16384 if process else None,
    }


def test_stopping_does_not_dispatch_launch_and_times_out_bounded():
    lifecycle, manager = lifecycle_for(
        [state("stopping")], stopping_settle_timeout=0
    )
    with pytest.raises(LifecycleError, match="stopping 状态收敛超时"):
        lifecycle.ensure_emulator_ready()
    assert manager.launches == lifecycle.emulator_launch_dispatches == 0


def test_stopping_to_stopped_dispatches_exactly_one_launch():
    lifecycle, manager = lifecycle_for(
        [
            state("stopping"),
            state("stopped", process=True),
            state("starting"),
            state("start_finished", android=True),
        ]
    )
    lifecycle.ensure_emulator_ready()
    assert manager.launches == lifecycle.emulator_launch_dispatches == 1
    assert [entry["state"] for entry in lifecycle.instance_state_history] == [
        EmulatorInstanceState.STOPPING.value,
        EmulatorInstanceState.STOPPED.value,
        EmulatorInstanceState.STARTING.value,
        EmulatorInstanceState.ANDROID_READY.value,
    ]


def test_stopping_to_starting_to_ready_never_launches():
    lifecycle, manager = lifecycle_for(
        [
            state("stopping"),
            state("starting"),
            state("start_finished", android=True),
        ]
    )
    lifecycle.ensure_emulator_ready()
    assert manager.launches == lifecycle.emulator_launch_dispatches == 0


def test_android_ready_never_launches():
    lifecycle, manager = lifecycle_for([state("start_finished", android=True)])
    lifecycle.ensure_emulator_ready()
    assert manager.launches == 0


def test_real_resource_update_copy_is_detected_with_structured_fields():
    detected = StateDetector().detect(resource_update_frame())
    assert detected.state is RuntimeState.RESOURCE_UPDATE_REQUIRED
    assert detected.resource_size_mb == pytest.approx(25.25)
    assert detected.resource_confirm_bbox == (643, 491, 691, 521)
    assert detected.resource_progress_percent == 0
    assert "confirm_button_match_count=1" in detected.evidence


@pytest.mark.parametrize(
    "frame",
    [
        Frame(
            resource_update_frame().image,
            [item("确认", (643, 491, 691, 521)), item("0%", (1211, 601, 1241, 622))],
        ),
        resource_update_frame(extra_texts=("领取奖励",)),
        resource_update_frame(extra_texts=("购买商品",)),
        resource_update_frame(confirmations=2),
    ],
)
def test_ambiguous_or_risky_confirm_is_not_resource_update(frame):
    detected = StateDetector().detect(frame)
    assert detected.state is RuntimeState.UNKNOWN
    assert ActionPlanner().plan(
        detected, budget=EpisodeActionBudget()
    ).action is RuntimeAction.OBSERVE_ONLY


def test_download_progress_is_observation_only():
    detected = StateDetector().detect(resource_update_frame(progress="18%", confirmations=0))
    plan = ActionPlanner().plan(detected, budget=EpisodeActionBudget())
    assert detected.state is RuntimeState.RESOURCE_UPDATE_DOWNLOADING
    assert plan.action is RuntimeAction.OBSERVE_ONLY


@pytest.mark.parametrize(
    "transfer_text",
    (
        "[13.2%]9/13(3.34MB/25.25M)2.87MB/s",
        "[74.8%]11/13(18.89MB/25.25MB)4.18MB/s",
    ),
)
def test_real_download_transfer_signature_is_detected_without_prompt_text(
    transfer_text,
):
    frame = Frame(
        resource_update_frame().image,
        [
            item(transfer_text, (40, 600, 379, 622)),
            item(transfer_text.split("%", 1)[0].lstrip("[") + "%", (1185, 600, 1243, 622)),
            item("App:1.7.2", (36, 633, 127, 655)),
        ],
    )
    detected = StateDetector().detect(frame)
    assert detected.state is RuntimeState.RESOURCE_UPDATE_DOWNLOADING
    assert detected.resource_size_mb == pytest.approx(25.25)
    assert ActionPlanner().plan(
        detected, budget=EpisodeActionBudget()
    ).action is RuntimeAction.OBSERVE_ONLY


def test_live_handler_rejects_unapproved_resource_size():
    detected = StateDetector().detect(resource_update_frame(size="30.00MB"))
    plan = ActionPlanner(
        ResourceUpdateHandler(authorized_resource_size_mb=25.25)
    ).plan(detected, budget=EpisodeActionBudget())
    assert detected.state is RuntimeState.RESOURCE_UPDATE_REQUIRED
    assert plan.action is RuntimeAction.OBSERVE_ONLY
    assert plan.reason == "resource_update_size_not_authorized"


def run_episode(frames, *, policy=None, recover=None):
    sequence = iter(frames)
    clicks = []
    clock = Clock()
    budget = EpisodeActionBudget(clock=clock.monotonic)
    episode = PersonalAutomationEpisode(
        frame_provider=lambda: next(sequence),
        detector=StateDetector(),
        planner=ActionPlanner(),
        executor=ActionExecutor(lambda point: clicks.append(point) or True),
        transform_provider=lambda detected: CoordinateTransform(
            detected.frame_dimensions,
            (853, 480),
            detected.frame_dimensions,
            (0, 0),
        ),
        budget=budget,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
        policy=policy or EpisodePolicy(maximum_observations=len(frames)),
        resource_update_recover_package=recover,
    )
    return episode, budget, clicks


def test_resource_confirm_and_city_share_budget_and_confirm_once():
    episode, budget, clicks = run_episode(
        [
            resource_update_frame(),
            resource_update_frame(),
            resource_update_frame(progress="20%", confirmations=0),
            home_frame(),
            city_frame(),
        ]
    )
    result = episode.run()
    assert result.status == "PASS"
    assert len(clicks) == 2
    assert budget.actions_by_action_type["CONFIRM_RESOURCE_UPDATE"] == 1
    assert budget.actions_by_action_type["ENTER_CITY"] == 1
    assert clicks[0] == (666, 506)


def test_read_only_preflight_plan_does_not_consume_resource_confirmation():
    frame = resource_update_frame()
    planner = ActionPlanner()
    budget = EpisodeActionBudget()
    assert planner.plan(
        StateDetector().detect(frame), budget=budget
    ).action is RuntimeAction.CONFIRM_RESOURCE_UPDATE
    clicks = []
    episode = PersonalAutomationEpisode(
        frame_provider=iter([frame, city_frame()]).__next__,
        detector=StateDetector(),
        planner=planner,
        executor=ActionExecutor(lambda point: clicks.append(point) or True),
        transform_provider=lambda detected: CoordinateTransform(
            detected.frame_dimensions, (853, 480), detected.frame_dimensions, (0, 0)
        ),
        budget=budget,
        policy=EpisodePolicy(maximum_observations=2),
    )
    assert episode.run().status == "PASS"
    assert len(clicks) == budget.actions_by_action_type["CONFIRM_RESOURCE_UPDATE"] == 1


def test_resource_stall_stops_without_second_confirm():
    frame = resource_update_frame()
    episode, budget, clicks = run_episode(
        [frame, frame, frame, frame],
        policy=EpisodePolicy(
            maximum_observations=4,
            minimum_action_interval_seconds=1.0,
            resource_update_stall_timeout_seconds=2.0,
        ),
    )
    result = episode.run()
    assert result.reason == "resource_update_progress_stalled"
    assert len(clicks) == 1
    assert budget.actions_by_action_type["CONFIRM_RESOURCE_UPDATE"] == 1


def test_resource_update_deadline_stops_without_extra_input():
    frames = [
        resource_update_frame(),
        resource_update_frame(progress="1%", confirmations=0),
        resource_update_frame(progress="2%", confirmations=0),
        resource_update_frame(progress="3%", confirmations=0),
    ]
    episode, budget, clicks = run_episode(
        frames,
        policy=EpisodePolicy(
            maximum_observations=len(frames),
            minimum_action_interval_seconds=1.0,
            resource_update_timeout_seconds=2.5,
            resource_update_stall_timeout_seconds=10.0,
        ),
    )
    result = episode.run()
    assert result.reason == "resource_update_timeout"
    assert result.final_state is RuntimeState.RESOURCE_UPDATE_DOWNLOADING
    assert len(clicks) == 1
    assert budget.actions_by_action_type["CONFIRM_RESOURCE_UPDATE"] == 1


def test_capture_failure_after_confirm_invokes_package_recovery_once():
    frames = iter(
        [
            resource_update_frame(),
            OSError("package_restarting"),
            resource_update_frame(progress="25%", confirmations=0),
            home_frame(),
            city_frame(),
        ]
    )
    recoveries = []

    def provider():
        item_or_error = next(frames)
        if isinstance(item_or_error, Exception):
            raise item_or_error
        return item_or_error

    clock = Clock()
    budget = EpisodeActionBudget(clock=clock.monotonic)
    clicks = []
    episode = PersonalAutomationEpisode(
        frame_provider=provider,
        detector=StateDetector(),
        planner=ActionPlanner(),
        executor=ActionExecutor(lambda point: clicks.append(point) or True),
        transform_provider=lambda detected: CoordinateTransform(
            detected.frame_dimensions, (853, 480), detected.frame_dimensions, (0, 0)
        ),
        budget=budget,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
        policy=EpisodePolicy(maximum_observations=4),
        resource_update_recover_package=lambda: recoveries.append("launch_or_reuse"),
    )
    assert episode.run().status == "PASS"
    assert recoveries == ["launch_or_reuse"]
    assert budget.actions_by_action_type["CONFIRM_RESOURCE_UPDATE"] == 1


def test_package_exit_after_confirm_uses_same_budget_and_one_launch():
    frames = iter(
        [
            resource_update_frame(),
            resource_update_frame(progress="25%", confirmations=0),
            home_frame(),
            city_frame(),
        ]
    )
    health = iter([False, True, True, True])
    clock = Clock()
    budget = EpisodeActionBudget(clock=clock.monotonic)
    launches = []

    def recover():
        decision = budget.authorize(
            state="PACKAGE_LIFECYCLE",
            action_type="START_PACKAGE",
            normalized_point=None,
        )
        assert decision.allowed
        budget.record_dispatch(decision)
        budget.record_result(decision, "RUNNING")
        launches.append("package")

    episode = PersonalAutomationEpisode(
        frame_provider=frames.__next__,
        detector=StateDetector(),
        planner=ActionPlanner(),
        executor=ActionExecutor(lambda _point: True),
        transform_provider=lambda detected: CoordinateTransform(
            detected.frame_dimensions, (853, 480), detected.frame_dimensions, (0, 0)
        ),
        budget=budget,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
        policy=EpisodePolicy(maximum_observations=4),
        resource_update_recover_package=recover,
        resource_update_package_running=lambda: next(health),
    )
    assert episode.run().status == "PASS"
    assert launches == ["package"]
    assert budget.actions_by_action_type["START_PACKAGE"] == 1
    assert budget.actions_by_action_type["CONFIRM_RESOURCE_UPDATE"] == 1


def test_unknown_state_remains_zero_action():
    episode, budget, clicks = run_episode(
        [unknown_frame()], policy=EpisodePolicy(maximum_observations=1)
    )
    episode.run()
    assert clicks == []
    assert budget.total_actions == 0
