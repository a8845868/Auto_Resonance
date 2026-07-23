from __future__ import annotations

import pytest

from core.services.personal_action_budget import EpisodeActionBudget


def dispatch(budget, *, state="HOME_READY", action="ENTER_CITY", point=(1, 1)):
    decision = budget.authorize(state=state, action_type=action, normalized_point=point)
    if decision.allowed:
        budget.record_dispatch(decision)
        budget.record_result(decision, "PASS")
    return decision


def test_action_is_counted_only_after_successful_dispatch_record():
    budget = EpisodeActionBudget()
    decision = budget.authorize(state="HOME_READY", action_type="ENTER_CITY", normalized_point=(1, 2))
    assert decision.allowed
    assert budget.total_actions == 0
    budget.record_result(decision, "DISPATCH_REJECTED")
    assert budget.total_actions == 0


def test_announcement_daily_and_city_share_total_count():
    budget = EpisodeActionBudget()
    dispatch(budget, state="ANNOUNCEMENT_VISIBLE", action="DISMISS_ANNOUNCEMENT", point=(1, 1))
    dispatch(budget, state="DAILY_CHECKIN", action="DISMISS_DAILY_CHECKIN", point=(2, 2))
    dispatch(budget, state="HOME_READY", action="ENTER_CITY", point=(3, 3))
    assert budget.total_actions == 3


def test_different_handlers_cannot_repeat_same_normalized_point():
    budget = EpisodeActionBudget()
    assert dispatch(budget, state="ANNOUNCEMENT_VISIBLE", action="DISMISS_ANNOUNCEMENT").allowed
    denied = budget.authorize(state="DAILY_CHECKIN", action_type="DISMISS_DAILY_CHECKIN", normalized_point=(1, 1))
    assert not denied.allowed
    assert denied.reason_code == "same_point_click_forbidden"


def test_unknown_state_action_is_rejected():
    denied = EpisodeActionBudget().authorize(state="UNKNOWN", action_type="ENTER_CITY", normalized_point=(1, 1))
    assert not denied.allowed
    assert denied.reason_code == "unknown_state_actions_forbidden"


def test_non_pointer_lifecycle_actions_share_total_without_point_counter():
    budget = EpisodeActionBudget()
    decision = budget.authorize(
        state="PACKAGE_LIFECYCLE",
        action_type="START_PACKAGE",
        normalized_point=None,
    )
    assert decision.allowed
    budget.record_dispatch(decision)
    budget.record_result(decision, "RUNNING")
    assert budget.total_actions == 1
    assert budget.actions_by_action_type["START_PACKAGE"] == 1
    assert budget.clicks_by_normalized_point == {}


def test_more_than_two_actions_in_one_state_is_rejected():
    budget = EpisodeActionBudget()
    dispatch(budget, state="HOME_READY", action="ENTER_SESSION", point=(1, 1))
    dispatch(budget, state="HOME_READY", action="ENTER_CITY", point=(2, 2))
    denied = budget.authorize(state="HOME_READY", action_type="ENTER_CITY", normalized_point=(3, 3))
    assert denied.reason_code == "state_action_budget_exhausted"


def test_total_budget_is_ten():
    budget = EpisodeActionBudget()
    for index in range(10):
        state = f"STATE_{index}"
        assert dispatch(budget, state=state, action="RECOVERY", point=(index, index)).allowed
    denied = budget.authorize(state="STATE_10", action_type="RECOVERY", normalized_point=(10, 10))
    assert denied.reason_code == "total_action_budget_exhausted"


def test_explicit_daily_limit_is_two():
    budget = EpisodeActionBudget()
    dispatch(budget, state="DAILY_A", action="DISMISS_DAILY_CHECKIN", point=(1, 1))
    dispatch(budget, state="DAILY_B", action="DISMISS_DAILY_CHECKIN", point=(2, 2))
    denied = budget.authorize(state="DAILY_C", action_type="DISMISS_DAILY_CHECKIN", normalized_point=(3, 3))
    assert denied.reason_code == "dismiss_daily_checkin_budget_exhausted"


def test_snapshot_restores_all_executed_counters():
    budget = EpisodeActionBudget(episode_id="episode-1")
    dispatch(budget, state="HOME_READY", action="ENTER_CITY", point=(10, 20))
    restored = EpisodeActionBudget.from_snapshot(budget.snapshot())
    assert restored.episode_id == "episode-1"
    assert restored.total_actions == 1
    assert restored.actions_by_state["HOME_READY"] == 1
    assert restored.actions_by_action_type["ENTER_CITY"] == 1
    assert restored.clicks_by_normalized_point[(10, 20)] == 1
    denied = restored.authorize(state="DAILY_CHECKIN", action_type="DISMISS_DAILY_CHECKIN", normalized_point=(10, 20))
    assert denied.reason_code == "same_point_click_forbidden"


def test_duplicate_dispatch_and_result_records_are_rejected():
    budget = EpisodeActionBudget()
    decision = budget.authorize(state="HOME_READY", action_type="ENTER_CITY", normalized_point=(1, 2))
    budget.record_dispatch(decision)
    with pytest.raises(PermissionError, match="already_recorded"):
        budget.record_dispatch(decision)
    budget.record_result(decision, "PASS")
    with pytest.raises(PermissionError, match="already_recorded"):
        budget.record_result(decision, "PASS")
