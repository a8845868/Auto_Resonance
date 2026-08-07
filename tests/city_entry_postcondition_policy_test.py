from __future__ import annotations

from types import SimpleNamespace

import pytest

from core.preset import presets
from core.services.city_entry_postcondition import (
    CITY_CONTEXT_VISIBLE,
    CITY_ENTRY_POSTCONDITION_POLICY,
    POSTCONDITION_TAXONOMY_MISMATCH,
    interpret_historical_gate_result,
)
from core.services.city_navigation import CityNavigationState
from core.services.runtime_navigation_kernel import (
    DEFAULT_CAPABILITIES,
    PROVEN_NAVIGATION_CONTRACTS,
)
from tools import nemu_city_entry_live_gate


def _evaluate(state: object, **overrides):
    values = {
        "frame_is_fresh": True,
        "frame_changed": True,
        "evidence_invariant_check": "PASS",
    }
    values.update(overrides)
    return CITY_ENTRY_POSTCONDITION_POLICY.evaluate(state, **values)


@pytest.mark.parametrize(
    "state",
    (CityNavigationState.CITY_DETAIL_VISIBLE, CityNavigationState.EXCHANGE_NPC_VISIBLE),
)
def test_trusted_city_leaf_states_receive_verified_context(state):
    decision = _evaluate(state)

    assert decision.city_entry_verified is True
    assert decision.post_context_state == CITY_CONTEXT_VISIBLE
    assert decision.post_canonical_leaf_state == state.value


def test_city_detail_and_exchange_npc_preserve_distinct_leaf_states():
    detail = _evaluate(CityNavigationState.CITY_DETAIL_VISIBLE)
    exchange = _evaluate(CityNavigationState.EXCHANGE_NPC_VISIBLE)

    assert detail.post_canonical_leaf_state == "CITY_DETAIL_VISIBLE"
    assert exchange.post_canonical_leaf_state == "EXCHANGE_NPC_VISIBLE"
    assert detail.post_canonical_leaf_state != exchange.post_canonical_leaf_state
    assert detail.exact_expected_leaf_match is True
    assert exchange.exact_expected_leaf_match is False


@pytest.mark.parametrize(
    "state",
    (
        "HOME_READY",
        "HOME_CITY_ENTRY_CONTROL_VISIBLE",
        "CITY_ENTRY_VISIBLE",
        "UNKNOWN",
        "INVENTORY_PAGE_VISIBLE",
        "MAILBOX_VISIBLE",
        "ACTION_SUMMARY_VISIBLE",
        "LOGIN_PAGE",
    ),
)
def test_forbidden_and_untrusted_pages_never_verify_city_entry(state):
    decision = _evaluate(state)

    assert decision.city_entry_verified is False
    assert decision.post_context_state == "UNKNOWN"


def test_frame_change_without_trusted_city_leaf_is_not_success():
    decision = _evaluate("FOREIGN_CHANGED_PAGE")

    assert decision.frame_changed is True
    assert decision.city_entry_verified is False


@pytest.mark.parametrize(
    ("overrides", "reason"),
    (
        ({"frame_is_fresh": False}, "post_frame_not_fresh"),
        ({"frame_changed": False}, "post_frame_unchanged"),
        ({"evidence_invariant_check": "FAIL"}, "evidence_invariant_failed"),
    ),
)
def test_trusted_leaf_still_requires_all_transition_evidence(overrides, reason):
    decision = _evaluate("EXCHANGE_NPC_VISIBLE", **overrides)

    assert decision.city_entry_verified is False
    assert reason in decision.reason_codes


def test_historical_block_is_preserved_and_explained_as_taxonomy_mismatch():
    interpretation = interpret_historical_gate_result(
        historical_status="BLOCKED",
        post_canonical_leaf_state="EXCHANGE_NPC_VISIBLE",
        expected_leaf_state="CITY_DETAIL_VISIBLE",
        frame_is_fresh=True,
        frame_changed=True,
        evidence_invariant_check="PASS",
    )

    assert interpretation.historical_status == "BLOCKED"
    assert interpretation.historical_status_preserved is True
    assert interpretation.block_classification == POSTCONDITION_TAXONOMY_MISMATCH
    assert interpretation.decision.city_entry_verified is True
    assert interpretation.decision.exact_expected_leaf_match is False


def test_navigation_kernel_enter_city_contract_reuses_shared_policy():
    contract = PROVEN_NAVIGATION_CONTRACTS["ENTER_CITY"]

    assert contract.allowed_post_pages == CITY_ENTRY_POSTCONDITION_POLICY.accepted_leaf_states
    assert DEFAULT_CAPABILITIES["CITY_DETAIL_VISIBLE"] >= {CITY_CONTEXT_VISIBLE}
    assert DEFAULT_CAPABILITIES["EXCHANGE_NPC_VISIBLE"] >= {CITY_CONTEXT_VISIBLE}


def test_gate_postcondition_evaluator_reuses_shared_policy():
    evidence = SimpleNamespace(
        post_observations=[SimpleNamespace(source_capture_id="fresh-post")],
        post_frame_changed=True,
        evidence_invariant_check="PASS",
    )
    navigation = SimpleNamespace(
        state=CityNavigationState.EXCHANGE_NPC_VISIBLE,
        post_canonical_leaf_state="EXCHANGE_NPC_VISIBLE",
        evidence=evidence,
    )

    decision = nemu_city_entry_live_gate._evaluate_navigation_postcondition(navigation)

    assert decision.policy_id == CITY_ENTRY_POSTCONDITION_POLICY.policy_id
    assert decision.city_entry_verified is True
    assert decision.exact_expected_leaf_match is False


def test_fatigue_city_entry_wrapper_preserves_leaf_and_revalidates_policy(monkeypatch):
    evidence = SimpleNamespace(
        post_observations=[SimpleNamespace(source_capture_id="fresh-post")],
        post_frame_changed=True,
        evidence_invariant_check="PASS",
    )
    resolved = SimpleNamespace(
        state=CityNavigationState.EXCHANGE_NPC_VISIBLE,
        status="PASS",
        reason="city_postcondition_verified",
        attempt_count=3,
        trace=(),
        entry_opened=True,
        station_confirmed=False,
        station_id=None,
        terminal=True,
        evidence_attempt_id="attempt",
        evidence=evidence,
        post_canonical_leaf_state="EXCHANGE_NPC_VISIBLE",
    )

    class _Adapter:
        def __init__(self, **_kwargs):
            pass

        def enter_city(self):
            return resolved

    monkeypatch.setattr(presets, "ReadOnlyCityNavigationAdapter", _Adapter)
    result = presets.go_city(monotonic=lambda: 1.0)

    assert result.success is True
    assert result.state is presets.CityNavigationState.EXCHANGE_NPC_VISIBLE
    assert result.post_canonical_leaf_state == "EXCHANGE_NPC_VISIBLE"
    assert result.post_context_state == CITY_CONTEXT_VISIBLE
    assert result.city_entry_verified is True
    assert result.exact_expected_leaf_match is False
    assert result.gate_postcondition_policy_id == CITY_ENTRY_POSTCONDITION_POLICY.policy_id
