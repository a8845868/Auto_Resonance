from types import SimpleNamespace

import pytest

from core.services.personal_action_budget import EpisodeActionBudget
from core.services.personal_runtime_episode import (
    ActionExecutor,
    ActionPlanner,
    CoordinateTransform,
    EpisodePolicy,
    PersonalAutomationEpisode,
    RuntimeAction,
    RuntimeState,
    StateDetector,
)
from tests.personal_runtime_fixtures import (
    announcement_frame,
    city_frame,
    daily_frame,
    home_frame,
    session_entry_frame,
    unknown_frame,
)


def run_episode(frames, *, budget=None):
    sequence = iter(frames)
    clicks = []
    clock = SimpleNamespace(now=0.0)

    def sleep(seconds):
        clock.now += seconds

    shared = budget or EpisodeActionBudget(clock=lambda: clock.now)
    episode = PersonalAutomationEpisode(
        frame_provider=lambda: next(sequence),
        detector=StateDetector(),
        planner=ActionPlanner(),
        executor=ActionExecutor(lambda point: clicks.append(point) or True),
        transform_provider=lambda detected: CoordinateTransform(
            detected.frame_dimensions, (853, 480), detected.frame_dimensions, (0, 0)
        ),
        budget=shared,
        sleep=sleep,
        monotonic=lambda: clock.now,
        policy=EpisodePolicy(maximum_observations=len(frames)),
    )
    return episode, shared, clicks


def test_announcement_home_city_uses_one_shared_budget():
    episode, budget, clicks = run_episode([announcement_frame(), home_frame(), city_frame()])
    result = episode.run()
    assert result.status == "PASS"
    assert result.final_state is RuntimeState.CITY_DETAIL
    assert len(clicks) == budget.total_actions == 2
    assert budget.actions_by_action_type == {
        "DISMISS_ANNOUNCEMENT": 1,
        "ENTER_CITY": 1,
    }


def test_claimed_daily_home_city_uses_one_shared_budget():
    episode, budget, clicks = run_episode([daily_frame(), home_frame(), city_frame()])
    result = episode.run()
    assert result.status == "PASS"
    assert len(clicks) == budget.total_actions == 2
    assert budget.actions_by_action_type["DISMISS_DAILY_CHECKIN"] == 1
    daily_point = clicks[0]
    assert not (174 <= daily_point[0] < 1105 and 64 <= daily_point[1] < 644)


@pytest.mark.parametrize("leading", [announcement_frame(), announcement_frame(), unknown_frame()])
def test_announcement_transition_can_continue_through_session_entry(leading):
    frames = [announcement_frame(), session_entry_frame(), home_frame(), city_frame()]
    if leading is not frames[0] and getattr(leading, "ocr")() == []:
        frames.insert(1, leading)
    episode, budget, clicks = run_episode(frames)
    result = episode.run()
    assert result.status == "PASS"
    assert budget.actions_by_action_type["ENTER_SESSION"] == 1
    assert budget.actions_by_state["UNKNOWN"] == 0


def test_ordinary_unknown_and_ambiguous_session_entry_do_not_enter_session():
    detector = StateDetector()
    planner = ActionPlanner()
    budget = EpisodeActionBudget()
    assert planner.plan(detector.detect(unknown_frame()), budget=budget).action is RuntimeAction.OBSERVE_ONLY
    detected = detector.detect(session_entry_frame(anchors=2))
    assert detected.state is RuntimeState.UNKNOWN
    assert planner.plan(detected, budget=budget).action is RuntimeAction.OBSERVE_ONLY


def test_actual_capture_reference_client_and_screen_mapping_are_explicit():
    mapped = CoordinateTransform(
        (851, 480),
        (853, 480),
        (1280, 720),
        (100, 200),
    ).map((428, 444))
    assert mapped.normalized_render_point == (429, 444)
    assert mapped.render_client_point == (644, 666)
    assert mapped.screen_point == (744, 866)


def test_unknown_transition_after_city_dispatch_is_observation_only():
    episode, budget, clicks = run_episode([home_frame(), unknown_frame(), city_frame()])
    result = episode.run()
    assert result.status == "PASS"
    assert len(clicks) == budget.total_actions == 1
    unknown_plans = [
        event for event in result.events if event["event"] == "plan" and event["state"] == "UNKNOWN"
    ]
    assert unknown_plans[0]["action"] == "OBSERVE_ONLY"


def test_city_detail_stops_without_action():
    episode, budget, clicks = run_episode([city_frame()])
    result = episode.run()
    assert result.status == "PASS"
    assert budget.total_actions == len(clicks) == 0


def test_reconstructing_detector_and_planner_reuses_budget_and_distinct_candidate():
    frame = announcement_frame()
    budget = EpisodeActionBudget()
    detected = StateDetector().detect(frame)
    first = ActionPlanner().plan(detected, budget=budget)
    normalized = CoordinateTransform((851, 480), (853, 480), (851, 480), (0, 0)).map(
        first.capture_point
    ).normalized_render_point
    decision = budget.authorize(
        state=first.state.value, action_type=first.action.value, normalized_point=normalized
    )
    budget.record_dispatch(decision)
    budget.record_result(decision, "DISPATCHED")
    second_detected = StateDetector().detect(frame)
    second = ActionPlanner().plan(second_detected, budget=budget)
    assert second.action is RuntimeAction.DISMISS_ANNOUNCEMENT
    assert second.capture_point != first.capture_point


def test_episode_requires_injected_budget_and_cannot_restart():
    with pytest.raises(TypeError, match="shared_episode_action_budget_required"):
        PersonalAutomationEpisode(
            frame_provider=lambda: city_frame(),
            detector=StateDetector(),
            planner=ActionPlanner(),
            executor=ActionExecutor(lambda _point: True),
            transform_provider=lambda detected: CoordinateTransform(
                detected.frame_dimensions, (853, 480), detected.frame_dimensions, (0, 0)
            ),
            budget=None,
        )
    episode, _, _ = run_episode([city_frame()])
    episode.run()
    with pytest.raises(RuntimeError, match="cannot_restart"):
        episode.run()


def test_dispatch_rejection_is_planned_but_not_counted():
    budget = EpisodeActionBudget()
    episode = PersonalAutomationEpisode(
        frame_provider=lambda: home_frame(),
        detector=StateDetector(),
        planner=ActionPlanner(),
        executor=ActionExecutor(lambda _point: False),
        transform_provider=lambda detected: CoordinateTransform(
            detected.frame_dimensions, (853, 480), detected.frame_dimensions, (0, 0)
        ),
        budget=budget,
        policy=EpisodePolicy(maximum_observations=1),
    )
    result = episode.run()
    assert result.status == "BLOCKED"
    assert budget.total_actions == 0
    assert [entry.phase for entry in budget.action_history] == ["PLANNED", "RESULT"]


def test_dispatch_exception_is_counted_once_and_stops_delivery_unknown():
    budget = EpisodeActionBudget()

    def delivery_unknown(_point):
        raise OSError("backend_lost_after_request")

    episode = PersonalAutomationEpisode(
        frame_provider=lambda: home_frame(),
        detector=StateDetector(),
        planner=ActionPlanner(),
        executor=ActionExecutor(delivery_unknown),
        transform_provider=lambda detected: CoordinateTransform(
            detected.frame_dimensions, (853, 480), detected.frame_dimensions, (0, 0)
        ),
        budget=budget,
        policy=EpisodePolicy(maximum_observations=1),
    )
    result = episode.run()
    assert result.status == "BLOCKED"
    assert result.reason == "action_delivery_unknown"
    assert budget.total_actions == 1
    assert budget.action_history[-1].result == "DELIVERY_UNKNOWN"


def test_state_detector_plans_expected_core_states():
    detector = StateDetector()
    planner = ActionPlanner()
    budget = EpisodeActionBudget()
    assert planner.plan(detector.detect(home_frame()), budget=budget).action is RuntimeAction.ENTER_CITY
    assert planner.plan(detector.detect(unknown_frame()), budget=budget).action is RuntimeAction.OBSERVE_ONLY
    assert planner.plan(detector.detect(city_frame()), budget=budget).action is RuntimeAction.STOP
