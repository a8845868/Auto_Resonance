from __future__ import annotations

from dataclasses import replace
import json

import pytest

from core.services.action_summary_advisory_policy import FactAcquisitionRequest
from core.services.action_summary_missing_fact_acquisition import (
    FACT_ACQUISITION_CONTRACTS,
    FactCardinality,
    FactSubjectScope,
    build_missing_fact_acquisition_plan,
    fact_instance_key,
    validate_acquired_fact,
)
from core.services.action_summary_policy_runtime_inputs import (
    AssemblyIntegrityStatus,
    assemble_action_summary_policy_runtime_inputs,
)
from core.services.action_summary_raw_frame_observer import (
    normalize_raw_action_summary_visual_observation,
    observe_action_summary_current_page_visuals,
)
from tests.action_summary_policy_prerequisite_model_test import card, page_model
from tests.action_summary_policy_runtime_input_assembly_test import policy_document
from tests.action_summary_product_model_test import Frame, trusted_items


POLICY_HASH = "a" * 64
RUNTIME_HASH = "b" * 64
FRAME_HASH = "a" * 64
CAPTURE_ID = "capture-policy-v1"
NOW = "2026-07-26T12:01:00+08:00"


def request(*card_keys: str) -> FactAcquisitionRequest:
    return FactAcquisitionRequest(
        missing_fact="resource_cost_unknown",
        required_scope="CURRENT_ACTION_SUMMARY_PAGE",
        preferred_source="raw_frame_observer",
        requires_navigation=None,
        requires_page_input=False,
        requires_business_input=False,
        priority=1,
        reason="scoped aggregation regression",
        target_subject_keys=tuple(card_keys),
    )


def cost_fact(
    card_key: str,
    cost: int,
    *,
    capture_id: str = CAPTURE_ID,
    frame_hash: str = FRAME_HASH,
    observed_at: str = "2026-07-26T12:00:00+08:00",
):
    raw_facts = normalize_raw_action_summary_visual_observation(
        observe_action_summary_current_page_visuals(Frame(
            trusted_items(),
            capture_id=capture_id,
            raw_frame_hash=frame_hash,
            captured_at=observed_at,
        )),
        runtime_input_fingerprint=RUNTIME_HASH,
    )
    template = raw_facts[0]
    return replace(
        template,
        value_json=json.dumps(
            {
                "card_match_key": card_key,
                "resource_cost_per_run": cost,
                "resource_id": "UNKNOWN",
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
        provenance_ids=(
            f"capture_id:{capture_id}",
            f"frame_sha256:{frame_hash}",
            f"card_match_key:{card_key}",
            f"cost-roi:{card_key}",
        ),
        subject_key=card_key,
        fact_instance_key=fact_instance_key(
            "resource_cost_unknown", FactSubjectScope.TASK_CARD, card_key
        ),
    )


def plan(facts, *card_keys: str):
    return build_missing_fact_acquisition_plan(
        (request(*card_keys),),
        observations=tuple(facts),
        policy_fingerprint=POLICY_HASH,
        runtime_input_fingerprint=RUNTIME_HASH,
        generated_at=NOW,
    )


def test_three_equal_costs_on_three_cards_resolve_three_instances():
    keys = ("CARD-A", "CARD-B", "CARD-C")
    result = plan((cost_fact(key, 40) for key in keys), *keys)

    assert len(result.resolved_fact_instances) == 3
    assert {fact.subject_key for fact in result.resolved_fact_instances} == set(keys)
    assert result.conflicting_fact_instances == ()
    assert result.unresolved_fact_instances == ()
    assert result.policy_evaluation_still_blocked is False


def test_different_values_on_different_cards_are_not_a_conflict():
    result = plan(
        (cost_fact("CARD-A", 40), cost_fact("CARD-B", 50), cost_fact("CARD-C", 60)),
        "CARD-A",
        "CARD-B",
        "CARD-C",
    )

    assert [fact.value()["resource_cost_per_run"] for fact in result.resolved_facts] == [
        40,
        50,
        60,
    ]
    assert result.conflicting_facts == ()


def test_same_card_different_values_is_one_scoped_conflict():
    result = plan((cost_fact("CARD-A", 40), cost_fact("CARD-A", 50)), "CARD-A")

    expected = fact_instance_key(
        "resource_cost_unknown", FactSubjectScope.TASK_CARD, "CARD-A"
    )
    assert result.resolved_fact_instances == ()
    assert result.conflicting_fact_instances == (expected,)
    assert result.conflicting_facts == ("resource_cost_unknown",)
    assert result.policy_evaluation_still_blocked is True


def test_same_card_same_value_deduplicates():
    first = cost_fact("CARD-A", 40)
    second = replace(first, observed_at="2026-07-26T12:00:30+08:00")
    result = plan((first, second), "CARD-A")

    assert len(result.resolved_fact_instances) == 1
    assert len(result.deduplicated_observations) == 1
    assert result.conflicting_fact_instances == ()


def test_input_order_does_not_change_scoped_result():
    facts = (
        cost_fact("CARD-B", 50),
        cost_fact("CARD-A", 40),
        cost_fact("CARD-C", 60),
    )

    assert plan(facts, "CARD-A", "CARD-B", "CARD-C").to_dict() == plan(
        reversed(facts), "CARD-A", "CARD-B", "CARD-C"
    ).to_dict()


def test_missing_expected_card_is_an_unresolved_instance():
    result = plan(
        (cost_fact("CARD-A", 40), cost_fact("CARD-C", 60)),
        "CARD-A",
        "CARD-B",
        "CARD-C",
    )

    expected = fact_instance_key(
        "resource_cost_unknown", FactSubjectScope.TASK_CARD, "CARD-B"
    )
    assert result.unresolved_fact_instances == (expected,)
    assert result.fact_instance_coverage[0].resolved_subject_keys == (
        "CARD-A",
        "CARD-C",
    )
    assert result.fact_instance_coverage[0].complete is False


def test_observation_for_unexpected_card_is_rejected():
    result = plan((cost_fact("CARD-X", 40),), "CARD-A")

    assert result.resolved_fact_instances == ()
    assert result.rejected_observations[0].reason == "unexpected_subject_key"


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("subject_key", "", "subject_key_missing"),
        ("subject_scope", FactSubjectScope.PAGE, "subject_scope_mismatch"),
        ("fact_instance_key", "resource_cost_unknown:TASK_CARD:OTHER", "fact_instance_key_mismatch"),
    ],
)
def test_invalid_instance_identity_is_rejected(field, value, reason):
    fact = replace(cost_fact("CARD-A", 40), **{field: value})

    assert validate_acquired_fact(
        fact,
        FACT_ACQUISITION_CONTRACTS["resource_cost_unknown"],
        POLICY_HASH,
        RUNTIME_HASH,
        NOW,
    ) == reason


def test_subject_key_cannot_disagree_with_card_value():
    fact = cost_fact("CARD-A", 40)
    mismatched = replace(
        fact,
        subject_key="CARD-B",
        fact_instance_key=fact_instance_key(
            fact.fact_id, FactSubjectScope.TASK_CARD, "CARD-B"
        ),
    )

    assert validate_acquired_fact(
        mismatched,
        FACT_ACQUISITION_CONTRACTS["resource_cost_unknown"],
        POLICY_HASH,
        RUNTIME_HASH,
        NOW,
    ) == "subject_key_value_mismatch"


def test_resource_cost_contract_is_per_task_card():
    contract = FACT_ACQUISITION_CONTRACTS["resource_cost_unknown"]

    assert contract.subject_scope is FactSubjectScope.TASK_CARD
    assert contract.subject_key_source == "card_match_key"
    assert contract.cardinality is FactCardinality.PER_TASK_CARD


@pytest.mark.parametrize(
    "fact_id",
    ("task_identity_unknown", "remaining_attempts_unknown", "reward_target_unknown"),
)
def test_other_card_facts_are_also_per_task_card(fact_id):
    contract = FACT_ACQUISITION_CONTRACTS[fact_id]

    assert contract.subject_scope is FactSubjectScope.TASK_CARD
    assert contract.cardinality is FactCardinality.PER_TASK_CARD


def test_config_facts_remain_singletons():
    for fact_id in (
        "objective_missing",
        "strategy_identity_missing",
        "strategy_provenance_missing",
        "recommended_run_count_limit_unknown",
    ):
        contract = FACT_ACQUISITION_CONTRACTS[fact_id]
        assert contract.subject_scope is FactSubjectScope.CONFIG
        assert contract.cardinality is FactCardinality.SINGLETON


def test_runtime_assembly_binds_scoped_costs_by_card_key_not_list_order():
    page = page_model(
        replace(card(match_key="CARD-A", semantic_id="TASK_A"), cost=None),
        replace(card(match_key="CARD-B", semantic_id="TASK_B"), cost=None),
        replace(card(match_key="CARD-C", semantic_id="TASK_C"), cost=None),
    )
    facts = (
        cost_fact("CARD-C", 60),
        cost_fact("CARD-A", 40),
        cost_fact("CARD-B", 50),
    )
    policy = policy_document(rewards=[
        {"task_semantic_id": "TASK_A", "reward_amount_per_execution": 10},
        {"task_semantic_id": "TASK_B", "reward_amount_per_execution": 20},
        {"task_semantic_id": "TASK_C", "reward_amount_per_execution": 30},
    ])

    result = assemble_action_summary_policy_runtime_inputs(
        page,
        policy,
        resource_observation=None,
        acquired_facts=facts,
        assembled_at=NOW,
    )

    assert [
        candidate.resource_balance.unit_cost
        for candidate in result.candidate_prerequisites
    ] == [40, 50, 60]
    assert result.scoped_acquired_fact_instance_keys == tuple(sorted(
        fact.fact_instance_key for fact in facts
    ))
    assert result.scoped_acquired_fact_rejections == ()
    assert all(
        "resource_cost_unknown" not in candidate.reason_codes
        for candidate in result.candidate_prerequisites
    )


def test_runtime_assembly_never_copies_one_card_cost_to_another():
    page = page_model(
        replace(card(match_key="CARD-A", semantic_id="TASK_A"), cost=None),
        replace(card(match_key="CARD-B", semantic_id="TASK_B"), cost=None),
    )
    result = assemble_action_summary_policy_runtime_inputs(
        page,
        policy_document(),
        resource_observation=None,
        acquired_facts=(cost_fact("CARD-B", 50),),
        assembled_at=NOW,
    )

    assert [
        candidate.resource_balance.unit_cost
        for candidate in result.candidate_prerequisites
    ] == [None, 50]


def test_runtime_assembly_fails_integrity_for_foreign_card_fact():
    page = page_model(card(match_key="CARD-A", semantic_id="TASK_A"))
    result = assemble_action_summary_policy_runtime_inputs(
        page,
        policy_document(rewards=[{
            "task_semantic_id": "TASK_A",
            "reward_amount_per_execution": 10,
        }]),
        resource_observation=None,
        acquired_facts=(cost_fact("CARD-X", 40),),
        assembled_at=NOW,
    )

    assert result.assembly_status is AssemblyIntegrityStatus.FAIL
    assert result.scoped_acquired_fact_rejections == (
        "scoped_resource_cost_unexpected_card:CARD-X",
    )


def test_runtime_assembly_fails_closed_on_same_card_cost_conflict():
    page = page_model(replace(
        card(match_key="CARD-A", semantic_id="TASK_A"),
        cost=None,
    ))
    result = assemble_action_summary_policy_runtime_inputs(
        page,
        policy_document(rewards=[{
            "task_semantic_id": "TASK_A",
            "reward_amount_per_execution": 10,
        }]),
        resource_observation=None,
        acquired_facts=(
            cost_fact("CARD-A", 40),
            cost_fact("CARD-A", 50),
        ),
        assembled_at=NOW,
    )

    assert result.assembly_status is AssemblyIntegrityStatus.FAIL
    assert result.candidate_prerequisites[0].resource_balance.unit_cost is None
    assert result.scoped_acquired_fact_rejections == (
        "scoped_resource_cost_conflict:CARD-A",
    )


def test_scoped_plan_remains_observe_only_and_non_authorizing():
    result = plan((cost_fact("CARD-A", 40),), "CARD-A")

    assert result.execution_authorized is False
    assert result.authorization_issued is False
    assert result.capture_calls == 0
    assert result.page_input_dispatches == 0
    assert result.business_dispatches == 0
    assert result.irreversible_actions == 0
