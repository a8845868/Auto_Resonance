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
    RuntimeAction,
    RuntimeState,
    StateDetector,
)
from tests.personal_runtime_fixtures import (
    announcement_frame,
    city_frame,
    daily_frame,
    home_frame,
    item,
    resource_update_complete_frame,
    unknown_frame,
)


@pytest.mark.parametrize(
    ("download_text", "enter_text"),
    [
        ("下载已经完成", "点击任意位置进入游戏"),
        ("下载完成", "点击屏幕进入游戏"),
        ("下载完成", "触碰任意位置进入游戏"),
    ],
)
def test_complete_and_enter_texts_together_detect_resource_complete(
    download_text, enter_text
):
    detected = StateDetector().detect(
        resource_update_complete_frame(
            download_text=download_text,
            enter_text=enter_text,
        )
    )
    assert detected.state is RuntimeState.RESOURCE_UPDATE_COMPLETE_TAP_TO_ENTER
    assert detected.download_complete_text == download_text
    assert detected.tap_to_enter_text == enter_text
    assert detected.confidence == 1.0
    assert detected.reason_codes == (
        "DOWNLOAD_COMPLETE_TEXT_MATCH=YES",
        "TAP_ANYWHERE_TO_ENTER_TEXT_MATCH=YES",
        "RISK_CUES_ABSENT",
    )


@pytest.mark.parametrize(
    ("download_text", "enter_text"),
    [("下载完成", ""), ("", "点击任意位置进入游戏")],
)
def test_partial_resource_complete_evidence_never_authorizes_center_click(
    download_text, enter_text
):
    detected = StateDetector().detect(
        resource_update_complete_frame(
            download_text=download_text,
            enter_text=enter_text,
        )
    )
    plan = ActionPlanner().plan(detected, budget=EpisodeActionBudget())
    assert detected.state is RuntimeState.UNKNOWN
    assert plan.action is RuntimeAction.OBSERVE_ONLY
    assert plan.capture_point is None


def test_unknown_reward_and_purchase_frames_do_not_use_center_strategy():
    purchase = resource_update_complete_frame()
    purchase._texts.append(item("购买确认", (500, 400, 700, 440)))
    for frame in (unknown_frame(), daily_frame(), purchase):
        detected = StateDetector().detect(frame)
        plan = ActionPlanner().plan(detected, budget=EpisodeActionBudget())
        assert plan.action is not RuntimeAction.ENTER_AFTER_RESOURCE_UPDATE


@pytest.mark.parametrize(
    ("capture_size", "client_size", "expected_capture", "expected_client"),
    [
        ((1280, 720), (1280, 720), (640, 360), (640, 360)),
        ((851, 480), (1280, 720), (425, 240), (640, 360)),
        ((853, 480), (1600, 900), (426, 240), (800, 450)),
    ],
)
def test_resource_complete_center_uses_current_geometry(
    capture_size, client_size, expected_capture, expected_client
):
    frame = resource_update_complete_frame(
        width=capture_size[0], height=capture_size[1]
    )
    detected = StateDetector().detect(frame)
    plan = ActionPlanner().plan(detected, budget=EpisodeActionBudget())
    mapping = CoordinateTransform(
        capture_size,
        (853, 480),
        client_size,
        (100, 200),
    ).map_render_client_center(plan.capture_point)
    assert plan.capture_point == expected_capture
    assert mapping.render_client_point == expected_client
    assert mapping.screen_point == (100 + expected_client[0], 200 + expected_client[1])
    assert mapping.normalized_unit_point == (0.5, 0.5)


def _run(frames):
    sequence = iter(frames)
    clicks = []
    clock = SimpleNamespace(now=0.0)

    def sleep(seconds):
        clock.now += seconds

    budget = EpisodeActionBudget(clock=lambda: clock.now)
    episode = PersonalAutomationEpisode(
        frame_provider=sequence.__next__,
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
        sleep=sleep,
        monotonic=lambda: clock.now,
        policy=EpisodePolicy(maximum_observations=len(frames)),
    )
    return episode.run(), budget, clicks


def test_resource_complete_entry_uses_shared_budget_once_then_announcement():
    result, budget, clicks = _run(
        [
            resource_update_complete_frame(),
            announcement_frame(),
            home_frame(),
            city_frame(),
        ]
    )
    assert result.status == "PASS"
    assert result.final_state is RuntimeState.CITY_DETAIL
    assert clicks[0] == (640, 360)
    assert budget.actions_by_action_type["ENTER_AFTER_RESOURCE_UPDATE"] == 1
    assert budget.actions_by_action_type["DISMISS_ANNOUNCEMENT"] == 1
    assert budget.actions_by_action_type["ENTER_CITY"] == 1
    assert len(clicks) == budget.total_actions == 3


def test_resource_complete_unknown_transition_is_zero_input_then_home_city():
    result, budget, clicks = _run(
        [
            resource_update_complete_frame(),
            unknown_frame(),
            home_frame(),
            city_frame(),
        ]
    )
    assert result.status == "PASS"
    assert len(clicks) == budget.total_actions == 2
    assert budget.actions_by_action_type["ENTER_AFTER_RESOURCE_UPDATE"] == 1
    assert budget.actions_by_state["UNKNOWN"] == 0


def test_same_resource_complete_page_never_clicks_twice():
    result, budget, clicks = _run(
        [resource_update_complete_frame(), resource_update_complete_frame()]
    )
    assert result.status == "BLOCKED"
    assert clicks == [(640, 360)]
    assert budget.actions_by_action_type["ENTER_AFTER_RESOURCE_UPDATE"] == 1


def test_pre_dispatch_identity_guard_blocks_other_instance_without_counting_action():
    budget = EpisodeActionBudget()
    episode = PersonalAutomationEpisode(
        frame_provider=lambda: resource_update_complete_frame(),
        detector=StateDetector(),
        planner=ActionPlanner(),
        executor=ActionExecutor(
            lambda _point: pytest.fail("must not dispatch"),
            pre_dispatch_guard=lambda: False,
        ),
        transform_provider=lambda detected: CoordinateTransform(
            detected.frame_dimensions,
            (853, 480),
            detected.frame_dimensions,
            (0, 0),
        ),
        budget=budget,
        policy=EpisodePolicy(maximum_observations=1),
    )
    result = episode.run()
    assert result.reason == "target_identity_guard_failed"
    assert budget.total_actions == 0


def test_city_detail_after_resource_chain_stops_without_extra_action():
    result, budget, clicks = _run(
        [resource_update_complete_frame(), city_frame()]
    )
    assert result.status == "PASS"
    assert clicks == [(640, 360)]
    assert budget.total_actions == 1
