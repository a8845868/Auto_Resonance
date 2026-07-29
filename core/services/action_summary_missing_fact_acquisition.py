"""Read-only acquisition contracts for missing Action Summary policy facts.

The objects in this module describe where missing facts may come from and can
normalize facts that were already present in a config snapshot or an already
captured Action Summary frame.  They cannot capture, navigate, dispatch input,
persist state, evaluate business policy, or issue authorization.
"""

from __future__ import annotations

import hashlib
import importlib
import json
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from enum import Enum
from typing import Mapping, Sequence

from core.services.action_summary_advisory_policy import (
    AdvisoryObjective,
    FactAcquisitionRequest,
)


class FactSourceLevel(str, Enum):
    LEVEL_0_EXISTING_CONFIG = "LEVEL_0_EXISTING_CONFIG"
    LEVEL_1_CURRENT_PAGE_READ_ONLY = "LEVEL_1_CURRENT_PAGE_READ_ONLY"
    LEVEL_2_PROVEN_NAVIGATION_READ_ONLY = "LEVEL_2_PROVEN_NAVIGATION_READ_ONLY"
    LEVEL_3_REVERSIBLE_DETAIL_OBSERVATION = (
        "LEVEL_3_REVERSIBLE_DETAIL_OBSERVATION"
    )
    LEVEL_4_UNAVAILABLE = "LEVEL_4_UNAVAILABLE"


class AcquisitionPlanStatus(str, Enum):
    PASS = "PASS"
    BLOCKED_CONTRACT_INVALID = "BLOCKED_CONTRACT_INVALID"


class ObserverImplementationStatus(str, Enum):
    NOT_IMPLEMENTED = "NOT_IMPLEMENTED"
    IMPLEMENTED = "IMPLEMENTED"
    OFFLINE_PROVEN = "OFFLINE_PROVEN"
    LIVE_PROVEN = "LIVE_PROVEN"


class FactSubjectScope(str, Enum):
    CONFIG = "CONFIG"
    PAGE = "PAGE"
    ACTIVITY = "ACTIVITY"
    TASK_CARD = "TASK_CARD"
    RESOURCE = "RESOURCE"
    ACCOUNT = "ACCOUNT"
    UNKNOWN = "UNKNOWN"


class FactCardinality(str, Enum):
    SINGLETON = "SINGLETON"
    PER_TASK_CARD = "PER_TASK_CARD"
    PER_RESOURCE = "PER_RESOURCE"
    SET = "SET"


@dataclass(frozen=True, slots=True)
class FactAcquisitionContract:
    fact_id: str
    fact_scope: str
    preferred_source: str
    source_level: FactSourceLevel
    required_page: str | None
    required_capability: str | None
    required_navigation_edges: tuple[str, ...]
    requires_capture: bool
    requires_page_input: bool
    requires_business_input: bool
    irreversible: bool
    maximum_dispatches: int
    freshness_seconds: int
    observer_id: str | None
    observer_status: ObserverImplementationStatus
    normalizer_only: bool
    observer_callable_id: str | None
    output_schema: str
    confidence_requirement: str
    provenance_requirements: tuple[str, ...]
    failure_reason: str
    enabled: bool
    subject_scope: FactSubjectScope
    subject_key_source: str
    cardinality: FactCardinality

    def to_dict(self) -> dict[str, object]:
        document = asdict(self)
        document["source_level"] = self.source_level.value
        document["observer_status"] = self.observer_status.value
        document["subject_scope"] = self.subject_scope.value
        document["cardinality"] = self.cardinality.value
        document["required_navigation_edges"] = list(
            self.required_navigation_edges
        )
        document["provenance_requirements"] = list(
            self.provenance_requirements
        )
        return document


@dataclass(frozen=True, slots=True)
class ActionSummaryAcquisitionPolicyConfig:
    schema_version: str
    config_id: str
    config_version: str
    config_fingerprint: str
    captured_at: str
    valid_until: str
    activity_family: str
    objective: AdvisoryObjective
    strategy_id: str
    strategy_version: str
    strategy_source: str
    reason: str
    preferred_task_ids: tuple[str, ...]
    reward_priority_ids: tuple[str, ...]
    maximum_cost_per_run: int
    maximum_total_cost: int
    minimum_resource_reserve: int
    maximum_task_runs: int
    fatigue_reserve: int
    available_fatigue: int | None
    fatigue_unit_id: str | None
    fatigue_cost_per_run: int | None
    fatigue_cost_applicable: bool | None
    resource_cost_per_run: int | None

    def to_dict(self) -> dict[str, object]:
        document = asdict(self)
        document["objective"] = self.objective.value
        document["preferred_task_ids"] = list(self.preferred_task_ids)
        document["reward_priority_ids"] = list(self.reward_priority_ids)
        return document


@dataclass(frozen=True, slots=True)
class CurrentActionSummaryVisualSnapshot:
    capture_id: str
    frame_sha256: str
    captured_at: str
    valid_until: str
    page_state: str
    card_match_key: str | None = None
    resource_icon_id: str | None = None
    resource_name: str | None = None
    resource_available_amount: int | None = None
    reward_icon_id: str | None = None
    reward_name: str | None = None
    displayed_resource_cost: int | None = None
    evidence_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class AcquiredFact:
    schema_version: str
    output_schema: str
    fact_id: str
    source_level: FactSourceLevel
    observer_id: str
    value_json: str
    observed_at: str
    valid_until: str
    source_fingerprint: str
    policy_fingerprint: str | None
    runtime_input_fingerprint: str | None
    provenance_ids: tuple[str, ...]
    confidence: str
    subject_scope: FactSubjectScope
    subject_key: str
    fact_instance_key: str

    def value(self) -> object:
        return json.loads(self.value_json)

    def to_dict(self) -> dict[str, object]:
        document = asdict(self)
        document["source_level"] = self.source_level.value
        document["subject_scope"] = self.subject_scope.value
        document.pop("value_json", None)
        document["value"] = self.value()
        document["provenance_ids"] = list(self.provenance_ids)
        return document

    @property
    def observation_id(self) -> str:
        document = asdict(self)
        document["source_level"] = self.source_level.value
        document["subject_scope"] = self.subject_scope.value
        document["provenance_ids"] = list(self.provenance_ids)
        payload = json.dumps(
            document,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True, slots=True)
class RejectedObservation:
    fact_id: str
    observation_id: str
    reason: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class FactInstanceCoverage:
    fact_id: str
    subject_scope: FactSubjectScope
    expected_subject_keys: tuple[str, ...]
    resolved_subject_keys: tuple[str, ...]
    unresolved_subject_keys: tuple[str, ...]
    conflicting_subject_keys: tuple[str, ...]
    complete: bool

    def to_dict(self) -> dict[str, object]:
        document = asdict(self)
        document["subject_scope"] = self.subject_scope.value
        for field in (
            "expected_subject_keys",
            "resolved_subject_keys",
            "unresolved_subject_keys",
            "conflicting_subject_keys",
        ):
            document[field] = list(document[field])
        return document


@dataclass(frozen=True, slots=True)
class MissingFactAcquisitionPlan:
    schema_version: str
    generated_at: str
    status: AcquisitionPlanStatus
    policy_fingerprint: str
    runtime_input_fingerprint: str
    requests: tuple[FactAcquisitionContract, ...]
    ready_level_0: tuple[str, ...]
    ready_level_1: tuple[str, ...]
    requires_level_2: tuple[str, ...]
    requires_level_3: tuple[str, ...]
    unavailable: tuple[str, ...]
    zero_input_collectable_facts: tuple[str, ...]
    zero_input_contract_facts: tuple[str, ...]
    zero_input_executable_facts: tuple[str, ...]
    zero_input_normalizer_only_facts: tuple[str, ...]
    navigation_only_collectable_facts: tuple[str, ...]
    page_input_required_facts: tuple[str, ...]
    unavailable_facts: tuple[str, ...]
    resolved_facts: tuple[AcquiredFact, ...]
    unresolved_facts: tuple[str, ...]
    contract_registered_facts: tuple[str, ...]
    observer_implemented_facts: tuple[str, ...]
    observer_offline_proven_facts: tuple[str, ...]
    observer_live_proven_facts: tuple[str, ...]
    normalizer_only_facts: tuple[str, ...]
    not_implemented_facts: tuple[str, ...]
    conflicting_facts: tuple[str, ...]
    resolved_fact_instances: tuple[AcquiredFact, ...]
    unresolved_fact_instances: tuple[str, ...]
    conflicting_fact_instances: tuple[str, ...]
    fact_instance_coverage: tuple[FactInstanceCoverage, ...]
    rejected_observations: tuple[RejectedObservation, ...]
    deduplicated_observations: tuple[str, ...]
    accepted_observation_ids: tuple[str, ...]
    recommended_next_observers: tuple[str, ...]
    blocked_reasons: tuple[str, ...]
    policy_evaluation_still_blocked: bool
    execution_authorized: bool = False
    authorization_issued: bool = False
    capture_calls: int = 0
    page_input_dispatches: int = 0
    business_dispatches: int = 0
    irreversible_actions: int = 0

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "generated_at": self.generated_at,
            "status": self.status.value,
            "policy_fingerprint": self.policy_fingerprint,
            "runtime_input_fingerprint": self.runtime_input_fingerprint,
            "requests": [request.to_dict() for request in self.requests],
            "ready_level_0": list(self.ready_level_0),
            "ready_level_1": list(self.ready_level_1),
            "requires_level_2": list(self.requires_level_2),
            "requires_level_3": list(self.requires_level_3),
            "unavailable": list(self.unavailable),
            "zero_input_collectable_facts": list(
                self.zero_input_collectable_facts
            ),
            "zero_input_collectable_facts_compatibility": (
                "contract_registered_zero_input_candidates"
            ),
            "zero_input_contract_facts": list(self.zero_input_contract_facts),
            "zero_input_executable_facts": list(
                self.zero_input_executable_facts
            ),
            "zero_input_normalizer_only_facts": list(
                self.zero_input_normalizer_only_facts
            ),
            "navigation_only_collectable_facts": list(
                self.navigation_only_collectable_facts
            ),
            "page_input_required_facts": list(
                self.page_input_required_facts
            ),
            "unavailable_facts": list(self.unavailable_facts),
            "resolved_facts": [fact.to_dict() for fact in self.resolved_facts],
            "unresolved_facts": list(self.unresolved_facts),
            "contract_registered_facts": list(self.contract_registered_facts),
            "observer_implemented_facts": list(self.observer_implemented_facts),
            "observer_offline_proven_facts": list(
                self.observer_offline_proven_facts
            ),
            "observer_live_proven_facts": list(self.observer_live_proven_facts),
            "normalizer_only_facts": list(self.normalizer_only_facts),
            "not_implemented_facts": list(self.not_implemented_facts),
            "conflicting_facts": list(self.conflicting_facts),
            "resolved_fact_instances": [
                fact.to_dict() for fact in self.resolved_fact_instances
            ],
            "unresolved_fact_instances": list(
                self.unresolved_fact_instances
            ),
            "conflicting_fact_instances": list(
                self.conflicting_fact_instances
            ),
            "fact_instance_coverage": [
                coverage.to_dict() for coverage in self.fact_instance_coverage
            ],
            "rejected_observations": [
                item.to_dict() for item in self.rejected_observations
            ],
            "deduplicated_observations": list(
                self.deduplicated_observations
            ),
            "accepted_observation_ids": list(self.accepted_observation_ids),
            "recommended_next_observers": list(self.recommended_next_observers),
            "blocked_reasons": list(self.blocked_reasons),
            "policy_evaluation_still_blocked": (
                self.policy_evaluation_still_blocked
            ),
            "execution_authorized": self.execution_authorized,
            "authorization_issued": self.authorization_issued,
            "capture_calls": self.capture_calls,
            "page_input_dispatches": self.page_input_dispatches,
            "business_dispatches": self.business_dispatches,
            "irreversible_actions": self.irreversible_actions,
        }


_SCHEMA_VERSION = "1.0"
_MODEL_SCOPE = "ACTION_SUMMARY_MISSING_FACT_ACQUISITION_PLAN_V1"
_HASH_FIELDS = ("config_fingerprint", "frame_sha256")


def _contract(
    fact_id: str,
    fact_scope: str,
    preferred_source: str,
    source_level: FactSourceLevel,
    *,
    required_page: str | None = None,
    required_capability: str | None = None,
    required_navigation_edges: Sequence[str] = (),
    requires_capture: bool = False,
    requires_page_input: bool = False,
    maximum_dispatches: int = 0,
    freshness_seconds: int = 300,
    observer_id: str | None = None,
    observer_status: ObserverImplementationStatus = (
        ObserverImplementationStatus.NOT_IMPLEMENTED
    ),
    normalizer_only: bool = False,
    observer_callable_id: str | None = None,
    output_schema: str = "ACTION_SUMMARY_ACQUIRED_FACT_V1",
    confidence_requirement: str = "EXPLICIT",
    provenance_requirements: Sequence[str] = (),
    failure_reason: str,
    enabled: bool = True,
    subject_scope: FactSubjectScope = FactSubjectScope.UNKNOWN,
    subject_key_source: str = "unresolved",
    cardinality: FactCardinality = FactCardinality.SINGLETON,
) -> FactAcquisitionContract:
    return FactAcquisitionContract(
        fact_id=fact_id,
        fact_scope=fact_scope,
        preferred_source=preferred_source,
        source_level=source_level,
        required_page=required_page,
        required_capability=required_capability,
        required_navigation_edges=tuple(required_navigation_edges),
        requires_capture=requires_capture,
        requires_page_input=requires_page_input,
        requires_business_input=False,
        irreversible=False,
        maximum_dispatches=maximum_dispatches,
        freshness_seconds=freshness_seconds,
        observer_id=observer_id,
        observer_status=observer_status,
        normalizer_only=normalizer_only,
        observer_callable_id=observer_callable_id,
        output_schema=output_schema,
        confidence_requirement=confidence_requirement,
        provenance_requirements=tuple(provenance_requirements),
        failure_reason=failure_reason,
        enabled=enabled,
        subject_scope=subject_scope,
        subject_key_source=subject_key_source,
        cardinality=cardinality,
    )


FACT_ACQUISITION_CONTRACTS: dict[str, FactAcquisitionContract] = {
    "objective_missing": _contract(
        "objective_missing", "USER_POLICY", "explicit_user_policy_config",
        FactSourceLevel.LEVEL_0_EXISTING_CONFIG,
        observer_id="action_summary_policy_config_observer",
        observer_status=ObserverImplementationStatus.OFFLINE_PROVEN,
        observer_callable_id=(
            "core.services.action_summary_missing_fact_acquisition."
            "observe_action_summary_policy_config"
        ),
        freshness_seconds=3600,
        provenance_requirements=("config_id", "config_version", "config_fingerprint"),
        failure_reason="objective must remain UNKNOWN until explicitly configured",
        subject_scope=FactSubjectScope.CONFIG,
        subject_key_source="config_fingerprint",
        cardinality=FactCardinality.SINGLETON,
    ),
    "strategy_identity_missing": _contract(
        "strategy_identity_missing", "USER_POLICY", "versioned_strategy_config",
        FactSourceLevel.LEVEL_0_EXISTING_CONFIG,
        observer_id="action_summary_strategy_config_observer",
        observer_status=ObserverImplementationStatus.OFFLINE_PROVEN,
        observer_callable_id=(
            "core.services.action_summary_missing_fact_acquisition."
            "observe_action_summary_policy_config"
        ),
        freshness_seconds=3600,
        provenance_requirements=("strategy_id", "strategy_version", "config_fingerprint"),
        failure_reason="strategy identity cannot be inferred from legacy task lists",
        subject_scope=FactSubjectScope.CONFIG,
        subject_key_source="config_fingerprint",
        cardinality=FactCardinality.SINGLETON,
    ),
    "strategy_provenance_missing": _contract(
        "strategy_provenance_missing", "USER_POLICY", "versioned_strategy_config",
        FactSourceLevel.LEVEL_0_EXISTING_CONFIG,
        observer_id="action_summary_strategy_config_observer",
        observer_status=ObserverImplementationStatus.OFFLINE_PROVEN,
        observer_callable_id=(
            "core.services.action_summary_missing_fact_acquisition."
            "observe_action_summary_policy_config"
        ),
        freshness_seconds=3600,
        provenance_requirements=("strategy_source", "config_fingerprint"),
        failure_reason="strategy provenance must be explicit and fingerprint-bound",
        subject_scope=FactSubjectScope.CONFIG,
        subject_key_source="config_fingerprint",
        cardinality=FactCardinality.SINGLETON,
    ),
    "task_identity_unknown": _contract(
        "task_identity_unknown", "TASK_CARD", "current_page_read_only_model",
        FactSourceLevel.LEVEL_1_CURRENT_PAGE_READ_ONLY,
        required_page="ACTION_SUMMARY_VISIBLE",
        requires_capture=True,
        observer_id="action_summary_task_card_identity_observer",
        provenance_requirements=("capture_id", "frame_sha256", "card_match_key"),
        failure_reason="a unique card-scoped semantic identity is required",
        subject_scope=FactSubjectScope.TASK_CARD,
        subject_key_source="card_match_key",
        cardinality=FactCardinality.PER_TASK_CARD,
    ),
    "remaining_attempts_unknown": _contract(
        "remaining_attempts_unknown", "TASK_CARD", "task_detail_read_only_observer",
        FactSourceLevel.LEVEL_3_REVERSIBLE_DETAIL_OBSERVATION,
        required_page="ACTION_SUMMARY_VISIBLE",
        required_capability="OPEN_TASK_DETAIL",
        requires_capture=True,
        requires_page_input=True,
        maximum_dispatches=1,
        observer_id=None,
        provenance_requirements=("card_match_key", "detail_capture_id", "frame_sha256"),
        failure_reason="page-level attempts cannot be copied to a task card",
        enabled=False,
        subject_scope=FactSubjectScope.TASK_CARD,
        subject_key_source="card_match_key",
        cardinality=FactCardinality.PER_TASK_CARD,
    ),
    "resource_identity_unknown": _contract(
        "resource_identity_unknown", "RESOURCE", "current_page_icon_name_contract",
        FactSourceLevel.LEVEL_1_CURRENT_PAGE_READ_ONLY,
        required_page="ACTION_SUMMARY_VISIBLE",
        requires_capture=True,
        observer_id="action_summary_current_page_visual_observer",
        observer_status=ObserverImplementationStatus.OFFLINE_PROVEN,
        normalizer_only=True,
        observer_callable_id=(
            "core.services.action_summary_missing_fact_acquisition."
            "observe_current_action_summary_page_facts"
        ),
        provenance_requirements=("capture_id", "frame_sha256", "icon_id", "resource_name"),
        failure_reason="resource identity requires a stable icon and name contract",
        subject_scope=FactSubjectScope.RESOURCE,
        subject_key_source="resource_identity",
        cardinality=FactCardinality.PER_RESOURCE,
    ),
    "resource_balance_unknown": _contract(
        "resource_balance_unknown", "RESOURCE", "current_page_resource_balance",
        FactSourceLevel.LEVEL_1_CURRENT_PAGE_READ_ONLY,
        required_page="ACTION_SUMMARY_VISIBLE",
        requires_capture=True,
        observer_id="action_summary_current_page_visual_observer",
        observer_status=ObserverImplementationStatus.OFFLINE_PROVEN,
        normalizer_only=True,
        observer_callable_id=(
            "core.services.action_summary_missing_fact_acquisition."
            "observe_current_action_summary_page_facts"
        ),
        provenance_requirements=("capture_id", "frame_sha256", "resource_identity"),
        failure_reason="absence of an insufficiency warning is not a balance",
        subject_scope=FactSubjectScope.RESOURCE,
        subject_key_source="resource_identity",
        cardinality=FactCardinality.PER_RESOURCE,
    ),
    "runtime_resource_observation_missing": _contract(
        "runtime_resource_observation_missing", "RESOURCE", "existing_runtime_resource_snapshot",
        FactSourceLevel.LEVEL_0_EXISTING_CONFIG,
        observer_id="action_summary_runtime_resource_snapshot_observer",
        provenance_requirements=("capture_id", "frame_sha256", "valid_until"),
        failure_reason="a fresh runtime resource snapshot is required",
        subject_scope=FactSubjectScope.RESOURCE,
        subject_key_source="resource_identity",
        cardinality=FactCardinality.PER_RESOURCE,
    ),
    "reward_target_unknown": _contract(
        "reward_target_unknown", "TASK_REWARD", "current_page_task_bound_reward",
        FactSourceLevel.LEVEL_1_CURRENT_PAGE_READ_ONLY,
        required_page="ACTION_SUMMARY_VISIBLE",
        requires_capture=True,
        observer_id="action_summary_current_page_visual_observer",
        observer_status=ObserverImplementationStatus.OFFLINE_PROVEN,
        normalizer_only=True,
        observer_callable_id=(
            "core.services.action_summary_missing_fact_acquisition."
            "observe_current_action_summary_page_facts"
        ),
        provenance_requirements=("capture_id", "frame_sha256", "card_match_key", "reward_identity"),
        failure_reason="a catalog reward is not a live task-bound reward fact",
        subject_scope=FactSubjectScope.TASK_CARD,
        subject_key_source="card_match_key",
        cardinality=FactCardinality.PER_TASK_CARD,
    ),
    "fatigue_budget_unknown": _contract(
        "fatigue_budget_unknown", "FATIGUE", "explicit_fatigue_config_or_state",
        FactSourceLevel.LEVEL_0_EXISTING_CONFIG,
        observer_id="action_summary_fatigue_config_observer",
        observer_status=ObserverImplementationStatus.OFFLINE_PROVEN,
        observer_callable_id=(
            "core.services.action_summary_missing_fact_acquisition."
            "observe_action_summary_fatigue_config"
        ),
        freshness_seconds=3600,
        provenance_requirements=("config_fingerprint", "fatigue_unit_id", "available_fatigue"),
        failure_reason="fatigue budget must come from explicit config or state",
        subject_scope=FactSubjectScope.CONFIG,
        subject_key_source="config_fingerprint",
        cardinality=FactCardinality.SINGLETON,
    ),
    "fatigue_cost_unknown": _contract(
        "fatigue_cost_unknown", "TASK_COST", "explicit_task_fatigue_config",
        FactSourceLevel.LEVEL_0_EXISTING_CONFIG,
        observer_id="action_summary_fatigue_config_observer",
        observer_status=ObserverImplementationStatus.OFFLINE_PROVEN,
        observer_callable_id=(
            "core.services.action_summary_missing_fact_acquisition."
            "observe_action_summary_fatigue_config"
        ),
        freshness_seconds=3600,
        provenance_requirements=("config_fingerprint", "fatigue_unit_id", "fatigue_cost_per_run"),
        failure_reason="fatigue cost must be independent from resource cost",
        subject_scope=FactSubjectScope.CONFIG,
        subject_key_source="config_fingerprint",
        cardinality=FactCardinality.SINGLETON,
    ),
    "resource_cost_unknown": _contract(
        "resource_cost_unknown", "TASK_COST", "current_page_resource_cost",
        FactSourceLevel.LEVEL_1_CURRENT_PAGE_READ_ONLY,
        required_page="ACTION_SUMMARY_VISIBLE",
        requires_capture=True,
        observer_id="action_summary_current_page_raw_frame_observer",
        observer_status=ObserverImplementationStatus.OFFLINE_PROVEN,
        normalizer_only=False,
        observer_callable_id=(
            "core.services.action_summary_raw_frame_observer."
            "observe_action_summary_current_page_visuals"
        ),
        provenance_requirements=(
            "capture_id", "frame_sha256", "card_match_key"
        ),
        failure_reason=(
            "resource cost requires an exact non-negative value bound to one "
            "fresh task card; resource identity may remain UNKNOWN"
        ),
        subject_scope=FactSubjectScope.TASK_CARD,
        subject_key_source="card_match_key",
        cardinality=FactCardinality.PER_TASK_CARD,
    ),
    "recommended_run_count_limit_unknown": _contract(
        "recommended_run_count_limit_unknown", "USER_POLICY", "explicit_user_policy_config",
        FactSourceLevel.LEVEL_0_EXISTING_CONFIG,
        observer_id="action_summary_policy_config_observer",
        provenance_requirements=("config_fingerprint", "maximum_task_runs"),
        failure_reason="run count limit must be explicit",
        subject_scope=FactSubjectScope.CONFIG,
        subject_key_source="config_fingerprint",
        cardinality=FactCardinality.SINGLETON,
    ),
}


def _aware(value: str, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{field}_invalid") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field}_timezone_missing")
    return parsed


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field}_missing")
    return value.strip()


def _integer(value: object, field: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise ValueError(f"{field}_invalid")
    return value


def _optional_integer(value: object, field: str) -> int | None:
    if value is None:
        return None
    return _integer(value, field)


def _optional_text(value: object, field: str) -> str | None:
    if value is None:
        return None
    return _text(value, field)


def _string_tuple(value: object, field: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if (
        not isinstance(value, Sequence)
        or isinstance(value, (str, bytes))
        or not all(isinstance(item, str) and item.strip() for item in value)
    ):
        raise ValueError(f"{field}_invalid")
    result = tuple(item.strip() for item in value)
    if len(set(result)) != len(result):
        raise ValueError(f"{field}_duplicate")
    return result


def _is_hash(value: object) -> bool:
    return bool(
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdefABCDEF" for character in value)
    )


def _canonical_hash(document: Mapping[str, object]) -> str:
    payload = json.dumps(
        document, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def parse_action_summary_acquisition_policy_config(
    document: Mapping[str, object],
) -> ActionSummaryAcquisitionPolicyConfig:
    """Parse a read-only config snapshot; omitted policy fields stay conservative."""

    if not isinstance(document, Mapping):
        raise ValueError("acquisition_policy_document_invalid")
    raw = document.get("ActionSummaryAcquisitionPolicy", document)
    if not isinstance(raw, Mapping):
        raise ValueError("acquisition_policy_section_invalid")
    if raw.get("schema_version", _SCHEMA_VERSION) != _SCHEMA_VERSION:
        raise ValueError("acquisition_policy_schema_unsupported")
    captured_at = _text(raw.get("captured_at"), "captured_at")
    valid_until = _text(raw.get("valid_until"), "valid_until")
    if _aware(valid_until, "valid_until") < _aware(captured_at, "captured_at"):
        raise ValueError("acquisition_policy_window_invalid")
    try:
        objective = AdvisoryObjective(raw.get("objective", "OBSERVE_ONLY"))
    except ValueError as error:
        raise ValueError("objective_invalid") from error
    maximum_task_runs = _integer(
        raw.get("maximum_task_runs", 0), "maximum_task_runs"
    )
    if objective is AdvisoryObjective.OBSERVE_ONLY and maximum_task_runs != 0:
        raise ValueError("observe_only_run_limit_invalid")
    fatigue_applicable = raw.get("fatigue_cost_applicable")
    if fatigue_applicable is not None and type(fatigue_applicable) is not bool:
        raise ValueError("fatigue_cost_applicable_invalid")
    normalized = ActionSummaryAcquisitionPolicyConfig(
        schema_version=_SCHEMA_VERSION,
        config_id=_text(raw.get("config_id", "default-observe-only"), "config_id"),
        config_version=_text(raw.get("config_version", "1"), "config_version"),
        config_fingerprint="",
        captured_at=captured_at,
        valid_until=valid_until,
        activity_family=_text(raw.get("activity_family", "SIEGE"), "activity_family"),
        objective=objective,
        strategy_id=_text(raw.get("strategy_id", "observe-only"), "strategy_id"),
        strategy_version=_text(raw.get("strategy_version", "1"), "strategy_version"),
        strategy_source=_text(raw.get("strategy_source", "USER_CONFIGURED_DEFAULT"), "strategy_source"),
        reason=_text(raw.get("reason", "read-only observation; no execution"), "reason"),
        preferred_task_ids=_string_tuple(raw.get("preferred_task_ids", ()), "preferred_task_ids"),
        reward_priority_ids=_string_tuple(raw.get("reward_priority_ids", ()), "reward_priority_ids"),
        maximum_cost_per_run=_integer(raw.get("maximum_cost_per_run", 0), "maximum_cost_per_run"),
        maximum_total_cost=_integer(raw.get("maximum_total_cost", 0), "maximum_total_cost"),
        minimum_resource_reserve=_integer(raw.get("minimum_resource_reserve", 0), "minimum_resource_reserve"),
        maximum_task_runs=maximum_task_runs,
        fatigue_reserve=_integer(raw.get("fatigue_reserve", 0), "fatigue_reserve"),
        available_fatigue=_optional_integer(raw.get("available_fatigue"), "available_fatigue"),
        fatigue_unit_id=(
            _text(raw.get("fatigue_unit_id"), "fatigue_unit_id")
            if raw.get("fatigue_unit_id") is not None
            else None
        ),
        fatigue_cost_per_run=_optional_integer(raw.get("fatigue_cost_per_run"), "fatigue_cost_per_run"),
        fatigue_cost_applicable=fatigue_applicable,
        resource_cost_per_run=_optional_integer(raw.get("resource_cost_per_run"), "resource_cost_per_run"),
    )
    if normalized.fatigue_cost_applicable is True and (
        normalized.fatigue_cost_per_run is None
        or normalized.fatigue_cost_per_run <= 0
    ):
        raise ValueError("fatigue_cost_contract_invalid")
    if normalized.fatigue_cost_applicable is False and (
        normalized.fatigue_cost_per_run not in (None, 0)
    ):
        raise ValueError("fatigue_cost_contract_invalid")
    payload = normalized.to_dict()
    payload.pop("config_fingerprint", None)
    return replace(normalized, config_fingerprint=_canonical_hash(payload))


def fact_instance_key(
    fact_id: str,
    subject_scope: FactSubjectScope,
    subject_key: str,
) -> str:
    normalized_fact_id = _text(fact_id, "fact_id")
    if not isinstance(subject_scope, FactSubjectScope):
        raise ValueError("fact_subject_scope_invalid")
    normalized_subject_key = _text(subject_key, "fact_subject_key")
    return ":".join((
        normalized_fact_id,
        subject_scope.value,
        normalized_subject_key,
    ))


def _fact_subject_key(
    contract: FactAcquisitionContract,
    value: object,
    *,
    policy_fingerprint: str | None,
    source_fingerprint: str,
) -> str:
    mapping = value if isinstance(value, Mapping) else {}
    if contract.subject_scope is FactSubjectScope.CONFIG:
        return _text(policy_fingerprint, "fact_subject_key")
    if contract.subject_scope is FactSubjectScope.PAGE:
        return _text(source_fingerprint, "fact_subject_key")
    if contract.subject_scope is FactSubjectScope.ACTIVITY:
        return _text(mapping.get("activity_family"), "fact_subject_key")
    if contract.subject_scope is FactSubjectScope.TASK_CARD:
        return _text(mapping.get("card_match_key"), "fact_subject_key")
    if contract.subject_scope is FactSubjectScope.RESOURCE:
        resource_id = mapping.get("resource_id")
        if _has_text(resource_id) and resource_id != "UNKNOWN":
            return str(resource_id).strip()
        icon_id = mapping.get("resource_icon_id")
        resource_name = mapping.get("resource_name")
        if _has_text(icon_id) and _has_text(resource_name):
            return f"{str(icon_id).strip()}:{str(resource_name).strip()}"
        raise ValueError("fact_subject_key_missing")
    if contract.subject_scope is FactSubjectScope.ACCOUNT:
        return _text(mapping.get("account_id"), "fact_subject_key")
    raise ValueError("fact_subject_scope_unknown")


def _fact(
    *,
    fact_id: str,
    source_level: FactSourceLevel,
    observer_id: str,
    value: object,
    observed_at: str,
    valid_until: str,
    source_fingerprint: str,
    policy_fingerprint: str | None,
    runtime_input_fingerprint: str | None,
    provenance_ids: Sequence[str],
) -> AcquiredFact:
    contract = FACT_ACQUISITION_CONTRACTS[fact_id]
    subject_key = _fact_subject_key(
        contract,
        value,
        policy_fingerprint=policy_fingerprint,
        source_fingerprint=source_fingerprint,
    )
    return AcquiredFact(
        schema_version=_SCHEMA_VERSION,
        output_schema=contract.output_schema,
        fact_id=fact_id,
        source_level=source_level,
        observer_id=observer_id,
        value_json=json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ),
        observed_at=observed_at,
        valid_until=valid_until,
        source_fingerprint=source_fingerprint,
        policy_fingerprint=policy_fingerprint,
        runtime_input_fingerprint=runtime_input_fingerprint,
        provenance_ids=tuple(provenance_ids),
        confidence="EXPLICIT",
        subject_scope=contract.subject_scope,
        subject_key=subject_key,
        fact_instance_key=fact_instance_key(
            fact_id, contract.subject_scope, subject_key
        ),
    )


def observe_action_summary_policy_config(
    config: ActionSummaryAcquisitionPolicyConfig,
) -> tuple[AcquiredFact, ...]:
    """Resolve only explicit objective and strategy facts from a config snapshot."""

    provenance = (
        f"config_id:{config.config_id}",
        f"config_version:{config.config_version}",
        f"config_fingerprint:{config.config_fingerprint}",
    )
    common = {
        "source_level": FactSourceLevel.LEVEL_0_EXISTING_CONFIG,
        "observed_at": config.captured_at,
        "valid_until": config.valid_until,
        "source_fingerprint": config.config_fingerprint,
        "policy_fingerprint": config.config_fingerprint,
        "runtime_input_fingerprint": None,
        "provenance_ids": provenance,
    }
    return (
        _fact(
            fact_id="objective_missing",
            observer_id="action_summary_policy_config_observer",
            value={"objective": config.objective.value, "reason": config.reason},
            **common,
        ),
        _fact(
            fact_id="strategy_identity_missing",
            observer_id="action_summary_strategy_config_observer",
            value={"strategy_id": config.strategy_id, "strategy_version": config.strategy_version},
            **common,
        ),
        _fact(
            fact_id="strategy_provenance_missing",
            observer_id="action_summary_strategy_config_observer",
            value={"strategy_source": config.strategy_source},
            **common,
        ),
    )


def observe_action_summary_fatigue_config(
    config: ActionSummaryAcquisitionPolicyConfig,
) -> tuple[AcquiredFact, ...]:
    """Resolve fatigue facts only when every independent field is explicit."""

    facts: list[AcquiredFact] = []
    common = {
        "source_level": FactSourceLevel.LEVEL_0_EXISTING_CONFIG,
        "observed_at": config.captured_at,
        "valid_until": config.valid_until,
        "source_fingerprint": config.config_fingerprint,
        "policy_fingerprint": config.config_fingerprint,
        "runtime_input_fingerprint": None,
        "provenance_ids": (
            f"config_id:{config.config_id}",
            f"config_version:{config.config_version}",
            f"config_fingerprint:{config.config_fingerprint}",
        ),
    }
    if config.available_fatigue is not None and config.fatigue_unit_id:
        facts.append(_fact(
            fact_id="fatigue_budget_unknown",
            observer_id="action_summary_fatigue_config_observer",
            value={
                "available_fatigue": config.available_fatigue,
                "fatigue_reserve": config.fatigue_reserve,
                "fatigue_unit_id": config.fatigue_unit_id,
            },
            **common,
        ))
    if (
        config.fatigue_cost_per_run is not None
        and config.fatigue_cost_applicable is not None
        and config.fatigue_unit_id
    ):
        facts.append(_fact(
            fact_id="fatigue_cost_unknown",
            observer_id="action_summary_fatigue_config_observer",
            value={
                "fatigue_cost_per_run": config.fatigue_cost_per_run,
                "fatigue_cost_applicable": config.fatigue_cost_applicable,
                "fatigue_unit_id": config.fatigue_unit_id,
                "resource_cost_per_run": config.resource_cost_per_run,
            },
            **common,
        ))
    return tuple(facts)


def observe_current_action_summary_page_facts(
    snapshot: CurrentActionSummaryVisualSnapshot,
    *,
    runtime_input_fingerprint: str,
) -> tuple[AcquiredFact, ...]:
    """Normalize high-confidence facts from an already captured page snapshot."""

    _text(snapshot.capture_id, "capture_id")
    if not _is_hash(snapshot.frame_sha256):
        raise ValueError("visual_snapshot_fingerprint_invalid")
    captured_at = _aware(snapshot.captured_at, "captured_at")
    valid_until = _aware(snapshot.valid_until, "valid_until")
    if valid_until < captured_at:
        raise ValueError("visual_snapshot_window_invalid")
    if not _is_hash(runtime_input_fingerprint):
        raise ValueError("runtime_input_fingerprint_invalid")
    page_state = _text(snapshot.page_state, "page_state")
    for field in (
        "card_match_key",
        "resource_icon_id",
        "resource_name",
        "reward_icon_id",
        "reward_name",
    ):
        _optional_text(getattr(snapshot, field), field)
    for field in ("resource_available_amount", "displayed_resource_cost"):
        _optional_integer(getattr(snapshot, field), field)
    _string_tuple(snapshot.evidence_ids, "evidence_ids")
    if page_state != "ACTION_SUMMARY_VISIBLE":
        return ()
    facts: list[AcquiredFact] = []
    common = {
        "source_level": FactSourceLevel.LEVEL_1_CURRENT_PAGE_READ_ONLY,
        "observer_id": "action_summary_current_page_visual_observer",
        "observed_at": snapshot.captured_at,
        "valid_until": snapshot.valid_until,
        "source_fingerprint": snapshot.frame_sha256,
        "policy_fingerprint": None,
        "runtime_input_fingerprint": runtime_input_fingerprint,
        "provenance_ids": tuple(
            value
            for value in (
                f"capture_id:{snapshot.capture_id}",
                f"frame_sha256:{snapshot.frame_sha256}",
                (
                    f"card_match_key:{snapshot.card_match_key}"
                    if snapshot.card_match_key
                    else None
                ),
                *snapshot.evidence_ids,
            )
            if value
        ),
    }
    resource_identity = bool(snapshot.resource_icon_id and snapshot.resource_name)
    if resource_identity:
        identity = {
            "resource_icon_id": snapshot.resource_icon_id,
            "resource_name": snapshot.resource_name,
        }
        facts.append(_fact(fact_id="resource_identity_unknown", value=identity, **common))
        if snapshot.resource_available_amount is not None:
            facts.append(_fact(
                fact_id="resource_balance_unknown",
                value={**identity, "available_amount": snapshot.resource_available_amount},
                **common,
            ))
    if snapshot.displayed_resource_cost is not None and snapshot.card_match_key:
        facts.append(_fact(
            fact_id="resource_cost_unknown",
            value={
                "card_match_key": snapshot.card_match_key,
                "resource_cost_per_run": snapshot.displayed_resource_cost,
                "resource_id": (
                    snapshot.resource_name if resource_identity else "UNKNOWN"
                ),
            },
            **common,
        ))
    if (
        snapshot.card_match_key
        and snapshot.reward_icon_id
        and snapshot.reward_name
    ):
        facts.append(_fact(
            fact_id="reward_target_unknown",
            value={
                "card_match_key": snapshot.card_match_key,
                "reward_icon_id": snapshot.reward_icon_id,
                "reward_name": snapshot.reward_name,
            },
            **common,
        ))
    return tuple(facts)


def _fact_value_mapping(fact: AcquiredFact) -> Mapping[str, object]:
    try:
        value = fact.value()
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise ValueError("fact_value_invalid") from error
    if not isinstance(value, Mapping):
        raise ValueError("fact_value_invalid")
    return value


def _has_text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _fact_value_valid(fact: AcquiredFact) -> bool:
    try:
        value = _fact_value_mapping(fact)
    except ValueError:
        return False
    integer_fields = {
        "available_amount",
        "resource_cost_per_run",
        "available_fatigue",
        "fatigue_reserve",
        "fatigue_cost_per_run",
        "maximum_task_runs",
    }
    for field in integer_fields.intersection(value):
        if type(value[field]) is not int or value[field] < 0:
            return False
    if "fatigue_cost_applicable" in value and (
        type(value["fatigue_cost_applicable"]) is not bool
    ):
        return False
    return True


def _provenance_complete(
    fact: AcquiredFact,
    contract: FactAcquisitionContract,
) -> bool:
    if not fact.provenance_ids or not all(
        _has_text(item) for item in fact.provenance_ids
    ):
        return False
    try:
        value = _fact_value_mapping(fact)
    except ValueError:
        return False
    provenance = set(fact.provenance_ids)
    for requirement in contract.provenance_requirements:
        if requirement == "config_fingerprint":
            if (
                not fact.policy_fingerprint
                or f"config_fingerprint:{fact.policy_fingerprint}" not in provenance
            ):
                return False
        elif requirement in {"capture_id", "detail_capture_id"}:
            prefix = f"{requirement}:"
            if not any(item.startswith(prefix) and item != prefix for item in provenance):
                return False
        elif requirement == "frame_sha256":
            if f"frame_sha256:{fact.source_fingerprint}" not in provenance:
                return False
        elif requirement == "card_match_key":
            card_key = value.get("card_match_key")
            if (
                not _has_text(card_key)
                or f"card_match_key:{card_key}" not in provenance
            ):
                return False
        elif requirement in {"icon_id", "resource_name", "resource_identity"}:
            if not (
                _has_text(value.get("resource_icon_id"))
                and _has_text(value.get("resource_name"))
            ):
                return False
        elif requirement == "reward_identity":
            if not (
                _has_text(value.get("reward_icon_id"))
                and _has_text(value.get("reward_name"))
            ):
                return False
        elif requirement == "valid_until":
            continue
        elif requirement in {"config_id", "config_version"}:
            prefix = f"{requirement}:"
            if not any(item.startswith(prefix) and item != prefix for item in provenance):
                return False
        elif requirement not in value:
            return False
        elif value[requirement] is None:
            return False
    return True


def validate_acquired_fact(
    fact: AcquiredFact,
    contract: FactAcquisitionContract,
    policy_fingerprint: str,
    runtime_input_fingerprint: str,
    generated_at: str,
) -> str | None:
    """Return an exact rejection reason, or ``None`` for a valid fact."""

    if fact.fact_id != contract.fact_id:
        return "fact_id_mismatch"
    if fact.subject_scope is not contract.subject_scope:
        return "subject_scope_mismatch"
    if not _has_text(fact.subject_key):
        return "subject_key_missing"
    try:
        expected_instance_key = fact_instance_key(
            fact.fact_id, fact.subject_scope, fact.subject_key
        )
    except ValueError:
        return "fact_instance_key_invalid"
    if fact.fact_instance_key != expected_instance_key:
        return "fact_instance_key_mismatch"
    if not contract.enabled:
        return "contract_disabled"
    if fact.schema_version != _SCHEMA_VERSION:
        return "schema_version_mismatch"
    if fact.output_schema != contract.output_schema:
        return "output_schema_mismatch"
    if fact.confidence != contract.confidence_requirement:
        return "confidence_mismatch"
    if fact.source_level is not contract.source_level:
        return "source_level_mismatch"
    if not contract.observer_id or fact.observer_id != contract.observer_id:
        return "observer_mismatch"
    if not _is_hash(fact.source_fingerprint):
        return "source_fingerprint_invalid"
    try:
        observed = _aware(fact.observed_at, "observed_at")
        valid_until = _aware(fact.valid_until, "valid_until")
        generated = _aware(generated_at, "generated_at")
    except ValueError:
        return "observation_window_invalid"
    if valid_until < observed:
        return "observation_window_invalid"
    if observed > generated:
        return "observation_from_future"
    if generated > valid_until:
        return "observation_stale"
    if (generated - observed).total_seconds() > contract.freshness_seconds:
        return "observation_stale"
    if fact.policy_fingerprint is None and fact.runtime_input_fingerprint is None:
        return "provenance_incomplete"
    if fact.policy_fingerprint is not None:
        if fact.policy_fingerprint != policy_fingerprint:
            return "policy_fingerprint_mismatch"
    if fact.runtime_input_fingerprint is not None:
        if fact.runtime_input_fingerprint != runtime_input_fingerprint:
            return "runtime_fingerprint_mismatch"
    if contract.source_level is FactSourceLevel.LEVEL_0_EXISTING_CONFIG:
        if fact.policy_fingerprint != policy_fingerprint:
            return "policy_fingerprint_mismatch"
    elif fact.runtime_input_fingerprint != runtime_input_fingerprint:
        return "runtime_fingerprint_mismatch"
    if not _fact_value_valid(fact):
        return "fact_value_invalid"
    try:
        fact_value = _fact_value_mapping(fact)
    except ValueError:
        return "fact_value_invalid"
    if not _provenance_complete(fact, contract):
        return "provenance_incomplete"
    if contract.subject_scope is FactSubjectScope.TASK_CARD and (
        fact_value.get("card_match_key") != fact.subject_key
    ):
        return "subject_key_value_mismatch"
    if contract.subject_scope is FactSubjectScope.RESOURCE:
        try:
            derived_subject_key = _fact_subject_key(
                contract,
                fact_value,
                policy_fingerprint=fact.policy_fingerprint,
                source_fingerprint=fact.source_fingerprint,
            )
        except ValueError:
            return "subject_key_missing"
        if derived_subject_key != fact.subject_key:
            return "subject_key_value_mismatch"
    return None


_OBSERVER_CALLABLES = {
    (
        "core.services.action_summary_missing_fact_acquisition."
        "observe_action_summary_policy_config"
    ): observe_action_summary_policy_config,
    (
        "core.services.action_summary_missing_fact_acquisition."
        "observe_action_summary_fatigue_config"
    ): observe_action_summary_fatigue_config,
    (
        "core.services.action_summary_missing_fact_acquisition."
        "observe_current_action_summary_page_facts"
    ): observe_current_action_summary_page_facts,
}


def _observer_callable_exists(callable_id: str) -> bool:
    if callable_id in _OBSERVER_CALLABLES:
        return True
    try:
        module_name, attribute_name = callable_id.rsplit(".", 1)
        observer = getattr(importlib.import_module(module_name), attribute_name)
    except (AttributeError, ImportError, ValueError):
        return False
    return callable(observer)


def _validate_contract(contract: FactAcquisitionContract) -> bool:
    if (
        not isinstance(contract.subject_scope, FactSubjectScope)
        or contract.subject_scope is FactSubjectScope.UNKNOWN
        or not _has_text(contract.subject_key_source)
        or not isinstance(contract.cardinality, FactCardinality)
    ):
        return False
    if (
        contract.cardinality is FactCardinality.PER_TASK_CARD
        and contract.subject_scope is not FactSubjectScope.TASK_CARD
    ):
        return False
    if (
        contract.cardinality is FactCardinality.PER_RESOURCE
        and contract.subject_scope is not FactSubjectScope.RESOURCE
    ):
        return False
    if (
        contract.observer_status is ObserverImplementationStatus.NOT_IMPLEMENTED
        and contract.observer_callable_id is not None
    ):
        return False
    if contract.observer_status is not ObserverImplementationStatus.NOT_IMPLEMENTED:
        if not contract.observer_id or not contract.observer_callable_id:
            return False
        if not _observer_callable_exists(contract.observer_callable_id):
            return False
    if contract.enabled and (
        contract.requires_page_input
        or contract.requires_business_input
        or contract.irreversible
        or contract.maximum_dispatches != 0
    ):
        return False
    if contract.source_level is FactSourceLevel.LEVEL_3_REVERSIBLE_DETAIL_OBSERVATION:
        return bool(not contract.enabled and contract.requires_page_input)
    return True


def build_missing_fact_acquisition_plan(
    requests: Sequence[FactAcquisitionRequest],
    *,
    observations: Sequence[AcquiredFact] = (),
    policy_fingerprint: str,
    runtime_input_fingerprint: str,
    generated_at: str,
) -> MissingFactAcquisitionPlan:
    """Classify acquisition requests without invoking any observer or action."""

    if not _is_hash(policy_fingerprint) or not _is_hash(runtime_input_fingerprint):
        raise ValueError("plan_fingerprint_invalid")
    generated = _aware(generated_at, "generated_at")
    ordered_requests = tuple(sorted(requests, key=lambda item: (item.priority, item.missing_fact)))
    contracts: list[FactAcquisitionContract] = []
    invalid: list[str] = []
    for request in ordered_requests:
        contract = FACT_ACQUISITION_CONTRACTS.get(request.missing_fact)
        if contract is None:
            contract = _contract(
                request.missing_fact,
                request.required_scope,
                request.preferred_source,
                FactSourceLevel.LEVEL_4_UNAVAILABLE,
                failure_reason="no read-only acquisition contract is registered",
                enabled=False,
            )
        if not _validate_contract(contract):
            invalid.append(contract.fact_id)
        contracts.append(contract)

    requested_ids = tuple(dict.fromkeys(
        contract.fact_id for contract in contracts
    ))
    request_by_id = {
        request.missing_fact: request for request in ordered_requests
    }
    contract_by_id = {
        contract.fact_id: contract for contract in contracts
    }
    grouped: dict[str, list[AcquiredFact]] = {}
    rejected: list[RejectedObservation] = []
    for observation in observations:
        contract = FACT_ACQUISITION_CONTRACTS.get(observation.fact_id)
        if contract is None or observation.fact_id not in requested_ids:
            continue
        rejection = validate_acquired_fact(
            observation,
            contract,
            policy_fingerprint,
            runtime_input_fingerprint,
            generated_at,
        )
        if rejection is not None:
            rejected.append(RejectedObservation(
                fact_id=observation.fact_id,
                observation_id=observation.observation_id,
                reason=rejection,
            ))
            continue
        request = request_by_id[observation.fact_id]
        expected_subject_keys = tuple(dict.fromkeys(
            key.strip()
            for key in request.target_subject_keys
            if isinstance(key, str) and key.strip()
        ))
        if expected_subject_keys and observation.subject_key not in expected_subject_keys:
            rejected.append(RejectedObservation(
                fact_id=observation.fact_id,
                observation_id=observation.observation_id,
                reason="unexpected_subject_key",
            ))
            continue
        grouped.setdefault(observation.fact_instance_key, []).append(observation)

    by_instance: dict[str, AcquiredFact] = {}
    conflicting_instances: list[str] = []
    deduplicated: list[str] = []
    accepted_ids: list[str] = []
    for instance_key in sorted(grouped):
        candidates = grouped[instance_key]
        fact_id = candidates[0].fact_id
        by_value: dict[str, list[AcquiredFact]] = {}
        for candidate in candidates:
            canonical_value = json.dumps(
                candidate.value(),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            by_value.setdefault(canonical_value, []).append(candidate)
        if len(by_value) != 1:
            conflicting_instances.append(instance_key)
            rejected.extend(
                RejectedObservation(
                    fact_id=fact_id,
                    observation_id=candidate.observation_id,
                    reason="fact_conflict",
                )
                for candidate in candidates
            )
            continue
        equivalent = next(iter(by_value.values()))
        equivalent.sort(key=lambda item: (
            _aware(item.observed_at, "observed_at"), item.observation_id
        ))
        winner = equivalent[-1]
        corroborating = tuple(sorted({
            provenance
            for candidate in equivalent
            for provenance in candidate.provenance_ids
        }))
        winner = replace(winner, provenance_ids=corroborating)
        by_instance[instance_key] = winner
        accepted_ids.append(winner.observation_id)
        deduplicated.extend(
            candidate.observation_id for candidate in equivalent[:-1]
        )

    coverage_items: list[FactInstanceCoverage] = []
    unresolved_instances: list[str] = []
    for fact_id in requested_ids:
        contract = contract_by_id[fact_id]
        request = request_by_id[fact_id]
        observed_subject_keys = tuple(sorted({
            candidate.subject_key
            for candidates in grouped.values()
            for candidate in candidates
            if candidate.fact_id == fact_id
        }))
        requested_subject_keys = tuple(sorted(dict.fromkeys(
            key.strip()
            for key in request.target_subject_keys
            if isinstance(key, str) and key.strip()
        )))
        if requested_subject_keys:
            expected_subject_keys = requested_subject_keys
        elif observed_subject_keys:
            expected_subject_keys = observed_subject_keys
        elif contract.cardinality is FactCardinality.SINGLETON:
            expected_subject_keys = ("UNSPECIFIED",)
        else:
            expected_subject_keys = ()
        resolved_subject_keys = tuple(
            subject_key
            for subject_key in expected_subject_keys
            if fact_instance_key(
                fact_id, contract.subject_scope, subject_key
            ) in by_instance
        )
        conflicting_subject_keys = tuple(
            subject_key
            for subject_key in expected_subject_keys
            if fact_instance_key(
                fact_id, contract.subject_scope, subject_key
            ) in conflicting_instances
        )
        unresolved_subject_keys = tuple(
            subject_key
            for subject_key in expected_subject_keys
            if subject_key not in resolved_subject_keys
            and subject_key not in conflicting_subject_keys
        )
        unresolved_instances.extend(
            fact_instance_key(fact_id, contract.subject_scope, subject_key)
            for subject_key in unresolved_subject_keys
        )
        coverage_items.append(FactInstanceCoverage(
            fact_id=fact_id,
            subject_scope=contract.subject_scope,
            expected_subject_keys=expected_subject_keys,
            resolved_subject_keys=resolved_subject_keys,
            unresolved_subject_keys=unresolved_subject_keys,
            conflicting_subject_keys=conflicting_subject_keys,
            complete=bool(expected_subject_keys) and not (
                unresolved_subject_keys or conflicting_subject_keys
            ),
        ))

    fact_order = {fact_id: index for index, fact_id in enumerate(requested_ids)}
    resolved = tuple(sorted(
        by_instance.values(),
        key=lambda fact: (fact_order[fact.fact_id], fact.subject_key),
    ))
    unresolved = tuple(
        coverage.fact_id
        for coverage in coverage_items
        if not coverage.complete
    )
    conflicting = tuple(
        coverage.fact_id
        for coverage in coverage_items
        if coverage.conflicting_subject_keys
    )

    def facts_at(level: FactSourceLevel, *, enabled: bool | None = None) -> tuple[str, ...]:
        return tuple(
            contract.fact_id
            for contract in contracts
            if contract.fact_id in unresolved
            and contract.source_level is level
            and (enabled is None or contract.enabled is enabled)
        )

    ready_level_0 = facts_at(FactSourceLevel.LEVEL_0_EXISTING_CONFIG, enabled=True)
    ready_level_1 = facts_at(FactSourceLevel.LEVEL_1_CURRENT_PAGE_READ_ONLY, enabled=True)
    requires_level_2 = facts_at(FactSourceLevel.LEVEL_2_PROVEN_NAVIGATION_READ_ONLY)
    requires_level_3 = facts_at(FactSourceLevel.LEVEL_3_REVERSIBLE_DETAIL_OBSERVATION)
    unavailable = facts_at(FactSourceLevel.LEVEL_4_UNAVAILABLE)
    zero_input_contract = tuple(
        contract.fact_id
        for contract in contracts
        if contract.enabled
        and not contract.requires_page_input
        and not contract.required_navigation_edges
    )
    zero_input_executable = tuple(
        contract.fact_id
        for contract in contracts
        if contract.fact_id in zero_input_contract
        and contract.observer_status
        is not ObserverImplementationStatus.NOT_IMPLEMENTED
        and not contract.normalizer_only
    )
    zero_input_normalizer = tuple(
        contract.fact_id
        for contract in contracts
        if contract.fact_id in zero_input_contract and contract.normalizer_only
    )
    navigation_only = tuple(
        contract.fact_id
        for contract in contracts
        if contract.fact_id in unresolved
        and contract.enabled
        and bool(contract.required_navigation_edges)
        and not contract.requires_page_input
    )
    page_input = tuple(
        contract.fact_id
        for contract in contracts
        if contract.fact_id in unresolved and contract.requires_page_input
    )
    observers = tuple(dict.fromkeys(
        contract.observer_callable_id
        for contract in contracts
        if contract.fact_id in unresolved
        and contract.fact_id in zero_input_executable
        and contract.observer_callable_id
    ))
    contract_registered = tuple(
        fact_id for fact_id in requested_ids if fact_id in FACT_ACQUISITION_CONTRACTS
    )
    implemented = tuple(
        contract.fact_id
        for contract in contracts
        if contract.observer_status is not ObserverImplementationStatus.NOT_IMPLEMENTED
    )
    offline_proven = tuple(
        contract.fact_id
        for contract in contracts
        if contract.observer_status in {
            ObserverImplementationStatus.OFFLINE_PROVEN,
            ObserverImplementationStatus.LIVE_PROVEN,
        }
    )
    live_proven = tuple(
        contract.fact_id
        for contract in contracts
        if contract.observer_status is ObserverImplementationStatus.LIVE_PROVEN
    )
    normalizer_only = tuple(
        contract.fact_id for contract in contracts if contract.normalizer_only
    )
    not_implemented = tuple(
        contract.fact_id
        for contract in contracts
        if contract.observer_status is ObserverImplementationStatus.NOT_IMPLEMENTED
    )
    blocked = tuple(
        [f"CONTRACT_INVALID:{fact_id}" for fact_id in invalid]
        + [
            f"FACT_CONFLICT:{instance_key}"
            for instance_key in conflicting_instances
        ]
        + [f"UNRESOLVED:{fact_id}" for fact_id in unresolved]
    )
    return MissingFactAcquisitionPlan(
        schema_version=_SCHEMA_VERSION,
        generated_at=generated_at,
        status=(
            AcquisitionPlanStatus.BLOCKED_CONTRACT_INVALID
            if invalid
            else AcquisitionPlanStatus.PASS
        ),
        policy_fingerprint=policy_fingerprint,
        runtime_input_fingerprint=runtime_input_fingerprint,
        requests=tuple(contracts),
        ready_level_0=ready_level_0,
        ready_level_1=ready_level_1,
        requires_level_2=requires_level_2,
        requires_level_3=requires_level_3,
        unavailable=unavailable,
        zero_input_collectable_facts=zero_input_contract,
        zero_input_contract_facts=zero_input_contract,
        zero_input_executable_facts=zero_input_executable,
        zero_input_normalizer_only_facts=zero_input_normalizer,
        navigation_only_collectable_facts=navigation_only,
        page_input_required_facts=page_input,
        unavailable_facts=unavailable,
        resolved_facts=resolved,
        unresolved_facts=unresolved,
        contract_registered_facts=contract_registered,
        observer_implemented_facts=implemented,
        observer_offline_proven_facts=offline_proven,
        observer_live_proven_facts=live_proven,
        normalizer_only_facts=normalizer_only,
        not_implemented_facts=not_implemented,
        conflicting_facts=tuple(conflicting),
        resolved_fact_instances=resolved,
        unresolved_fact_instances=tuple(sorted(unresolved_instances)),
        conflicting_fact_instances=tuple(sorted(conflicting_instances)),
        fact_instance_coverage=tuple(coverage_items),
        rejected_observations=tuple(sorted(
            rejected,
            key=lambda item: (item.fact_id, item.observation_id, item.reason),
        )),
        deduplicated_observations=tuple(sorted(deduplicated)),
        accepted_observation_ids=tuple(sorted(accepted_ids)),
        recommended_next_observers=observers,
        blocked_reasons=blocked,
        policy_evaluation_still_blocked=bool(unresolved or conflicting),
    )


def validate_contract_registry() -> tuple[str, ...]:
    """Return invalid fact ids; no I/O and no mutation."""

    return tuple(
        fact_id
        for fact_id, contract in FACT_ACQUISITION_CONTRACTS.items()
        if fact_id != contract.fact_id or not _validate_contract(contract)
    )


__all__ = [
    "AcquiredFact",
    "AcquisitionPlanStatus",
    "ActionSummaryAcquisitionPolicyConfig",
    "CurrentActionSummaryVisualSnapshot",
    "FACT_ACQUISITION_CONTRACTS",
    "FactCardinality",
    "FactAcquisitionContract",
    "FactInstanceCoverage",
    "FactSourceLevel",
    "FactSubjectScope",
    "MissingFactAcquisitionPlan",
    "ObserverImplementationStatus",
    "RejectedObservation",
    "build_missing_fact_acquisition_plan",
    "fact_instance_key",
    "observe_action_summary_fatigue_config",
    "observe_action_summary_policy_config",
    "observe_current_action_summary_page_facts",
    "parse_action_summary_acquisition_policy_config",
    "validate_acquired_fact",
    "validate_contract_registry",
]
