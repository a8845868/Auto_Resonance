from __future__ import annotations

from dataclasses import replace
import subprocess
import sys

from core.services.action_summary_business_policy import (
    BusinessPolicyStatus,
    CandidateDisposition,
    evaluate_action_summary_business_policy,
)
from core.services.action_summary_policy_prerequisites import (
    PrerequisiteModelStatus,
    StrategyObjective,
    evaluate_action_summary_policy_prerequisites,
    task_identity_from_page_model,
)
from tests.action_summary_policy_prerequisite_model_test import (
    card,
    complete_inputs,
    page_model,
)


def candidate_models(
    *,
    objective: StrategyObjective = StrategyObjective.MAXIMIZE_TARGET_REWARD,
    target_current: int = 20,
    target_amount: int = 100,
    first_yield: int = 30,
    second_yield: int = 25,
):
    first_card = replace(
        card(match_key="MATCH_A", semantic_id="TASK_A", cost=40),
        available_actions=frozenset({
            "CARD_SELECTABLE",
            "CHALLENGE_AVAILABLE",
            "TASK_EXECUTION_AVAILABLE",
        }),
    )
    second_card = replace(
        card(match_key="MATCH_B", semantic_id="TASK_B", cost=20),
        available_actions=frozenset({
            "CARD_SELECTABLE",
            "SWEEP_AVAILABLE",
            "TASK_EXECUTION_AVAILABLE",
        }),
    )
    page = page_model(first_card, second_card)
    base = complete_inputs(page)
    strategy = replace(
        base.strategy,
        objective=objective,
        allowed_action_types=frozenset({"CHALLENGE", "SWEEP"}),
    )

    def build(match_key: str, cost: int, reward_yield: int):
        identity = task_identity_from_page_model(page, match_key)
        inputs = replace(
            base,
            task_identity=identity,
            resource_balance=replace(base.resource_balance, unit_cost=cost),
            reward_target=replace(
                base.reward_target,
                current_amount=target_current,
                target_amount=target_amount,
                candidate_reward_amount=reward_yield,
            ),
            strategy=strategy,
        )
        result = evaluate_action_summary_policy_prerequisites(page, inputs)
        assert result.status is PrerequisiteModelStatus.READY_FOR_POLICY_EVALUATION
        return result

    return (
        build("MATCH_A", 40, first_yield),
        build("MATCH_B", 20, second_yield),
    )


def test_maximize_reward_selects_highest_bounded_gain():
    candidates = candidate_models()
    result = evaluate_action_summary_business_policy(candidates)

    assert result.status is BusinessPolicyStatus.RECOMMENDATION_AVAILABLE
    assert result.recommendation is not None
    assert result.recommendation.candidate_card_match_key == "MATCH_A"
    assert result.recommendation.task_semantic_id == "TASK_A"
    assert result.recommendation.recommended_action_type == "CHALLENGE"
    assert result.recommendation.recommended_executions == 2
    assert result.recommendation.estimated_resource_spend == 80
    assert result.recommendation.estimated_reward_gain == 60


def test_conserve_fatigue_selects_best_cost_per_reward():
    result = evaluate_action_summary_business_policy(candidate_models(
        objective=StrategyObjective.CONSERVE_FATIGUE,
    ))

    assert result.status is BusinessPolicyStatus.RECOMMENDATION_AVAILABLE
    assert result.recommendation is not None
    assert result.recommendation.candidate_card_match_key == "MATCH_B"
    assert result.recommendation.recommended_action_type == "SWEEP"
    assert result.recommendation.estimated_resource_spend == 40
    assert result.recommendation.estimated_reward_gain == 50


def test_complete_target_prefers_candidate_that_can_finish():
    result = evaluate_action_summary_business_policy(candidate_models(
        objective=StrategyObjective.COMPLETE_TARGET,
        first_yield=30,
        second_yield=50,
    ))

    assert result.recommendation is not None
    assert result.recommendation.candidate_card_match_key == "MATCH_B"
    assert result.recommendation.completes_reward_target is True
    assert result.recommendation.recommended_executions == 2


def test_recommendation_is_capped_to_reward_target_need():
    result = evaluate_action_summary_business_policy(candidate_models(
        target_current=90,
        target_amount=100,
    ))

    assert result.recommendation is not None
    assert result.recommendation.recommended_executions == 1
    assert result.recommendation.estimated_reward_gain == 10
    assert result.recommendation.completes_reward_target is True


def test_strategy_observe_only_never_selects_action():
    candidates = candidate_models(objective=StrategyObjective.OBSERVE_ONLY)
    candidates = tuple(replace(
        candidate,
        strategy=replace(
            candidate.strategy,
            allowed_action_types=frozenset(),
            max_task_executions=0,
        ),
        bounded_candidate_executions=0,
    ) for candidate in candidates)
    result = evaluate_action_summary_business_policy(candidates)

    assert result.status is BusinessPolicyStatus.NO_ACTION_REQUIRED
    assert result.reason_codes == ("strategy_observe_only",)
    assert result.recommendation is None


def test_target_already_met_is_no_action_required():
    result = evaluate_action_summary_business_policy(candidate_models(
        target_current=100,
        target_amount=100,
    ))

    assert result.status is BusinessPolicyStatus.NO_ACTION_REQUIRED
    assert result.reason_codes == ("reward_target_already_met",)
    assert all(
        assessment.disposition is CandidateDisposition.TARGET_ALREADY_MET
        for assessment in result.candidate_assessments
    )


def test_zero_capacity_is_no_feasible_candidate():
    candidates = tuple(replace(
        candidate,
        resource_balance=replace(
            candidate.resource_balance,
            available_amount=0,
        ),
        fatigue_budget=replace(
            candidate.fatigue_budget,
            available_fatigue=0,
            reserved_fatigue=0,
            max_policy_spend=0,
        ),
        bounded_candidate_executions=0,
    ) for candidate in candidate_models())
    result = evaluate_action_summary_business_policy(candidates)

    assert result.status is BusinessPolicyStatus.NO_FEASIBLE_CANDIDATE
    assert result.reason_codes == ("candidate_execution_capacity_zero",)
    assert result.recommendation is None


def test_no_shared_allowed_action_is_no_feasible_candidate():
    candidates = tuple(replace(
        candidate,
        strategy=replace(
            candidate.strategy,
            allowed_action_types=frozenset(),
        ),
    ) for candidate in candidate_models())
    result = evaluate_action_summary_business_policy(candidates)

    assert result.status is BusinessPolicyStatus.NO_FEASIBLE_CANDIDATE
    assert result.reason_codes == ("candidate_has_no_policy_allowed_action",)


def test_sweep_is_preferred_when_both_actions_are_feasible():
    first, second = candidate_models()
    both = replace(
        first,
        candidate_available_action_types=frozenset({"CHALLENGE", "SWEEP"}),
    )
    result = evaluate_action_summary_business_policy((both, second))

    assert result.recommendation is not None
    assert result.recommendation.recommended_action_type == "SWEEP"


def test_partial_candidate_set_is_rejected():
    first, _second = candidate_models()
    result = evaluate_action_summary_business_policy((first,))

    assert result.status is BusinessPolicyStatus.BLOCKED_CONFLICTING_INPUTS
    assert "policy_candidate_set_incomplete" in result.reason_codes


def test_empty_candidate_set_blocks_without_hash_or_recommendation():
    result = evaluate_action_summary_business_policy(())

    assert result.status is BusinessPolicyStatus.BLOCKED_PREREQUISITES
    assert result.reason_codes == ("no_policy_candidates",)
    assert result.policy_input_sha256 is None
    assert result.recommendation is None


def test_any_incomplete_candidate_blocks_global_ranking():
    first, second = candidate_models()
    incomplete = replace(
        second,
        status=PrerequisiteModelStatus.BLOCKED_INCOMPLETE_FACTS,
        reason_codes=("remaining_attempts_unknown",),
        policy_evaluation_allowed=False,
    )
    result = evaluate_action_summary_business_policy((first, incomplete))

    assert result.status is BusinessPolicyStatus.BLOCKED_PREREQUISITES
    assert result.reason_codes == ("candidate_prerequisites_not_ready",)
    assert result.recommendation is None
    assert result.candidate_assessments[1].disposition is (
        CandidateDisposition.BLOCKED_PREREQUISITES
    )


def test_candidates_must_share_one_capture_binding():
    first, second = candidate_models()
    second = replace(second, source_capture_id="another-capture")
    result = evaluate_action_summary_business_policy((first, second))

    assert result.status is BusinessPolicyStatus.BLOCKED_CONFLICTING_INPUTS
    assert "candidate_capture_binding_conflict" in result.reason_codes


def test_duplicate_candidate_key_blocks_selection():
    first, second = candidate_models()
    second = replace(second, candidate_card_match_key="MATCH_A")
    result = evaluate_action_summary_business_policy((first, second))

    assert result.status is BusinessPolicyStatus.BLOCKED_CONFLICTING_INPUTS
    assert "candidate_identity_conflict" in result.reason_codes


def test_candidates_must_share_reward_target():
    first, second = candidate_models()
    second = replace(
        second,
        reward_target=replace(second.reward_target, target_amount=200),
    )
    result = evaluate_action_summary_business_policy((first, second))

    assert result.status is BusinessPolicyStatus.BLOCKED_CONFLICTING_INPUTS
    assert "reward_target_conflict" in result.reason_codes


def test_candidates_must_share_strategy_contract():
    first, second = candidate_models()
    second = replace(
        second,
        strategy=replace(second.strategy, strategy_version="2"),
    )
    result = evaluate_action_summary_business_policy((first, second))

    assert result.status is BusinessPolicyStatus.BLOCKED_CONFLICTING_INPUTS
    assert "strategy_contract_conflict" in result.reason_codes


def test_candidates_must_share_resource_and_fatigue_balances():
    first, second = candidate_models()
    second = replace(
        second,
        resource_balance=replace(second.resource_balance, available_amount=100),
    )
    result = evaluate_action_summary_business_policy((first, second))

    assert result.status is BusinessPolicyStatus.BLOCKED_CONFLICTING_INPUTS
    assert "shared_budget_conflict" in result.reason_codes


def test_prerequisite_scope_must_be_exact():
    first, second = candidate_models()
    first = replace(first, model_scope="SOMETHING_ELSE")
    result = evaluate_action_summary_business_policy((first, second))

    assert result.status is BusinessPolicyStatus.BLOCKED_CONFLICTING_INPUTS
    assert "prerequisite_model_scope_invalid" in result.reason_codes


def test_forged_ready_model_cannot_cross_zero_authority_boundary():
    first, second = candidate_models()
    forged = replace(first, execution_authorized=True)
    result = evaluate_action_summary_business_policy((forged, second))

    assert result.status is BusinessPolicyStatus.BLOCKED_CONFLICTING_INPUTS
    assert "prerequisite_authority_boundary_invalid" in result.reason_codes
    assert result.recommendation is None


def test_forged_execution_bound_is_recomputed_and_rejected():
    first, second = candidate_models()
    forged = replace(first, bounded_candidate_executions=10)
    result = evaluate_action_summary_business_policy((forged, second))

    assert result.status is BusinessPolicyStatus.BLOCKED_CONFLICTING_INPUTS
    assert "candidate_execution_bound_invalid" in result.reason_codes


def test_non_policy_candidate_action_type_is_rejected():
    first, second = candidate_models()
    forged = replace(
        first,
        candidate_available_action_types=frozenset({"CLAIM"}),
    )
    result = evaluate_action_summary_business_policy((forged, second))

    assert result.status is BusinessPolicyStatus.BLOCKED_CONFLICTING_INPUTS
    assert "candidate_action_types_invalid" in result.reason_codes


def test_deterministic_input_and_decision_hashes():
    candidates = candidate_models()
    first = evaluate_action_summary_business_policy(candidates)
    second = evaluate_action_summary_business_policy(candidates)
    changed_candidates = (
        replace(
            candidates[0],
            reward_target=replace(
                candidates[0].reward_target,
                candidate_reward_amount=31,
            ),
        ),
        candidates[1],
    )
    changed = evaluate_action_summary_business_policy(changed_candidates)

    assert first.policy_input_sha256 == second.policy_input_sha256
    assert first.recommendation is not None
    assert second.recommendation is not None
    assert first.recommendation.decision_id == second.recommendation.decision_id
    assert changed.policy_input_sha256 != first.policy_input_sha256


def test_tie_breaking_is_stable_by_card_match_key():
    first, second = candidate_models(first_yield=30, second_yield=30)
    second = replace(
        second,
        resource_balance=replace(second.resource_balance, unit_cost=40),
    )
    result = evaluate_action_summary_business_policy((second, first))

    assert result.recommendation is not None
    assert result.recommendation.candidate_card_match_key == "MATCH_A"


def test_recommendation_is_advisory_and_cannot_authorize_execution():
    result = evaluate_action_summary_business_policy(candidate_models())

    assert result.advisory_only is True
    assert result.execution_authorized is False
    assert result.authorization_issued is False
    assert result.business_dispatches == 0
    assert result.irreversible_actions == 0
    assert result.recommendation is not None
    assert result.recommendation.advisory_only is True
    assert result.recommendation.execution_authorized is False
    assert result.recommendation.required_future_authorization == (
        "EXPLICIT_POLICY_ADOPTION",
        "EXECUTION_AUTHORITY",
    )


def test_serialized_policy_result_is_machine_readable_and_non_executable():
    document = evaluate_action_summary_business_policy(candidate_models()).to_dict()

    assert document["schema_version"] == "1.0"
    assert document["model_scope"] == "ACTION_SUMMARY_BUSINESS_POLICY_EVALUATOR_V1"
    assert document["status"] == "RECOMMENDATION_AVAILABLE"
    assert document["candidate_count"] == 2
    assert document["candidate_assessments"][0]["disposition"] == "ELIGIBLE"
    assert document["recommendation"]["objective"] == "MAXIMIZE_TARGET_REWARD"
    assert document["execution_authorized"] is False
    assert document["authorization_issued"] is False
    assert document["business_dispatches"] == 0


def test_policy_module_has_no_control_auto_or_interlock_dependency():
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; "
                "import core.services.action_summary_business_policy; "
                "assert 'core.control.control' not in sys.modules; "
                "assert 'auto' not in sys.modules; "
                "assert 'core.services.action_summary_execution_interlock' "
                "not in sys.modules"
            ),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
