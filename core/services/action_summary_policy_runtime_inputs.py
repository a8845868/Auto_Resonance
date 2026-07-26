"""Read-only runtime input assembly for action-summary business policy.

The assembler binds one immutable ACTION_SUMMARY page model to fresh observed
resource facts and an explicit user policy document.  It produces prerequisite
models only.  It has no executor, backend, authorization, retry, or input path.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Mapping, Sequence

from core.services.action_summary_policy_prerequisites import (
    ActionSummaryPolicyPrerequisiteInput,
    ActionSummaryPolicyPrerequisiteModel,
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
    evaluate_action_summary_policy_prerequisites,
    task_identity_from_page_model,
)
from core.services.action_summary_product_model import (
    ActionSummaryPageModel,
    ActionSummaryTaskCard,
    TaskCardState,
)


class RuntimeInputAssemblyStatus(str, Enum):
    READY_FOR_POLICY_EVALUATION = "READY_FOR_POLICY_EVALUATION"
    BLOCKED_SOURCE_PAGE = "BLOCKED_SOURCE_PAGE"
    BLOCKED_USER_CONFIG = "BLOCKED_USER_CONFIG"
    BLOCKED_RUNTIME_FACTS = "BLOCKED_RUNTIME_FACTS"


class AssemblyIntegrityStatus(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"


class PolicyInputReadiness(str, Enum):
    READY_FOR_POLICY_EVALUATION = "READY_FOR_POLICY_EVALUATION"
    BLOCKED_MISSING_FACTS = "BLOCKED_MISSING_FACTS"
    BLOCKED_CONFLICTING_INPUTS = "BLOCKED_CONFLICTING_INPUTS"


class SourceRelationship(str, Enum):
    SAME_CAPTURE = "SAME_CAPTURE"
    DIFFERENT_CAPTURE_WITHIN_WINDOW = "DIFFERENT_CAPTURE_WITHIN_WINDOW"
    DIFFERENT_CAPTURE_STALE = "DIFFERENT_CAPTURE_STALE"
    UNKNOWN = "UNKNOWN"


class PolicyTargetMatchStatus(str, Enum):
    UNIQUE = "UNIQUE"
    NOT_FOUND = "NOT_FOUND"
    AMBIGUOUS = "AMBIGUOUS"
    UNSUPPORTED = "UNSUPPORTED"
    NOT_REQUESTED = "NOT_REQUESTED"


@dataclass(frozen=True, slots=True)
class ActionSummaryRuntimeResourceObservation:
    source_capture_id: str
    source_frame_sha256: str
    revision: str
    observed_at: str
    valid_until: str
    resource_id: str
    available_amount: int
    available_fatigue: int
    evidence_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CandidateRewardPolicy:
    task_semantic_id: str
    reward_amount_per_execution: int


@dataclass(frozen=True, slots=True)
class ActionSummaryUserPolicyConfig:
    schema_version: str
    policy_id: str
    policy_version: str
    revision: str
    effective_at: str
    valid_until: str
    objective: StrategyObjective
    allowed_action_types: frozenset[str]
    max_task_executions: int
    reward_target_id: str
    reward_current_amount: int
    reward_target_amount: int
    reserved_fatigue: int
    max_policy_spend: int
    candidate_rewards: tuple[CandidateRewardPolicy, ...]
    captured_at: str | None = None
    requested_known_task_id: str | None = None
    requested_task_title_hash: str | None = None


@dataclass(frozen=True, slots=True)
class ActionSummaryRuntimeInputAssembly:
    schema_version: str
    model_scope: str
    status: RuntimeInputAssemblyStatus
    assembly_status: AssemblyIntegrityStatus
    policy_input_readiness: PolicyInputReadiness
    reason_codes: tuple[str, ...]
    missing_runtime_inputs: tuple[str, ...]
    assembled_at: str
    source_capture_id: str | None
    source_frame_sha256: str | None
    source_model_freshness_token: str | None
    page_model_captured_at: str | None
    resource_observation_capture_id: str | None
    resource_observation_frame_sha256: str | None
    resource_observation_captured_at: str | None
    resource_observation_freshness_token: str | None
    source_relationship: SourceRelationship
    source_age_seconds: float | None
    maximum_allowed_age_seconds: int
    policy_config_id: str | None
    policy_config_version: str | None
    policy_config_captured_at: str | None
    policy_config_sha256: str | None
    policy_config_fingerprint_bound: bool
    stale_resource_input_accepted: bool
    policy_target_match_status: PolicyTargetMatchStatus
    policy_target_card_match_key: str | None
    source_policy_candidate_count: int
    candidate_prerequisites: tuple[ActionSummaryPolicyPrerequisiteModel, ...]
    policy_evaluation_allowed: bool
    business_policy_evaluated: bool
    executor_connected: bool
    execution_authorized: bool
    authorization_issued: bool
    business_dispatches: int
    irreversible_actions: int

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "model_scope": self.model_scope,
            "status": self.status.value,
            "assembly_status": self.assembly_status.value,
            "policy_input_readiness": self.policy_input_readiness.value,
            "reason_codes": list(self.reason_codes),
            "missing_runtime_inputs": list(self.missing_runtime_inputs),
            "assembled_at": self.assembled_at,
            "source_capture_id": self.source_capture_id,
            "source_frame_sha256": self.source_frame_sha256,
            "source_model_freshness_token": self.source_model_freshness_token,
            "page_model_capture_id": self.source_capture_id,
            "page_model_frame_sha256": self.source_frame_sha256,
            "page_model_captured_at": self.page_model_captured_at,
            "page_model_freshness_token": self.source_model_freshness_token,
            "resource_observation_capture_id": self.resource_observation_capture_id,
            "resource_observation_frame_sha256": self.resource_observation_frame_sha256,
            "resource_observation_captured_at": self.resource_observation_captured_at,
            "resource_observation_freshness_token": (
                self.resource_observation_freshness_token
            ),
            "source_relationship": self.source_relationship.value,
            "source_age_seconds": self.source_age_seconds,
            "maximum_allowed_age_seconds": self.maximum_allowed_age_seconds,
            "policy_config_id": self.policy_config_id,
            "policy_config_version": self.policy_config_version,
            "policy_config_captured_at": self.policy_config_captured_at,
            "policy_config_sha256": self.policy_config_sha256,
            "policy_config_fingerprint": self.policy_config_sha256,
            "policy_config_fingerprint_bound": (
                self.policy_config_fingerprint_bound
            ),
            "stale_resource_input_accepted": self.stale_resource_input_accepted,
            "policy_target_match_status": self.policy_target_match_status.value,
            "policy_target_card_match_key": self.policy_target_card_match_key,
            "source_policy_candidate_count": self.source_policy_candidate_count,
            "candidate_prerequisites": [
                candidate.to_dict() for candidate in self.candidate_prerequisites
            ],
            "policy_evaluation_allowed": self.policy_evaluation_allowed,
            "business_policy_evaluated": self.business_policy_evaluated,
            "executor_connected": self.executor_connected,
            "execution_authorized": self.execution_authorized,
            "authorization_issued": self.authorization_issued,
            "business_dispatches": self.business_dispatches,
            "irreversible_actions": self.irreversible_actions,
        }

    def matches_policy_config(
        self,
        value: ActionSummaryUserPolicyConfig | Mapping[str, object],
    ) -> bool:
        """Reject reuse after the frozen user-policy snapshot changes."""

        try:
            fingerprint = action_summary_policy_config_fingerprint(value)
        except (TypeError, ValueError):
            return False
        return bool(
            self.policy_config_sha256
            and self.policy_config_sha256 == fingerprint
        )


_SCHEMA_VERSION = "1.0"
_MODEL_SCOPE = "ACTION_SUMMARY_POLICY_RUNTIME_INPUT_ASSEMBLY_V1"
_ALLOWED_ACTION_TYPES = frozenset({"CHALLENGE", "SWEEP"})


def _exact_int(value: object) -> bool:
    return type(value) is int


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field}_missing")
    return value.strip()


def _integer(value: object, field: str, *, minimum: int = 0) -> int:
    if not _exact_int(value) or value < minimum:
        raise ValueError(f"{field}_invalid")
    return value


def _section(document: Mapping[str, object]) -> Mapping[str, object]:
    value = document.get("ActionSummaryPolicy", document)
    if not isinstance(value, Mapping):
        raise ValueError("action_summary_policy_section_invalid")
    return value


def parse_action_summary_user_policy_config(
    document: Mapping[str, object],
) -> ActionSummaryUserPolicyConfig:
    """Parse one strict user policy mapping without applying defaults."""

    if not isinstance(document, Mapping):
        raise ValueError("policy_document_invalid")
    data = _section(document)
    if data.get("schema_version") != _SCHEMA_VERSION:
        raise ValueError("policy_schema_unsupported")
    try:
        objective = StrategyObjective(_text(data.get("objective"), "objective"))
    except ValueError as error:
        if str(error).endswith("_missing"):
            raise
        raise ValueError("strategy_objective_invalid") from error
    raw_actions = data.get("allowed_action_types")
    if (
        not isinstance(raw_actions, Sequence)
        or isinstance(raw_actions, (str, bytes))
        or not all(isinstance(action, str) for action in raw_actions)
    ):
        raise ValueError("strategy_action_types_invalid")
    actions = frozenset(action.strip().upper() for action in raw_actions)
    if len(actions) != len(raw_actions) or not actions.issubset(_ALLOWED_ACTION_TYPES):
        raise ValueError("strategy_action_types_invalid")
    max_executions = _integer(
        data.get("max_task_executions"), "max_task_executions"
    )
    if max_executions > 10:
        raise ValueError("max_task_executions_invalid")
    if objective is not StrategyObjective.OBSERVE_ONLY and max_executions == 0:
        raise ValueError("strategy_execution_limit_conflicts_with_objective")

    raw_rewards = data.get("candidate_rewards")
    if (
        not isinstance(raw_rewards, Sequence)
        or isinstance(raw_rewards, (str, bytes))
    ):
        raise ValueError("candidate_rewards_invalid")
    rewards: list[CandidateRewardPolicy] = []
    seen: set[str] = set()
    for item in raw_rewards:
        if not isinstance(item, Mapping):
            raise ValueError("candidate_reward_invalid")
        semantic_id = _text(item.get("task_semantic_id"), "task_semantic_id")
        if semantic_id in seen:
            raise ValueError("candidate_reward_duplicate")
        seen.add(semantic_id)
        rewards.append(CandidateRewardPolicy(
            task_semantic_id=semantic_id,
            reward_amount_per_execution=_integer(
                item.get("reward_amount_per_execution"),
                "reward_amount_per_execution",
                minimum=1,
            ),
        ))

    current = _integer(data.get("reward_current_amount"), "reward_current_amount")
    target = _integer(
        data.get("reward_target_amount"), "reward_target_amount", minimum=1
    )
    if current > target:
        raise ValueError("reward_target_invalid")
    reserved = _integer(data.get("reserved_fatigue"), "reserved_fatigue")
    maximum_spend = _integer(data.get("max_policy_spend"), "max_policy_spend")
    requested_known_task_id = data.get("requested_known_task_id")
    if requested_known_task_id is not None:
        requested_known_task_id = _text(
            requested_known_task_id, "requested_known_task_id"
        )
    requested_task_title_hash = data.get("requested_task_title_hash")
    if requested_task_title_hash is not None:
        requested_task_title_hash = _text(
            requested_task_title_hash, "requested_task_title_hash"
        )
    captured_at = data.get("captured_at")
    if captured_at is not None:
        captured_at = _text(captured_at, "captured_at")
    return ActionSummaryUserPolicyConfig(
        schema_version=_SCHEMA_VERSION,
        policy_id=_text(data.get("policy_id"), "policy_id"),
        policy_version=_text(data.get("policy_version"), "policy_version"),
        revision=_text(data.get("revision"), "revision"),
        effective_at=_text(data.get("effective_at"), "effective_at"),
        valid_until=_text(data.get("valid_until"), "valid_until"),
        objective=objective,
        allowed_action_types=actions,
        max_task_executions=max_executions,
        reward_target_id=_text(data.get("reward_target_id"), "reward_target_id"),
        reward_current_amount=current,
        reward_target_amount=target,
        reserved_fatigue=reserved,
        max_policy_spend=maximum_spend,
        candidate_rewards=tuple(rewards),
        captured_at=captured_at,
        requested_known_task_id=requested_known_task_id,
        requested_task_title_hash=requested_task_title_hash,
    )


def load_action_summary_user_policy_config(
    path: str | Path,
) -> ActionSummaryUserPolicyConfig:
    """Read an explicitly selected JSON policy file; never create or modify it."""

    try:
        document = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("policy_config_unreadable") from error
    if not isinstance(document, Mapping):
        raise ValueError("policy_document_invalid")
    return parse_action_summary_user_policy_config(document)


def _canonical_policy_hash(config: ActionSummaryUserPolicyConfig) -> str:
    document = asdict(config)
    document["objective"] = config.objective.value
    document["allowed_action_types"] = sorted(config.allowed_action_types)
    payload = json.dumps(
        document, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def action_summary_policy_config_fingerprint(
    value: ActionSummaryUserPolicyConfig | Mapping[str, object],
) -> str:
    """Hash the exact normalized policy snapshot, including incomplete input."""

    if isinstance(value, ActionSummaryUserPolicyConfig):
        return _canonical_policy_hash(value)
    if not isinstance(value, Mapping):
        raise ValueError("policy_document_invalid")
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _policy_metadata(
    value: ActionSummaryUserPolicyConfig | Mapping[str, object],
    config: ActionSummaryUserPolicyConfig | None,
) -> tuple[str | None, str | None, str | None, str | None, str | None]:
    if config is not None:
        return (
            config.policy_id,
            config.policy_version,
            config.captured_at or config.effective_at,
            config.requested_known_task_id,
            config.requested_task_title_hash,
        )
    if not isinstance(value, Mapping):
        return None, None, None, None, None
    try:
        data = _section(value)
    except ValueError:
        return None, None, None, None, None

    def optional_text(name: str) -> str | None:
        item = data.get(name)
        return item.strip() if isinstance(item, str) and item.strip() else None

    return (
        optional_text("policy_id"),
        optional_text("policy_version"),
        optional_text("captured_at") or optional_text("effective_at"),
        optional_text("requested_known_task_id"),
        optional_text("requested_task_title_hash"),
    )


def _page_provenance(
    model: ActionSummaryPageModel,
    card: ActionSummaryTaskCard,
) -> FactProvenance | None:
    if not model.captured_at:
        return None
    return FactProvenance(
        source=FactSource.PAGE_MODEL,
        revision=model.model_freshness_token or "",
        observed_at=model.captured_at,
        valid_until=model.captured_at,
        evidence_ids=card.evidence_ids,
    )


def _observed_provenance(
    observation: ActionSummaryRuntimeResourceObservation | None,
) -> FactProvenance | None:
    if observation is None:
        return None
    return FactProvenance(
        source=FactSource.GAME_OBSERVED,
        revision=observation.revision,
        observed_at=observation.observed_at,
        valid_until=observation.valid_until,
        evidence_ids=tuple(dict.fromkeys((
            *observation.evidence_ids,
            (
                f"capture:{observation.source_capture_id}:"
                f"{observation.source_frame_sha256}"
            ),
        ))),
    )


def _user_provenance(config: ActionSummaryUserPolicyConfig) -> FactProvenance:
    return FactProvenance(
        source=FactSource.USER_CONFIGURED,
        revision=config.revision,
        observed_at=config.effective_at,
        valid_until=config.valid_until,
        evidence_ids=(f"policy:{config.policy_id}:{config.policy_version}",),
    )


def _parse_aware(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed


def _fatigue_provenance(
    observation: ActionSummaryRuntimeResourceObservation | None,
    config: ActionSummaryUserPolicyConfig | None,
) -> FactProvenance | None:
    if observation is None or config is None:
        return None
    observed_start = _parse_aware(observation.observed_at)
    observed_end = _parse_aware(observation.valid_until)
    configured_start = _parse_aware(config.effective_at)
    configured_end = _parse_aware(config.valid_until)
    if None in {observed_start, observed_end, configured_start, configured_end}:
        observed_at = observation.observed_at
        valid_until = observation.valid_until
    else:
        observed_at = max(observed_start, configured_start).isoformat()
        valid_until = min(observed_end, configured_end).isoformat()
    revision_payload = f"{observation.revision}|{config.revision}"
    revision = "runtime-assembly:" + hashlib.sha256(
        revision_payload.encode("utf-8")
    ).hexdigest()
    return FactProvenance(
        source=FactSource.RUNTIME_ASSEMBLED,
        revision=revision,
        observed_at=observed_at,
        valid_until=valid_until,
        evidence_ids=tuple(dict.fromkeys((
            *observation.evidence_ids,
            f"policy:{config.policy_id}:{config.policy_version}",
        ))),
    )


def _candidate_cards(model: ActionSummaryPageModel) -> tuple[ActionSummaryTaskCard, ...]:
    return tuple(
        card
        for card in model.task_cards
        if card.state is TaskCardState.AVAILABLE
        and "TASK_EXECUTION_AVAILABLE" in card.available_actions
    )


def _resource_observation_errors(
    observation: ActionSummaryRuntimeResourceObservation | None,
) -> tuple[str, ...]:
    if observation is None:
        return ("runtime_resource_observation_missing",)
    errors: list[str] = []
    if (
        not isinstance(observation.source_capture_id, str)
        or not observation.source_capture_id.strip()
    ):
        errors.append("runtime_resource_capture_id_missing")
    frame_hash = observation.source_frame_sha256
    if (
        not isinstance(frame_hash, str)
        or len(frame_hash) != 64
        or any(character not in "0123456789abcdefABCDEF" for character in frame_hash)
    ):
        errors.append("runtime_resource_frame_hash_invalid")
    if not isinstance(observation.revision, str) or not observation.revision.strip():
        errors.append("runtime_resource_revision_missing")
    observed_at = _parse_aware(observation.observed_at)
    valid_until = _parse_aware(observation.valid_until)
    if observed_at is None or valid_until is None or observed_at > valid_until:
        errors.append("runtime_resource_freshness_invalid")
    if not isinstance(observation.resource_id, str) or not observation.resource_id.strip():
        errors.append("runtime_resource_id_missing")
    if not _exact_int(observation.available_amount) or observation.available_amount < 0:
        errors.append("runtime_resource_amount_invalid")
    if not _exact_int(observation.available_fatigue) or observation.available_fatigue < 0:
        errors.append("runtime_fatigue_amount_invalid")
    if (
        not isinstance(observation.evidence_ids, tuple)
        or not observation.evidence_ids
        or not all(isinstance(value, str) and value.strip() for value in observation.evidence_ids)
    ):
        errors.append("runtime_resource_evidence_missing")
    return tuple(errors)


def _unknown_inputs(
    model: ActionSummaryPageModel,
    card: ActionSummaryTaskCard,
    config: ActionSummaryUserPolicyConfig | None,
    observation: ActionSummaryRuntimeResourceObservation | None,
) -> ActionSummaryPolicyPrerequisiteInput:
    page_provenance = _page_provenance(model, card)
    observed_provenance = _observed_provenance(observation)
    user_provenance = _user_provenance(config) if config else None
    fatigue_provenance = _fatigue_provenance(observation, config)
    reward_by_task = {
        item.task_semantic_id: item.reward_amount_per_execution
        for item in (config.candidate_rewards if config else ())
    }
    reward_amount = reward_by_task.get(card.semantic_id)
    attempts_known = (
        card.remaining_attempts is not None
        and card.total_attempts is not None
        and page_provenance is not None
    )
    resource_known = observation is not None and observed_provenance is not None
    reward_known = config is not None and reward_amount is not None
    fatigue_known = resource_known and config is not None
    return ActionSummaryPolicyPrerequisiteInput(
        task_identity=task_identity_from_page_model(model, card.card_match_key),
        remaining_attempts=RemainingAttemptsFact(
            status=(
                PrerequisiteFactStatus.KNOWN
                if attempts_known else PrerequisiteFactStatus.UNKNOWN
            ),
            remaining_attempts=card.remaining_attempts if attempts_known else None,
            total_attempts=card.total_attempts if attempts_known else None,
            provenance=page_provenance if attempts_known else None,
        ),
        resource_balance=ResourceBalanceFact(
            status=(
                PrerequisiteFactStatus.KNOWN
                if resource_known else PrerequisiteFactStatus.UNKNOWN
            ),
            resource_id=observation.resource_id if resource_known else None,
            available_amount=(
                observation.available_amount if resource_known else None
            ),
            unit_cost=card.cost if resource_known else None,
            provenance=observed_provenance if resource_known else None,
        ),
        reward_target=RewardTargetFact(
            status=(
                PrerequisiteFactStatus.KNOWN
                if reward_known else PrerequisiteFactStatus.UNKNOWN
            ),
            reward_target_id=config.reward_target_id if reward_known else None,
            current_amount=(
                config.reward_current_amount if reward_known else None
            ),
            target_amount=config.reward_target_amount if reward_known else None,
            candidate_reward_amount=reward_amount,
            provenance=user_provenance if reward_known else None,
        ),
        fatigue_budget=FatigueBudgetFact(
            status=(
                PrerequisiteFactStatus.KNOWN
                if fatigue_known else PrerequisiteFactStatus.UNKNOWN
            ),
            available_fatigue=(
                observation.available_fatigue if fatigue_known else None
            ),
            reserved_fatigue=config.reserved_fatigue if fatigue_known else None,
            max_policy_spend=config.max_policy_spend if fatigue_known else None,
            provenance=fatigue_provenance if fatigue_known else None,
        ),
        strategy=StrategyInputContract(
            schema_version=config.schema_version if config else _SCHEMA_VERSION,
            strategy_id=config.policy_id if config else "",
            strategy_version=config.policy_version if config else "",
            objective=(
                config.objective if config else StrategyObjective.OBSERVE_ONLY
            ),
            allowed_action_types=(
                config.allowed_action_types if config else frozenset()
            ),
            max_task_executions=(config.max_task_executions if config else 0),
            provenance=user_provenance,
        ),
    )


def _parse_config_or_reason(
    value: ActionSummaryUserPolicyConfig | Mapping[str, object],
) -> tuple[ActionSummaryUserPolicyConfig | None, str | None]:
    if isinstance(value, ActionSummaryUserPolicyConfig):
        return value, None
    try:
        return parse_action_summary_user_policy_config(value), None
    except (TypeError, ValueError) as error:
        return None, str(error) or "policy_config_invalid"


def _resource_freshness_token(
    observation: ActionSummaryRuntimeResourceObservation | None,
) -> str | None:
    if observation is None:
        return None
    payload = "|".join((
        observation.source_capture_id,
        observation.source_frame_sha256,
        observation.revision,
        observation.observed_at,
        observation.valid_until,
    ))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _source_relationship(
    page_model: ActionSummaryPageModel,
    observation: ActionSummaryRuntimeResourceObservation | None,
    *,
    maximum_allowed_age_seconds: int,
) -> tuple[SourceRelationship, float | None]:
    if observation is None:
        return SourceRelationship.UNKNOWN, None
    if not (
        page_model.source_capture_id
        and page_model.source_frame_sha256
        and observation.source_capture_id
        and observation.source_frame_sha256
    ):
        return SourceRelationship.UNKNOWN, None
    page_time = _parse_aware(page_model.captured_at or "")
    resource_time = _parse_aware(observation.observed_at)
    valid_until = _parse_aware(observation.valid_until)
    if page_time is None or resource_time is None or valid_until is None:
        return SourceRelationship.UNKNOWN, None
    age_seconds = abs((page_time - resource_time).total_seconds())
    within_window = (
        resource_time <= page_time <= valid_until
        and age_seconds <= maximum_allowed_age_seconds
    )
    if not within_window:
        return SourceRelationship.DIFFERENT_CAPTURE_STALE, age_seconds
    same_capture = (
        page_model.source_capture_id == observation.source_capture_id
        and page_model.source_frame_sha256 == observation.source_frame_sha256
    )
    return (
        SourceRelationship.SAME_CAPTURE
        if same_capture
        else SourceRelationship.DIFFERENT_CAPTURE_WITHIN_WINDOW,
        age_seconds,
    )


def _target_match(
    page_model: ActionSummaryPageModel,
    cards: tuple[ActionSummaryTaskCard, ...],
    *,
    requested_known_task_id: str | None,
    requested_task_title_hash: str | None,
) -> tuple[PolicyTargetMatchStatus, str | None]:
    if requested_known_task_id is None and requested_task_title_hash is None:
        return PolicyTargetMatchStatus.NOT_REQUESTED, None
    if page_model.activity_family == "UNKNOWN":
        return PolicyTargetMatchStatus.UNSUPPORTED, None

    expected_hash = requested_task_title_hash
    if expected_hash is not None and (
        len(expected_hash) != 64
        or any(character not in "0123456789abcdefABCDEF" for character in expected_hash)
    ):
        return PolicyTargetMatchStatus.UNSUPPORTED, None

    matches = tuple(
        card
        for card in cards
        if (
            requested_known_task_id is None
            or card.semantic_id == requested_known_task_id
        )
        and (expected_hash is None or card.title_hash == expected_hash)
    )
    if not matches:
        return PolicyTargetMatchStatus.NOT_FOUND, None
    if len(matches) != 1:
        return PolicyTargetMatchStatus.AMBIGUOUS, None
    return PolicyTargetMatchStatus.UNIQUE, matches[0].card_match_key


_CONFIG_ERROR_FIELDS = {
    "policy_schema_unsupported": "schema_version",
    "strategy_action_types_invalid": "allowed_action_types",
    "candidate_rewards_invalid": "candidate_rewards",
    "max_task_executions_invalid": "max_task_executions",
    "reward_current_amount_invalid": "reward_current_amount",
    "reward_target_amount_invalid": "reward_target_amount",
    "reserved_fatigue_invalid": "reserved_fatigue",
    "max_policy_spend_invalid": "max_policy_spend",
}


def _config_error_is_missing(
    value: ActionSummaryUserPolicyConfig | Mapping[str, object],
    error: str | None,
) -> bool:
    if error is None:
        return False
    if error.endswith("_missing"):
        return True
    if not isinstance(value, Mapping):
        return False
    try:
        data = _section(value)
    except ValueError:
        return False
    field = _CONFIG_ERROR_FIELDS.get(error)
    return bool(field and data.get(field) is None)


def _missing_runtime_inputs(reason_codes: tuple[str, ...]) -> tuple[str, ...]:
    missing_markers = (
        "_missing",
        "_unknown",
    )
    return tuple(
        reason
        for reason in reason_codes
        if reason.endswith(missing_markers)
        or reason in {
            "runtime_resource_observation_missing",
            "candidate_rewards_invalid",
            "strategy_action_types_invalid",
        }
    )


def assemble_action_summary_policy_runtime_inputs(
    page_model: ActionSummaryPageModel,
    user_policy: ActionSummaryUserPolicyConfig | Mapping[str, object],
    *,
    resource_observation: ActionSummaryRuntimeResourceObservation | None = None,
    assembled_at: str | None = None,
    maximum_allowed_age_seconds: int = 300,
) -> ActionSummaryRuntimeInputAssembly:
    """Assemble validated candidate facts without evaluating or executing policy."""

    if not _exact_int(maximum_allowed_age_seconds) or maximum_allowed_age_seconds <= 0:
        raise ValueError("maximum_allowed_age_seconds_invalid")
    assembled_at = assembled_at or datetime.now().astimezone().isoformat(
        timespec="milliseconds"
    )
    if _parse_aware(assembled_at) is None:
        raise ValueError("assembled_at_invalid")

    config, config_error = _parse_config_or_reason(user_policy)
    policy_fingerprint = action_summary_policy_config_fingerprint(user_policy)
    (
        policy_config_id,
        policy_config_version,
        policy_config_captured_at,
        requested_known_task_id,
        requested_task_title_hash,
    ) = _policy_metadata(user_policy, config)
    cards = _candidate_cards(page_model)
    observation_errors = _resource_observation_errors(resource_observation)
    source_relationship, source_age_seconds = _source_relationship(
        page_model,
        resource_observation,
        maximum_allowed_age_seconds=maximum_allowed_age_seconds,
    )
    stale_resource = (
        source_relationship is SourceRelationship.DIFFERENT_CAPTURE_STALE
    )
    usable_observation = (
        None if observation_errors or stale_resource else resource_observation
    )
    page_errors: list[str] = []
    if (
        page_model.page_state != "ACTION_SUMMARY_VISIBLE"
        or page_model.overlay_states
        or not page_model.source_capture_id
        or not page_model.source_frame_sha256
        or not page_model.model_freshness_token
        or not page_model.captured_at
    ):
        page_errors.append("source_page_not_runtime_eligible")
    if not cards:
        page_errors.append("no_policy_candidates")

    target_match_status, target_card_match_key = _target_match(
        page_model,
        cards,
        requested_known_task_id=requested_known_task_id,
        requested_task_title_hash=requested_task_title_hash,
    )
    target_errors: list[str] = []
    if target_match_status in {
        PolicyTargetMatchStatus.NOT_FOUND,
        PolicyTargetMatchStatus.AMBIGUOUS,
        PolicyTargetMatchStatus.UNSUPPORTED,
    }:
        target_errors.append(
            f"policy_target_{target_match_status.value.lower()}"
        )

    prerequisites = tuple(
        evaluate_action_summary_policy_prerequisites(
            page_model,
            _unknown_inputs(page_model, card, config, usable_observation),
        )
        for card in cards
    )
    reasons: list[str] = []
    if config_error:
        reasons.append(config_error)
    reasons.extend(page_errors)
    reasons.extend(observation_errors)
    if stale_resource:
        reasons.append("runtime_resource_observation_stale")
    reasons.extend(target_errors)
    reasons.extend(
        reason
        for candidate in prerequisites
        for reason in candidate.reason_codes
    )
    reason_codes = tuple(dict.fromkeys(reasons))
    missing_runtime_inputs = _missing_runtime_inputs(reason_codes)
    config_missing = _config_error_is_missing(user_policy, config_error)

    if page_errors:
        status = RuntimeInputAssemblyStatus.BLOCKED_SOURCE_PAGE
    elif target_errors or config_error or any(
        reason.startswith(("reward_target_", "strategy_"))
        for reason in reason_codes
    ):
        status = RuntimeInputAssemblyStatus.BLOCKED_USER_CONFIG
    elif all(
        candidate.status is PrerequisiteModelStatus.READY_FOR_POLICY_EVALUATION
        for candidate in prerequisites
    ) and len(prerequisites) == len(cards):
        status = RuntimeInputAssemblyStatus.READY_FOR_POLICY_EVALUATION
    else:
        status = RuntimeInputAssemblyStatus.BLOCKED_RUNTIME_FACTS
    ready = status is RuntimeInputAssemblyStatus.READY_FOR_POLICY_EVALUATION
    assembly_status = (
        AssemblyIntegrityStatus.FAIL
        if page_errors or target_errors or (config_error and not config_missing)
        else AssemblyIntegrityStatus.PASS
    )
    if ready:
        policy_input_readiness = PolicyInputReadiness.READY_FOR_POLICY_EVALUATION
    elif assembly_status is AssemblyIntegrityStatus.PASS:
        policy_input_readiness = PolicyInputReadiness.BLOCKED_MISSING_FACTS
    else:
        policy_input_readiness = PolicyInputReadiness.BLOCKED_CONFLICTING_INPUTS
    return ActionSummaryRuntimeInputAssembly(
        schema_version=_SCHEMA_VERSION,
        model_scope=_MODEL_SCOPE,
        status=status,
        assembly_status=assembly_status,
        policy_input_readiness=policy_input_readiness,
        reason_codes=reason_codes,
        missing_runtime_inputs=missing_runtime_inputs,
        assembled_at=assembled_at,
        source_capture_id=page_model.source_capture_id,
        source_frame_sha256=page_model.source_frame_sha256,
        source_model_freshness_token=page_model.model_freshness_token,
        page_model_captured_at=page_model.captured_at,
        resource_observation_capture_id=(
            resource_observation.source_capture_id
            if resource_observation is not None
            else None
        ),
        resource_observation_frame_sha256=(
            resource_observation.source_frame_sha256
            if resource_observation is not None
            else None
        ),
        resource_observation_captured_at=(
            resource_observation.observed_at
            if resource_observation is not None
            else None
        ),
        resource_observation_freshness_token=_resource_freshness_token(
            resource_observation
        ),
        source_relationship=source_relationship,
        source_age_seconds=source_age_seconds,
        maximum_allowed_age_seconds=maximum_allowed_age_seconds,
        policy_config_id=policy_config_id,
        policy_config_version=policy_config_version,
        policy_config_captured_at=policy_config_captured_at,
        policy_config_sha256=policy_fingerprint,
        policy_config_fingerprint_bound=True,
        stale_resource_input_accepted=False,
        policy_target_match_status=target_match_status,
        policy_target_card_match_key=target_card_match_key,
        source_policy_candidate_count=len(cards),
        candidate_prerequisites=prerequisites,
        policy_evaluation_allowed=ready,
        business_policy_evaluated=False,
        executor_connected=False,
        execution_authorized=False,
        authorization_issued=False,
        business_dispatches=0,
        irreversible_actions=0,
    )


__all__ = [
    "AssemblyIntegrityStatus",
    "ActionSummaryRuntimeInputAssembly",
    "ActionSummaryRuntimeResourceObservation",
    "ActionSummaryUserPolicyConfig",
    "CandidateRewardPolicy",
    "PolicyInputReadiness",
    "PolicyTargetMatchStatus",
    "RuntimeInputAssemblyStatus",
    "SourceRelationship",
    "action_summary_policy_config_fingerprint",
    "assemble_action_summary_policy_runtime_inputs",
    "load_action_summary_user_policy_config",
    "parse_action_summary_user_policy_config",
]
