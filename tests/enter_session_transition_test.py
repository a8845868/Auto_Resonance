from __future__ import annotations

from types import SimpleNamespace

import pytest

from core.services.personal_action_budget import EpisodeActionBudget
from core.services.personal_runtime_episode import (
    ActionExecutor,
    ActionPlanner,
    CoordinateTransform,
    EpisodePolicy,
    PersonalAutomationEpisode,
    RuntimeState,
    StateDetector,
)
from tests.personal_runtime_fixtures import (
    announcement_frame,
    city_frame,
    daily_frame,
    home_frame,
    resource_update_frame,
    session_entry_frame,
    unknown_frame,
)


def _episode(
    frames,
    *,
    policy: EpisodePolicy | None = None,
    detector=None,
    cancelled=None,
):
    iterator = iter(frames) if not callable(frames) else None
    provider = frames if callable(frames) else iterator.__next__
    clock = SimpleNamespace(now=0.0)
    clicks = []
    budget = EpisodeActionBudget(clock=lambda: clock.now)

    def sleep(seconds):
        clock.now += seconds

    episode = PersonalAutomationEpisode(
        frame_provider=provider,
        detector=detector or StateDetector(),
        planner=ActionPlanner(target_city_id=None),
        executor=ActionExecutor(lambda point: clicks.append(point) or True),
        transform_provider=lambda detected: CoordinateTransform(
            detected.frame_dimensions,
            (853, 480),
            detected.frame_dimensions,
            (0, 0),
        ),
        budget=budget,
        policy=policy
        or EpisodePolicy(
            maximum_observations=64,
            minimum_action_interval_seconds=0.25,
            observation_interval_seconds=0.25,
        ),
        sleep=sleep,
        monotonic=lambda: clock.now,
        cancelled=cancelled,
        target_city_id=None,
    )
    return episode, budget, clicks, clock


def _transition_events(result, name):
    return [event for event in result.events if event["event"] == name]


def test_real_failure_sequence_observes_without_input_then_hands_daily_to_planner():
    episode, budget, clicks, _ = _episode(
        [
            session_entry_frame(),
            session_entry_frame(),
            unknown_frame(),
            daily_frame(),
            home_frame(),
        ]
    )

    result = episode.run()

    assert result.status == "PASS"
    assert result.final_state is RuntimeState.HOME_READY
    assert budget.actions_by_action_type == {
        "ENTER_SESSION": 1,
        "DISMISS_DAILY_CHECKIN": 1,
    }
    assert budget.actions_by_state.get("UNKNOWN", 0) == 0
    observations = _transition_events(result, "session_transition_observation")
    assert [event["state"] for event in observations] == [
        "SESSION_ENTRY",
        "UNKNOWN",
        "DAILY_CHECKIN",
    ]
    assert all(event["input_actions"] == 0 for event in observations)
    transition = _transition_events(result, "session_transition_result")[-1]
    assert transition["result"] == "PASS"
    assert transition["known_landing_state"] == "DAILY_CHECKIN"
    assert len(clicks) == budget.total_actions == 2


@pytest.mark.parametrize(
    ("landing", "expected"),
    [(home_frame(), RuntimeState.HOME_READY), (city_frame(), RuntimeState.CITY_DETAIL)],
)
def test_ready_landing_states_finish_after_one_enter_session(landing, expected):
    episode, budget, clicks, _ = _episode([session_entry_frame(), landing])

    result = episode.run()

    assert result.status == "PASS"
    assert result.final_state is expected
    assert budget.actions_by_action_type == {"ENTER_SESSION": 1}
    assert len(clicks) == 1


def test_announcement_landing_is_returned_to_planner_before_task_ready():
    episode, budget, _, _ = _episode(
        [session_entry_frame(), announcement_frame(), home_frame()]
    )

    result = episode.run()

    assert result.status == "PASS"
    assert budget.actions_by_action_type == {
        "ENTER_SESSION": 1,
        "DISMISS_ANNOUNCEMENT": 1,
    }
    transition = _transition_events(result, "session_transition_result")[-1]
    assert transition["known_landing_state"] == "ANNOUNCEMENT_VISIBLE"


def test_resource_update_landing_returns_latest_frame_to_existing_planner():
    frames = iter(
        [
            session_entry_frame(),
            resource_update_frame(),
            home_frame(),
        ]
    )
    episode, budget, _, _ = _episode(frames.__next__)

    result = episode.run()

    assert result.status == "PASS"
    assert budget.actions_by_action_type == {
        "ENTER_SESSION": 1,
        "CONFIRM_RESOURCE_UPDATE": 1,
    }
    transition = _transition_events(result, "session_transition_result")[-1]
    assert transition["known_landing_state"] == "RESOURCE_UPDATE_REQUIRED"


def test_transition_stability_is_configured_and_does_not_redispatch():
    policy = EpisodePolicy(
        maximum_observations=1,
        minimum_action_interval_seconds=0.25,
        observation_interval_seconds=0.25,
        enter_session_transition_timeout_seconds=5.0,
        enter_session_transition_stable_frames=2,
    )
    episode, budget, clicks, _ = _episode(
        [session_entry_frame(), home_frame(), home_frame()], policy=policy
    )

    result = episode.run()

    assert result.status == "PASS"
    assert result.observation_count == 3
    assert budget.actions_by_action_type == {"ENTER_SESSION": 1}
    assert len(clicks) == 1
    assert len(_transition_events(result, "session_transition_observation")) == 2


@pytest.mark.parametrize("pending_frame", [session_entry_frame(), unknown_frame()])
def test_transition_timeout_is_deadline_driven_and_has_zero_followup_input(
    pending_frame,
):
    calls = 0

    def capture():
        nonlocal calls
        calls += 1
        return session_entry_frame() if calls == 1 else pending_frame

    policy = EpisodePolicy(
        maximum_observations=1,
        minimum_action_interval_seconds=0.0,
        observation_interval_seconds=0.25,
        enter_session_transition_timeout_seconds=1.0,
    )
    episode, budget, clicks, clock = _episode(capture, policy=policy)

    result = episode.run()

    assert result.status == "BLOCKED"
    assert result.reason == "enter_session_transition_timeout"
    assert result.observation_count > policy.maximum_observations
    assert clock.now == pytest.approx(1.0)
    assert budget.actions_by_action_type == {"ENTER_SESSION": 1}
    assert len(clicks) == 1
    assert _transition_events(result, "session_transition_result")[-1]["result"] == "TIMEOUT"


def test_transition_capture_failure_is_explicit_and_never_retries_input():
    calls = 0

    def capture():
        nonlocal calls
        calls += 1
        if calls == 1:
            return session_entry_frame()
        raise OSError("capture_lost")

    episode, budget, clicks, _ = _episode(capture)

    result = episode.run()

    assert result.reason == "enter_session_transition_capture_failed"
    assert budget.actions_by_action_type == {"ENTER_SESSION": 1}
    assert len(clicks) == 1
    event = _transition_events(result, "session_transition_result")[-1]
    assert event["result"] == "CAPTURE_FAILED"
    assert event["error_type"] == "OSError"


def test_transition_detection_failure_is_explicit_and_never_retries_input():
    class FailingDetector:
        def __init__(self):
            self.calls = 0
            self.base = StateDetector()

        def detect(self, frame):
            self.calls += 1
            if self.calls > 1:
                raise ValueError("detector_failed")
            return self.base.detect(frame)

    episode, budget, clicks, _ = _episode(
        [session_entry_frame(), unknown_frame()], detector=FailingDetector()
    )

    result = episode.run()

    assert result.reason == "enter_session_transition_detection_failed"
    assert budget.actions_by_action_type == {"ENTER_SESSION": 1}
    assert len(clicks) == 1
    event = _transition_events(result, "session_transition_result")[-1]
    assert event["result"] == "DETECTION_FAILED"
    assert event["error_type"] == "ValueError"


def test_transition_cancellation_is_explicit_and_has_no_followup_input():
    budget_ref = {}

    def cancelled():
        return budget_ref["budget"].total_actions > 0

    episode, budget, clicks, _ = _episode(
        [session_entry_frame(), home_frame()], cancelled=cancelled
    )
    budget_ref["budget"] = budget

    result = episode.run()

    assert result.reason == "personal_startup_cancelled"
    assert budget.actions_by_action_type == {"ENTER_SESSION": 1}
    assert len(clicks) == 1
    assert _transition_events(result, "session_transition_result")[-1]["result"] == "CANCELLED"


def test_transition_policy_rejects_nonpositive_timeout_and_stability():
    with pytest.raises(ValueError, match="episode_observation_policy_invalid"):
        EpisodePolicy(enter_session_transition_timeout_seconds=0).validate()
    with pytest.raises(ValueError, match="episode_observation_policy_invalid"):
        EpisodePolicy(enter_session_transition_stable_frames=0).validate()
