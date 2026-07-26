"""Pure prerequisite facts for a future action-summary business policy.

This module deliberately stops before task selection or execution.  A complete
model means only that a future policy evaluator has coherent inputs; it never
authorizes challenge, sweep, reward claim, fatigue use, or any other input.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from enum import Enum
from typing import Iterable

from core.services.action_summary_product_model import (
    ActionSummaryPageModel,
    ActionSummaryTaskCard,
    TaskCardState,
)


class PrerequisiteFactStatus(str, Enum):
    KNOWN = "KNOWN"
    UNKNOWN = "UNKNOWN"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class PrerequisiteModelStatus(str, Enum):
    READY_FOR_POLICY_EVALUATION = "READY_FOR_POLICY_EVALUATION"
    BLOCKED_INCOMPLETE_FACTS = "BLOCKED_INCOMPLETE_FACTS"
    BLOCKED_CONFLICTING_FACTS = "BLOCKED_CONFLICTING_FACTS"


class FactSource(str, Enum):
    PAGE_MODEL = "PAGE_MODEL"
    GAME_OBSERVED = "GAME_OBSERVED"
    USER_CONFIGURED = "USER_CONFIGURED"


class StrategyObjective(str, Enum):
    COMPLETE_TARGET = "COMPLETE_TARGET"
    MAXIMIZE_TARGET_REWARD = "MAXIMIZE_TARGET_REWARD"
    CONSERVE_FATIGUE = "CONSERVE_FATIGUE"
    OBSERVE_ONLY = "OBSERVE_ONLY"


@dataclass(frozen=True, slots=True)
class FactProvenance:
    source: FactSource
    revision: str
    observed_at: str
    valid_until: str
    evidence_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class TaskSemanticIdentityFact:
    status: PrerequisiteFactStatus
    activity_family: str | None = None
    task_semantic_id: str | None = None
    card_match_key: str | None = None
    title_hash: str | None = None
    source_model_scope: str | None = None
    source_capture_id: str | None = None
    source_frame_sha256: str | None = None
    model_freshness_token: str | None = None
    evidence_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RemainingAttemptsFact:
    status: PrerequisiteFactStatus
    remaining_attempts: int | None = None
    total_attempts: int | None = None
    provenance: FactProvenance | None = None


@dataclass(frozen=True, slots=True)
class ResourceBalanceFact:
    status: PrerequisiteFactStatus
    resource_id: str | None = None
    available_amount: int | None = None
    unit_cost: int | None = None
    provenance: FactProvenance | None = None

    @property
    def affordable_attempts(self) -> int | None:
        if (
            self.status is not PrerequisiteFactStatus.KNOWN
            or not _exact_int(self.available_amount)
            or not _exact_int(self.unit_cost)
            or self.unit_cost <= 0
        ):
            return None
        return self.available_amount // self.unit_cost


@dataclass(frozen=True, slots=True)
class RewardTargetFact:
    status: PrerequisiteFactStatus
    reward_target_id: str | None = None
    current_amount: int | None = None
    target_amount: int | None = None
    candidate_reward_amount: int | None = None
    provenance: FactProvenance | None = None

    @property
    def remaining_to_target(self) -> int | None:
        if (
            self.status is not PrerequisiteFactStatus.KNOWN
            or not _exact_int(self.current_amount)
            or not _exact_int(self.target_amount)
        ):
            return None
        return max(0, self.target_amount - self.current_amount)


@dataclass(frozen=True, slots=True)
class FatigueBudgetFact:
    status: PrerequisiteFactStatus
    available_fatigue: int | None = None
    reserved_fatigue: int | None = None
    max_policy_spend: int | None = None
    provenance: FactProvenance | None = None

    @property
    def spendable_fatigue(self) -> int | None:
        if (
            self.status is not PrerequisiteFactStatus.KNOWN
            or not _exact_int(self.available_fatigue)
            or not _exact_int(self.reserved_fatigue)
        ):
            return None
        return max(0, self.available_fatigue - self.reserved_fatigue)


@dataclass(frozen=True, slots=True)
class StrategyInputContract:
    schema_version: str
    strategy_id: str
    strategy_version: str
    objective: StrategyObjective
    allowed_action_types: frozenset[str]
    max_task_executions: int
    provenance: FactProvenance | None = None


@dataclass(frozen=True, slots=True)
class ActionSummaryPolicyPrerequisiteInput:
    task_identity: TaskSemanticIdentityFact
    remaining_attempts: RemainingAttemptsFact
    resource_balance: ResourceBalanceFact
    reward_target: RewardTargetFact
    fatigue_budget: FatigueBudgetFact
    strategy: StrategyInputContract


@dataclass(frozen=True, slots=True)
class ActionSummaryPolicyPrerequisiteModel:
    schema_version: str
    model_scope: str
    source_model_scope: str
    source_capture_id: str | None
    source_frame_sha256: str | None
    source_model_freshness_token: str | None
    source_policy_candidate_count: int
    candidate_card_match_key: str | None
    status: PrerequisiteModelStatus
    reason_codes: tuple[str, ...]
    task_identity: TaskSemanticIdentityFact
    remaining_attempts: RemainingAttemptsFact
    resource_balance: ResourceBalanceFact
    reward_target: RewardTargetFact
    fatigue_budget: FatigueBudgetFact
    strategy: StrategyInputContract
    candidate_available_action_types: frozenset[str]
    bounded_candidate_executions: int | None
    policy_evaluation_allowed: bool
    execution_authorized: bool
    selected_action: str | None
    business_dispatches: int
    irreversible_actions: int

    def to_dict(self) -> dict[str, object]:
        document = asdict(self)
        document["status"] = _enum_value(self.status)
        document["task_identity"]["status"] = _enum_value(
            self.task_identity.status
        )
        document["remaining_attempts"]["status"] = (
            _enum_value(self.remaining_attempts.status)
        )
        document["resource_balance"]["status"] = _enum_value(
            self.resource_balance.status
        )
        document["resource_balance"]["affordable_attempts"] = (
            self.resource_balance.affordable_attempts
        )
        document["reward_target"]["status"] = _enum_value(
            self.reward_target.status
        )
        document["reward_target"]["remaining_to_target"] = (
            self.reward_target.remaining_to_target
        )
        document["fatigue_budget"]["status"] = _enum_value(
            self.fatigue_budget.status
        )
        document["fatigue_budget"]["spendable_fatigue"] = (
            self.fatigue_budget.spendable_fatigue
        )
        document["strategy"]["objective"] = _enum_value(
            self.strategy.objective
        )
        document["strategy"]["allowed_action_types"] = (
            sorted(self.strategy.allowed_action_types)
            if isinstance(self.strategy.allowed_action_types, frozenset)
            else []
        )
        document["candidate_available_action_types"] = sorted(
            self.candidate_available_action_types
        )
        for fact_name in (
            "remaining_attempts",
            "resource_balance",
            "reward_target",
            "fatigue_budget",
            "strategy",
        ):
            provenance = document[fact_name].get("provenance")
            if isinstance(provenance, dict):
                provenance["source"] = _enum_value(getattr(
                    getattr(self, fact_name).provenance, "source"
                ))
        return document


_MODEL_SCOPE = "ACTION_SUMMARY_POLICY_PREREQUISITES_V1"
_SCHEMA_VERSION = "1.0"
_ALLOWED_ACTION_TYPES = frozenset({"CHALLENGE", "SWEEP"})


def _enum_value(value: object) -> str:
    return str(value.value) if isinstance(value, Enum) else str(value)


def _exact_int(value: object) -> bool:
    return type(value) is int


def task_identity_from_page_model(
    model: ActionSummaryPageModel,
    card_match_key: str,
) -> TaskSemanticIdentityFact:
    """Bind one candidate identity to the immutable read-only page model."""

    matches = [
        card for card in model.task_cards if card.card_match_key == card_match_key
    ]
    if len(matches) != 1:
        return TaskSemanticIdentityFact(PrerequisiteFactStatus.UNKNOWN)
    card = matches[0]
    return TaskSemanticIdentityFact(
        status=PrerequisiteFactStatus.KNOWN,
        activity_family=model.activity_family,
        task_semantic_id=card.semantic_id,
        card_match_key=card.card_match_key,
        title_hash=card.title_hash,
        source_model_scope=model.model_scope,
        source_capture_id=model.source_capture_id,
        source_frame_sha256=model.source_frame_sha256,
        model_freshness_token=model.model_freshness_token,
        evidence_ids=card.evidence_ids,
    )


def _parse_aware(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed


def _provenance_errors(
    name: str,
    provenance: FactProvenance | None,
    *,
    allowed_sources: frozenset[FactSource],
    captured_at: str | None,
) -> list[str]:
    if provenance is None:
        return [f"{name}_provenance_missing"]
    if not isinstance(provenance, FactProvenance):
        return [f"{name}_provenance_invalid"]
    errors: list[str] = []
    if provenance.source not in allowed_sources:
        errors.append(f"{name}_source_invalid")
    if not isinstance(provenance.revision, str) or not provenance.revision.strip():
        errors.append(f"{name}_revision_missing")
    observed_at = _parse_aware(provenance.observed_at)
    valid_until = _parse_aware(provenance.valid_until)
    captured = _parse_aware(captured_at or "")
    if observed_at is None or valid_until is None:
        errors.append(f"{name}_freshness_invalid")
    elif observed_at > valid_until:
        errors.append(f"{name}_freshness_invalid")
    elif captured is None:
        errors.append("source_capture_time_missing")
    elif not observed_at <= captured <= valid_until:
        errors.append(f"{name}_stale")
    return errors


def _candidate_card(
    model: ActionSummaryPageModel,
    identity: TaskSemanticIdentityFact,
) -> tuple[ActionSummaryTaskCard | None, list[str]]:
    if identity.status is not PrerequisiteFactStatus.KNOWN:
        return None, ["task_identity_unknown"]
    required = {
        "activity_family": identity.activity_family,
        "task_semantic_id": identity.task_semantic_id,
        "card_match_key": identity.card_match_key,
        "title_hash": identity.title_hash,
        "source_model_scope": identity.source_model_scope,
        "source_capture_id": identity.source_capture_id,
        "source_frame_sha256": identity.source_frame_sha256,
        "model_freshness_token": identity.model_freshness_token,
    }
    if any(value is None or not str(value).strip() for value in required.values()):
        return None, ["task_identity_incomplete"]
    binding = (
        identity.activity_family == model.activity_family
        and identity.source_model_scope == model.model_scope
        and identity.source_capture_id == model.source_capture_id
        and identity.source_frame_sha256 == model.source_frame_sha256
        and identity.model_freshness_token == model.model_freshness_token
    )
    if not binding:
        return None, ["task_identity_model_binding_mismatch"]
    matches = [
        card
        for card in model.task_cards
        if card.card_match_key == identity.card_match_key
    ]
    if len(matches) != 1:
        return None, [
            "task_identity_card_not_found"
            if not matches
            else "task_identity_card_ambiguous"
        ]
    card = matches[0]
    if (
        card.semantic_id != identity.task_semantic_id
        or card.title_hash != identity.title_hash
        or tuple(card.evidence_ids) != tuple(identity.evidence_ids)
    ):
        return None, ["task_identity_card_mismatch"]
    return card, []


def _known_or_missing(name: str, status: PrerequisiteFactStatus) -> list[str]:
    if not isinstance(status, PrerequisiteFactStatus):
        return [f"{name}_status_invalid"]
    if status is PrerequisiteFactStatus.UNKNOWN:
        return [f"{name}_unknown"]
    if status is PrerequisiteFactStatus.NOT_APPLICABLE:
        return [f"{name}_not_applicable"]
    return []


def _deduplicate(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))


def _record_provenance_findings(
    findings: Iterable[str],
    *,
    incomplete: list[str],
    conflicts: list[str],
) -> None:
    for finding in findings:
        if finding.endswith(("_provenance_missing", "_revision_missing")) or (
            finding == "source_capture_time_missing"
        ):
            incomplete.append(finding)
        else:
            conflicts.append(finding)


def evaluate_action_summary_policy_prerequisites(
    page_model: ActionSummaryPageModel,
    inputs: ActionSummaryPolicyPrerequisiteInput,
) -> ActionSummaryPolicyPrerequisiteModel:
    """Validate future-policy inputs without making a business decision."""

    incomplete: list[str] = []
    conflicts: list[str] = []
    if (
        page_model.page_state != "ACTION_SUMMARY_VISIBLE"
        or page_model.overlay_states
        or not page_model.source_capture_id
        or not page_model.source_frame_sha256
        or not page_model.model_freshness_token
    ):
        conflicts.append("source_page_not_policy_eligible")

    card, identity_errors = _candidate_card(page_model, inputs.task_identity)
    if inputs.task_identity.status is PrerequisiteFactStatus.UNKNOWN:
        incomplete.extend(identity_errors)
    else:
        conflicts.extend(identity_errors)
    if card is not None and card.state is not TaskCardState.AVAILABLE:
        conflicts.append("candidate_task_not_available")

    attempts = inputs.remaining_attempts
    attempt_status = _known_or_missing("remaining_attempts", attempts.status)
    if "remaining_attempts_status_invalid" in attempt_status:
        conflicts.extend(attempt_status)
    else:
        incomplete.extend(attempt_status)
    if attempts.status is PrerequisiteFactStatus.KNOWN:
        if (
            not _exact_int(attempts.remaining_attempts)
            or not _exact_int(attempts.total_attempts)
        ):
            incomplete.append("remaining_attempts_value_missing")
        elif (
            attempts.remaining_attempts < 0
            or attempts.total_attempts < 0
            or attempts.remaining_attempts > attempts.total_attempts
        ):
            conflicts.append("remaining_attempts_invalid")
        if card is not None and card.remaining_attempts is not None:
            if attempts.remaining_attempts != card.remaining_attempts:
                conflicts.append("remaining_attempts_page_mismatch")
        _record_provenance_findings(_provenance_errors(
            "remaining_attempts",
            attempts.provenance,
            allowed_sources=frozenset({FactSource.GAME_OBSERVED}),
            captured_at=page_model.captured_at,
        ), incomplete=incomplete, conflicts=conflicts)

    resource = inputs.resource_balance
    resource_status = _known_or_missing("resource_balance", resource.status)
    if "resource_balance_status_invalid" in resource_status:
        conflicts.extend(resource_status)
    else:
        incomplete.extend(resource_status)
    if resource.status is PrerequisiteFactStatus.KNOWN:
        if (
            not isinstance(resource.resource_id, str)
            or not resource.resource_id.strip()
            or resource.resource_id == "UNKNOWN"
            or not _exact_int(resource.available_amount)
            or not _exact_int(resource.unit_cost)
        ):
            incomplete.append("resource_balance_value_missing")
        elif resource.available_amount < 0 or resource.unit_cost <= 0:
            conflicts.append("resource_balance_invalid")
        if card is not None and card.cost is not None:
            if resource.unit_cost != card.cost:
                conflicts.append("resource_unit_cost_page_mismatch")
        if card is not None and card.cost_resource_id not in {None, "UNKNOWN"}:
            if resource.resource_id != card.cost_resource_id:
                conflicts.append("resource_identity_page_mismatch")
        _record_provenance_findings(_provenance_errors(
            "resource_balance",
            resource.provenance,
            allowed_sources=frozenset({FactSource.GAME_OBSERVED}),
            captured_at=page_model.captured_at,
        ), incomplete=incomplete, conflicts=conflicts)

    reward = inputs.reward_target
    strategy = inputs.strategy
    if not isinstance(reward.status, PrerequisiteFactStatus):
        conflicts.append("reward_target_status_invalid")
    elif reward.status is PrerequisiteFactStatus.NOT_APPLICABLE:
        if strategy.objective is not StrategyObjective.OBSERVE_ONLY:
            incomplete.append("reward_target_required_for_strategy")
    else:
        incomplete.extend(_known_or_missing("reward_target", reward.status))
    if reward.status is PrerequisiteFactStatus.KNOWN:
        if (
            not isinstance(reward.reward_target_id, str)
            or not reward.reward_target_id.strip()
            or not _exact_int(reward.current_amount)
            or not _exact_int(reward.target_amount)
            or not _exact_int(reward.candidate_reward_amount)
        ):
            incomplete.append("reward_target_value_missing")
        elif (
            reward.current_amount < 0
            or reward.target_amount <= 0
            or reward.current_amount > reward.target_amount
            or reward.candidate_reward_amount <= 0
        ):
            conflicts.append("reward_target_invalid")
        _record_provenance_findings(_provenance_errors(
            "reward_target",
            reward.provenance,
            allowed_sources=frozenset({FactSource.USER_CONFIGURED}),
            captured_at=page_model.captured_at,
        ), incomplete=incomplete, conflicts=conflicts)

    fatigue = inputs.fatigue_budget
    fatigue_status = _known_or_missing("fatigue_budget", fatigue.status)
    if "fatigue_budget_status_invalid" in fatigue_status:
        conflicts.extend(fatigue_status)
    else:
        incomplete.extend(fatigue_status)
    if fatigue.status is PrerequisiteFactStatus.KNOWN:
        if (
            not _exact_int(fatigue.available_fatigue)
            or not _exact_int(fatigue.reserved_fatigue)
            or not _exact_int(fatigue.max_policy_spend)
        ):
            incomplete.append("fatigue_budget_value_missing")
        elif (
            fatigue.available_fatigue < 0
            or fatigue.reserved_fatigue < 0
            or fatigue.max_policy_spend < 0
            or fatigue.reserved_fatigue > fatigue.available_fatigue
            or fatigue.max_policy_spend > (
                fatigue.available_fatigue - fatigue.reserved_fatigue
            )
        ):
            conflicts.append("fatigue_budget_invalid")
        if (
            resource.status is PrerequisiteFactStatus.KNOWN
            and resource.resource_id == "FATIGUE"
            and resource.available_amount != fatigue.available_fatigue
        ):
            conflicts.append("fatigue_resource_balance_mismatch")
        _record_provenance_findings(_provenance_errors(
            "fatigue_budget",
            fatigue.provenance,
            allowed_sources=frozenset({FactSource.GAME_OBSERVED}),
            captured_at=page_model.captured_at,
        ), incomplete=incomplete, conflicts=conflicts)

    if not isinstance(strategy.objective, StrategyObjective):
        conflicts.append("strategy_objective_invalid")
    if strategy.schema_version != _SCHEMA_VERSION:
        conflicts.append("strategy_schema_unsupported")
    if (
        not isinstance(strategy.strategy_id, str)
        or not strategy.strategy_id.strip()
        or not isinstance(strategy.strategy_version, str)
        or not strategy.strategy_version.strip()
    ):
        incomplete.append("strategy_identity_missing")
    if (
        not _exact_int(strategy.max_task_executions)
        or strategy.max_task_executions < 0
        or strategy.max_task_executions > 10
    ):
        conflicts.append("strategy_execution_limit_invalid")
    if not isinstance(strategy.allowed_action_types, frozenset) or not all(
        isinstance(value, str) for value in strategy.allowed_action_types
    ):
        conflicts.append("strategy_action_types_invalid")
    elif not strategy.allowed_action_types.issubset(_ALLOWED_ACTION_TYPES):
        conflicts.append("strategy_action_type_unsupported")
    if (
        strategy.objective is not StrategyObjective.OBSERVE_ONLY
        and strategy.max_task_executions == 0
    ):
        conflicts.append("strategy_execution_limit_conflicts_with_objective")
    _record_provenance_findings(_provenance_errors(
        "strategy",
        strategy.provenance,
        allowed_sources=frozenset({FactSource.USER_CONFIGURED}),
        captured_at=page_model.captured_at,
    ), incomplete=incomplete, conflicts=conflicts)

    reason_codes = _deduplicate((*conflicts, *incomplete))
    if conflicts:
        status = PrerequisiteModelStatus.BLOCKED_CONFLICTING_FACTS
    elif incomplete:
        status = PrerequisiteModelStatus.BLOCKED_INCOMPLETE_FACTS
    else:
        status = PrerequisiteModelStatus.READY_FOR_POLICY_EVALUATION
    bounded_candidate_executions: int | None = None
    if status is PrerequisiteModelStatus.READY_FOR_POLICY_EVALUATION:
        limits = [
            attempts.remaining_attempts,
            resource.affordable_attempts,
            strategy.max_task_executions,
        ]
        if (
            resource.resource_id == "FATIGUE"
            and fatigue.max_policy_spend is not None
            and resource.unit_cost is not None
            and resource.unit_cost > 0
        ):
            limits.append(fatigue.max_policy_spend // resource.unit_cost)
        if all(value is not None for value in limits):
            bounded_candidate_executions = min(int(value) for value in limits)
    candidate_available_action_types = frozenset(
        action
        for action, capability in (
            ("CHALLENGE", "CHALLENGE_AVAILABLE"),
            ("SWEEP", "SWEEP_AVAILABLE"),
        )
        if card is not None and capability in card.available_actions
    )
    return ActionSummaryPolicyPrerequisiteModel(
        schema_version=_SCHEMA_VERSION,
        model_scope=_MODEL_SCOPE,
        source_model_scope=page_model.model_scope,
        source_capture_id=page_model.source_capture_id,
        source_frame_sha256=page_model.source_frame_sha256,
        source_model_freshness_token=page_model.model_freshness_token,
        source_policy_candidate_count=sum(
            1
            for source_card in page_model.task_cards
            if source_card.state is TaskCardState.AVAILABLE
            and "TASK_EXECUTION_AVAILABLE" in source_card.available_actions
        ),
        candidate_card_match_key=inputs.task_identity.card_match_key,
        status=status,
        reason_codes=reason_codes,
        task_identity=inputs.task_identity,
        remaining_attempts=attempts,
        resource_balance=resource,
        reward_target=reward,
        fatigue_budget=fatigue,
        strategy=strategy,
        candidate_available_action_types=candidate_available_action_types,
        bounded_candidate_executions=bounded_candidate_executions,
        policy_evaluation_allowed=(
            status is PrerequisiteModelStatus.READY_FOR_POLICY_EVALUATION
        ),
        execution_authorized=False,
        selected_action=None,
        business_dispatches=0,
        irreversible_actions=0,
    )


__all__ = [
    "ActionSummaryPolicyPrerequisiteInput",
    "ActionSummaryPolicyPrerequisiteModel",
    "FactProvenance",
    "FactSource",
    "FatigueBudgetFact",
    "PrerequisiteFactStatus",
    "PrerequisiteModelStatus",
    "RemainingAttemptsFact",
    "ResourceBalanceFact",
    "RewardTargetFact",
    "StrategyInputContract",
    "StrategyObjective",
    "TaskSemanticIdentityFact",
    "evaluate_action_summary_policy_prerequisites",
    "task_identity_from_page_model",
]
