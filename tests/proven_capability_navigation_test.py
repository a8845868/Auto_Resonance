from __future__ import annotations

import subprocess
import sys
from types import SimpleNamespace

import pytest

from core.services.claimed_daily_checkin_navigation import (
    dismiss_claimed_daily_checkin,
)
from core.services.personal_action_budget import EpisodeActionBudget
from core.services.proven_capability_navigation import (
    EdgeExecutionResult,
    PROVEN_NAVIGATION_EDGES,
    ProvenNavigationEdge,
    ProvenNavigationGraph,
    ensure_capability,
)
from core.services.runtime_navigation_kernel import (
    PROVEN_NAVIGATION_CONTRACTS,
    normalize_legacy_state,
)
from tests.personal_runtime_fixtures import daily_frame, home_frame


class _Frame:
    def __init__(self, page: str, capture_id: str, *, overlays=()):
        self.page = page
        self.capture_id = capture_id
        self.overlays = tuple(overlays)


def _resolver(frame: _Frame):
    return normalize_legacy_state(
        frame.page,
        overlays=frame.overlays,
        frame_hash=f"hash:{frame.capture_id}",
        capture_id=frame.capture_id,
    )


def _provider(*frames: _Frame):
    values = iter(frames)
    return lambda: next(values)


def _registry(budget: EpisodeActionBudget, calls: list[str], *, fail_on=None):
    def execute(edge, state):
        calls.append(edge.edge_id)
        if edge.edge_id == fail_on:
            return EdgeExecutionResult(False, "fake_failure")
        decision = budget.authorize(
            state=state.base_page,
            action_type="ACTION_SUMMARY_NAVIGATION",
            normalized_point=(100 + len(calls), 200 + len(calls)),
        )
        assert decision.allowed
        budget.record_dispatch(decision)
        budget.record_result(decision, "PASS")
        return EdgeExecutionResult(True, "fake_pass", 1, 0, (edge.edge_id,))

    return {edge.action_contract_id: execute for edge in PROVEN_NAVIGATION_EDGES}


def test_graph_contains_only_four_live_proven_reversible_contract_edges():
    graph = ProvenNavigationGraph()

    assert graph.edges == PROVEN_NAVIGATION_EDGES
    assert len(graph.edges) == 4
    assert all(edge.enabled for edge in graph.edges)
    assert all(not edge.irreversible for edge in graph.edges)
    assert all(edge.max_dispatches == 1 for edge in graph.edges)
    assert all(edge.live_proof_reference for edge in graph.edges)
    assert all(
        edge.action_contract_id in PROVEN_NAVIGATION_CONTRACTS
        for edge in graph.edges
    )
    assert not hasattr(graph, "save")
    assert not hasattr(graph, "learn")


@pytest.mark.parametrize(
    ("source", "expected"),
    (
        ("HOME_READY", (
            "home_to_activity_overview",
            "activity_overview_to_global_prep",
            "global_prep_to_action_summary",
        )),
        ("ACTIVITY_OVERVIEW_VISIBLE", (
            "activity_overview_to_global_prep",
            "global_prep_to_action_summary",
        )),
        ("GLOBAL_PREP_PAGE", ("global_prep_to_action_summary",)),
        ("ACTION_SUMMARY_VISIBLE", ()),
    ),
)
def test_bfs_returns_shortest_proven_path(source, expected):
    path = ProvenNavigationGraph().shortest_path(
        source, "ACTION_SUMMARY_VISIBLE"
    )

    assert path is not None
    assert tuple(edge.edge_id for edge in path) == expected


def test_home_to_action_summary_revalidates_and_executes_three_edges():
    budget = EpisodeActionBudget()
    calls: list[str] = []
    frames = _provider(
        _Frame("HOME_READY", "0"),
        _Frame("HOME_READY", "1"),
        _Frame("ACTIVITY_OVERVIEW_VISIBLE", "2"),
        _Frame("ACTIVITY_OVERVIEW_VISIBLE", "3"),
        _Frame("GLOBAL_PREP_PAGE", "4"),
        _Frame("GLOBAL_PREP_PAGE", "5"),
        _Frame("ACTION_SUMMARY_VISIBLE", "6"),
    )

    result = ensure_capability(
        "ACTION_SUMMARY_VISIBLE",
        frame_provider=frames,
        state_resolver=_resolver,
        adapter_registry=_registry(budget, calls),
        action_budget=budget,
        max_steps=4,
    )

    assert result.success
    assert result.reason == "target_capability_reached"
    assert result.initial_state == "HOME_READY"
    assert result.final_state == "ACTION_SUMMARY_VISIBLE"
    assert result.planned_edge_ids == (
        "home_to_activity_overview",
        "activity_overview_to_global_prep",
        "global_prep_to_action_summary",
    )
    assert result.completed_edge_ids == result.planned_edge_ids
    assert result.physical_dispatches == 3
    assert result.unknown_state_actions == result.irreversible_actions == 0
    assert calls == list(result.planned_edge_ids)


def test_target_already_present_is_zero_input_success():
    budget = EpisodeActionBudget()
    calls: list[str] = []

    result = ensure_capability(
        "ACTION_SUMMARY_VISIBLE",
        frame_provider=_provider(_Frame("ACTION_SUMMARY_VISIBLE", "0")),
        state_resolver=_resolver,
        adapter_registry=_registry(budget, calls),
        action_budget=budget,
    )

    assert result.success
    assert result.reason == "target_capability_already_present"
    assert result.physical_dispatches == 0
    assert calls == []


@pytest.mark.parametrize(
    ("frame", "reason"),
    (
        (_Frame("UNKNOWN", "0"), "unknown_start_state"),
        (_Frame("HOME_READY", "0", overlays=("HELP",)), "overlay_blocks_capability"),
        (_Frame("INVENTORY", "0"), "no_proven_path"),
    ),
)
def test_untrusted_or_unreachable_start_is_zero_input(frame, reason):
    budget = EpisodeActionBudget()
    calls: list[str] = []

    result = ensure_capability(
        "ACTION_SUMMARY_VISIBLE",
        frame_provider=_provider(frame),
        state_resolver=_resolver,
        adapter_registry=_registry(budget, calls),
        action_budget=budget,
    )

    assert not result.success
    assert result.reason == reason
    assert result.physical_dispatches == result.unknown_state_actions == 0
    assert calls == []


def test_stale_fresh_confirmation_stops_before_input():
    budget = EpisodeActionBudget()
    calls: list[str] = []
    result = ensure_capability(
        "ACTION_SUMMARY_VISIBLE",
        frame_provider=_provider(
            _Frame("HOME_READY", "same"), _Frame("HOME_READY", "same")
        ),
        state_resolver=_resolver,
        adapter_registry=_registry(budget, calls),
        action_budget=budget,
    )

    assert not result.success
    assert result.reason == "stale_state"
    assert result.physical_dispatches == 0
    assert calls == []


def test_unknown_after_first_edge_stops_without_later_edges():
    budget = EpisodeActionBudget()
    calls: list[str] = []
    result = ensure_capability(
        "ACTION_SUMMARY_VISIBLE",
        frame_provider=_provider(
            _Frame("HOME_READY", "0"),
            _Frame("HOME_READY", "1"),
            _Frame("UNKNOWN", "2"),
        ),
        state_resolver=_resolver,
        adapter_registry=_registry(budget, calls),
        action_budget=budget,
    )

    assert not result.success
    assert result.reason == "unknown_start_state"
    assert result.physical_dispatches == 1
    assert calls == ["home_to_activity_overview"]


def test_failed_edge_does_not_fallback_or_execute_later_edge():
    budget = EpisodeActionBudget()
    calls: list[str] = []
    result = ensure_capability(
        "ACTION_SUMMARY_VISIBLE",
        frame_provider=_provider(
            _Frame("HOME_READY", "0"), _Frame("HOME_READY", "1")
        ),
        state_resolver=_resolver,
        adapter_registry=_registry(
            budget, calls, fail_on="home_to_activity_overview"
        ),
        action_budget=budget,
    )

    assert not result.success
    assert result.reason == "edge_dispatch_failed"
    assert result.physical_dispatches == 0
    assert calls == ["home_to_activity_overview"]


def test_unregistered_edge_is_not_executable():
    budget = EpisodeActionBudget()
    result = ensure_capability(
        "ACTION_SUMMARY_VISIBLE",
        frame_provider=_provider(
            _Frame("GLOBAL_PREP_PAGE", "0"),
            _Frame("GLOBAL_PREP_PAGE", "1"),
        ),
        state_resolver=_resolver,
        adapter_registry={},
        action_budget=budget,
    )

    assert not result.success
    assert result.reason == "edge_dispatch_failed"
    assert result.physical_dispatches == 0


def test_action_budget_and_cancellation_stop_without_extra_dispatch():
    budget = EpisodeActionBudget()
    cancelled = ensure_capability(
        "ACTION_SUMMARY_VISIBLE",
        frame_provider=_provider(_Frame("HOME_READY", "0")),
        state_resolver=_resolver,
        adapter_registry=_registry(budget, []),
        action_budget=budget,
        cancellation=lambda: True,
    )

    assert cancelled.reason == "cancelled"
    assert cancelled.physical_dispatches == 0

    limited_budget = EpisodeActionBudget()
    calls: list[str] = []
    limited = ensure_capability(
        "ACTION_SUMMARY_VISIBLE",
        frame_provider=_provider(
            _Frame("HOME_READY", "0"),
            _Frame("HOME_READY", "1"),
            _Frame("ACTIVITY_OVERVIEW_VISIBLE", "2"),
        ),
        state_resolver=_resolver,
        adapter_registry=_registry(limited_budget, calls),
        action_budget=limited_budget,
        max_steps=1,
    )
    assert limited.reason == "action_budget_exhausted"
    assert limited.physical_dispatches == 1
    assert calls == ["home_to_activity_overview"]


def test_claimed_daily_adapter_dispatches_once_and_reaches_home():
    budget = EpisodeActionBudget()
    taps = []
    frames = iter([daily_frame(), daily_frame(), home_frame()])
    result = dismiss_claimed_daily_checkin(
        frame_provider=lambda: next(frames),
        tap=lambda point, **kwargs: taps.append((point, kwargs)) or True,
        geometry_provider=lambda: SimpleNamespace(
            physical_width=1280, physical_height=720
        ),
        action_budget=budget,
        sleep=lambda _seconds: None,
        monotonic=iter((0.0, 0.0, 0.1, 0.2, 0.3)).__next__,
        timeout_seconds=1.0,
        interval_seconds=0.1,
    )

    assert result.success
    assert result.final_state == "HOME_READY"
    assert result.physical_dispatches == 1
    assert len(taps) == 1
    assert taps[0][1]["random_offset"] is False


def test_planner_import_does_not_initialize_real_backends():
    script = (
        "import sys; import core.services.proven_capability_navigation; "
        "blocked=('core.control.control','core.control.adb','core.control.nemu',"
        "'core.image.ocr'); print([name for name in blocked if name in sys.modules])"
    )
    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=True,
        capture_output=True,
        text=True,
    )

    assert completed.stdout.strip().splitlines()[-1] == "[]"
