"""Idempotent personal runtime facade with one injected episode budget."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Iterable

from core.services.city_entry_resolver import (
    CityEntryObservation,
    CityEntryState,
    observe_city_entry_frame,
)
from core.services.emulator_lifecycle import GAME_PACKAGE
from core.services.personal_action_budget import EpisodeActionBudget
from core.services.personal_city_target import PersonalCityTarget
from core.services.runtime_mode import RuntimeMode, RuntimeModePolicy, runtime_policy


PERSONAL_INSTANCE_INDEX = 0
MUMU_RUNTIME_FAMILY = "MuMuV5"


class PersonalRuntimeError(RuntimeError):
    pass


@dataclass(frozen=True)
class GameWindowCandidate:
    window_handle: str
    process_id: int
    instance_index: int
    package_id: str
    runtime_family: str
    title: str = ""
    render_child_handle: str = ""


def select_unique_game_window(
    candidates: Iterable[GameWindowCandidate],
    *,
    instance_index: int = PERSONAL_INSTANCE_INDEX,
    package_id: str = GAME_PACKAGE,
) -> GameWindowCandidate:
    matches = [
        candidate
        for candidate in candidates
        if candidate.instance_index == instance_index
        and candidate.package_id == package_id
        and candidate.runtime_family.casefold() == MUMU_RUNTIME_FAMILY.casefold()
        and bool(candidate.window_handle)
        and candidate.process_id > 0
    ]
    if not matches:
        raise PersonalRuntimeError("game_window_not_found")
    if len(matches) != 1:
        raise PersonalRuntimeError("game_window_ambiguous")
    return matches[0]


@dataclass(frozen=True)
class PersonalCityEntryPolicy:
    minimum_click_interval_seconds: float = 1.0
    city_detail_timeout: float = 30.0
    poll_interval: float = 0.5
    maximum_observations: int = 90
    home_reconfirmation_frames: int = 2

    def validate(self) -> None:
        if self.minimum_click_interval_seconds < 0:
            raise ValueError("minimum_click_interval_invalid")
        if self.city_detail_timeout <= 0 or self.maximum_observations < 1:
            raise ValueError("city_observation_policy_invalid")
        if self.home_reconfirmation_frames < 1:
            raise ValueError("home_reconfirmation_invalid")


@dataclass(frozen=True)
class PersonalCityEntryResult:
    state: CityEntryState
    status: str
    reason: str
    click_count: int
    observation_count: int
    last_observation: CityEntryObservation | None


class PersonalCityEntryRunner:
    """Compatibility runner; every dispatch uses the shared episode budget."""

    _BLOCKING_REASONS = frozenset(
        {
            "LOGIN_REQUIRED",
            "REWARD_PAGE_BLOCKED",
            "PURCHASE_PAGE_BLOCKED",
            "CONSUMABLE_PAGE_BLOCKED",
            "UNKNOWN_OVERLAY_BLOCKED",
            "OVERLAY_BLOCKED",
            "NPC_DIALOG_BLOCKED",
        }
    )

    def __init__(
        self,
        *,
        frame_provider: Callable[[], object],
        tap: Callable[..., object],
        budget: EpisodeActionBudget,
        normalize_point: Callable[[tuple[int, int]], tuple[int, int]],
        target: PersonalCityTarget = PersonalCityTarget(),
        policy: PersonalCityEntryPolicy = PersonalCityEntryPolicy(),
        observer: Callable[..., CityEntryObservation] = observe_city_entry_frame,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        policy.validate()
        target.validate()
        if not isinstance(budget, EpisodeActionBudget):
            raise TypeError("shared_episode_action_budget_required")
        self.frame_provider = frame_provider
        self.tap = tap
        self.budget = budget
        self.normalize_point = normalize_point
        self.target = target
        self.policy = policy
        self.observer = observer
        self.sleep = sleep
        self.monotonic = monotonic
        self._started = False

    @staticmethod
    def _center(bounds: tuple[int, int, int, int]) -> tuple[int, int]:
        return (round((bounds[0] + bounds[2]) / 2), round((bounds[1] + bounds[3]) / 2))

    def _observe(self) -> CityEntryObservation:
        return self.observer(self.frame_provider())

    def _dispatch(self, coordinate: tuple[int, int]) -> tuple[bool, str]:
        decision = self.budget.authorize(
            state="HOME_READY",
            action_type="ENTER_CITY",
            normalized_point=self.normalize_point(coordinate),
        )
        if not decision.allowed:
            return False, decision.reason_code
        try:
            result = self.tap(coordinate)
        except Exception:
            self.budget.record_dispatch(decision)
            self.budget.record_result(decision, "DELIVERY_UNKNOWN")
            raise
        if result is False:
            self.budget.record_result(decision, "DISPATCH_REJECTED")
            return False, "city_entry_dispatch_rejected"
        self.budget.record_dispatch(decision)
        self.budget.record_result(decision, "DISPATCHED")
        return True, "city_entry_dispatched"

    def run(self) -> PersonalCityEntryResult:
        if self._started:
            raise RuntimeError("city_runner_instance_cannot_restart")
        self._started = True
        started = self.monotonic()
        deadline = started + self.policy.city_detail_timeout
        observations = 1
        observation = self._observe()
        if observation.state == CityEntryState.CITY_DETAIL:
            return PersonalCityEntryResult(
                observation.state, "PASS", "city_detail_already_visible", 0, observations, observation
            )
        if observation.state != CityEntryState.CITY_ENTRY_VISIBLE or observation.anchor_bbox is None:
            return PersonalCityEntryResult(
                observation.state, "BLOCKED", observation.reason, 0, observations, observation
            )
        coordinate = self._center(observation.anchor_bbox)
        dispatched, reason = self._dispatch(coordinate)
        if not dispatched:
            return PersonalCityEntryResult(
                CityEntryState.FAILED, "BLOCKED", reason, 0, observations, observation
            )
        click_count = 1
        last_click_at = self.monotonic()
        reconfirmed_home = 0
        reconfirm_hashes: set[str] = set()
        while observations < self.policy.maximum_observations and self.monotonic() < deadline:
            self.sleep(self.policy.poll_interval)
            observation = self._observe()
            observations += 1
            if observation.state == CityEntryState.CITY_DETAIL:
                return PersonalCityEntryResult(
                    observation.state, "PASS", "city_detail_confirmed", click_count, observations, observation
                )
            if observation.reason in self._BLOCKING_REASONS:
                return PersonalCityEntryResult(
                    observation.state, "BLOCKED", observation.reason, click_count, observations, observation
                )
            if observation.state == CityEntryState.CITY_ENTRY_VISIBLE and observation.anchor_bbox:
                if observation.screenshot_hash and observation.screenshot_hash not in reconfirm_hashes:
                    reconfirm_hashes.add(observation.screenshot_hash)
                    reconfirmed_home += 1
                if (
                    reconfirmed_home >= self.policy.home_reconfirmation_frames
                    and self.monotonic() - last_click_at >= self.policy.minimum_click_interval_seconds
                ):
                    retry_coordinate = self._center(observation.anchor_bbox)
                    dispatched, reason = self._dispatch(retry_coordinate)
                    if not dispatched:
                        return PersonalCityEntryResult(
                            observation.state,
                            "BLOCKED",
                            reason,
                            click_count,
                            observations,
                            observation,
                        )
                    click_count += 1
                    last_click_at = self.monotonic()
                    reconfirmed_home = 0
                    reconfirm_hashes.clear()
                continue
            # UNKNOWN/loading is observation-only and never authorizes retry.
            reconfirmed_home = 0
            reconfirm_hashes.clear()
        return PersonalCityEntryResult(
            CityEntryState.TIMEOUT,
            "FAIL",
            "city_detail_timeout",
            click_count,
            observations,
            observation,
        )


class PersonalAutomationRuntime:
    """Idempotent instance/package/window preparation for one budget owner."""

    def __init__(
        self,
        *,
        lifecycle: object,
        window_candidates: Callable[[], Iterable[GameWindowCandidate]],
        foreground_window: Callable[[GameWindowCandidate], object],
        budget: EpisodeActionBudget,
        mode: RuntimeMode | str | None = None,
        audit_gate: Callable[[], bool] | None = None,
        home_ready_waiter: Callable[[], object] | None = None,
        target: PersonalCityTarget = PersonalCityTarget(),
    ) -> None:
        target.validate()
        if not isinstance(budget, EpisodeActionBudget):
            raise TypeError("shared_episode_action_budget_required")
        device = getattr(lifecycle, "device", None)
        if int(getattr(device, "index", -1)) != target.instance_index or not bool(
            getattr(device, "is_mumu", False)
        ):
            raise PersonalRuntimeError("lifecycle_target_identity_mismatch")
        self.lifecycle = lifecycle
        self.window_candidates = window_candidates
        self.foreground_window = foreground_window
        self.budget = budget
        self.policy: RuntimeModePolicy = runtime_policy(mode)
        self.audit_gate = audit_gate
        self.home_ready_waiter = home_ready_waiter
        self.target = target

    def _require_mode_gate(self) -> None:
        if self.policy.authority_required and (
            self.audit_gate is None or self.audit_gate() is not True
        ):
            raise PermissionError("audit_authority_gate_required")

    def _authorize_non_pointer(self, *, state: str, action_type: str):
        decision = self.budget.authorize(
            state=state,
            action_type=action_type,
            normalized_point=None,
        )
        if not decision.allowed:
            raise PersonalRuntimeError(decision.reason_code)
        return decision

    def ensure_mumu_instance_running(self) -> object:
        self._require_mode_gate()
        state_reader = getattr(self.lifecycle, "emulator_state", None)
        before = state_reader() if callable(state_reader) else None
        was_ready = bool(
            before
            and before.get("is_process_started")
            and before.get("is_android_started", True)
            and int(before.get("adb_port") or 0) > 0
        )
        options = getattr(self.lifecycle, "options", None)
        auto_start = bool(getattr(options, "auto_start_emulator", True))
        decision = None
        if not was_ready and before is not None and auto_start:
            decision = self._authorize_non_pointer(
                state="INSTANCE_LIFECYCLE", action_type="START_EMULATOR"
            )
        try:
            result = self.lifecycle.ensure_emulator_ready()
        except Exception:
            if decision is not None:
                if bool(getattr(self.lifecycle, "emulator_started_by_us", False)):
                    self.budget.record_dispatch(decision)
                    self.budget.record_result(decision, "START_DELIVERY_UNKNOWN")
                else:
                    self.budget.record_result(decision, "START_FAILED_BEFORE_DISPATCH")
            raise
        if decision is not None:
            self.budget.record_dispatch(decision)
            self.budget.record_result(decision, "READY")
        return result

    def ensure_package_running(self) -> object:
        self._require_mode_gate()
        if self.lifecycle.is_game_running():
            return getattr(self.lifecycle, "device", None)
        decision = self._authorize_non_pointer(
            state="PACKAGE_LIFECYCLE", action_type="START_PACKAGE"
        )
        try:
            self.lifecycle.start_game()
        except Exception:
            if bool(getattr(self.lifecycle, "game_started_by_us", False)):
                self.budget.record_dispatch(decision)
                self.budget.record_result(decision, "START_DELIVERY_UNKNOWN")
            else:
                self.budget.record_result(decision, "START_FAILED_BEFORE_DISPATCH")
            raise
        self.budget.record_dispatch(decision)
        if not self.lifecycle.is_game_running():
            self.budget.record_result(decision, "PACKAGE_NOT_RUNNING")
            raise PersonalRuntimeError("game_package_start_failed")
        self.budget.record_result(decision, "RUNNING")
        return getattr(self.lifecycle, "device", None)

    def ensure_game_window_available(self) -> GameWindowCandidate:
        self._require_mode_gate()
        return select_unique_game_window(
            self.window_candidates(),
            instance_index=self.target.instance_index,
            package_id=self.target.package_id,
        )

    def ensure_game_window_foreground(self) -> GameWindowCandidate:
        candidate = self.ensure_game_window_available()
        decision = self._authorize_non_pointer(
            state="WINDOW_LIFECYCLE", action_type="FOREGROUND_WINDOW"
        )
        try:
            foregrounded = self.foreground_window(candidate)
        except Exception:
            self.budget.record_dispatch(decision)
            self.budget.record_result(decision, "FOREGROUND_DELIVERY_UNKNOWN")
            raise
        if foregrounded is False:
            self.budget.record_result(decision, "FOREGROUND_REJECTED")
            raise PersonalRuntimeError("game_window_foreground_failed")
        self.budget.record_dispatch(decision)
        self.budget.record_result(decision, "FOREGROUND")
        return candidate

    def wait_for_home_ready(self) -> object:
        self._require_mode_gate()
        if self.home_ready_waiter is None:
            raise PersonalRuntimeError("home_ready_waiter_missing")
        result = self.home_ready_waiter()
        raw_state = getattr(result, "state", result)
        state = getattr(raw_state, "value", raw_state)
        if str(state) != CityEntryState.HOME_READY.value:
            raise PersonalRuntimeError(f"home_ready_not_reached:{state}")
        return result

    def prepare(self) -> GameWindowCandidate:
        self.ensure_mumu_instance_running()
        self.ensure_package_running()
        return self.ensure_game_window_foreground()

    def prepare_until_home_ready(self) -> tuple[GameWindowCandidate, object]:
        candidate = self.prepare()
        return candidate, self.wait_for_home_ready()

    def enter_city(self, runner: PersonalCityEntryRunner) -> PersonalCityEntryResult:
        self._require_mode_gate()
        if self.policy.mode is RuntimeMode.AUDIT:
            raise PermissionError("audit_mode_requires_controlled_authority_runner")
        if runner.budget is not self.budget:
            raise PermissionError("runtime_episode_budget_identity_mismatch")
        return runner.run()


__all__ = [
    "GameWindowCandidate",
    "MUMU_RUNTIME_FAMILY",
    "PERSONAL_INSTANCE_INDEX",
    "PersonalAutomationRuntime",
    "PersonalCityEntryPolicy",
    "PersonalCityEntryResult",
    "PersonalCityEntryRunner",
    "PersonalRuntimeError",
    "select_unique_game_window",
]
