"""Resident activity adapters for the proven-edge capability planner."""

from __future__ import annotations

from typing import Callable, Mapping

from core.services.action_summary_navigation import (
    ActionSummaryNavigator,
    observe_action_summary,
)
from core.services.claimed_daily_checkin_navigation import (
    dismiss_claimed_daily_checkin,
)
from core.services.personal_action_budget import EpisodeActionBudget
from core.services.personal_runtime_episode import RuntimeState, StateDetector
from core.services.proven_capability_navigation import (
    EdgeAdapter,
    EdgeExecutionResult,
    ProvenNavigationEdge,
)
from core.services.runtime_navigation_kernel import UiState, normalize_legacy_state


def _capture_id(frame: object, fallback: str) -> str:
    for name in ("source_capture_id", "backend_capture_id", "capture_id"):
        value = str(getattr(frame, name, "") or "")
        if value:
            return value
    return fallback


def observe_resident_navigation_state(frame: object) -> UiState:
    """Recognize the one claimed overlay before normal action-summary pages."""

    detected = StateDetector().detect(frame)
    if detected.state is RuntimeState.DAILY_CHECKIN:
        return normalize_legacy_state(
            "DAILY_CHECKIN",
            phase="PROVEN_EDGE_CAPABILITY_NAVIGATION",
            evidence=detected.evidence,
            frame_hash=detected.frame_hash,
            capture_id=_capture_id(frame, detected.frame_hash),
        )
    return observe_action_summary(frame).to_ui_state()


class ResidentActivityEdgeAdapters:
    """Delegate one graph edge to the existing single-edge-safe adapters."""

    def __init__(
        self,
        *,
        frame_provider: Callable[[], object],
        tap: Callable[..., object],
        geometry_provider: Callable[[], object],
        action_budget: EpisodeActionBudget,
        sleep: Callable[[float], None],
        cancellation: Callable[[], bool] = lambda: False,
    ) -> None:
        self.frame_provider = frame_provider
        self.tap = tap
        self.geometry_provider = geometry_provider
        self.action_budget = action_budget
        self.sleep = sleep
        self.cancellation = cancellation

    def registry(self) -> Mapping[str, EdgeAdapter]:
        return {
            "DISMISS_CLAIMED_DAILY_CHECKIN": self._dismiss_daily,
            "OPEN_ACTION_TERMINAL": self._navigate_action_summary_edge,
            "OPEN_GLOBAL_PREP": self._navigate_action_summary_edge,
            "OPEN_ACTION_SUMMARY": self._navigate_action_summary_edge,
        }

    def _dismiss_daily(
        self, edge: ProvenNavigationEdge, _state: UiState
    ) -> EdgeExecutionResult:
        before = self.action_budget.total_actions
        result = dismiss_claimed_daily_checkin(
            frame_provider=self.frame_provider,
            tap=self.tap,
            geometry_provider=self.geometry_provider,
            action_budget=self.action_budget,
            sleep=self.sleep,
            cancellation=self.cancellation,
        )
        return EdgeExecutionResult(
            result.success,
            result.reason,
            self.action_budget.total_actions - before,
            0,
            result.evidence_ids,
        )

    def _navigate_action_summary_edge(
        self, edge: ProvenNavigationEdge, _state: UiState
    ) -> EdgeExecutionResult:
        before = self.action_budget.total_actions
        navigator = ActionSummaryNavigator(
            frame_provider=self.frame_provider,
            tap=self.tap,
            geometry_provider=self.geometry_provider,
            budget=self.action_budget,
            sleep=self.sleep,
            cancellation=self.cancellation,
            stop_after_first_stage=(
                edge.action_contract_id == "OPEN_ACTION_TERMINAL"
            ),
            stop_after_global_prep_stage=(
                edge.action_contract_id == "OPEN_GLOBAL_PREP"
            ),
        )
        result = navigator.navigate()
        evidence_ids = tuple(item.attempt_id for item in result.evidences)
        return EdgeExecutionResult(
            result.success,
            result.reason,
            self.action_budget.total_actions - before,
            0,
            evidence_ids,
        )


__all__ = ["ResidentActivityEdgeAdapters", "observe_resident_navigation_state"]
