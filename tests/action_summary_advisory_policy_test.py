from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import subprocess
import sys

import pytest

from core.services.action_summary_advisory_policy import (
    ActionSummaryAdvisoryPolicyProfile,
    AdvisoryObjective,
    AdvisoryPolicyStatus,
    action_summary_advisory_policy_fingerprint,
    create_action_summary_advisory_policy_profile,
    evaluate_action_summary_policy_advisory,
    parse_action_summary_advisory_policy_profile,
)
from core.services.action_summary_policy_prerequisites import (
    PrerequisiteFactStatus,
)
from core.services.action_summary_policy_runtime_inputs import (
    AssemblyIntegrityStatus,
    PolicyInputReadiness,
    PolicyTargetMatchStatus,
    SourceRelationship,
    assemble_action_summary_policy_runtime_inputs,
)
from core.services.action_summary_product_model import action_summary_title_hash
from tests.action_summary_policy_prerequisite_model_test import card, page_model
from tests.action_summary_policy_runtime_input_assembly_test import (
    observation,
    policy_document,
    runtime_page,
)


EVALUATED_AT = "2026-07-26T12:00:02+08:00"
FIXTURE = Path(
    "tests/fixtures/action_summary_advisory_policy/canonical_incomplete_v1.json"
)


def complete_runtime_input():
    return assemble_action_summary_policy_runtime_inputs(
        runtime_page(),
        policy_document(),
        resource_observation=observation(),
        assembled_at="2026-07-26T12:00:01+08:00",
    )


def canonical_runtime_input():
    hashes = [action_summary_title_hash(f"CANONICAL_TASK_{index}") for index in range(3)]
    cards = tuple(
        replace(
            card(
                match_key=f"CANONICAL_MATCH_{index}",
                semantic_id=f"TASK_{chr(65 + index)}",
                cost=40,
            ),
            title_hash=hashes[index],
        )
        for index in range(3)
    )
    policy = {
        "ActionSummaryPolicy": {
            "schema_version": "1.0",
            "policy_id": "resident-activity-app-config",
            "policy_version": "fixture-v1",
            "captured_at": "2026-07-26T12:00:00+08:00",
            "requested_known_task_id": "TASK_A",
            "requested_task_title_hash": hashes[0],
        }
    }
    return assemble_action_summary_policy_runtime_inputs(
        page_model(*cards),
        policy,
        resource_observation=None,
        assembled_at="2026-07-26T12:00:01+08:00",
    )


def incomplete_policy_snapshot() -> dict[str, object]:
    return {
        "ActionSummaryAdvisoryPolicy": {
            "schema_version": "1.0",
            "policy_id": "canonical-incomplete",
            "policy_version": "1",
            "captured_at": "2026-07-26T12:00:00+08:00",
        }
    }


def profile(
    objective: AdvisoryObjective = AdvisoryObjective.FIXED_TASK,
    **changes,
) -> ActionSummaryAdvisoryPolicyProfile:
    values = {
        "policy_id": "advisory-policy",
        "policy_version": "1",
        "captured_at": "2026-07-26T11:59:30+08:00",
        "activity_family": "SIEGE",
        "strategy_id": "personal-siege-policy",
        "strategy_version": "1",
        "objective": objective,
        "preferred_task_ids": ("TASK_A",) if objective is AdvisoryObjective.FIXED_TASK else (),
        "excluded_task_ids": (),
        "reward_priority_ids": ("SIEGE_PROGRESS",),
        "reward_priority_families": (),
        "maximum_cost_per_run": 40,
        "maximum_total_cost": 200,
        "minimum_resource_reserve": 0,
        "minimum_remaining_attempts": 1,
        "maximum_task_runs": 5,
        "fatigue_reserve": 20,
        "allow_unknown_reward": False,
        "allow_unknown_attempts": False,
        "allow_unknown_resource_identity": False,
        "allow_unknown_resource_balance": False,
        "tie_break_order": ("PAGE_ORDER", "TASK_ID"),
        "reason": "deterministic fixture policy",
    }
    values.update(changes)
    return create_action_summary_advisory_policy_profile(**values)


def evaluate(runtime=None, selected_profile=None, **kwargs):
    return evaluate_action_summary_policy_advisory(
        runtime or complete_runtime_input(),
        selected_profile or profile(),
        evaluated_at=kwargs.pop("evaluated_at", EVALUATED_AT),
        **kwargs,
    )


def replace_candidate(runtime, index: int, **changes):
    candidates = list(runtime.candidate_prerequisites)
    candidates[index] = replace(candidates[index], **changes)
    return replace(runtime, candidate_prerequisites=tuple(candidates))


def test_current_canonical_fixture_blocks_missing_facts_without_ranking():
    expected = json.loads(FIXTURE.read_text(encoding="utf-8"))
    runtime = canonical_runtime_input()
    result = evaluate_action_summary_policy_advisory(
        runtime,
        incomplete_policy_snapshot(),
        evaluated_at=EVALUATED_AT,
    )

    assert runtime.assembly_status is AssemblyIntegrityStatus.PASS
    assert runtime.policy_input_readiness is PolicyInputReadiness.BLOCKED_MISSING_FACTS
    assert result.status.value == expected["expected_advisory_status"]
    assert result.candidate_count == 0
    assert result.ranked_candidates == ()
    assert result.recommended_card_match_key is None
    assert result.recommended_run_count is None
    assert set(expected["required_missing_facts"]).issubset(result.missing_facts)


def test_current_fixture_costs_do_not_create_a_recommendation():
    runtime = canonical_runtime_input()
    assert [
        candidate.resource_balance.unit_cost
        for candidate in runtime.candidate_prerequisites
    ] == [None, None, None]
    result = evaluate_action_summary_policy_advisory(
        runtime, incomplete_policy_snapshot(), evaluated_at=EVALUATED_AT
    )
    assert result.status is AdvisoryPolicyStatus.BLOCKED_MISSING_FACTS
    assert result.recommended_known_task_id is None


def test_observe_only_completes_with_no_recommendation_even_when_facts_missing():
    runtime = canonical_runtime_input()
    result = evaluate(
        runtime,
        profile(
            AdvisoryObjective.OBSERVE_ONLY,
            maximum_cost_per_run=0,
            maximum_total_cost=0,
            maximum_task_runs=0,
            preferred_task_ids=(),
        ),
    )
    assert result.status is AdvisoryPolicyStatus.OBSERVE_ONLY_COMPLETE
    assert result.ready is True
    assert result.ranked_candidates == ()
    assert result.recommended_run_count is None


def test_default_profile_objective_is_observe_only_and_unknown_flags_are_false():
    value = create_action_summary_advisory_policy_profile(
        policy_id="observe",
        policy_version="1",
        captured_at="2026-07-26T12:00:00+08:00",
        activity_family="SIEGE",
        strategy_id="observe",
        strategy_version="1",
    )
    assert value.objective is AdvisoryObjective.OBSERVE_ONLY
    assert value.allow_unknown_reward is False
    assert value.allow_unknown_attempts is False
    assert value.allow_unknown_resource_identity is False
    assert value.allow_unknown_resource_balance is False


def test_fixed_task_complete_facts_produce_one_advisory_recommendation():
    result = evaluate()
    assert result.status is AdvisoryPolicyStatus.ADVISORY_RECOMMENDATION_READY
    assert result.ready is True
    assert result.recommended_known_task_id == "TASK_A"
    assert result.recommended_card_match_key == "MATCH_A"
    assert result.recommended_run_count == 2


def test_fixed_task_unknown_reward_is_optional_but_recorded():
    runtime = complete_runtime_input()
    reward = replace(
        runtime.candidate_prerequisites[0].reward_target,
        status=PrerequisiteFactStatus.UNKNOWN,
        reward_target_id=None,
        candidate_reward_amount=None,
    )
    runtime = replace_candidate(runtime, 0, reward_target=reward)
    result = evaluate(runtime)
    assert result.status is AdvisoryPolicyStatus.ADVISORY_RECOMMENDATION_READY
    assert "reward_target_unknown" in result.warnings
    assert result.ranked_candidates[0].reward_target_id is None


def test_fixed_task_not_found_blocks():
    result = evaluate(selected_profile=profile(preferred_task_ids=("TASK_Z",)))
    assert result.status is AdvisoryPolicyStatus.BLOCKED_TARGET_NOT_FOUND
    assert result.recommended_card_match_key is None


def test_fixed_task_duplicate_semantic_identity_blocks_as_ambiguous():
    runtime = complete_runtime_input()
    duplicate_identity = replace(
        runtime.candidate_prerequisites[1].task_identity,
        task_semantic_id="TASK_A",
    )
    runtime = replace_candidate(runtime, 1, task_identity=duplicate_identity)
    result = evaluate(runtime)
    assert result.status is AdvisoryPolicyStatus.BLOCKED_TARGET_AMBIGUOUS
    assert result.candidate_count == 0


def test_assembly_target_not_found_is_preserved():
    runtime = replace(
        complete_runtime_input(),
        policy_target_match_status=PolicyTargetMatchStatus.NOT_FOUND,
        policy_target_card_match_key=None,
    )
    assert evaluate(runtime).status is AdvisoryPolicyStatus.BLOCKED_TARGET_NOT_FOUND


def test_assembly_target_ambiguous_is_preserved():
    runtime = replace(
        complete_runtime_input(),
        policy_target_match_status=PolicyTargetMatchStatus.AMBIGUOUS,
        policy_target_card_match_key=None,
    )
    assert evaluate(runtime).status is AdvisoryPolicyStatus.BLOCKED_TARGET_AMBIGUOUS


def test_reward_objective_missing_reward_fact_blocks_all_ranking():
    runtime = complete_runtime_input()
    reward = replace(
        runtime.candidate_prerequisites[0].reward_target,
        status=PrerequisiteFactStatus.UNKNOWN,
        reward_target_id=None,
        candidate_reward_amount=None,
    )
    runtime = replace_candidate(runtime, 0, reward_target=reward)
    result = evaluate(
        runtime,
        profile(AdvisoryObjective.MAXIMIZE_PRIORITY_REWARD, preferred_task_ids=()),
    )
    assert result.status is AdvisoryPolicyStatus.BLOCKED_MISSING_FACTS
    assert "reward_target_unknown" in result.missing_facts
    assert result.ranked_candidates == ()


def test_attempts_objective_missing_card_attempts_blocks():
    runtime = complete_runtime_input()
    attempts = replace(
        runtime.candidate_prerequisites[0].remaining_attempts,
        status=PrerequisiteFactStatus.UNKNOWN,
        remaining_attempts=None,
        total_attempts=None,
    )
    runtime = replace_candidate(runtime, 0, remaining_attempts=attempts)
    result = evaluate(
        runtime,
        profile(AdvisoryObjective.MAXIMIZE_AVAILABLE_ATTEMPTS, preferred_task_ids=()),
    )
    assert result.status is AdvisoryPolicyStatus.BLOCKED_MISSING_FACTS
    assert "remaining_attempts_unknown" in result.missing_facts


def test_resource_objective_missing_resource_identity_blocks():
    runtime = complete_runtime_input()
    resource = replace(runtime.candidate_prerequisites[0].resource_balance, resource_id="UNKNOWN")
    runtime = replace_candidate(runtime, 0, resource_balance=resource)
    result = evaluate(
        runtime,
        profile(AdvisoryObjective.MINIMIZE_RESOURCE_COST, preferred_task_ids=()),
    )
    assert result.status is AdvisoryPolicyStatus.BLOCKED_MISSING_FACTS
    assert "resource_identity_unknown" in result.missing_facts


def test_missing_balance_is_not_treated_as_sufficient():
    runtime = complete_runtime_input()
    resource = replace(
        runtime.candidate_prerequisites[0].resource_balance,
        status=PrerequisiteFactStatus.UNKNOWN,
        available_amount=None,
    )
    runtime = replace_candidate(runtime, 0, resource_balance=resource)
    result = evaluate(runtime)
    assert result.status is AdvisoryPolicyStatus.BLOCKED_MISSING_FACTS
    assert "resource_balance_unknown" in result.missing_facts


def test_page_level_attempts_are_never_used_as_card_fallback():
    runtime = canonical_runtime_input()
    assert all(
        candidate.remaining_attempts.remaining_attempts is None
        and candidate.remaining_attempts.total_attempts is None
        for candidate in runtime.candidate_prerequisites
    )
    result = evaluate_action_summary_policy_advisory(
        runtime, incomplete_policy_snapshot(), evaluated_at=EVALUATED_AT
    )
    assert "remaining_attempts_unknown" in result.missing_facts


def test_unknown_candidate_does_not_enter_ranking_behind_known_candidate():
    runtime = complete_runtime_input()
    attempts = replace(
        runtime.candidate_prerequisites[1].remaining_attempts,
        status=PrerequisiteFactStatus.UNKNOWN,
        remaining_attempts=None,
        total_attempts=None,
    )
    runtime = replace_candidate(runtime, 1, remaining_attempts=attempts)
    result = evaluate(
        runtime,
        profile(AdvisoryObjective.MAXIMIZE_AVAILABLE_ATTEMPTS, preferred_task_ids=()),
    )
    assert result.status is AdvisoryPolicyStatus.BLOCKED_MISSING_FACTS
    assert result.ranked_candidates == ()


def test_excluded_task_is_removed_before_ranking():
    result = evaluate(
        selected_profile=profile(
            AdvisoryObjective.MAXIMIZE_AVAILABLE_ATTEMPTS,
            preferred_task_ids=(),
            excluded_task_ids=("TASK_A",),
        )
    )
    assert result.status is AdvisoryPolicyStatus.ADVISORY_RECOMMENDATION_READY
    assert all(candidate.known_task_id != "TASK_A" for candidate in result.ranked_candidates)


def test_zero_attempt_candidate_is_not_eligible():
    runtime = complete_runtime_input()
    attempts = replace(
        runtime.candidate_prerequisites[0].remaining_attempts,
        remaining_attempts=0,
    )
    runtime = replace_candidate(runtime, 0, remaining_attempts=attempts)
    result = evaluate(runtime)
    assert result.status is AdvisoryPolicyStatus.NO_ELIGIBLE_TASK
    assert "candidate_attempts_insufficient" in result.block_reason_codes


def test_explicit_resource_insufficiency_filters_candidate():
    runtime = complete_runtime_input()
    resource = replace(runtime.candidate_prerequisites[0].resource_balance, available_amount=20)
    runtime = replace_candidate(runtime, 0, resource_balance=resource)
    result = evaluate(runtime)
    assert result.status is AdvisoryPolicyStatus.NO_ELIGIBLE_TASK
    assert "candidate_budget_insufficient" in result.block_reason_codes


def test_explicit_fatigue_budget_insufficiency_filters_candidate():
    runtime = complete_runtime_input()
    fatigue = replace(
        runtime.candidate_prerequisites[0].fatigue_budget,
        available_fatigue=40,
        reserved_fatigue=40,
        max_policy_spend=0,
    )
    runtime = replace_candidate(runtime, 0, fatigue_budget=fatigue)
    result = evaluate(runtime)
    assert result.status is AdvisoryPolicyStatus.NO_ELIGIBLE_TASK


def test_recommended_run_count_is_minimum_of_every_hard_limit():
    result = evaluate(
        selected_profile=profile(maximum_total_cost=80, maximum_task_runs=5)
    )
    assert result.recommended_run_count == 2


def test_unknown_run_count_limit_never_falls_back_to_one():
    runtime = complete_runtime_input()
    resource = replace(runtime.candidate_prerequisites[0].resource_balance, available_amount=None)
    runtime = replace_candidate(runtime, 0, resource_balance=resource)
    result = evaluate(runtime)
    assert result.status is AdvisoryPolicyStatus.BLOCKED_MISSING_FACTS
    assert result.recommended_run_count is None


def test_allow_unknown_flags_never_invent_a_run_count():
    runtime = complete_runtime_input()
    attempts = replace(
        runtime.candidate_prerequisites[0].remaining_attempts,
        status=PrerequisiteFactStatus.UNKNOWN,
        remaining_attempts=None,
        total_attempts=None,
    )
    runtime = replace_candidate(runtime, 0, remaining_attempts=attempts)
    selected_profile = profile(allow_unknown_attempts=True)
    result = evaluate(runtime, selected_profile)
    assert result.status is AdvisoryPolicyStatus.BLOCKED_MISSING_FACTS
    assert result.recommended_run_count is None


def test_policy_fingerprint_change_invalidates_old_profile():
    original = profile()
    forged = replace(original, maximum_task_runs=1)
    result = evaluate(selected_profile=forged)
    assert result.status is AdvisoryPolicyStatus.BLOCKED_POLICY_INVALID
    assert "policy_fingerprint_mismatch" in result.block_reason_codes


def test_result_binds_policy_and_runtime_fingerprints():
    runtime = complete_runtime_input()
    selected_profile = profile()
    result = evaluate(runtime, selected_profile)
    assert result.matches_policy_profile(selected_profile) is True
    assert result.matches_runtime_input(runtime) is True
    assert result.matches_runtime_input(replace(runtime, assembled_at="2026-07-26T12:00:02+08:00")) is False


def test_stale_runtime_input_blocks_before_ranking():
    result = evaluate(evaluated_at="2026-07-26T12:10:02+08:00")
    assert result.status is AdvisoryPolicyStatus.BLOCKED_PROVENANCE_INVALID
    assert "runtime_input_stale" in result.block_reason_codes


def test_stale_resource_relationship_blocks():
    runtime = replace(
        complete_runtime_input(),
        source_relationship=SourceRelationship.DIFFERENT_CAPTURE_STALE,
    )
    result = evaluate(runtime)
    assert result.status is AdvisoryPolicyStatus.BLOCKED_PROVENANCE_INVALID
    assert "stale_resource_input_invalid" in result.block_reason_codes


def test_missing_page_provenance_blocks():
    runtime = replace(complete_runtime_input(), source_frame_sha256=None)
    result = evaluate(runtime)
    assert result.status is AdvisoryPolicyStatus.BLOCKED_PROVENANCE_INVALID


def test_tampered_runtime_authority_boundary_blocks_before_ranking():
    runtime = replace(complete_runtime_input(), execution_authorized=True)
    result = evaluate(runtime)
    assert result.status is AdvisoryPolicyStatus.BLOCKED_PROVENANCE_INVALID
    assert "runtime_input_authority_boundary_invalid" in result.block_reason_codes


def test_attempts_ranking_is_deterministic_and_prefers_more_attempts():
    runtime = complete_runtime_input()
    attempts = replace(runtime.candidate_prerequisites[1].remaining_attempts, remaining_attempts=5)
    runtime = replace_candidate(runtime, 1, remaining_attempts=attempts)
    selected_profile = profile(
        AdvisoryObjective.MAXIMIZE_AVAILABLE_ATTEMPTS,
        preferred_task_ids=(),
    )
    first = evaluate(runtime, selected_profile)
    second = evaluate(runtime, selected_profile)
    assert first.ranked_candidates == second.ranked_candidates
    assert first.recommended_known_task_id == "TASK_B"


def test_reward_priority_ranking_is_deterministic():
    runtime = complete_runtime_input()
    first_reward = replace(runtime.candidate_prerequisites[0].reward_target, reward_target_id="LOW")
    second_reward = replace(runtime.candidate_prerequisites[1].reward_target, reward_target_id="HIGH")
    runtime = replace_candidate(runtime, 0, reward_target=first_reward)
    runtime = replace_candidate(runtime, 1, reward_target=second_reward)
    selected_profile = profile(
        AdvisoryObjective.MAXIMIZE_PRIORITY_REWARD,
        preferred_task_ids=(),
        reward_priority_ids=("HIGH", "LOW"),
    )
    assert evaluate(runtime, selected_profile).recommended_known_task_id == "TASK_B"


def test_minimum_cost_ranking_prefers_lower_comparable_cost():
    runtime = complete_runtime_input()
    resource = replace(runtime.candidate_prerequisites[1].resource_balance, unit_cost=20)
    runtime = replace_candidate(runtime, 1, resource_balance=resource)
    result = evaluate(
        runtime,
        profile(AdvisoryObjective.MINIMIZE_RESOURCE_COST, preferred_task_ids=()),
    )
    assert result.recommended_known_task_id == "TASK_B"


def test_different_resource_costs_are_not_compared_without_conversion():
    runtime = complete_runtime_input()
    resource = replace(runtime.candidate_prerequisites[1].resource_balance, resource_id="TOKEN")
    runtime = replace_candidate(runtime, 1, resource_balance=resource)
    result = evaluate(
        runtime,
        profile(AdvisoryObjective.MINIMIZE_RESOURCE_COST, preferred_task_ids=()),
    )
    assert result.status is AdvisoryPolicyStatus.BLOCKED_POLICY_INVALID
    assert "resource_conversion_rule_missing" in result.block_reason_codes


def test_reward_objective_requires_priority_contract():
    result = evaluate(
        selected_profile=profile(
            AdvisoryObjective.MAXIMIZE_PRIORITY_REWARD,
            preferred_task_ids=(),
            reward_priority_ids=(),
        )
    )
    assert result.status is AdvisoryPolicyStatus.BLOCKED_POLICY_INVALID
    assert "reward_priority_missing" in result.block_reason_codes


def test_balanced_without_weights_is_policy_invalid():
    result = evaluate(
        selected_profile=profile(AdvisoryObjective.BALANCED, preferred_task_ids=())
    )
    assert result.status is AdvisoryPolicyStatus.BLOCKED_POLICY_INVALID
    assert result.block_reason_codes == ("balanced_weights_missing",)


def test_strategy_profile_binding_mismatch_blocks():
    result = evaluate(selected_profile=profile(strategy_version="other"))
    assert result.status is AdvisoryPolicyStatus.BLOCKED_POLICY_INVALID
    assert "strategy_profile_binding_mismatch" in result.block_reason_codes


def test_fact_acquisition_requests_are_read_only_and_do_not_execute_collection():
    result = evaluate_action_summary_policy_advisory(
        canonical_runtime_input(),
        incomplete_policy_snapshot(),
        evaluated_at=EVALUATED_AT,
    )
    requests = {request.missing_fact: request for request in result.fact_acquisition_requests}
    assert requests["resource_balance_unknown"].requires_page_input is False
    assert requests["remaining_attempts_unknown"].requires_page_input is None
    assert all(request.requires_business_input is False for request in requests.values())


def test_profile_round_trip_and_fingerprint_are_stable():
    original = profile()
    parsed = parse_action_summary_advisory_policy_profile(original.to_dict())
    assert parsed == original
    assert action_summary_advisory_policy_fingerprint(parsed) == original.policy_fingerprint


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("allow_unknown_reward", 1, "allow_unknown_reward_invalid"),
        ("maximum_task_runs", True, "maximum_task_runs_invalid"),
        ("objective", "DO_ANYTHING", "objective_invalid"),
        ("tie_break_order", ["RANDOM"], "tie_break_order_unsupported"),
    ],
)
def test_invalid_policy_fields_fail_closed(field, value, reason):
    document = profile().to_dict()
    document[field] = value
    with pytest.raises(ValueError, match=reason):
        parse_action_summary_advisory_policy_profile(document)


def test_every_result_has_zero_execution_and_authorization_authority():
    results = (
        evaluate(),
        evaluate_action_summary_policy_advisory(
            canonical_runtime_input(), incomplete_policy_snapshot(), evaluated_at=EVALUATED_AT
        ),
        evaluate(selected_profile=profile(AdvisoryObjective.BALANCED, preferred_task_ids=())),
    )
    assert all(result.execution_authorized is False for result in results)
    assert all(result.authorization_issued is False for result in results)
    assert all(result.business_dispatches == 0 for result in results)
    assert all(result.irreversible_actions == 0 for result in results)


def test_evaluator_module_has_no_control_automation_or_executor_dependency():
    source = Path("core/services/action_summary_advisory_policy.py").read_text(
        encoding="utf-8"
    )
    assert "core.control" not in source
    assert "auto.resident_activity" not in source
    assert "input_tap" not in source
    assert "input_swipe" not in source
    assert "evaluate_action_summary_business_policy" not in source
    assert "authorization_issuer" not in source


def test_import_does_not_initialize_real_backend_or_gui():
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; "
                "import core.services.action_summary_advisory_policy; "
                "assert 'core.control.control' not in sys.modules; "
                "assert 'auto.resident_activity' not in sys.modules; "
                "assert 'PySide6' not in sys.modules"
            ),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
