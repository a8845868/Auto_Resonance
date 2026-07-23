"""One shared action budget for a bounded personal automation episode."""

from __future__ import annotations

import time
import uuid
from collections import Counter
from dataclasses import asdict, dataclass
from typing import Callable, Mapping


@dataclass(frozen=True)
class EpisodeActionBudgetPolicy:
    max_total_reversible_ui_actions: int = 10
    max_actions_per_state: int = 2
    max_same_point_clicks: int = 1
    max_announcement_dismiss_attempts: int = 2
    max_daily_checkin_dismiss_attempts: int = 2
    max_enter_city_attempts: int = 2
    unknown_state_actions: int = 0

    def validate(self) -> None:
        expected = {
            "max_total_reversible_ui_actions": 10,
            "max_actions_per_state": 2,
            "max_same_point_clicks": 1,
            "max_announcement_dismiss_attempts": 2,
            "max_daily_checkin_dismiss_attempts": 2,
            "max_enter_city_attempts": 2,
            "unknown_state_actions": 0,
        }
        for name, value in expected.items():
            if getattr(self, name) != value:
                raise ValueError(f"personal_action_budget_policy_invalid:{name}")


@dataclass(frozen=True)
class BudgetDecision:
    decision_id: str
    allowed: bool
    reason_code: str
    state: str
    action_type: str
    normalized_point: tuple[int, int] | None


@dataclass(frozen=True)
class ActionHistoryEntry:
    sequence: int
    decision_id: str
    phase: str
    state: str
    action_type: str
    normalized_point: tuple[int, int] | None
    timestamp: float
    reason_code: str
    result: str = ""


class EpisodeActionBudget:
    """Authorize, record and restore every physical action in one episode.

    Planned actions are recorded but do not consume limits. A successful
    physical dispatch is counted exactly once by :meth:`record_dispatch`.
    """

    _ACTION_LIMIT_FIELDS = {
        "DISMISS_ANNOUNCEMENT": "max_announcement_dismiss_attempts",
        "DISMISS_DAILY_CHECKIN": "max_daily_checkin_dismiss_attempts",
        "ENTER_CITY": "max_enter_city_attempts",
    }
    _NON_POINTER_ACTIONS = frozenset(
        {"START_EMULATOR", "START_PACKAGE", "FOREGROUND_WINDOW"}
    )

    def __init__(
        self,
        *,
        policy: EpisodeActionBudgetPolicy = EpisodeActionBudgetPolicy(),
        clock: Callable[[], float] = time.monotonic,
        episode_id: str | None = None,
    ) -> None:
        policy.validate()
        self.policy = policy
        self.clock = clock
        self.episode_id = episode_id or uuid.uuid4().hex
        self.total_actions = 0
        self.actions_by_state: Counter[str] = Counter()
        self.actions_by_action_type: Counter[str] = Counter()
        self.clicks_by_normalized_point: Counter[tuple[int, int]] = Counter()
        self.last_action_timestamp: float | None = None
        self.last_state: str | None = None
        self.action_history: list[ActionHistoryEntry] = []
        self._decisions: dict[str, BudgetDecision] = {}
        self._dispatched: set[str] = set()
        self._completed: set[str] = set()

    @property
    def executed_points(self) -> frozenset[tuple[int, int]]:
        return frozenset(self.clicks_by_normalized_point)

    def _record(
        self,
        decision: BudgetDecision,
        *,
        phase: str,
        reason_code: str,
        result: str = "",
    ) -> None:
        self.action_history.append(
            ActionHistoryEntry(
                sequence=len(self.action_history) + 1,
                decision_id=decision.decision_id,
                phase=phase,
                state=decision.state,
                action_type=decision.action_type,
                normalized_point=decision.normalized_point,
                timestamp=float(self.clock()),
                reason_code=reason_code,
                result=result,
            )
        )

    def _pending_count(self, *, state: str | None = None, action: str | None = None) -> int:
        return sum(
            decision.allowed
            and decision.decision_id not in self._completed
            and decision.decision_id not in self._dispatched
            and (state is None or decision.state == state)
            and (action is None or decision.action_type == action)
            for decision in self._decisions.values()
        )

    def _pending_point_count(self, point: tuple[int, int]) -> int:
        return sum(
            decision.allowed
            and decision.normalized_point == point
            and decision.decision_id not in self._completed
            and decision.decision_id not in self._dispatched
            for decision in self._decisions.values()
        )

    def authorize(
        self,
        *,
        state: str,
        action_type: str,
        normalized_point: tuple[int, int] | None,
    ) -> BudgetDecision:
        state = str(state)
        action_type = str(action_type)
        point = (
            (int(normalized_point[0]), int(normalized_point[1]))
            if normalized_point is not None
            else None
        )
        reason = "authorized"
        if state == "UNKNOWN":
            reason = "unknown_state_actions_forbidden"
        elif point is None and action_type not in self._NON_POINTER_ACTIONS:
            reason = "action_point_missing"
        elif self.total_actions + self._pending_count() >= self.policy.max_total_reversible_ui_actions:
            reason = "total_action_budget_exhausted"
        elif self.actions_by_state[state] + self._pending_count(state=state) >= self.policy.max_actions_per_state:
            reason = "state_action_budget_exhausted"
        elif point is not None and self.clicks_by_normalized_point[point] + self._pending_point_count(point) >= self.policy.max_same_point_clicks:
            reason = "same_point_click_forbidden"
        else:
            limit_name = self._ACTION_LIMIT_FIELDS.get(action_type)
            if limit_name is not None:
                used = self.actions_by_action_type[action_type] + self._pending_count(action=action_type)
                if used >= getattr(self.policy, limit_name):
                    reason = f"{action_type.lower()}_budget_exhausted"
        decision = BudgetDecision(
            decision_id=uuid.uuid4().hex,
            allowed=reason == "authorized",
            reason_code=reason,
            state=state,
            action_type=action_type,
            normalized_point=point,
        )
        self._decisions[decision.decision_id] = decision
        self._record(
            decision,
            phase="PLANNED" if decision.allowed else "REJECTED",
            reason_code=reason,
        )
        if not decision.allowed:
            self._completed.add(decision.decision_id)
        return decision

    def record_dispatch(self, decision: BudgetDecision) -> None:
        stored = self._decisions.get(decision.decision_id)
        if stored != decision or not decision.allowed:
            raise PermissionError("budget_decision_not_authorized")
        if decision.decision_id in self._dispatched:
            raise PermissionError("budget_dispatch_already_recorded")
        if decision.decision_id in self._completed:
            raise PermissionError("budget_decision_already_completed")
        self._dispatched.add(decision.decision_id)
        self.total_actions += 1
        self.actions_by_state[decision.state] += 1
        self.actions_by_action_type[decision.action_type] += 1
        if decision.normalized_point is not None:
            self.clicks_by_normalized_point[decision.normalized_point] += 1
        self.last_action_timestamp = float(self.clock())
        self.last_state = decision.state
        self._record(decision, phase="DISPATCHED", reason_code="dispatch_recorded")

    def record_result(self, decision: BudgetDecision, result: str) -> None:
        if self._decisions.get(decision.decision_id) != decision:
            raise PermissionError("budget_decision_unknown")
        if decision.decision_id in self._completed:
            raise PermissionError("budget_result_already_recorded")
        self._completed.add(decision.decision_id)
        self._record(
            decision,
            phase="RESULT",
            reason_code="result_recorded",
            result=str(result),
        )

    def snapshot(self) -> dict[str, object]:
        return {
            "episode_id": self.episode_id,
            "policy": asdict(self.policy),
            "total_actions": self.total_actions,
            "actions_by_state": dict(self.actions_by_state),
            "actions_by_action_type": dict(self.actions_by_action_type),
            "clicks_by_normalized_point": [
                {"point": list(point), "count": count}
                for point, count in sorted(self.clicks_by_normalized_point.items())
            ],
            "last_action_timestamp": self.last_action_timestamp,
            "last_state": self.last_state,
            "action_history": [asdict(entry) for entry in self.action_history],
        }

    @classmethod
    def from_snapshot(
        cls,
        snapshot: Mapping[str, object],
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> "EpisodeActionBudget":
        policy = EpisodeActionBudgetPolicy(**dict(snapshot.get("policy", {})))
        budget = cls(policy=policy, clock=clock, episode_id=str(snapshot["episode_id"]))
        budget.total_actions = int(snapshot.get("total_actions", 0))
        budget.actions_by_state.update(dict(snapshot.get("actions_by_state", {})))
        budget.actions_by_action_type.update(
            dict(snapshot.get("actions_by_action_type", {}))
        )
        for item in snapshot.get("clicks_by_normalized_point", []):
            point = tuple(map(int, item["point"]))
            budget.clicks_by_normalized_point[point] = int(item["count"])
        raw_timestamp = snapshot.get("last_action_timestamp")
        budget.last_action_timestamp = (
            None if raw_timestamp is None else float(raw_timestamp)
        )
        raw_state = snapshot.get("last_state")
        budget.last_state = None if raw_state is None else str(raw_state)
        budget.action_history = [
            ActionHistoryEntry(**dict(item))
            for item in snapshot.get("action_history", [])
        ]
        return budget


__all__ = [
    "ActionHistoryEntry",
    "BudgetDecision",
    "EpisodeActionBudget",
    "EpisodeActionBudgetPolicy",
]
