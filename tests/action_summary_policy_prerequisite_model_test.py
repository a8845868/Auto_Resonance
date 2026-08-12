from __future__ import annotations

from dataclasses import replace
import subprocess
import sys

from core.services.action_summary_policy_prerequisites import (
    ActionSummaryPolicyPrerequisiteInput,
    FactProvenance,
    FactSource,
    FatigueBudgetFact,
    PrerequisiteFactStatus,
    PrerequisiteModelStatus,
    RemainingAttemptsFact,
    ResourceBalanceFact,
    RewardTargetFact,
    StrategyInputContract,
    StrategyObjective,
    TaskSemanticIdentityFact,
    evaluate_action_summary_policy_prerequisites,
    task_identity_from_page_model,
)
from core.services.action_summary_product_model import (
    ActionSummaryPageActions,
    ActionSummaryPageModel,
    ActionSummaryTaskCard,
    PageConfidence,
    RewardState,
    TaskCardState,
)


CAPTURED_AT = "2026-07-26T12:00:00+08:00"
FRAME_HASH = "a" * 64
FRESHNESS_TOKEN = "b" * 64


def page_provenance(source: FactSource) -> FactProvenance:
    return FactProvenance(
        source=source,
        revision=f"{source.value.lower()}-v1",
        observed_at="2026-07-26T11:59:00+08:00",
        valid_until="2026-07-26T12:05:00+08:00",
        evidence_ids=(f"evidence-{source.value.lower()}",),
    )


def card(
    *,
    match_key: str = "MATCH_CARD_1",
    semantic_id: str = "TASK_SEMANTIC_1",
    state: TaskCardState = TaskCardState.AVAILABLE,
    remaining_attempts: int | None = None,
    cost: int | None = 40,
    cost_resource_id: str | None = "UNKNOWN",
) -> ActionSummaryTaskCard:
    return ActionSummaryTaskCard(
        semantic_id=semantic_id,
        card_instance_id=f"INSTANCE_{semantic_id}",
        card_match_key=match_key,
        title_hash="c" * 64,
        bbox=(100, 200, 300, 500),
        state=state,
        available_actions=frozenset({
            "CARD_SELECTABLE",
            "CHALLENGE_AVAILABLE",
            "TASK_EXECUTION_AVAILABLE",
        }),
        remaining_attempts=remaining_attempts,
        cost=cost,
        cost_resource_id=cost_resource_id,
        reward_state=RewardState.UNKNOWN,
        confidence=PageConfidence.HIGH,
        evidence_ids=("card-title", "card-anchor"),
    )


def page_model(*cards: ActionSummaryTaskCard) -> ActionSummaryPageModel:
    cards = cards or (card(),)
    return ActionSummaryPageModel(
        model_scope="ACTION_SUMMARY_READ_ONLY_V1",
        activity_family="SIEGE",
        activity_title_hash="d" * 64,
        source_capture_id="capture-policy-v1",
        source_frame_sha256=FRAME_HASH,
        captured_at=CAPTURED_AT,
        model_freshness_token=FRESHNESS_TOKEN,
        page_state="ACTION_SUMMARY_VISIBLE",
        page_confidence=PageConfidence.HIGH,
        overlay_states=(),
        visible_task_cards=len(cards),
        selected_task_id=None,
        task_cards=tuple(cards),
        page_actions=ActionSummaryPageActions(can_open_task=True, can_challenge=True),
        page_capabilities=frozenset({
            "VIEW_TASK_LIST",
            "CARD_SELECTABLE",
            "CHALLENGE_AVAILABLE",
            "TASK_EXECUTION_AVAILABLE",
        }),
        attempts_exhausted=None,
        resource_insufficient=None,
        evidence_ids=("page-title",),
    )


def complete_inputs(model: ActionSummaryPageModel) -> ActionSummaryPolicyPrerequisiteInput:
    return ActionSummaryPolicyPrerequisiteInput(
        task_identity=task_identity_from_page_model(model, "MATCH_CARD_1"),
        remaining_attempts=RemainingAttemptsFact(
            status=PrerequisiteFactStatus.KNOWN,
            remaining_attempts=3,
            total_attempts=5,
            provenance=page_provenance(FactSource.GAME_OBSERVED),
        ),
        resource_balance=ResourceBalanceFact(
            status=PrerequisiteFactStatus.KNOWN,
            resource_id="FATIGUE",
            available_amount=120,
            unit_cost=40,
            provenance=page_provenance(FactSource.GAME_OBSERVED),
        ),
        reward_target=RewardTargetFact(
            status=PrerequisiteFactStatus.KNOWN,
            reward_target_id="ACTIVITY_PROGRESS",
            current_amount=20,
            target_amount=100,
            candidate_reward_amount=30,
            provenance=page_provenance(FactSource.USER_CONFIGURED),
        ),
        fatigue_budget=FatigueBudgetFact(
            status=PrerequisiteFactStatus.KNOWN,
            available_fatigue=120,
            reserved_fatigue=20,
            max_policy_spend=80,
            fatigue_unit_id="FATIGUE",
            fatigue_cost_per_run=10,
            fatigue_cost_applicable=True,
            provenance=page_provenance(FactSource.GAME_OBSERVED),
        ),
        strategy=StrategyInputContract(
            schema_version="1.0",
            strategy_id="complete-target",
            strategy_version="1",
            objective=StrategyObjective.COMPLETE_TARGET,
            allowed_action_types=frozenset({"CHALLENGE"}),
            max_task_executions=2,
            provenance=page_provenance(FactSource.USER_CONFIGURED),
        ),
    )


def evaluate(
    model: ActionSummaryPageModel | None = None,
    inputs: ActionSummaryPolicyPrerequisiteInput | None = None,
):
    model = model or page_model()
    return evaluate_action_summary_policy_prerequisites(
        model,
        inputs or complete_inputs(model),
    )


def test_complete_facts_are_ready_for_policy_evaluation_only():
    result = evaluate()

    assert result.status is PrerequisiteModelStatus.READY_FOR_POLICY_EVALUATION
    assert result.reason_codes == ()
    assert result.policy_evaluation_allowed is True
    assert result.candidate_available_action_types == frozenset({"CHALLENGE"})
    assert result.bounded_candidate_executions == 2
    assert result.execution_authorized is False
    assert result.selected_action is None
    assert result.business_dispatches == 0
    assert result.irreversible_actions == 0


def test_task_identity_is_bound_to_page_capture_and_card():
    model = page_model()
    identity = task_identity_from_page_model(model, "MATCH_CARD_1")

    assert identity.status is PrerequisiteFactStatus.KNOWN
    assert identity.activity_family == model.activity_family
    assert identity.task_semantic_id == "TASK_SEMANTIC_1"
    assert identity.card_match_key == "MATCH_CARD_1"
    assert identity.source_capture_id == model.source_capture_id
    assert identity.source_frame_sha256 == model.source_frame_sha256
    assert identity.model_freshness_token == model.model_freshness_token


def test_unknown_card_key_does_not_invent_task_identity():
    identity = task_identity_from_page_model(page_model(), "NOT_PRESENT")

    assert identity == TaskSemanticIdentityFact(PrerequisiteFactStatus.UNKNOWN)


def test_unknown_task_identity_is_incomplete_not_ready():
    model = page_model()
    inputs = replace(
        complete_inputs(model),
        task_identity=TaskSemanticIdentityFact(PrerequisiteFactStatus.UNKNOWN),
    )
    result = evaluate(model, inputs)

    assert result.status is PrerequisiteModelStatus.BLOCKED_INCOMPLETE_FACTS
    assert "task_identity_unknown" in result.reason_codes


def test_task_identity_from_other_capture_is_conflicting():
    model = page_model()
    identity = replace(
        task_identity_from_page_model(model, "MATCH_CARD_1"),
        source_capture_id="different-capture",
    )
    result = evaluate(model, replace(complete_inputs(model), task_identity=identity))

    assert result.status is PrerequisiteModelStatus.BLOCKED_CONFLICTING_FACTS
    assert "task_identity_model_binding_mismatch" in result.reason_codes


def test_duplicate_card_match_key_is_conflicting():
    duplicate = replace(card(), card_instance_id="INSTANCE_DUPLICATE")
    model = page_model(card(), duplicate)
    inputs = complete_inputs(model)
    inputs = replace(
        inputs,
        task_identity=task_identity_from_page_model(
            page_model(), "MATCH_CARD_1"
        ),
    )
    result = evaluate(model, inputs)

    assert result.status is PrerequisiteModelStatus.BLOCKED_CONFLICTING_FACTS
    assert "task_identity_card_ambiguous" in result.reason_codes


def test_non_available_candidate_is_not_promoted_to_policy_input():
    model = page_model(card(state=TaskCardState.LOCKED))
    result = evaluate(model, complete_inputs(model))

    assert result.status is PrerequisiteModelStatus.BLOCKED_CONFLICTING_FACTS
    assert "candidate_task_not_available" in result.reason_codes


def test_unknown_remaining_attempts_are_incomplete():
    model = page_model()
    inputs = replace(
        complete_inputs(model),
        remaining_attempts=RemainingAttemptsFact(PrerequisiteFactStatus.UNKNOWN),
    )
    result = evaluate(model, inputs)

    assert result.status is PrerequisiteModelStatus.BLOCKED_INCOMPLETE_FACTS
    assert "remaining_attempts_unknown" in result.reason_codes
    assert result.bounded_candidate_executions is None


def test_invalid_or_page_conflicting_attempt_counts_fail_closed():
    model = page_model(card(remaining_attempts=2))
    inputs = complete_inputs(model)
    invalid = replace(
        inputs.remaining_attempts,
        remaining_attempts=3,
        total_attempts=2,
    )
    result = evaluate(model, replace(inputs, remaining_attempts=invalid))

    assert result.status is PrerequisiteModelStatus.BLOCKED_CONFLICTING_FACTS
    assert "remaining_attempts_invalid" in result.reason_codes
    assert "remaining_attempts_page_mismatch" in result.reason_codes


def test_unknown_resource_identity_or_balance_is_incomplete():
    model = page_model()
    resource = replace(
        complete_inputs(model).resource_balance,
        resource_id="UNKNOWN",
    )
    result = evaluate(model, replace(complete_inputs(model), resource_balance=resource))

    assert result.status is PrerequisiteModelStatus.BLOCKED_INCOMPLETE_FACTS
    assert "resource_balance_value_missing" in result.reason_codes


def test_resource_cost_must_match_page_fact():
    model = page_model(card(cost=40))
    inputs = complete_inputs(model)
    resource = replace(inputs.resource_balance, unit_cost=30)
    result = evaluate(model, replace(inputs, resource_balance=resource))

    assert result.status is PrerequisiteModelStatus.BLOCKED_CONFLICTING_FACTS
    assert "resource_unit_cost_page_mismatch" in result.reason_codes


def test_insufficient_resource_is_a_known_fact_not_an_execution_decision():
    model = page_model()
    inputs = complete_inputs(model)
    resource = replace(inputs.resource_balance, available_amount=20)
    fatigue = replace(inputs.fatigue_budget, available_fatigue=20, reserved_fatigue=0)
    result = evaluate(
        model,
        replace(inputs, resource_balance=resource, fatigue_budget=fatigue),
    )

    assert result.status is PrerequisiteModelStatus.BLOCKED_CONFLICTING_FACTS
    assert "fatigue_budget_invalid" in result.reason_codes
    assert result.execution_authorized is False


def test_zero_affordability_can_be_complete_without_authorizing_action():
    model = page_model()
    inputs = complete_inputs(model)
    resource = replace(inputs.resource_balance, available_amount=20)
    fatigue = replace(
        inputs.fatigue_budget,
        available_fatigue=20,
        reserved_fatigue=0,
        max_policy_spend=20,
    )
    result = evaluate(
        model,
        replace(inputs, resource_balance=resource, fatigue_budget=fatigue),
    )

    assert result.status is PrerequisiteModelStatus.READY_FOR_POLICY_EVALUATION
    assert result.resource_balance.affordable_attempts == 0
    assert result.bounded_candidate_executions == 0
    assert result.selected_action is None


def test_reward_target_is_advisory_and_never_claim_authority():
    result = evaluate()

    assert result.reward_target.reward_target_id == "ACTIVITY_PROGRESS"
    assert result.reward_target.remaining_to_target == 80
    assert result.execution_authorized is False
    assert "CLAIM" not in result.strategy.allowed_action_types


def test_reward_target_can_be_not_applicable_only_for_observe_only():
    model = page_model()
    inputs = complete_inputs(model)
    reward = RewardTargetFact(PrerequisiteFactStatus.NOT_APPLICABLE)
    observe = replace(
        inputs.strategy,
        objective=StrategyObjective.OBSERVE_ONLY,
        allowed_action_types=frozenset(),
        max_task_executions=0,
    )
    observe_result = evaluate(
        model,
        replace(inputs, reward_target=reward, strategy=observe),
    )
    action_result = evaluate(model, replace(inputs, reward_target=reward))

    assert observe_result.status is (
        PrerequisiteModelStatus.READY_FOR_POLICY_EVALUATION
    )
    assert action_result.status is PrerequisiteModelStatus.BLOCKED_INCOMPLETE_FACTS
    assert "reward_target_required_for_strategy" in action_result.reason_codes


def test_fatigue_budget_cannot_spend_reserved_fatigue():
    model = page_model()
    inputs = complete_inputs(model)
    fatigue = replace(inputs.fatigue_budget, max_policy_spend=101)
    result = evaluate(model, replace(inputs, fatigue_budget=fatigue))

    assert result.status is PrerequisiteModelStatus.BLOCKED_CONFLICTING_FACTS
    assert "fatigue_budget_invalid" in result.reason_codes


def test_fatigue_resource_balance_must_reconcile():
    model = page_model()
    inputs = complete_inputs(model)
    fatigue = replace(inputs.fatigue_budget, available_fatigue=100)
    result = evaluate(model, replace(inputs, fatigue_budget=fatigue))

    assert result.status is PrerequisiteModelStatus.BLOCKED_CONFLICTING_FACTS
    assert "fatigue_resource_balance_mismatch" in result.reason_codes


def test_strategy_contract_rejects_unknown_actions_and_unbounded_limit():
    model = page_model()
    inputs = complete_inputs(model)
    strategy = replace(
        inputs.strategy,
        allowed_action_types=frozenset({"CLAIM"}),
        max_task_executions=999,
    )
    result = evaluate(model, replace(inputs, strategy=strategy))

    assert result.status is PrerequisiteModelStatus.BLOCKED_CONFLICTING_FACTS
    assert "strategy_action_type_unsupported" in result.reason_codes
    assert "strategy_execution_limit_invalid" in result.reason_codes


def test_boolean_values_are_not_accepted_as_integer_facts():
    model = page_model()
    inputs = complete_inputs(model)
    attempts = replace(inputs.remaining_attempts, remaining_attempts=True)
    resource = replace(inputs.resource_balance, available_amount=True)
    fatigue = replace(inputs.fatigue_budget, max_policy_spend=True)
    strategy = replace(inputs.strategy, max_task_executions=True)
    result = evaluate(model, replace(
        inputs,
        remaining_attempts=attempts,
        resource_balance=resource,
        fatigue_budget=fatigue,
        strategy=strategy,
    ))

    assert result.status is PrerequisiteModelStatus.BLOCKED_CONFLICTING_FACTS
    assert "remaining_attempts_value_missing" in result.reason_codes
    assert "resource_balance_value_missing" in result.reason_codes
    assert "fatigue_budget_value_missing" in result.reason_codes
    assert "strategy_execution_limit_invalid" in result.reason_codes
    assert result.bounded_candidate_executions is None


def test_malformed_enum_values_fail_closed_and_remain_serializable():
    model = page_model()
    inputs = complete_inputs(model)
    resource = replace(inputs.resource_balance, status="KNOWN")  # type: ignore[arg-type]
    strategy = replace(
        inputs.strategy,
        objective="DO_ANYTHING",  # type: ignore[arg-type]
    )
    result = evaluate(
        model,
        replace(inputs, resource_balance=resource, strategy=strategy),
    )
    document = result.to_dict()

    assert result.status is PrerequisiteModelStatus.BLOCKED_CONFLICTING_FACTS
    assert "resource_balance_status_invalid" in result.reason_codes
    assert "strategy_objective_invalid" in result.reason_codes
    assert document["resource_balance"]["status"] == "KNOWN"
    assert document["strategy"]["objective"] == "DO_ANYTHING"
    assert document["execution_authorized"] is False


def test_stale_or_wrong_source_provenance_is_conflicting():
    model = page_model()
    inputs = complete_inputs(model)
    stale = replace(
        page_provenance(FactSource.USER_CONFIGURED),
        valid_until="2026-07-26T11:59:30+08:00",
    )
    attempts = replace(inputs.remaining_attempts, provenance=stale)
    result = evaluate(model, replace(inputs, remaining_attempts=attempts))

    assert result.status is PrerequisiteModelStatus.BLOCKED_CONFLICTING_FACTS
    assert "remaining_attempts_source_invalid" in result.reason_codes
    assert "remaining_attempts_stale" in result.reason_codes


def test_missing_provenance_is_incomplete():
    model = page_model()
    inputs = complete_inputs(model)
    resource = replace(inputs.resource_balance, provenance=None)
    result = evaluate(model, replace(inputs, resource_balance=resource))

    assert result.status is PrerequisiteModelStatus.BLOCKED_INCOMPLETE_FACTS
    assert "resource_balance_provenance_missing" in result.reason_codes


def test_untrusted_page_or_overlay_cannot_become_policy_ready():
    model = replace(page_model(), overlay_states=("DAILY_CHECKIN",))
    result = evaluate(model, complete_inputs(model))

    assert result.status is PrerequisiteModelStatus.BLOCKED_CONFLICTING_FACTS
    assert "source_page_not_policy_eligible" in result.reason_codes


def test_serialization_is_machine_readable_and_contains_no_action():
    document = evaluate().to_dict()

    assert document["schema_version"] == "1.0"
    assert document["model_scope"] == "ACTION_SUMMARY_POLICY_PREREQUISITES_V1"
    assert document["status"] == "READY_FOR_POLICY_EVALUATION"
    assert document["remaining_attempts"]["status"] == "KNOWN"
    assert document["resource_balance"]["affordable_attempts"] == 3
    assert document["reward_target"]["remaining_to_target"] == 80
    assert document["fatigue_budget"]["spendable_fatigue"] == 100
    assert document["strategy"]["allowed_action_types"] == ["CHALLENGE"]
    assert document["candidate_available_action_types"] == ["CHALLENGE"]
    assert document["execution_authorized"] is False
    assert document["selected_action"] is None
    assert document["business_dispatches"] == 0


def test_policy_prerequisite_module_has_no_control_or_auto_dependency():
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; "
                "import core.services.action_summary_policy_prerequisites; "
                "assert 'core.control.control' not in sys.modules; "
                "assert 'auto' not in sys.modules"
            ),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
