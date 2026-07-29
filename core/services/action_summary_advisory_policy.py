"""Pure, read-only advisory policy for action-summary runtime inputs.

This module validates a frozen policy profile and a provenance-bound runtime
input assembly.  It may rank fully known candidates, but it cannot capture,
navigate, dispatch input, persist state, issue authorization, or execute a
recommendation.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from enum import Enum
from typing import Mapping, Sequence

from core.services.action_summary_policy_prerequisites import (
    ActionSummaryPolicyPrerequisiteModel,
    PrerequisiteFactStatus,
)
from core.services.action_summary_policy_runtime_inputs import (
    ActionSummaryRuntimeInputAssembly,
    AssemblyIntegrityStatus,
    PolicyTargetMatchStatus,
    SourceRelationship,
)


class AdvisoryObjective(str, Enum):
    FIXED_TASK = "FIXED_TASK"
    MAXIMIZE_PRIORITY_REWARD = "MAXIMIZE_PRIORITY_REWARD"
    MAXIMIZE_AVAILABLE_ATTEMPTS = "MAXIMIZE_AVAILABLE_ATTEMPTS"
    MINIMIZE_RESOURCE_COST = "MINIMIZE_RESOURCE_COST"
    BALANCED = "BALANCED"
    OBSERVE_ONLY = "OBSERVE_ONLY"


class AdvisoryPolicyStatus(str, Enum):
    OBSERVE_ONLY_COMPLETE = "OBSERVE_ONLY_COMPLETE"
    BLOCKED_MISSING_FACTS = "BLOCKED_MISSING_FACTS"
    BLOCKED_POLICY_INVALID = "BLOCKED_POLICY_INVALID"
    BLOCKED_PROVENANCE_INVALID = "BLOCKED_PROVENANCE_INVALID"
    BLOCKED_TARGET_NOT_FOUND = "BLOCKED_TARGET_NOT_FOUND"
    BLOCKED_TARGET_AMBIGUOUS = "BLOCKED_TARGET_AMBIGUOUS"
    NO_ELIGIBLE_TASK = "NO_ELIGIBLE_TASK"
    ADVISORY_RECOMMENDATION_READY = "ADVISORY_RECOMMENDATION_READY"


@dataclass(frozen=True, slots=True)
class ActionSummaryAdvisoryPolicyProfile:
    schema_version: str
    policy_id: str
    policy_version: str
    policy_fingerprint: str
    captured_at: str
    activity_family: str
    objective: AdvisoryObjective
    strategy_id: str
    strategy_version: str
    preferred_task_ids: tuple[str, ...]
    excluded_task_ids: frozenset[str]
    reward_priority_ids: tuple[str, ...]
    reward_priority_families: tuple[str, ...]
    maximum_cost_per_run: int
    maximum_total_cost: int
    minimum_resource_reserve: int
    minimum_remaining_attempts: int
    maximum_task_runs: int
    fatigue_reserve: int
    allow_unknown_reward: bool
    allow_unknown_attempts: bool
    allow_unknown_resource_identity: bool
    allow_unknown_resource_balance: bool
    tie_break_order: tuple[str, ...]
    reason: str

    def to_dict(self) -> dict[str, object]:
        document = asdict(self)
        document["objective"] = self.objective.value
        document["excluded_task_ids"] = sorted(self.excluded_task_ids)
        return document


@dataclass(frozen=True, slots=True)
class FactAcquisitionRequest:
    missing_fact: str
    required_scope: str
    preferred_source: str
    requires_navigation: bool | None
    requires_page_input: bool | None
    requires_business_input: bool
    priority: int
    reason: str
    target_subject_keys: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class AdvisoryRankedCandidate:
    rank: int
    card_match_key: str
    known_task_id: str
    recommended_run_count: int
    remaining_attempts: int
    resource_id: str
    unit_cost: int
    reward_target_id: str | None
    page_order: int
    reason_codes: tuple[str, ...]
    evidence_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RuntimeInputProvenanceBinding:
    assembly_schema_version: str
    assembly_model_scope: str
    assembly_fingerprint: str
    assembled_at: str
    evaluated_at: str
    page_model_capture_id: str | None
    page_model_frame_sha256: str | None
    page_model_captured_at: str | None
    page_model_freshness_token: str | None
    resource_observation_capture_id: str | None
    resource_observation_frame_sha256: str | None
    resource_observation_captured_at: str | None
    resource_observation_freshness_token: str | None
    source_relationship: str
    policy_config_fingerprint: str | None


@dataclass(frozen=True, slots=True)
class ActionSummaryAdvisoryPolicyResult:
    schema_version: str
    status: AdvisoryPolicyStatus
    ready: bool
    terminal: bool
    activity_family: str | None
    objective: str | None
    strategy_id: str | None
    strategy_version: str | None
    policy_fingerprint: str | None
    runtime_input_fingerprint: str
    runtime_input_provenance: RuntimeInputProvenanceBinding
    source_candidate_count: int
    candidate_count: int
    ranked_candidates: tuple[AdvisoryRankedCandidate, ...]
    recommended_card_match_key: str | None
    recommended_known_task_id: str | None
    recommended_run_count: int | None
    missing_facts: tuple[str, ...]
    block_reason_codes: tuple[str, ...]
    warnings: tuple[str, ...]
    reason_codes: tuple[str, ...]
    evidence_ids: tuple[str, ...]
    fact_acquisition_requests: tuple[FactAcquisitionRequest, ...]
    execution_authorized: bool
    authorization_issued: bool
    business_dispatches: int
    irreversible_actions: int

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "status": self.status.value,
            "ready": self.ready,
            "terminal": self.terminal,
            "activity_family": self.activity_family,
            "objective": self.objective,
            "strategy_id": self.strategy_id,
            "strategy_version": self.strategy_version,
            "policy_fingerprint": self.policy_fingerprint,
            "runtime_input_fingerprint": self.runtime_input_fingerprint,
            "runtime_input_provenance": asdict(self.runtime_input_provenance),
            "source_candidate_count": self.source_candidate_count,
            "candidate_count": self.candidate_count,
            "ranked_candidates": [
                asdict(candidate) for candidate in self.ranked_candidates
            ],
            "recommended_card_match_key": self.recommended_card_match_key,
            "recommended_known_task_id": self.recommended_known_task_id,
            "recommended_run_count": self.recommended_run_count,
            "missing_facts": list(self.missing_facts),
            "block_reason_codes": list(self.block_reason_codes),
            "warnings": list(self.warnings),
            "reason_codes": list(self.reason_codes),
            "evidence_ids": list(self.evidence_ids),
            "fact_acquisition_requests": [
                asdict(request) for request in self.fact_acquisition_requests
            ],
            "execution_authorized": self.execution_authorized,
            "authorization_issued": self.authorization_issued,
            "business_dispatches": self.business_dispatches,
            "irreversible_actions": self.irreversible_actions,
        }

    def matches_policy_profile(
        self, profile: ActionSummaryAdvisoryPolicyProfile
    ) -> bool:
        return bool(
            self.policy_fingerprint
            and self.policy_fingerprint
            == action_summary_advisory_policy_fingerprint(profile)
        )

    def matches_runtime_input(
        self, runtime_input: ActionSummaryRuntimeInputAssembly
    ) -> bool:
        return self.runtime_input_fingerprint == runtime_input_fingerprint(
            runtime_input
        )


_SCHEMA_VERSION = "1.0"
_MODEL_SCOPE = "ACTION_SUMMARY_POLICY_RUNTIME_INPUT_ASSEMBLY_V1"
_ALLOWED_TIE_BREAKS = frozenset({"PAGE_ORDER", "TASK_ID", "CARD_MATCH_KEY"})


def _deduplicate(values: Sequence[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(value for value in values if value))


def _parse_aware(value: str | None) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed


def _is_hash(value: object) -> bool:
    return bool(
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdefABCDEF" for character in value)
    )


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field}_missing")
    return value.strip()


def _exact_int(value: object, field: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise ValueError(f"{field}_invalid")
    return value


def _exact_bool(value: object, field: str) -> bool:
    if type(value) is not bool:
        raise ValueError(f"{field}_invalid")
    return value


def _string_tuple(
    value: object,
    field: str,
    *,
    allow_empty: bool = True,
) -> tuple[str, ...]:
    if (
        not isinstance(value, Sequence)
        or isinstance(value, (str, bytes))
        or not all(isinstance(item, str) and item.strip() for item in value)
    ):
        raise ValueError(f"{field}_invalid")
    result = tuple(item.strip() for item in value)
    if not allow_empty and not result:
        raise ValueError(f"{field}_missing")
    if len(set(result)) != len(result):
        raise ValueError(f"{field}_duplicate")
    return result


def _profile_payload(profile: ActionSummaryAdvisoryPolicyProfile) -> bytes:
    document = profile.to_dict()
    document.pop("policy_fingerprint", None)
    return json.dumps(
        document,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def action_summary_advisory_policy_fingerprint(
    profile: ActionSummaryAdvisoryPolicyProfile,
) -> str:
    return hashlib.sha256(_profile_payload(profile)).hexdigest()


def create_action_summary_advisory_policy_profile(
    *,
    policy_id: str,
    policy_version: str,
    captured_at: str,
    activity_family: str,
    strategy_id: str,
    strategy_version: str,
    objective: AdvisoryObjective = AdvisoryObjective.OBSERVE_ONLY,
    preferred_task_ids: Sequence[str] = (),
    excluded_task_ids: Sequence[str] = (),
    reward_priority_ids: Sequence[str] = (),
    reward_priority_families: Sequence[str] = (),
    maximum_cost_per_run: int = 0,
    maximum_total_cost: int = 0,
    minimum_resource_reserve: int = 0,
    minimum_remaining_attempts: int = 0,
    maximum_task_runs: int = 0,
    fatigue_reserve: int = 0,
    allow_unknown_reward: bool = False,
    allow_unknown_attempts: bool = False,
    allow_unknown_resource_identity: bool = False,
    allow_unknown_resource_balance: bool = False,
    tie_break_order: Sequence[str] = ("PAGE_ORDER", "TASK_ID"),
    reason: str = "read_only_advisory",
) -> ActionSummaryAdvisoryPolicyProfile:
    """Create one immutable profile and bind its canonical fingerprint."""

    profile = ActionSummaryAdvisoryPolicyProfile(
        schema_version=_SCHEMA_VERSION,
        policy_id=_text(policy_id, "policy_id"),
        policy_version=_text(policy_version, "policy_version"),
        policy_fingerprint="",
        captured_at=_text(captured_at, "captured_at"),
        activity_family=_text(activity_family, "activity_family"),
        objective=AdvisoryObjective(objective),
        strategy_id=_text(strategy_id, "strategy_id"),
        strategy_version=_text(strategy_version, "strategy_version"),
        preferred_task_ids=_string_tuple(preferred_task_ids, "preferred_task_ids"),
        excluded_task_ids=frozenset(
            _string_tuple(excluded_task_ids, "excluded_task_ids")
        ),
        reward_priority_ids=_string_tuple(
            reward_priority_ids, "reward_priority_ids"
        ),
        reward_priority_families=_string_tuple(
            reward_priority_families, "reward_priority_families"
        ),
        maximum_cost_per_run=_exact_int(
            maximum_cost_per_run, "maximum_cost_per_run"
        ),
        maximum_total_cost=_exact_int(
            maximum_total_cost, "maximum_total_cost"
        ),
        minimum_resource_reserve=_exact_int(
            minimum_resource_reserve, "minimum_resource_reserve"
        ),
        minimum_remaining_attempts=_exact_int(
            minimum_remaining_attempts, "minimum_remaining_attempts"
        ),
        maximum_task_runs=_exact_int(maximum_task_runs, "maximum_task_runs"),
        fatigue_reserve=_exact_int(fatigue_reserve, "fatigue_reserve"),
        allow_unknown_reward=_exact_bool(
            allow_unknown_reward, "allow_unknown_reward"
        ),
        allow_unknown_attempts=_exact_bool(
            allow_unknown_attempts, "allow_unknown_attempts"
        ),
        allow_unknown_resource_identity=_exact_bool(
            allow_unknown_resource_identity, "allow_unknown_resource_identity"
        ),
        allow_unknown_resource_balance=_exact_bool(
            allow_unknown_resource_balance, "allow_unknown_resource_balance"
        ),
        tie_break_order=_string_tuple(
            tie_break_order, "tie_break_order", allow_empty=False
        ),
        reason=_text(reason, "reason"),
    )
    _validate_profile(profile, fingerprint_required=False)
    return replace(
        profile,
        policy_fingerprint=action_summary_advisory_policy_fingerprint(profile),
    )


def _profile_section(document: Mapping[str, object]) -> Mapping[str, object]:
    section = document.get("ActionSummaryAdvisoryPolicy", document)
    if not isinstance(section, Mapping):
        raise ValueError("advisory_policy_section_invalid")
    return section


def parse_action_summary_advisory_policy_profile(
    document: Mapping[str, object],
) -> ActionSummaryAdvisoryPolicyProfile:
    if not isinstance(document, Mapping):
        raise ValueError("advisory_policy_document_invalid")
    data = _profile_section(document)
    if data.get("schema_version") != _SCHEMA_VERSION:
        raise ValueError("advisory_policy_schema_unsupported")
    try:
        objective = AdvisoryObjective(_text(data.get("objective"), "objective"))
    except ValueError as error:
        if str(error).endswith("_missing"):
            raise
        raise ValueError("objective_invalid") from error
    profile = ActionSummaryAdvisoryPolicyProfile(
        schema_version=_SCHEMA_VERSION,
        policy_id=_text(data.get("policy_id"), "policy_id"),
        policy_version=_text(data.get("policy_version"), "policy_version"),
        policy_fingerprint=_text(
            data.get("policy_fingerprint"), "policy_fingerprint"
        ),
        captured_at=_text(data.get("captured_at"), "captured_at"),
        activity_family=_text(data.get("activity_family"), "activity_family"),
        objective=objective,
        strategy_id=_text(data.get("strategy_id"), "strategy_id"),
        strategy_version=_text(data.get("strategy_version"), "strategy_version"),
        preferred_task_ids=_string_tuple(
            data.get("preferred_task_ids"), "preferred_task_ids"
        ),
        excluded_task_ids=frozenset(_string_tuple(
            data.get("excluded_task_ids"), "excluded_task_ids"
        )),
        reward_priority_ids=_string_tuple(
            data.get("reward_priority_ids"), "reward_priority_ids"
        ),
        reward_priority_families=_string_tuple(
            data.get("reward_priority_families"), "reward_priority_families"
        ),
        maximum_cost_per_run=_exact_int(
            data.get("maximum_cost_per_run"), "maximum_cost_per_run"
        ),
        maximum_total_cost=_exact_int(
            data.get("maximum_total_cost"), "maximum_total_cost"
        ),
        minimum_resource_reserve=_exact_int(
            data.get("minimum_resource_reserve"), "minimum_resource_reserve"
        ),
        minimum_remaining_attempts=_exact_int(
            data.get("minimum_remaining_attempts"),
            "minimum_remaining_attempts",
        ),
        maximum_task_runs=_exact_int(
            data.get("maximum_task_runs"), "maximum_task_runs"
        ),
        fatigue_reserve=_exact_int(
            data.get("fatigue_reserve"), "fatigue_reserve"
        ),
        allow_unknown_reward=_exact_bool(
            data.get("allow_unknown_reward"), "allow_unknown_reward"
        ),
        allow_unknown_attempts=_exact_bool(
            data.get("allow_unknown_attempts"), "allow_unknown_attempts"
        ),
        allow_unknown_resource_identity=_exact_bool(
            data.get("allow_unknown_resource_identity"),
            "allow_unknown_resource_identity",
        ),
        allow_unknown_resource_balance=_exact_bool(
            data.get("allow_unknown_resource_balance"),
            "allow_unknown_resource_balance",
        ),
        tie_break_order=_string_tuple(
            data.get("tie_break_order"), "tie_break_order", allow_empty=False
        ),
        reason=_text(data.get("reason"), "reason"),
    )
    _validate_profile(profile, fingerprint_required=True)
    return profile


def _validate_profile(
    profile: ActionSummaryAdvisoryPolicyProfile,
    *,
    fingerprint_required: bool,
) -> None:
    if _parse_aware(profile.captured_at) is None:
        raise ValueError("policy_captured_at_invalid")
    if not set(profile.tie_break_order).issubset(_ALLOWED_TIE_BREAKS):
        raise ValueError("tie_break_order_unsupported")
    if set(profile.preferred_task_ids) & profile.excluded_task_ids:
        raise ValueError("preferred_task_excluded_conflict")
    if profile.objective is AdvisoryObjective.FIXED_TASK:
        if not profile.preferred_task_ids:
            raise ValueError("preferred_task_ids_missing")
        if len(profile.preferred_task_ids) != 1:
            raise ValueError("fixed_task_target_count_invalid")
    if profile.objective is not AdvisoryObjective.OBSERVE_ONLY and (
        profile.maximum_cost_per_run <= 0
        or profile.maximum_total_cost <= 0
        or profile.maximum_task_runs <= 0
    ):
        raise ValueError("policy_execution_bounds_invalid")
    if (
        profile.allow_unknown_reward
        and profile.objective
        not in {
            AdvisoryObjective.FIXED_TASK,
            AdvisoryObjective.MAXIMIZE_AVAILABLE_ATTEMPTS,
            AdvisoryObjective.MINIMIZE_RESOURCE_COST,
            AdvisoryObjective.OBSERVE_ONLY,
        }
    ):
        raise ValueError("allow_unknown_reward_not_supported_for_objective")
    if (
        profile.objective is not AdvisoryObjective.OBSERVE_ONLY
        and profile.allow_unknown_attempts
    ):
        raise ValueError("allow_unknown_attempts_not_supported_for_recommendation")
    if (
        profile.objective is not AdvisoryObjective.OBSERVE_ONLY
        and profile.allow_unknown_resource_identity
    ):
        raise ValueError(
            "allow_unknown_resource_identity_not_supported_for_recommendation"
        )
    if (
        profile.objective is not AdvisoryObjective.OBSERVE_ONLY
        and profile.allow_unknown_resource_balance
    ):
        raise ValueError(
            "allow_unknown_resource_balance_not_supported_for_recommendation"
        )
    if fingerprint_required and (
        not _is_hash(profile.policy_fingerprint)
        or profile.policy_fingerprint
        != action_summary_advisory_policy_fingerprint(profile)
    ):
        raise ValueError("policy_fingerprint_mismatch")


def runtime_input_fingerprint(
    runtime_input: ActionSummaryRuntimeInputAssembly,
) -> str:
    payload = json.dumps(
        runtime_input.to_dict(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _runtime_provenance(
    runtime_input: ActionSummaryRuntimeInputAssembly,
    *,
    fingerprint: str,
    evaluated_at: str,
) -> RuntimeInputProvenanceBinding:
    return RuntimeInputProvenanceBinding(
        assembly_schema_version=runtime_input.schema_version,
        assembly_model_scope=runtime_input.model_scope,
        assembly_fingerprint=fingerprint,
        assembled_at=runtime_input.assembled_at,
        evaluated_at=evaluated_at,
        page_model_capture_id=runtime_input.source_capture_id,
        page_model_frame_sha256=runtime_input.source_frame_sha256,
        page_model_captured_at=runtime_input.page_model_captured_at,
        page_model_freshness_token=runtime_input.source_model_freshness_token,
        resource_observation_capture_id=(
            runtime_input.resource_observation_capture_id
        ),
        resource_observation_frame_sha256=(
            runtime_input.resource_observation_frame_sha256
        ),
        resource_observation_captured_at=(
            runtime_input.resource_observation_captured_at
        ),
        resource_observation_freshness_token=(
            runtime_input.resource_observation_freshness_token
        ),
        source_relationship=runtime_input.source_relationship.value,
        policy_config_fingerprint=runtime_input.policy_config_sha256,
    )


def _provenance_errors(
    runtime_input: ActionSummaryRuntimeInputAssembly,
    evaluated_at: str,
) -> tuple[str, ...]:
    errors: list[str] = []
    if runtime_input.schema_version != _SCHEMA_VERSION:
        errors.append("runtime_input_schema_unsupported")
    if runtime_input.model_scope != _MODEL_SCOPE:
        errors.append("runtime_input_model_scope_invalid")
    if runtime_input.assembly_status is not AssemblyIntegrityStatus.PASS:
        errors.append("runtime_input_assembly_failed")
    if (
        runtime_input.business_policy_evaluated
        or runtime_input.executor_connected
        or runtime_input.execution_authorized
        or runtime_input.authorization_issued
        or runtime_input.business_dispatches != 0
        or runtime_input.irreversible_actions != 0
    ):
        errors.append("runtime_input_authority_boundary_invalid")
    if not (
        runtime_input.source_capture_id
        and _is_hash(runtime_input.source_frame_sha256)
        and _is_hash(runtime_input.source_model_freshness_token)
        and _parse_aware(runtime_input.page_model_captured_at)
    ):
        errors.append("page_model_provenance_incomplete")
    if not (
        runtime_input.policy_config_fingerprint_bound
        and _is_hash(runtime_input.policy_config_sha256)
    ):
        errors.append("runtime_policy_fingerprint_unbound")
    assembled = _parse_aware(runtime_input.assembled_at)
    evaluated = _parse_aware(evaluated_at)
    if assembled is None or evaluated is None or evaluated < assembled:
        errors.append("runtime_input_time_invalid")
    elif (
        evaluated - assembled
    ).total_seconds() > runtime_input.maximum_allowed_age_seconds:
        errors.append("runtime_input_stale")
    if (
        runtime_input.stale_resource_input_accepted
        or runtime_input.source_relationship
        is SourceRelationship.DIFFERENT_CAPTURE_STALE
    ):
        errors.append("stale_resource_input_invalid")
    resource_present = any((
        runtime_input.resource_observation_capture_id,
        runtime_input.resource_observation_frame_sha256,
        runtime_input.resource_observation_captured_at,
        runtime_input.resource_observation_freshness_token,
    ))
    if resource_present and not (
        runtime_input.resource_observation_capture_id
        and _is_hash(runtime_input.resource_observation_frame_sha256)
        and _parse_aware(runtime_input.resource_observation_captured_at)
        and _is_hash(runtime_input.resource_observation_freshness_token)
        and runtime_input.source_relationship
        in {
            SourceRelationship.SAME_CAPTURE,
            SourceRelationship.DIFFERENT_CAPTURE_WITHIN_WINDOW,
        }
    ):
        errors.append("resource_observation_provenance_incomplete")
    candidates = runtime_input.candidate_prerequisites
    if runtime_input.source_policy_candidate_count != len(candidates):
        errors.append("runtime_candidate_set_incomplete")
    keys = [candidate.candidate_card_match_key for candidate in candidates]
    if any(not key for key in keys) or len(set(keys)) != len(keys):
        errors.append("runtime_candidate_identity_ambiguous")
    for candidate in candidates:
        if (
            candidate.source_capture_id != runtime_input.source_capture_id
            or candidate.source_frame_sha256 != runtime_input.source_frame_sha256
            or candidate.source_model_freshness_token
            != runtime_input.source_model_freshness_token
        ):
            errors.append("runtime_candidate_provenance_mismatch")
        if (
            candidate.execution_authorized
            or candidate.selected_action is not None
            or candidate.business_dispatches != 0
            or candidate.irreversible_actions != 0
        ):
            errors.append("candidate_authority_boundary_invalid")
        if any(reason.endswith("_stale") for reason in candidate.reason_codes):
            errors.append("candidate_fact_provenance_stale")
    return _deduplicate(errors)


def _profile_or_error(
    value: ActionSummaryAdvisoryPolicyProfile | Mapping[str, object],
) -> tuple[ActionSummaryAdvisoryPolicyProfile | None, str | None, str | None]:
    if isinstance(value, ActionSummaryAdvisoryPolicyProfile):
        try:
            _validate_profile(value, fingerprint_required=True)
        except ValueError as error:
            return None, str(error), value.policy_fingerprint or None
        return value, None, value.policy_fingerprint
    try:
        profile = parse_action_summary_advisory_policy_profile(value)
    except (TypeError, ValueError) as error:
        snapshot = None
        try:
            snapshot = hashlib.sha256(json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")).hexdigest()
        except (TypeError, ValueError):
            pass
        return None, str(error) or "advisory_policy_invalid", snapshot
    return profile, None, profile.policy_fingerprint


def _normalized_missing_facts(
    runtime_input: ActionSummaryRuntimeInputAssembly,
) -> tuple[str, ...]:
    missing = list(runtime_input.missing_runtime_inputs)
    for candidate in runtime_input.candidate_prerequisites:
        if candidate.remaining_attempts.status is not PrerequisiteFactStatus.KNOWN:
            missing.append("remaining_attempts_unknown")
        resource = candidate.resource_balance
        if (
            resource.status is not PrerequisiteFactStatus.KNOWN
            or not resource.resource_id
            or resource.resource_id == "UNKNOWN"
        ):
            missing.append("resource_identity_unknown")
        if (
            resource.status is not PrerequisiteFactStatus.KNOWN
            or resource.available_amount is None
        ):
            missing.append("resource_balance_unknown")
        reward = candidate.reward_target
        if reward.status is not PrerequisiteFactStatus.KNOWN:
            missing.append("reward_target_unknown")
        if candidate.fatigue_budget.status is not PrerequisiteFactStatus.KNOWN:
            missing.append("fatigue_budget_unknown")
        fatigue = candidate.fatigue_budget
        if (
            fatigue.fatigue_cost_applicable is None
            or fatigue.fatigue_cost_per_run is None
            or not fatigue.fatigue_unit_id
            or fatigue.fatigue_unit_id == "UNKNOWN"
        ):
            missing.append("fatigue_cost_unknown")
        if not candidate.strategy.strategy_id or not candidate.strategy.strategy_version:
            missing.append("strategy_identity_missing")
        if candidate.strategy.provenance is None:
            missing.append("strategy_provenance_missing")
    return _deduplicate(missing)


_ACQUISITION_CONTRACTS: dict[
    str, tuple[str, str, bool | None, bool | None, int, str]
] = {
    "objective_missing": (
        "USER_POLICY", "user_policy_configuration", False, False, 1,
        "objective must be explicitly configured",
    ),
    "task_identity_unknown": (
        "TASK_CARD", "current_page_read_only_model", False, False, 1,
        "task identity must be bound before target selection",
    ),
    "strategy_identity_missing": (
        "USER_POLICY", "user_policy_configuration", False, False, 1,
        "strategy identity must be frozen",
    ),
    "strategy_provenance_missing": (
        "USER_POLICY", "user_policy_configuration", False, False, 1,
        "strategy provenance must be bound",
    ),
    "remaining_attempts_unknown": (
        "TASK_CARD", "card_or_detail_read_only_observer", None, None, 2,
        "card-scoped remaining and total attempts are required",
    ),
    "resource_identity_unknown": (
        "RESOURCE", "existing_read_only_resource_observer", False, False, 1,
        "cost resource identity is required",
    ),
    "resource_balance_unknown": (
        "RESOURCE", "existing_read_only_resource_observer", False, False, 1,
        "current resource balance is required",
    ),
    "reward_target_unknown": (
        "TASK_REWARD", "task_reward_read_only_observer", None, None, 3,
        "reward target must be bound to the current task",
    ),
    "fatigue_budget_unknown": (
        "RESOURCE", "existing_read_only_resource_observer", False, False, 1,
        "fatigue balance and reserve are required",
    ),
    "fatigue_cost_unknown": (
        "TASK_COST", "task_cost_read_only_observer", None, None, 2,
        "fatigue cost per run must be observed independently of resource cost",
    ),
}


def _acquisition_requests(
    missing_facts: Sequence[str],
) -> tuple[FactAcquisitionRequest, ...]:
    requests: list[FactAcquisitionRequest] = []
    for fact in _deduplicate(missing_facts):
        contract = _ACQUISITION_CONTRACTS.get(fact)
        if contract is None:
            contract = (
                "UNKNOWN", "read_only_observer_required", None, None, 4,
                "missing fact requires a separate read-only source",
            )
        scope, source, navigation, page_input, priority, reason = contract
        requests.append(FactAcquisitionRequest(
            missing_fact=fact,
            required_scope=scope,
            preferred_source=source,
            requires_navigation=navigation,
            requires_page_input=page_input,
            requires_business_input=False,
            priority=priority,
            reason=reason,
        ))
    return tuple(sorted(requests, key=lambda request: (
        request.priority, request.missing_fact
    )))


def _empty_result(
    *,
    runtime_input: ActionSummaryRuntimeInputAssembly,
    provenance: RuntimeInputProvenanceBinding,
    status: AdvisoryPolicyStatus,
    profile: ActionSummaryAdvisoryPolicyProfile | None,
    policy_fingerprint: str | None,
    missing_facts: Sequence[str] = (),
    block_reasons: Sequence[str] = (),
    warnings: Sequence[str] = (),
    reason_codes: Sequence[str] = (),
) -> ActionSummaryAdvisoryPolicyResult:
    missing = _deduplicate(missing_facts)
    activity_family = profile.activity_family if profile else (
        runtime_input.candidate_prerequisites[0].task_identity.activity_family
        if runtime_input.candidate_prerequisites
        else None
    )
    return ActionSummaryAdvisoryPolicyResult(
        schema_version=_SCHEMA_VERSION,
        status=status,
        ready=status is AdvisoryPolicyStatus.OBSERVE_ONLY_COMPLETE,
        terminal=True,
        activity_family=activity_family,
        objective=profile.objective.value if profile else None,
        strategy_id=profile.strategy_id if profile else None,
        strategy_version=profile.strategy_version if profile else None,
        policy_fingerprint=policy_fingerprint,
        runtime_input_fingerprint=provenance.assembly_fingerprint,
        runtime_input_provenance=provenance,
        source_candidate_count=runtime_input.source_policy_candidate_count,
        candidate_count=0,
        ranked_candidates=(),
        recommended_card_match_key=None,
        recommended_known_task_id=None,
        recommended_run_count=None,
        missing_facts=missing,
        block_reason_codes=_deduplicate(block_reasons),
        warnings=_deduplicate(warnings),
        reason_codes=_deduplicate(reason_codes),
        evidence_ids=(),
        fact_acquisition_requests=_acquisition_requests(missing),
        execution_authorized=False,
        authorization_issued=False,
        business_dispatches=0,
        irreversible_actions=0,
    )


def _candidate_missing(
    candidate: ActionSummaryPolicyPrerequisiteModel,
    profile: ActionSummaryAdvisoryPolicyProfile,
) -> tuple[str, ...]:
    missing: list[str] = []
    identity = candidate.task_identity
    if (
        identity.status is not PrerequisiteFactStatus.KNOWN
        or not identity.task_semantic_id
        or not identity.card_match_key
    ):
        missing.append("task_identity_unknown")
    attempts = candidate.remaining_attempts
    if (
        attempts.status is not PrerequisiteFactStatus.KNOWN
        or attempts.remaining_attempts is None
        or attempts.total_attempts is None
    ):
        missing.append("remaining_attempts_unknown")
    resource = candidate.resource_balance
    if (
        resource.status is not PrerequisiteFactStatus.KNOWN
        or not resource.resource_id
        or resource.resource_id == "UNKNOWN"
    ):
        missing.append("resource_identity_unknown")
    if (
        resource.status is not PrerequisiteFactStatus.KNOWN
        or resource.available_amount is None
    ):
        missing.append("resource_balance_unknown")
    if resource.unit_cost is None:
        missing.append("resource_cost_unknown")
    fatigue = candidate.fatigue_budget
    if (
        fatigue.status is not PrerequisiteFactStatus.KNOWN
        or fatigue.available_fatigue is None
        or fatigue.reserved_fatigue is None
        or fatigue.max_policy_spend is None
    ):
        missing.append("fatigue_budget_unknown")
    if (
        fatigue.fatigue_cost_applicable is None
        or fatigue.fatigue_cost_per_run is None
        or not fatigue.fatigue_unit_id
        or fatigue.fatigue_unit_id == "UNKNOWN"
    ):
        missing.append("fatigue_cost_unknown")
    if (
        not candidate.strategy.strategy_id
        or not candidate.strategy.strategy_version
        or candidate.strategy.provenance is None
        or candidate.strategy.max_task_executions <= 0
    ):
        missing.append("strategy_identity_missing")
    reward = candidate.reward_target
    reward_unknown = (
        reward.status is not PrerequisiteFactStatus.KNOWN
        or not reward.reward_target_id
        or reward.candidate_reward_amount is None
    )
    if reward_unknown and (
        profile.objective is AdvisoryObjective.MAXIMIZE_PRIORITY_REWARD
        or not profile.allow_unknown_reward
    ):
        missing.append("reward_target_unknown")
    return _deduplicate(missing)


def _candidate_ineligibility(
    candidate: ActionSummaryPolicyPrerequisiteModel,
) -> str | None:
    if candidate.candidate_task_state != "available":
        return "candidate_task_not_available"
    strategy = candidate.strategy
    strategy_complete = bool(
        strategy.strategy_id
        and strategy.strategy_version
        and strategy.provenance is not None
        and strategy.max_task_executions > 0
    )
    if strategy_complete and not candidate.candidate_execution_supported:
        return "candidate_execution_unsupported"
    return None


def _run_count(
    candidate: ActionSummaryPolicyPrerequisiteModel,
    profile: ActionSummaryAdvisoryPolicyProfile,
) -> int | None:
    attempts = candidate.remaining_attempts.remaining_attempts
    resource = candidate.resource_balance
    fatigue = candidate.fatigue_budget
    resource_cost = resource.unit_cost
    fatigue_cost = fatigue.fatigue_cost_per_run
    if any(value is None for value in (
        attempts,
        resource.available_amount,
        resource_cost,
        fatigue.available_fatigue,
        fatigue.reserved_fatigue,
        fatigue.max_policy_spend,
        fatigue.fatigue_cost_applicable,
        fatigue_cost,
    )):
        return None
    if resource_cost <= 0 or candidate.strategy.max_task_executions <= 0:
        return None
    if fatigue.fatigue_cost_applicable and fatigue_cost <= 0:
        return None
    if not fatigue.fatigue_cost_applicable and fatigue_cost != 0:
        return None
    resource_budget = max(
        0, resource.available_amount - profile.minimum_resource_reserve
    )
    fatigue_budget = min(
        max(
            0,
            fatigue.available_fatigue
            - max(fatigue.reserved_fatigue, profile.fatigue_reserve),
        ),
        fatigue.max_policy_spend,
    )
    limits = [
        attempts,
        profile.maximum_task_runs,
        profile.maximum_total_cost // resource_cost,
        resource_budget // resource_cost,
        candidate.strategy.max_task_executions,
    ]
    if fatigue.fatigue_cost_applicable:
        limits.append(fatigue_budget // fatigue_cost)
    return min(limits)


def _reward_rank(
    candidate: ActionSummaryPolicyPrerequisiteModel,
    profile: ActionSummaryAdvisoryPolicyProfile,
) -> int:
    reward_id = candidate.reward_target.reward_target_id or ""
    if reward_id in profile.reward_priority_ids:
        return profile.reward_priority_ids.index(reward_id)
    family = reward_id.split(":", 1)[0] if ":" in reward_id else ""
    if family in profile.reward_priority_families:
        return len(profile.reward_priority_ids) + profile.reward_priority_families.index(
            family
        )
    return len(profile.reward_priority_ids) + len(profile.reward_priority_families)


def _tie_break(
    candidate: ActionSummaryPolicyPrerequisiteModel,
    page_order: int,
    profile: ActionSummaryAdvisoryPolicyProfile,
) -> tuple[object, ...]:
    values: list[object] = []
    for token in profile.tie_break_order:
        if token == "PAGE_ORDER":
            values.append(page_order)
        elif token == "TASK_ID":
            values.append(candidate.task_identity.task_semantic_id or "")
        elif token == "CARD_MATCH_KEY":
            values.append(candidate.candidate_card_match_key or "")
    return tuple(values)


def _sort_key(
    candidate: ActionSummaryPolicyPrerequisiteModel,
    page_order: int,
    profile: ActionSummaryAdvisoryPolicyProfile,
) -> tuple[object, ...]:
    attempts = candidate.remaining_attempts.remaining_attempts or 0
    cost = candidate.resource_balance.unit_cost or 0
    tie = _tie_break(candidate, page_order, profile)
    if profile.objective is AdvisoryObjective.FIXED_TASK:
        task_id = candidate.task_identity.task_semantic_id or ""
        preferred = profile.preferred_task_ids.index(task_id)
        return (preferred, page_order, *tie)
    if profile.objective is AdvisoryObjective.MAXIMIZE_PRIORITY_REWARD:
        return (_reward_rank(candidate, profile), -attempts, cost, page_order, *tie)
    if profile.objective is AdvisoryObjective.MAXIMIZE_AVAILABLE_ATTEMPTS:
        return (-attempts, cost, page_order, *tie)
    return (cost, page_order, *tie)


def evaluate_action_summary_policy_advisory(
    runtime_input: ActionSummaryRuntimeInputAssembly,
    profile_value: ActionSummaryAdvisoryPolicyProfile | Mapping[str, object],
    *,
    evaluated_at: str | None = None,
) -> ActionSummaryAdvisoryPolicyResult:
    """Return a deterministic advisory result with permanent zero authority."""

    evaluated_at = evaluated_at or datetime.now().astimezone().isoformat(
        timespec="milliseconds"
    )
    input_fingerprint = runtime_input_fingerprint(runtime_input)
    provenance = _runtime_provenance(
        runtime_input,
        fingerprint=input_fingerprint,
        evaluated_at=evaluated_at,
    )
    profile, profile_error, policy_fingerprint = _profile_or_error(profile_value)
    baseline_missing = _normalized_missing_facts(runtime_input)
    provenance_errors = _provenance_errors(runtime_input, evaluated_at)
    if provenance_errors:
        return _empty_result(
            runtime_input=runtime_input,
            provenance=provenance,
            status=AdvisoryPolicyStatus.BLOCKED_PROVENANCE_INVALID,
            profile=profile,
            policy_fingerprint=policy_fingerprint,
            missing_facts=baseline_missing,
            block_reasons=provenance_errors,
            reason_codes=provenance_errors,
        )
    if profile_error:
        missing = profile_error.endswith("_missing")
        return _empty_result(
            runtime_input=runtime_input,
            provenance=provenance,
            status=(
                AdvisoryPolicyStatus.BLOCKED_MISSING_FACTS
                if missing
                else AdvisoryPolicyStatus.BLOCKED_POLICY_INVALID
            ),
            profile=None,
            policy_fingerprint=policy_fingerprint,
            missing_facts=(*baseline_missing, profile_error) if missing else baseline_missing,
            block_reasons=(profile_error,),
            reason_codes=(profile_error,),
        )
    assert profile is not None
    if profile.activity_family != (
        runtime_input.candidate_prerequisites[0].task_identity.activity_family
        if runtime_input.candidate_prerequisites else None
    ):
        return _empty_result(
            runtime_input=runtime_input,
            provenance=provenance,
            status=AdvisoryPolicyStatus.BLOCKED_POLICY_INVALID,
            profile=profile,
            policy_fingerprint=profile.policy_fingerprint,
            block_reasons=("activity_family_mismatch",),
            reason_codes=("activity_family_mismatch",),
        )
    if runtime_input.policy_target_match_status is PolicyTargetMatchStatus.NOT_FOUND:
        return _empty_result(
            runtime_input=runtime_input, provenance=provenance,
            status=AdvisoryPolicyStatus.BLOCKED_TARGET_NOT_FOUND,
            profile=profile, policy_fingerprint=profile.policy_fingerprint,
            block_reasons=("policy_target_not_found",),
            reason_codes=("policy_target_not_found",),
        )
    if runtime_input.policy_target_match_status is PolicyTargetMatchStatus.AMBIGUOUS:
        return _empty_result(
            runtime_input=runtime_input, provenance=provenance,
            status=AdvisoryPolicyStatus.BLOCKED_TARGET_AMBIGUOUS,
            profile=profile, policy_fingerprint=profile.policy_fingerprint,
            block_reasons=("policy_target_ambiguous",),
            reason_codes=("policy_target_ambiguous",),
        )
    if profile.objective is AdvisoryObjective.OBSERVE_ONLY:
        return _empty_result(
            runtime_input=runtime_input,
            provenance=provenance,
            status=AdvisoryPolicyStatus.OBSERVE_ONLY_COMPLETE,
            profile=profile,
            policy_fingerprint=profile.policy_fingerprint,
            missing_facts=baseline_missing,
            warnings=baseline_missing,
            reason_codes=("observe_only_no_recommendation",),
        )
    if profile.objective is AdvisoryObjective.BALANCED:
        return _empty_result(
            runtime_input=runtime_input, provenance=provenance,
            status=AdvisoryPolicyStatus.BLOCKED_POLICY_INVALID,
            profile=profile, policy_fingerprint=profile.policy_fingerprint,
            block_reasons=("balanced_weights_missing",),
            reason_codes=("balanced_weights_missing",),
        )
    strategy_bindings = {
        (candidate.strategy.strategy_id, candidate.strategy.strategy_version)
        for candidate in runtime_input.candidate_prerequisites
        if candidate.strategy.strategy_id and candidate.strategy.strategy_version
    }
    complete_strategy_binding = all(
        candidate.strategy.strategy_id and candidate.strategy.strategy_version
        for candidate in runtime_input.candidate_prerequisites
    )
    if complete_strategy_binding and strategy_bindings != {
        (profile.strategy_id, profile.strategy_version)
    }:
        return _empty_result(
            runtime_input=runtime_input, provenance=provenance,
            status=AdvisoryPolicyStatus.BLOCKED_POLICY_INVALID,
            profile=profile, policy_fingerprint=profile.policy_fingerprint,
            block_reasons=("strategy_profile_binding_mismatch",),
            reason_codes=("strategy_profile_binding_mismatch",),
        )
    if profile.objective is AdvisoryObjective.MAXIMIZE_PRIORITY_REWARD and not (
        profile.reward_priority_ids or profile.reward_priority_families
    ):
        return _empty_result(
            runtime_input=runtime_input, provenance=provenance,
            status=AdvisoryPolicyStatus.BLOCKED_POLICY_INVALID,
            profile=profile, policy_fingerprint=profile.policy_fingerprint,
            block_reasons=("reward_priority_missing",),
            reason_codes=("reward_priority_missing",),
        )

    candidates = list(enumerate(runtime_input.candidate_prerequisites))
    if profile.objective is AdvisoryObjective.FIXED_TASK:
        if any(
            candidate.task_identity.status is not PrerequisiteFactStatus.KNOWN
            or not candidate.task_identity.task_semantic_id
            for _index, candidate in candidates
        ):
            return _empty_result(
                runtime_input=runtime_input,
                provenance=provenance,
                status=AdvisoryPolicyStatus.BLOCKED_MISSING_FACTS,
                profile=profile,
                policy_fingerprint=profile.policy_fingerprint,
                missing_facts=("task_identity_unknown",),
                block_reasons=("task_identity_unknown",),
                reason_codes=("task_identity_unknown",),
            )
        preferred = set(profile.preferred_task_ids)
        matches = [
            pair for pair in candidates
            if pair[1].task_identity.task_semantic_id in preferred
        ]
        if not matches:
            return _empty_result(
                runtime_input=runtime_input, provenance=provenance,
                status=AdvisoryPolicyStatus.BLOCKED_TARGET_NOT_FOUND,
                profile=profile, policy_fingerprint=profile.policy_fingerprint,
                block_reasons=("fixed_task_not_found",),
                reason_codes=("fixed_task_not_found",),
            )
        identities = [pair[1].task_identity.task_semantic_id for pair in matches]
        if len(identities) != len(set(identities)):
            return _empty_result(
                runtime_input=runtime_input, provenance=provenance,
                status=AdvisoryPolicyStatus.BLOCKED_TARGET_AMBIGUOUS,
                profile=profile, policy_fingerprint=profile.policy_fingerprint,
                block_reasons=("fixed_task_ambiguous",),
                reason_codes=("fixed_task_ambiguous",),
            )
        candidates = matches

    candidates = [
        pair for pair in candidates
        if pair[1].task_identity.task_semantic_id not in profile.excluded_task_ids
    ]
    filtered_reasons: list[str] = []
    eligible_candidates: list[tuple[int, ActionSummaryPolicyPrerequisiteModel]] = []
    for pair in candidates:
        reason = _candidate_ineligibility(pair[1])
        if reason is None:
            eligible_candidates.append(pair)
        else:
            filtered_reasons.append(reason)
    candidates = eligible_candidates
    if not candidates:
        return _empty_result(
            runtime_input=runtime_input,
            provenance=provenance,
            status=AdvisoryPolicyStatus.NO_ELIGIBLE_TASK,
            profile=profile,
            policy_fingerprint=profile.policy_fingerprint,
            block_reasons=filtered_reasons or ("no_eligible_task",),
            reason_codes=filtered_reasons or ("no_eligible_task",),
        )
    all_missing: list[str] = []
    for _index, candidate in candidates:
        all_missing.extend(_candidate_missing(candidate, profile))
    if all_missing:
        return _empty_result(
            runtime_input=runtime_input,
            provenance=provenance,
            status=AdvisoryPolicyStatus.BLOCKED_MISSING_FACTS,
            profile=profile,
            policy_fingerprint=profile.policy_fingerprint,
            missing_facts=all_missing,
            block_reasons=all_missing,
            reason_codes=all_missing,
        )
    if profile.objective is AdvisoryObjective.MINIMIZE_RESOURCE_COST:
        resource_ids = {
            candidate.resource_balance.resource_id
            for _index, candidate in candidates
        }
        if len(resource_ids) > 1:
            return _empty_result(
                runtime_input=runtime_input, provenance=provenance,
                status=AdvisoryPolicyStatus.BLOCKED_POLICY_INVALID,
                profile=profile, policy_fingerprint=profile.policy_fingerprint,
                block_reasons=("resource_conversion_rule_missing",),
                reason_codes=("resource_conversion_rule_missing",),
            )

    eligible: list[tuple[int, ActionSummaryPolicyPrerequisiteModel, int]] = []
    for index, candidate in candidates:
        attempts = candidate.remaining_attempts.remaining_attempts
        cost = candidate.resource_balance.unit_cost
        assert attempts is not None and cost is not None
        if attempts == 0 or attempts < profile.minimum_remaining_attempts:
            filtered_reasons.append("candidate_attempts_insufficient")
            continue
        if cost > profile.maximum_cost_per_run:
            filtered_reasons.append("candidate_cost_exceeds_policy")
            continue
        run_count = _run_count(candidate, profile)
        if run_count is None:
            return _empty_result(
                runtime_input=runtime_input, provenance=provenance,
                status=AdvisoryPolicyStatus.BLOCKED_MISSING_FACTS,
                profile=profile, policy_fingerprint=profile.policy_fingerprint,
                missing_facts=("recommended_run_count_limit_unknown",),
                block_reasons=("recommended_run_count_limit_unknown",),
                reason_codes=("recommended_run_count_limit_unknown",),
            )
        if run_count <= 0:
            filtered_reasons.append("candidate_budget_insufficient")
            continue
        eligible.append((index, candidate, run_count))
    if not eligible:
        return _empty_result(
            runtime_input=runtime_input,
            provenance=provenance,
            status=AdvisoryPolicyStatus.NO_ELIGIBLE_TASK,
            profile=profile,
            policy_fingerprint=profile.policy_fingerprint,
            block_reasons=filtered_reasons or ("no_eligible_task",),
            reason_codes=filtered_reasons or ("no_eligible_task",),
        )

    eligible.sort(key=lambda item: _sort_key(item[1], item[0], profile))
    ranked: list[AdvisoryRankedCandidate] = []
    evidence_ids: list[str] = []
    advisory_warnings: list[str] = []
    for rank, (page_order, candidate, run_count) in enumerate(eligible, start=1):
        identity = candidate.task_identity
        evidence_ids.extend(identity.evidence_ids)
        if candidate.reward_target.status is not PrerequisiteFactStatus.KNOWN:
            advisory_warnings.append("reward_target_unknown")
        ranked.append(AdvisoryRankedCandidate(
            rank=rank,
            card_match_key=candidate.candidate_card_match_key or "",
            known_task_id=identity.task_semantic_id or "",
            recommended_run_count=run_count,
            remaining_attempts=candidate.remaining_attempts.remaining_attempts or 0,
            resource_id=candidate.resource_balance.resource_id or "",
            unit_cost=candidate.resource_balance.unit_cost or 0,
            reward_target_id=candidate.reward_target.reward_target_id,
            page_order=page_order,
            reason_codes=(f"ranked_for_{profile.objective.value.lower()}",),
            evidence_ids=identity.evidence_ids,
        ))
    selected = ranked[0]
    return ActionSummaryAdvisoryPolicyResult(
        schema_version=_SCHEMA_VERSION,
        status=AdvisoryPolicyStatus.ADVISORY_RECOMMENDATION_READY,
        ready=True,
        terminal=True,
        activity_family=profile.activity_family,
        objective=profile.objective.value,
        strategy_id=profile.strategy_id,
        strategy_version=profile.strategy_version,
        policy_fingerprint=profile.policy_fingerprint,
        runtime_input_fingerprint=input_fingerprint,
        runtime_input_provenance=provenance,
        source_candidate_count=runtime_input.source_policy_candidate_count,
        candidate_count=len(ranked),
        ranked_candidates=tuple(ranked),
        recommended_card_match_key=selected.card_match_key,
        recommended_known_task_id=selected.known_task_id,
        recommended_run_count=selected.recommended_run_count,
        missing_facts=(),
        block_reason_codes=(),
        warnings=_deduplicate(advisory_warnings),
        reason_codes=("advisory_recommendation_ready",),
        evidence_ids=_deduplicate(evidence_ids),
        fact_acquisition_requests=_acquisition_requests(advisory_warnings),
        execution_authorized=False,
        authorization_issued=False,
        business_dispatches=0,
        irreversible_actions=0,
    )


__all__ = [
    "ActionSummaryAdvisoryPolicyProfile",
    "ActionSummaryAdvisoryPolicyResult",
    "AdvisoryObjective",
    "AdvisoryPolicyStatus",
    "AdvisoryRankedCandidate",
    "FactAcquisitionRequest",
    "RuntimeInputProvenanceBinding",
    "action_summary_advisory_policy_fingerprint",
    "create_action_summary_advisory_policy_profile",
    "evaluate_action_summary_policy_advisory",
    "parse_action_summary_advisory_policy_profile",
    "runtime_input_fingerprint",
]
