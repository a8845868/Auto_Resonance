"""Advisory-only business policy for action-summary candidates.

The evaluator consumes only validated prerequisite models.  It can produce a
deterministic recommendation, but it cannot issue authorization, call an
executor, persist an occurrence, or dispatch game input.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from enum import Enum
from fractions import Fraction
from typing import Sequence

from core.services.action_summary_policy_prerequisites import (
    ActionSummaryPolicyPrerequisiteModel,
    PrerequisiteModelStatus,
    StrategyObjective,
)


class BusinessPolicyStatus(str, Enum):
    RECOMMENDATION_AVAILABLE = "RECOMMENDATION_AVAILABLE"
    NO_ACTION_REQUIRED = "NO_ACTION_REQUIRED"
    BLOCKED_PREREQUISITES = "BLOCKED_PREREQUISITES"
    BLOCKED_CONFLICTING_INPUTS = "BLOCKED_CONFLICTING_INPUTS"
    NO_FEASIBLE_CANDIDATE = "NO_FEASIBLE_CANDIDATE"


class CandidateDisposition(str, Enum):
    ELIGIBLE = "ELIGIBLE"
    NO_CAPACITY = "NO_CAPACITY"
    TARGET_ALREADY_MET = "TARGET_ALREADY_MET"
    NO_ALLOWED_ACTION = "NO_ALLOWED_ACTION"
    BLOCKED_PREREQUISITES = "BLOCKED_PREREQUISITES"


@dataclass(frozen=True, slots=True)
class ActionSummaryCandidateAssessment:
    candidate_card_match_key: str | None
    task_semantic_id: str | None
    disposition: CandidateDisposition
    reason_codes: tuple[str, ...]
    available_action_types: frozenset[str]
    policy_allowed_action_types: frozenset[str]
    feasible_action_types: frozenset[str]
    recommended_action_type: str | None
    bounded_executions: int | None
    recommended_executions: int
    unit_resource_cost: int | None
    reward_per_execution: int | None
    estimated_resource_spend: int
    estimated_reward_gain: int
    completes_reward_target: bool

    def to_dict(self) -> dict[str, object]:
        document = asdict(self)
        document["disposition"] = self.disposition.value
        for field in (
            "available_action_types",
            "policy_allowed_action_types",
            "feasible_action_types",
        ):
            document[field] = sorted(document[field])
        return document


@dataclass(frozen=True, slots=True)
class ActionSummaryPolicyRecommendation:
    decision_id: str
    policy_id: str
    policy_version: str
    objective: StrategyObjective
    candidate_card_match_key: str
    task_semantic_id: str
    recommended_action_type: str
    recommended_executions: int
    estimated_resource_spend: int
    estimated_reward_gain: int
    completes_reward_target: bool
    advisory_only: bool
    execution_authorized: bool
    required_future_authorization: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        document = asdict(self)
        document["objective"] = self.objective.value
        return document


@dataclass(frozen=True, slots=True)
class ActionSummaryBusinessPolicyEvaluation:
    schema_version: str
    model_scope: str
    policy_id: str
    policy_version: str
    status: BusinessPolicyStatus
    reason_codes: tuple[str, ...]
    source_capture_id: str | None
    source_frame_sha256: str | None
    source_model_freshness_token: str | None
    candidate_count: int
    candidate_assessments: tuple[ActionSummaryCandidateAssessment, ...]
    recommendation: ActionSummaryPolicyRecommendation | None
    policy_input_sha256: str | None
    advisory_only: bool
    execution_authorized: bool
    authorization_issued: bool
    business_dispatches: int
    irreversible_actions: int

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "model_scope": self.model_scope,
            "policy_id": self.policy_id,
            "policy_version": self.policy_version,
            "status": self.status.value,
            "reason_codes": list(self.reason_codes),
            "source_capture_id": self.source_capture_id,
            "source_frame_sha256": self.source_frame_sha256,
            "source_model_freshness_token": self.source_model_freshness_token,
            "candidate_count": self.candidate_count,
            "candidate_assessments": [
                assessment.to_dict() for assessment in self.candidate_assessments
            ],
            "recommendation": (
                self.recommendation.to_dict() if self.recommendation else None
            ),
            "policy_input_sha256": self.policy_input_sha256,
            "advisory_only": self.advisory_only,
            "execution_authorized": self.execution_authorized,
            "authorization_issued": self.authorization_issued,
            "business_dispatches": self.business_dispatches,
            "irreversible_actions": self.irreversible_actions,
        }


_SCHEMA_VERSION = "1.0"
_MODEL_SCOPE = "ACTION_SUMMARY_BUSINESS_POLICY_EVALUATOR_V1"
_POLICY_ID = "BOUNDED_REWARD_PROGRESS"
_POLICY_VERSION = "1.0"
_PREREQUISITE_SCOPE = "ACTION_SUMMARY_POLICY_PREREQUISITES_V1"
_ACTION_TYPES = frozenset({"CHALLENGE", "SWEEP"})


def _deduplicate(values: Sequence[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))


def _input_sha256(
    candidates: Sequence[ActionSummaryPolicyPrerequisiteModel],
) -> str:
    payload = json.dumps(
        [candidate.to_dict() for candidate in candidates],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _shared_input_errors(
    candidates: Sequence[ActionSummaryPolicyPrerequisiteModel],
) -> list[str]:
    first = candidates[0]
    errors: list[str] = []
    if any(candidate.model_scope != _PREREQUISITE_SCOPE for candidate in candidates):
        errors.append("prerequisite_model_scope_invalid")
    capture_bindings = {
        (
            candidate.source_model_scope,
            candidate.source_capture_id,
            candidate.source_frame_sha256,
            candidate.source_model_freshness_token,
        )
        for candidate in candidates
    }
    if len(capture_bindings) != 1:
        errors.append("candidate_capture_binding_conflict")
    candidate_counts = {
        candidate.source_policy_candidate_count for candidate in candidates
    }
    if len(candidate_counts) != 1 or next(iter(candidate_counts)) != len(candidates):
        errors.append("policy_candidate_set_incomplete")
    card_keys = [candidate.candidate_card_match_key for candidate in candidates]
    if any(not key for key in card_keys) or len(set(card_keys)) != len(card_keys):
        errors.append("candidate_identity_conflict")
    reward_contracts = {
        (
            candidate.reward_target.reward_target_id,
            candidate.reward_target.current_amount,
            candidate.reward_target.target_amount,
        )
        for candidate in candidates
    }
    if len(reward_contracts) != 1:
        errors.append("reward_target_conflict")
    strategy_contracts = {
        (
            candidate.strategy.schema_version,
            candidate.strategy.strategy_id,
            candidate.strategy.strategy_version,
            candidate.strategy.objective,
            candidate.strategy.allowed_action_types,
            candidate.strategy.max_task_executions,
        )
        for candidate in candidates
    }
    if len(strategy_contracts) != 1:
        errors.append("strategy_contract_conflict")
    resource_contracts = {
        (
            candidate.resource_balance.resource_id,
            candidate.resource_balance.available_amount,
            candidate.fatigue_budget.available_fatigue,
            candidate.fatigue_budget.reserved_fatigue,
            candidate.fatigue_budget.max_policy_spend,
        )
        for candidate in candidates
    }
    if len(resource_contracts) != 1:
        errors.append("shared_budget_conflict")
    if not first.source_capture_id or not first.source_frame_sha256:
        errors.append("source_capture_binding_missing")
    for candidate in candidates:
        errors.extend(_candidate_integrity_errors(candidate))
    return errors


def _exact_int(value: object) -> bool:
    return type(value) is int


def _candidate_integrity_errors(
    candidate: ActionSummaryPolicyPrerequisiteModel,
) -> list[str]:
    errors: list[str] = []
    if (
        not candidate.policy_evaluation_allowed
        or candidate.execution_authorized
        or candidate.selected_action is not None
        or candidate.business_dispatches != 0
        or candidate.irreversible_actions != 0
    ):
        errors.append("prerequisite_authority_boundary_invalid")
    if (
        not candidate.candidate_card_match_key
        or candidate.task_identity.card_match_key
        != candidate.candidate_card_match_key
        or not candidate.task_identity.task_semantic_id
    ):
        errors.append("candidate_identity_binding_invalid")
    if (
        not isinstance(candidate.candidate_available_action_types, frozenset)
        or not candidate.candidate_available_action_types.issubset(_ACTION_TYPES)
    ):
        errors.append("candidate_action_types_invalid")
    reward = candidate.reward_target
    resource = candidate.resource_balance
    attempts = candidate.remaining_attempts
    fatigue = candidate.fatigue_budget
    strategy = candidate.strategy
    if (
        not _exact_int(reward.current_amount)
        or not _exact_int(reward.target_amount)
        or not _exact_int(reward.candidate_reward_amount)
        or reward.current_amount < 0
        or reward.target_amount <= 0
        or reward.current_amount > reward.target_amount
        or reward.candidate_reward_amount <= 0
    ):
        errors.append("candidate_reward_fact_invalid")
    if (
        not _exact_int(resource.unit_cost)
        or resource.unit_cost <= 0
        or not _exact_int(attempts.remaining_attempts)
        or attempts.remaining_attempts < 0
        or not _exact_int(strategy.max_task_executions)
        or strategy.max_task_executions < 0
    ):
        errors.append("candidate_budget_fact_invalid")
    expected_limits = [
        attempts.remaining_attempts,
        resource.affordable_attempts,
        strategy.max_task_executions,
    ]
    if (
        resource.resource_id == "FATIGUE"
        and _exact_int(fatigue.max_policy_spend)
        and _exact_int(resource.unit_cost)
        and resource.unit_cost > 0
    ):
        expected_limits.append(fatigue.max_policy_spend // resource.unit_cost)
    if (
        not _exact_int(candidate.bounded_candidate_executions)
        or candidate.bounded_candidate_executions < 0
        or any(not _exact_int(value) for value in expected_limits)
        or candidate.bounded_candidate_executions != min(expected_limits)
    ):
        errors.append("candidate_execution_bound_invalid")
    return errors


def _assessment(
    candidate: ActionSummaryPolicyPrerequisiteModel,
) -> ActionSummaryCandidateAssessment:
    identity = candidate.task_identity
    available = candidate.candidate_available_action_types
    allowed = candidate.strategy.allowed_action_types
    feasible = available & allowed
    target_remaining = candidate.reward_target.remaining_to_target
    reward_per_execution = candidate.reward_target.candidate_reward_amount
    bounded = candidate.bounded_candidate_executions
    unit_cost = candidate.resource_balance.unit_cost

    if candidate.status is not PrerequisiteModelStatus.READY_FOR_POLICY_EVALUATION:
        return ActionSummaryCandidateAssessment(
            candidate.candidate_card_match_key,
            identity.task_semantic_id,
            CandidateDisposition.BLOCKED_PREREQUISITES,
            candidate.reason_codes or ("candidate_prerequisites_not_ready",),
            available,
            allowed,
            feasible,
            None,
            bounded,
            0,
            unit_cost,
            reward_per_execution,
            0,
            0,
            False,
        )
    if target_remaining == 0:
        disposition = CandidateDisposition.TARGET_ALREADY_MET
        reasons = ("reward_target_already_met",)
    elif bounded == 0:
        disposition = CandidateDisposition.NO_CAPACITY
        reasons = ("candidate_execution_capacity_zero",)
    elif not feasible:
        disposition = CandidateDisposition.NO_ALLOWED_ACTION
        reasons = ("candidate_has_no_policy_allowed_action",)
    else:
        disposition = CandidateDisposition.ELIGIBLE
        reasons = ()

    recommended_action = None
    executions = 0
    spend = 0
    gain = 0
    completes = False
    if disposition is CandidateDisposition.ELIGIBLE:
        recommended_action = "SWEEP" if "SWEEP" in feasible else "CHALLENGE"
        assert target_remaining is not None
        assert reward_per_execution is not None and reward_per_execution > 0
        assert bounded is not None and bounded > 0
        assert unit_cost is not None and unit_cost > 0
        executions_needed = (
            target_remaining + reward_per_execution - 1
        ) // reward_per_execution
        executions = min(bounded, executions_needed)
        spend = executions * unit_cost
        gain = min(target_remaining, executions * reward_per_execution)
        completes = gain >= target_remaining
    return ActionSummaryCandidateAssessment(
        candidate.candidate_card_match_key,
        identity.task_semantic_id,
        disposition,
        reasons,
        available,
        allowed,
        feasible,
        recommended_action,
        bounded,
        executions,
        unit_cost,
        reward_per_execution,
        spend,
        gain,
        completes,
    )


def _ranking_key(
    assessment: ActionSummaryCandidateAssessment,
    objective: StrategyObjective,
) -> tuple[object, ...]:
    gain = assessment.estimated_reward_gain
    spend = assessment.estimated_resource_spend
    efficiency_cost = Fraction(spend, gain) if gain > 0 else Fraction(10**9, 1)
    stable_key = assessment.candidate_card_match_key or ""
    if objective is StrategyObjective.COMPLETE_TARGET:
        return (
            0 if assessment.completes_reward_target else 1,
            assessment.recommended_executions if assessment.completes_reward_target else 0,
            -gain,
            spend,
            stable_key,
        )
    if objective is StrategyObjective.MAXIMIZE_TARGET_REWARD:
        return (-gain, efficiency_cost, spend, stable_key)
    return (efficiency_cost, spend, -gain, stable_key)


def _recommendation(
    selected: ActionSummaryCandidateAssessment,
    *,
    objective: StrategyObjective,
    input_hash: str,
) -> ActionSummaryPolicyRecommendation:
    assert selected.candidate_card_match_key
    assert selected.task_semantic_id
    assert selected.recommended_action_type
    decision_payload = "|".join((
        _POLICY_ID,
        _POLICY_VERSION,
        objective.value,
        input_hash,
        selected.candidate_card_match_key,
        selected.recommended_action_type,
        str(selected.recommended_executions),
    ))
    return ActionSummaryPolicyRecommendation(
        decision_id=hashlib.sha256(decision_payload.encode("utf-8")).hexdigest(),
        policy_id=_POLICY_ID,
        policy_version=_POLICY_VERSION,
        objective=objective,
        candidate_card_match_key=selected.candidate_card_match_key,
        task_semantic_id=selected.task_semantic_id,
        recommended_action_type=selected.recommended_action_type,
        recommended_executions=selected.recommended_executions,
        estimated_resource_spend=selected.estimated_resource_spend,
        estimated_reward_gain=selected.estimated_reward_gain,
        completes_reward_target=selected.completes_reward_target,
        advisory_only=True,
        execution_authorized=False,
        required_future_authorization=(
            "EXPLICIT_POLICY_ADOPTION",
            "EXECUTION_AUTHORITY",
        ),
    )


def evaluate_action_summary_business_policy(
    candidates: Sequence[ActionSummaryPolicyPrerequisiteModel],
) -> ActionSummaryBusinessPolicyEvaluation:
    """Produce one deterministic advisory recommendation, never authority."""

    candidates = tuple(candidates)
    input_hash = _input_sha256(candidates) if candidates else None
    assessments = tuple(_assessment(candidate) for candidate in candidates)
    status = BusinessPolicyStatus.BLOCKED_PREREQUISITES
    reasons: list[str] = []
    recommendation = None
    if not candidates:
        reasons.append("no_policy_candidates")
    elif any(
        candidate.status is not PrerequisiteModelStatus.READY_FOR_POLICY_EVALUATION
        for candidate in candidates
    ):
        reasons.append("candidate_prerequisites_not_ready")
    else:
        conflicts = _shared_input_errors(candidates)
        if conflicts:
            status = BusinessPolicyStatus.BLOCKED_CONFLICTING_INPUTS
            reasons.extend(conflicts)
        else:
            objective = candidates[0].strategy.objective
            if objective is StrategyObjective.OBSERVE_ONLY:
                status = BusinessPolicyStatus.NO_ACTION_REQUIRED
                reasons.append("strategy_observe_only")
            else:
                eligible = [
                    assessment
                    for assessment in assessments
                    if assessment.disposition is CandidateDisposition.ELIGIBLE
                ]
                if eligible:
                    selected = min(
                        eligible,
                        key=lambda assessment: _ranking_key(assessment, objective),
                    )
                    recommendation = _recommendation(
                        selected,
                        objective=objective,
                        input_hash=input_hash or "",
                    )
                    status = BusinessPolicyStatus.RECOMMENDATION_AVAILABLE
                elif assessments and all(
                    assessment.disposition
                    is CandidateDisposition.TARGET_ALREADY_MET
                    for assessment in assessments
                ):
                    status = BusinessPolicyStatus.NO_ACTION_REQUIRED
                    reasons.append("reward_target_already_met")
                else:
                    status = BusinessPolicyStatus.NO_FEASIBLE_CANDIDATE
                    reasons.extend(
                        reason
                        for assessment in assessments
                        for reason in assessment.reason_codes
                    )
    first = candidates[0] if candidates else None
    return ActionSummaryBusinessPolicyEvaluation(
        schema_version=_SCHEMA_VERSION,
        model_scope=_MODEL_SCOPE,
        policy_id=_POLICY_ID,
        policy_version=_POLICY_VERSION,
        status=status,
        reason_codes=_deduplicate(reasons),
        source_capture_id=first.source_capture_id if first else None,
        source_frame_sha256=first.source_frame_sha256 if first else None,
        source_model_freshness_token=(
            first.source_model_freshness_token if first else None
        ),
        candidate_count=len(candidates),
        candidate_assessments=assessments,
        recommendation=recommendation,
        policy_input_sha256=input_hash,
        advisory_only=True,
        execution_authorized=False,
        authorization_issued=False,
        business_dispatches=0,
        irreversible_actions=0,
    )


__all__ = [
    "ActionSummaryBusinessPolicyEvaluation",
    "ActionSummaryCandidateAssessment",
    "ActionSummaryPolicyRecommendation",
    "BusinessPolicyStatus",
    "CandidateDisposition",
    "evaluate_action_summary_business_policy",
]
