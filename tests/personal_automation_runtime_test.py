from types import SimpleNamespace

import pytest

from core.services.city_entry_resolver import CityEntryObservation, CityEntryState
from core.services.personal_action_budget import EpisodeActionBudget
from core.services.personal_automation_runtime import (
    GameWindowCandidate,
    PersonalAutomationRuntime,
    PersonalCityEntryPolicy,
    PersonalCityEntryRunner,
    PersonalRuntimeError,
    select_unique_game_window,
)
from core.services.runtime_mode import RuntimeMode, resolve_runtime_mode, runtime_policy


class FakeLifecycle:
    def __init__(self, *, index=0, emulator_running=True, package_running=True):
        self.device = SimpleNamespace(index=index, is_mumu=True)
        self.emulator_running = emulator_running
        self.package_running = package_running
        self.emulator_launch_count = 0
        self.game_launch_count = 0
        self.emulator_started_by_us = False

    def emulator_state(self):
        return {
            "is_process_started": self.emulator_running,
            "is_android_started": self.emulator_running,
            "adb_port": 16384 if self.emulator_running else 0,
        }

    def ensure_emulator_ready(self):
        if not self.emulator_running:
            self.emulator_launch_count += 1
            self.emulator_running = True
            self.emulator_started_by_us = True
        return self.device

    def is_game_running(self):
        return self.package_running

    def start_game(self):
        self.game_launch_count += 1
        self.package_running = True


def window(*, index=0, package="com.hermes.goda", handle="100"):
    return GameWindowCandidate(handle, 200, index, package, "MuMuV5", "game")


def runtime(lifecycle, *, budget=None, windows=None, **kwargs):
    return PersonalAutomationRuntime(
        lifecycle=lifecycle,
        window_candidates=lambda: [window()] if windows is None else windows,
        foreground_window=lambda _candidate: True,
        budget=budget or EpisodeActionBudget(),
        **kwargs,
    )


def test_instance_and_package_lifecycle_are_idempotent():
    running = FakeLifecycle()
    running_budget = EpisodeActionBudget()
    runtime(running, budget=running_budget).prepare()
    assert running.emulator_launch_count == running.game_launch_count == 0
    assert running_budget.actions_by_action_type == {"FOREGROUND_WINDOW": 1}
    stopped = FakeLifecycle(emulator_running=False, package_running=False)
    stopped_budget = EpisodeActionBudget()
    runtime(stopped, budget=stopped_budget).prepare()
    assert stopped.emulator_launch_count == stopped.game_launch_count == 1
    assert stopped_budget.total_actions == 3
    assert stopped_budget.actions_by_action_type == {
        "START_EMULATOR": 1,
        "START_PACKAGE": 1,
        "FOREGROUND_WINDOW": 1,
    }


def test_reconstructing_runtime_reuses_lifecycle_budget_and_cannot_hide_actions():
    lifecycle = FakeLifecycle(emulator_running=False, package_running=False)
    budget = EpisodeActionBudget()
    runtime(lifecycle, budget=budget).prepare()
    runtime(lifecycle, budget=budget).prepare()
    assert lifecycle.emulator_launch_count == lifecycle.game_launch_count == 1
    assert budget.actions_by_action_type["START_EMULATOR"] == 1
    assert budget.actions_by_action_type["START_PACKAGE"] == 1
    assert budget.actions_by_action_type["FOREGROUND_WINDOW"] == 2


def test_other_instances_games_and_ambiguous_windows_stop():
    with pytest.raises(PersonalRuntimeError, match="not_found"):
        select_unique_game_window([window(index=6), window(index=7)])
    with pytest.raises(PersonalRuntimeError, match="not_found"):
        select_unique_game_window([window(package="com.hypergryph.arknights")])
    with pytest.raises(PersonalRuntimeError, match="ambiguous"):
        select_unique_game_window([window(handle="1"), window(handle="2")])


def test_runtime_requires_shared_budget_and_exact_instance_zero():
    with pytest.raises(TypeError, match="shared_episode_action_budget_required"):
        runtime(FakeLifecycle(), budget="not-budget")
    with pytest.raises(PersonalRuntimeError, match="identity_mismatch"):
        runtime(FakeLifecycle(index=6))


def observation(state, frame_hash, bbox=(100, 200, 180, 240), reason="ok"):
    return CityEntryObservation(
        visible=state == CityEntryState.CITY_ENTRY_VISIBLE,
        anchor_bbox=bbox if state == CityEntryState.CITY_ENTRY_VISIBLE else None,
        confidence="HIGH",
        page_fingerprint=state.value,
        screenshot_hash=frame_hash,
        capture_id=frame_hash,
        state=state,
        evidence=(state.value,),
        back_anchor_bbox=None,
        text_count=2,
        reason=reason,
        timestamp="2026-07-24T12:00:00+08:00",
    )


class Clock:
    def __init__(self):
        self.now = 0.0

    def sleep(self, seconds):
        self.now += seconds

    def monotonic(self):
        return self.now


def runner(frames, clicks, budget, *, normalize=lambda point: point):
    sequence = iter(frames)
    clock = Clock()
    return PersonalCityEntryRunner(
        frame_provider=lambda: next(sequence),
        tap=lambda point: clicks.append(point) or True,
        budget=budget,
        normalize_point=normalize,
        observer=lambda frame: frame,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
        policy=PersonalCityEntryPolicy(maximum_observations=len(frames)),
    )


def test_city_runner_unknown_transition_never_retries():
    budget = EpisodeActionBudget()
    clicks = []
    result = runner(
        [
            observation(CityEntryState.CITY_ENTRY_VISIBLE, "home"),
            observation(CityEntryState.UNKNOWN, "unknown", reason="unknown"),
            observation(CityEntryState.CITY_DETAIL, "city"),
        ],
        clicks,
        budget,
    ).run()
    assert result.status == "PASS"
    assert clicks == [(140, 220)]
    assert budget.total_actions == 1


def test_city_runner_cannot_repeat_same_point():
    budget = EpisodeActionBudget()
    clicks = []
    result = runner(
        [
            observation(CityEntryState.CITY_ENTRY_VISIBLE, "home-0"),
            observation(CityEntryState.CITY_ENTRY_VISIBLE, "home-1"),
            observation(CityEntryState.CITY_ENTRY_VISIBLE, "home-2"),
        ],
        clicks,
        budget,
    ).run()
    assert result.status == "BLOCKED"
    assert result.reason == "same_point_click_forbidden"
    assert len(clicks) == budget.total_actions == 1


def test_cross_handler_prior_point_blocks_city_runner():
    budget = EpisodeActionBudget()
    prior = budget.authorize(
        state="ANNOUNCEMENT_VISIBLE",
        action_type="DISMISS_ANNOUNCEMENT",
        normalized_point=(140, 220),
    )
    budget.record_dispatch(prior)
    budget.record_result(prior, "PASS")
    clicks = []
    result = runner(
        [observation(CityEntryState.CITY_ENTRY_VISIBLE, "home")], clicks, budget
    ).run()
    assert result.status == "BLOCKED"
    assert result.reason == "same_point_click_forbidden"
    assert clicks == []


def test_runtime_rejects_runner_with_different_budget():
    owner = EpisodeActionBudget()
    other = EpisodeActionBudget()
    city_runner = runner(
        [observation(CityEntryState.CITY_DETAIL, "city")], [], other
    )
    with pytest.raises(PermissionError, match="budget_identity"):
        runtime(FakeLifecycle(), budget=owner).enter_city(city_runner)


def test_modes_keep_personal_default_and_audit_gate():
    assert resolve_runtime_mode(environment={}) is RuntimeMode.PERSONAL_AUTOMATION
    assert runtime_policy(RuntimeMode.PERSONAL_AUTOMATION).approval_required is False
    audit = runtime(FakeLifecycle(), mode=RuntimeMode.AUDIT)
    with pytest.raises(PermissionError, match="audit_authority_gate_required"):
        audit.ensure_mumu_instance_running()
