"""In-memory planning over explicitly proven reversible navigation edges.

The graph is source-declared and immutable.  It never learns from screenshots,
UNKNOWN observations, logs, or persisted evidence.  Physical input remains in
injected adapters; this module only replans and revalidates one edge at a time.
"""

from __future__ import annotations

from collections import deque
from dataclasses import asdict, dataclass, field
from types import MappingProxyType
from typing import Callable, Iterable, Mapping

from core.services.personal_action_budget import EpisodeActionBudget
from core.services.runtime_navigation_kernel import (
    ActionContract,
    Confidence,
    DEFAULT_CAPABILITIES,
    PROVEN_NAVIGATION_CONTRACTS,
    UiState,
)


@dataclass(frozen=True, slots=True)
class ProvenNavigationEdge:
    edge_id: str
    source_base_page: str
    required_capability: str
    action_contract_id: str
    expected_target_page: str
    allowed_overlays: frozenset[str] = field(default_factory=frozenset)
    max_dispatches: int = 1
    irreversible: bool = False
    live_proof_reference: str = ""
    enabled: bool = True


@dataclass(frozen=True, slots=True)
class EdgeExecutionResult:
    success: bool
    reason: str
    physical_dispatches: int = 0
    irreversible_actions: int = 0
    evidence_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CapabilityNavigationStepResult:
    edge_id: str
    source_state: str
    expected_target_state: str
    observed_target_state: str
    success: bool
    reason: str
    physical_dispatches: int
    evidence_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CapabilityNavigationResult:
    success: bool
    terminal: bool
    target_capability: str
    initial_state: str
    final_state: str
    planned_edge_ids: tuple[str, ...]
    completed_edge_ids: tuple[str, ...]
    failed_edge_id: str | None
    physical_dispatches: int
    unknown_state_actions: int
    irreversible_actions: int
    reason: str
    step_results: tuple[CapabilityNavigationStepResult, ...] = ()
    evidence_ids: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        document = asdict(self)
        document["step_results"] = [asdict(item) for item in self.step_results]
        return document


PROVEN_NAVIGATION_EDGES = (
    ProvenNavigationEdge(
        edge_id="claimed_daily_checkin_to_home",
        source_base_page="DAILY_CHECKIN",
        required_capability="DISMISS_CLAIMED_DAILY_CHECKIN",
        action_contract_id="DISMISS_CLAIMED_DAILY_CHECKIN",
        expected_target_page="HOME_READY",
        max_dispatches=1,
        irreversible=False,
        live_proof_reference="claimed-daily-checkin-safe-blank-live-v1",
    ),
    ProvenNavigationEdge(
        edge_id="home_to_activity_overview",
        source_base_page="HOME_READY",
        required_capability="OPEN_ACTION_TERMINAL",
        action_contract_id="OPEN_ACTION_TERMINAL",
        expected_target_page="ACTIVITY_OVERVIEW_VISIBLE",
        max_dispatches=1,
        irreversible=False,
        live_proof_reference="action-terminal-first-stage-live-v1",
    ),
    ProvenNavigationEdge(
        edge_id="activity_overview_to_global_prep",
        source_base_page="ACTIVITY_OVERVIEW_VISIBLE",
        required_capability="OPEN_GLOBAL_PREP",
        action_contract_id="OPEN_GLOBAL_PREP",
        expected_target_page="GLOBAL_PREP_PAGE",
        max_dispatches=1,
        irreversible=False,
        live_proof_reference="global-prep-single-stage-live-v1",
    ),
    ProvenNavigationEdge(
        edge_id="global_prep_to_action_summary",
        source_base_page="GLOBAL_PREP_PAGE",
        required_capability="OPEN_ACTION_SUMMARY",
        action_contract_id="OPEN_ACTION_SUMMARY",
        expected_target_page="ACTION_SUMMARY_VISIBLE",
        max_dispatches=1,
        irreversible=False,
        live_proof_reference="runtime-navigation-kernel-v1-live:5b6bbb6",
    ),
)


class ProvenNavigationGraph:
    """Read-only graph containing only source-reviewed live-proven edges."""

    def __init__(
        self,
        edges: Iterable[ProvenNavigationEdge] = PROVEN_NAVIGATION_EDGES,
        *,
        contracts: Mapping[str, ActionContract] = PROVEN_NAVIGATION_CONTRACTS,
    ) -> None:
        declared = tuple(edges)
        ids = [edge.edge_id for edge in declared]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate_proven_navigation_edge")
        by_source: dict[str, list[ProvenNavigationEdge]] = {}
        for edge in declared:
            contract = contracts.get(edge.action_contract_id)
            if contract is None:
                raise ValueError(f"edge_action_contract_missing:{edge.edge_id}")
            if edge.source_base_page not in contract.allowed_pre_pages:
                raise ValueError(f"edge_source_contract_mismatch:{edge.edge_id}")
            if edge.required_capability not in contract.required_capabilities:
                raise ValueError(f"edge_capability_contract_mismatch:{edge.edge_id}")
            if edge.expected_target_page not in contract.allowed_post_pages:
                raise ValueError(f"edge_target_contract_mismatch:{edge.edge_id}")
            if edge.irreversible or contract.irreversible:
                raise ValueError(f"irreversible_edge_forbidden:{edge.edge_id}")
            if edge.max_dispatches != 1 or edge.max_dispatches > contract.max_dispatches:
                raise ValueError(f"edge_dispatch_limit_invalid:{edge.edge_id}")
            if not edge.live_proof_reference:
                raise ValueError(f"edge_live_proof_missing:{edge.edge_id}")
            by_source.setdefault(edge.source_base_page, []).append(edge)
        self._edges = declared
        self._contracts = MappingProxyType(dict(contracts))
        self._by_source = MappingProxyType({
            source: tuple(sorted(values, key=lambda item: item.edge_id))
            for source, values in by_source.items()
        })
        self._assert_acyclic()

    @property
    def edges(self) -> tuple[ProvenNavigationEdge, ...]:
        return self._edges

    def contract_for(self, edge: ProvenNavigationEdge) -> ActionContract:
        return self._contracts[edge.action_contract_id]

    def _assert_acyclic(self) -> None:
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(page: str) -> None:
            if page in visiting:
                raise ValueError("proven_navigation_graph_cycle")
            if page in visited:
                return
            visiting.add(page)
            for edge in self._by_source.get(page, ()):
                if edge.enabled:
                    visit(edge.expected_target_page)
            visiting.remove(page)
            visited.add(page)

        for source in self._by_source:
            visit(source)

    @staticmethod
    def _page_has_capability(page: str, capability: str) -> bool:
        return capability in DEFAULT_CAPABILITIES.get(page, frozenset())

    def shortest_path(
        self, source_page: str, target_capability: str
    ) -> tuple[ProvenNavigationEdge, ...] | None:
        if source_page == "UNKNOWN":
            return None
        if self._page_has_capability(source_page, target_capability):
            return ()
        queue = deque([(source_page, ())])
        visited = {source_page}
        while queue:
            page, path = queue.popleft()
            for edge in self._by_source.get(page, ()):
                if not edge.enabled:
                    continue
                candidate = path + (edge,)
                target = edge.expected_target_page
                if self._page_has_capability(target, target_capability):
                    return candidate
                if target not in visited:
                    visited.add(target)
                    queue.append((target, candidate))
        return None


EdgeAdapter = Callable[[ProvenNavigationEdge, UiState], EdgeExecutionResult]


def _result(
    *,
    success: bool,
    target_capability: str,
    initial_state: str,
    final_state: str,
    planned: tuple[str, ...],
    completed: list[str],
    failed: str | None,
    physical_dispatches: int,
    irreversible_actions: int,
    reason: str,
    steps: list[CapabilityNavigationStepResult],
    evidence_ids: list[str],
) -> CapabilityNavigationResult:
    return CapabilityNavigationResult(
        success=success,
        terminal=True,
        target_capability=target_capability,
        initial_state=initial_state,
        final_state=final_state,
        planned_edge_ids=planned,
        completed_edge_ids=tuple(completed),
        failed_edge_id=failed,
        physical_dispatches=physical_dispatches,
        unknown_state_actions=0,
        irreversible_actions=irreversible_actions,
        reason=reason,
        step_results=tuple(steps),
        evidence_ids=tuple(evidence_ids),
    )


def ensure_capability(
    target_capability: str,
    *,
    frame_provider: Callable[[], object],
    state_resolver: Callable[[object], UiState],
    adapter_registry: Mapping[str, EdgeAdapter],
    action_budget: EpisodeActionBudget,
    cancellation: Callable[[], bool] = lambda: False,
    max_steps: int = 4,
    graph: ProvenNavigationGraph | None = None,
) -> CapabilityNavigationResult:
    """Reach one target capability by revalidating one proven edge at a time."""

    graph = graph or ProvenNavigationGraph()
    target_capability = str(target_capability)
    max_steps = min(4, max(0, int(max_steps)))
    completed: list[str] = []
    steps: list[CapabilityNavigationStepResult] = []
    evidence_ids: list[str] = []
    physical_dispatches = 0
    irreversible_actions = 0
    try:
        current = state_resolver(frame_provider())
    except Exception:
        return _result(
            success=False, target_capability=target_capability,
            initial_state="UNKNOWN", final_state="UNKNOWN", planned=(),
            completed=completed, failed=None, physical_dispatches=0,
            irreversible_actions=0, reason="unknown_start_state", steps=steps,
            evidence_ids=evidence_ids,
        )
    initial_state = current.base_page
    if current.is_unknown:
        return _result(
            success=False, target_capability=target_capability,
            initial_state=initial_state, final_state=current.base_page, planned=(),
            completed=completed, failed=None, physical_dispatches=0,
            irreversible_actions=0, reason="unknown_start_state", steps=steps,
            evidence_ids=evidence_ids,
        )
    if current.overlays:
        return _result(
            success=False, target_capability=target_capability,
            initial_state=initial_state, final_state=current.base_page, planned=(),
            completed=completed, failed=None, physical_dispatches=0,
            irreversible_actions=0, reason="overlay_blocks_capability", steps=steps,
            evidence_ids=evidence_ids,
        )
    if current.has_capability(
        target_capability,
        current_capture_id=current.capture_id,
        current_frame_hash=current.frame_hash,
    ):
        return _result(
            success=True, target_capability=target_capability,
            initial_state=initial_state, final_state=current.base_page, planned=(),
            completed=completed, failed=None, physical_dispatches=0,
            irreversible_actions=0, reason="target_capability_already_present",
            steps=steps, evidence_ids=evidence_ids,
        )
    initial_path = graph.shortest_path(current.base_page, target_capability)
    if initial_path is None:
        return _result(
            success=False, target_capability=target_capability,
            initial_state=initial_state, final_state=current.base_page, planned=(),
            completed=completed, failed=None, physical_dispatches=0,
            irreversible_actions=0, reason="no_proven_path", steps=steps,
            evidence_ids=evidence_ids,
        )
    planned = tuple(edge.edge_id for edge in initial_path)
    dispatches_by_edge: dict[str, int] = {}

    while True:
        if cancellation():
            return _result(
                success=False, target_capability=target_capability,
                initial_state=initial_state, final_state=current.base_page,
                planned=planned, completed=completed, failed=None,
                physical_dispatches=physical_dispatches,
                irreversible_actions=irreversible_actions, reason="cancelled",
                steps=steps, evidence_ids=evidence_ids,
            )
        if len(completed) >= max_steps or physical_dispatches >= 4:
            return _result(
                success=False, target_capability=target_capability,
                initial_state=initial_state, final_state=current.base_page,
                planned=planned, completed=completed, failed=None,
                physical_dispatches=physical_dispatches,
                irreversible_actions=irreversible_actions,
                reason="action_budget_exhausted", steps=steps,
                evidence_ids=evidence_ids,
            )
        path = graph.shortest_path(current.base_page, target_capability)
        if path is None or not path:
            reason = (
                "target_capability_reached"
                if current.has_capability(
                    target_capability,
                    current_capture_id=current.capture_id,
                    current_frame_hash=current.frame_hash,
                )
                else "no_proven_path"
            )
            return _result(
                success=reason == "target_capability_reached",
                target_capability=target_capability, initial_state=initial_state,
                final_state=current.base_page, planned=planned,
                completed=completed, failed=None,
                physical_dispatches=physical_dispatches,
                irreversible_actions=irreversible_actions, reason=reason,
                steps=steps, evidence_ids=evidence_ids,
            )
        edge = path[0]
        contract = graph.contract_for(edge)
        try:
            fresh = state_resolver(frame_provider())
        except Exception:
            return _result(
                success=False, target_capability=target_capability,
                initial_state=initial_state, final_state=current.base_page,
                planned=planned, completed=completed, failed=edge.edge_id,
                physical_dispatches=physical_dispatches,
                irreversible_actions=irreversible_actions,
                reason="edge_precondition_failed", steps=steps,
                evidence_ids=evidence_ids,
            )
        if fresh.is_unknown:
            return _result(
                success=False, target_capability=target_capability,
                initial_state=initial_state, final_state=fresh.base_page,
                planned=planned, completed=completed, failed=edge.edge_id,
                physical_dispatches=physical_dispatches,
                irreversible_actions=irreversible_actions,
                reason="unknown_start_state", steps=steps,
                evidence_ids=evidence_ids,
            )
        if fresh.overlays and not set(fresh.overlays).issubset(edge.allowed_overlays):
            return _result(
                success=False, target_capability=target_capability,
                initial_state=initial_state, final_state=fresh.base_page,
                planned=planned, completed=completed, failed=edge.edge_id,
                physical_dispatches=physical_dispatches,
                irreversible_actions=irreversible_actions,
                reason="overlay_blocks_capability", steps=steps,
                evidence_ids=evidence_ids,
            )
        same_capture = (
            fresh.capture_id == current.capture_id
            if fresh.capture_id and current.capture_id
            else bool(fresh.frame_hash and fresh.frame_hash == current.frame_hash)
        )
        if same_capture:
            return _result(
                success=False, target_capability=target_capability,
                initial_state=initial_state, final_state=fresh.base_page,
                planned=planned, completed=completed, failed=edge.edge_id,
                physical_dispatches=physical_dispatches,
                irreversible_actions=irreversible_actions, reason="stale_state",
                steps=steps, evidence_ids=evidence_ids,
            )
        if fresh.base_page != edge.source_base_page:
            return _result(
                success=False, target_capability=target_capability,
                initial_state=initial_state, final_state=fresh.base_page,
                planned=planned, completed=completed, failed=edge.edge_id,
                physical_dispatches=physical_dispatches,
                irreversible_actions=irreversible_actions,
                reason="edge_precondition_failed", steps=steps,
                evidence_ids=evidence_ids,
            )
        decision = contract.authorize(
            fresh,
            current_capture_id=fresh.capture_id,
            current_frame_hash=fresh.frame_hash,
            dispatch_count=dispatches_by_edge.get(edge.edge_id, 0),
        )
        if not decision.allowed:
            reason = "stale_state" if "stale" in decision.reason else "edge_precondition_failed"
            return _result(
                success=False, target_capability=target_capability,
                initial_state=initial_state, final_state=fresh.base_page,
                planned=planned, completed=completed, failed=edge.edge_id,
                physical_dispatches=physical_dispatches,
                irreversible_actions=irreversible_actions, reason=reason,
                steps=steps, evidence_ids=evidence_ids,
            )
        adapter = adapter_registry.get(edge.action_contract_id)
        if adapter is None:
            return _result(
                success=False, target_capability=target_capability,
                initial_state=initial_state, final_state=fresh.base_page,
                planned=planned, completed=completed, failed=edge.edge_id,
                physical_dispatches=physical_dispatches,
                irreversible_actions=irreversible_actions,
                reason="edge_dispatch_failed", steps=steps,
                evidence_ids=evidence_ids,
            )
        before_budget = action_budget.total_actions
        try:
            executed = adapter(edge, fresh)
        except Exception:
            executed = EdgeExecutionResult(False, "adapter_exception")
        budget_delta = action_budget.total_actions - before_budget
        if executed.physical_dispatches != budget_delta:
            executed = EdgeExecutionResult(
                False, "adapter_budget_dispatch_mismatch",
                max(0, budget_delta), executed.irreversible_actions,
                executed.evidence_ids,
            )
        physical_dispatches += executed.physical_dispatches
        irreversible_actions += executed.irreversible_actions
        dispatches_by_edge[edge.edge_id] = (
            dispatches_by_edge.get(edge.edge_id, 0) + executed.physical_dispatches
        )
        evidence_ids.extend(executed.evidence_ids)
        if (
            executed.physical_dispatches > edge.max_dispatches
            or irreversible_actions
            or physical_dispatches > 4
        ):
            executed = EdgeExecutionResult(
                False, "adapter_action_contract_violation",
                executed.physical_dispatches, executed.irreversible_actions,
                executed.evidence_ids,
            )
        if not executed.success:
            failure_reason = (
                "edge_postcondition_failed"
                if executed.physical_dispatches
                else "edge_dispatch_failed"
            )
            steps.append(CapabilityNavigationStepResult(
                edge.edge_id, fresh.base_page, edge.expected_target_page,
                fresh.base_page, False, executed.reason,
                executed.physical_dispatches, executed.evidence_ids,
            ))
            return _result(
                success=False, target_capability=target_capability,
                initial_state=initial_state, final_state=fresh.base_page,
                planned=planned, completed=completed, failed=edge.edge_id,
                physical_dispatches=physical_dispatches,
                irreversible_actions=irreversible_actions, reason=failure_reason,
                steps=steps, evidence_ids=evidence_ids,
            )
        try:
            observed = state_resolver(frame_provider())
        except Exception:
            observed = UiState("UNKNOWN")
        observed_target = observed.base_page
        if observed.is_unknown:
            step_reason = "unknown_post_state"
        elif observed.overlays:
            step_reason = "overlay_after_edge"
        elif (
            observed.capture_id == fresh.capture_id
            if observed.capture_id and fresh.capture_id
            else bool(observed.frame_hash and observed.frame_hash == fresh.frame_hash)
        ):
            step_reason = "stale_post_state"
        else:
            target_present = observed.has_capability(
                target_capability,
                current_capture_id=observed.capture_id,
                current_frame_hash=observed.frame_hash,
            )
            allowed_direct_target = (
                target_present and observed.base_page in contract.allowed_post_pages
            )
            if observed.base_page == edge.expected_target_page or allowed_direct_target:
                step_reason = "edge_postcondition_reached"
            else:
                step_reason = "unexpected_post_state"
        step_success = step_reason == "edge_postcondition_reached"
        steps.append(CapabilityNavigationStepResult(
            edge.edge_id, fresh.base_page, edge.expected_target_page,
            observed_target, step_success, step_reason,
            executed.physical_dispatches, executed.evidence_ids,
        ))
        if not step_success:
            return _result(
                success=False, target_capability=target_capability,
                initial_state=initial_state, final_state=observed_target,
                planned=planned, completed=completed, failed=edge.edge_id,
                physical_dispatches=physical_dispatches,
                irreversible_actions=irreversible_actions,
                reason=(
                    "unknown_start_state" if observed.is_unknown else (
                        "overlay_blocks_capability" if observed.overlays else (
                            "stale_state" if step_reason == "stale_post_state"
                            else "edge_postcondition_failed"
                        )
                    )
                ),
                steps=steps, evidence_ids=evidence_ids,
            )
        completed.append(edge.edge_id)
        current = observed
        if current.has_capability(
            target_capability,
            current_capture_id=current.capture_id,
            current_frame_hash=current.frame_hash,
        ):
            return _result(
                success=True, target_capability=target_capability,
                initial_state=initial_state, final_state=current.base_page,
                planned=planned, completed=completed, failed=None,
                physical_dispatches=physical_dispatches,
                irreversible_actions=irreversible_actions,
                reason="target_capability_reached", steps=steps,
                evidence_ids=evidence_ids,
            )


__all__ = [
    "CapabilityNavigationResult",
    "CapabilityNavigationStepResult",
    "EdgeAdapter",
    "EdgeExecutionResult",
    "PROVEN_NAVIGATION_EDGES",
    "ProvenNavigationEdge",
    "ProvenNavigationGraph",
    "ensure_capability",
]
