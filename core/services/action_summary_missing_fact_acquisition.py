"""Read-only acquisition contracts for missing Action Summary policy facts.

The objects in this module describe where missing facts may come from and can
normalize facts that were already present in a config snapshot or an already
captured Action Summary frame.  They cannot capture, navigate, dispatch input,
persist state, evaluate business policy, or issue authorization.
"""

from __future__ import annotations

import hashlib
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
    output_schema: str
    confidence_requirement: str
    provenance_requirements: tuple[str, ...]
    failure_reason: str
    enabled: bool

    def to_dict(self) -> dict[str, object]:
        document = asdict(self)
        document["source_level"] = self.source_level.value
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

    def value(self) -> object:
        return json.loads(self.value_json)

    def to_dict(self) -> dict[str, object]:
        document = asdict(self)
        document["source_level"] = self.source_level.value
        document["value"] = self.value()
        document["provenance_ids"] = list(self.provenance_ids)
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
    navigation_only_collectable_facts: tuple[str, ...]
    page_input_required_facts: tuple[str, ...]
    unavailable_facts: tuple[str, ...]
    resolved_facts: tuple[AcquiredFact, ...]
    unresolved_facts: tuple[str, ...]
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
            "navigation_only_collectable_facts": list(
                self.navigation_only_collectable_facts
            ),
            "page_input_required_facts": list(
                self.page_input_required_facts
            ),
            "unavailable_facts": list(self.unavailable_facts),
            "resolved_facts": [fact.to_dict() for fact in self.resolved_facts],
            "unresolved_facts": list(self.unresolved_facts),
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
    output_schema: str = "ACTION_SUMMARY_ACQUIRED_FACT_V1",
    confidence_requirement: str = "EXPLICIT",
    provenance_requirements: Sequence[str] = (),
    failure_reason: str,
    enabled: bool = True,
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
        output_schema=output_schema,
        confidence_requirement=confidence_requirement,
        provenance_requirements=tuple(provenance_requirements),
        failure_reason=failure_reason,
        enabled=enabled,
    )


FACT_ACQUISITION_CONTRACTS: dict[str, FactAcquisitionContract] = {
    "objective_missing": _contract(
        "objective_missing", "USER_POLICY", "explicit_user_policy_config",
        FactSourceLevel.LEVEL_0_EXISTING_CONFIG,
        observer_id="action_summary_policy_config_observer",
        provenance_requirements=("config_id", "config_version", "config_fingerprint"),
        failure_reason="objective must remain UNKNOWN until explicitly configured",
    ),
    "strategy_identity_missing": _contract(
        "strategy_identity_missing", "USER_POLICY", "versioned_strategy_config",
        FactSourceLevel.LEVEL_0_EXISTING_CONFIG,
        observer_id="action_summary_strategy_config_observer",
        provenance_requirements=("strategy_id", "strategy_version", "config_fingerprint"),
        failure_reason="strategy identity cannot be inferred from legacy task lists",
    ),
    "strategy_provenance_missing": _contract(
        "strategy_provenance_missing", "USER_POLICY", "versioned_strategy_config",
        FactSourceLevel.LEVEL_0_EXISTING_CONFIG,
        observer_id="action_summary_strategy_config_observer",
        provenance_requirements=("strategy_source", "config_fingerprint"),
        failure_reason="strategy provenance must be explicit and fingerprint-bound",
    ),
    "task_identity_unknown": _contract(
        "task_identity_unknown", "TASK_CARD", "current_page_read_only_model",
        FactSourceLevel.LEVEL_1_CURRENT_PAGE_READ_ONLY,
        required_page="ACTION_SUMMARY_VISIBLE",
        requires_capture=True,
        observer_id="action_summary_task_card_identity_observer",
        provenance_requirements=("capture_id", "frame_sha256", "card_match_key"),
        failure_reason="a unique card-scoped semantic identity is required",
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
    ),
    "resource_identity_unknown": _contract(
        "resource_identity_unknown", "RESOURCE", "current_page_icon_name_contract",
        FactSourceLevel.LEVEL_1_CURRENT_PAGE_READ_ONLY,
        required_page="ACTION_SUMMARY_VISIBLE",
        requires_capture=True,
        observer_id="action_summary_current_page_visual_observer",
        provenance_requirements=("capture_id", "frame_sha256", "icon_id", "resource_name"),
        failure_reason="resource identity requires a stable icon and name contract",
    ),
    "resource_balance_unknown": _contract(
        "resource_balance_unknown", "RESOURCE", "current_page_resource_balance",
        FactSourceLevel.LEVEL_1_CURRENT_PAGE_READ_ONLY,
        required_page="ACTION_SUMMARY_VISIBLE",
        requires_capture=True,
        observer_id="action_summary_current_page_visual_observer",
        provenance_requirements=("capture_id", "frame_sha256", "resource_identity"),
        failure_reason="absence of an insufficiency warning is not a balance",
    ),
    "runtime_resource_observation_missing": _contract(
        "runtime_resource_observation_missing", "RESOURCE", "existing_runtime_resource_snapshot",
        FactSourceLevel.LEVEL_0_EXISTING_CONFIG,
        observer_id="action_summary_runtime_resource_snapshot_observer",
        provenance_requirements=("capture_id", "frame_sha256", "valid_until"),
        failure_reason="a fresh runtime resource snapshot is required",
    ),
    "reward_target_unknown": _contract(
        "reward_target_unknown", "TASK_REWARD", "current_page_task_bound_reward",
        FactSourceLevel.LEVEL_1_CURRENT_PAGE_READ_ONLY,
        required_page="ACTION_SUMMARY_VISIBLE",
        requires_capture=True,
        observer_id="action_summary_current_page_visual_observer",
        provenance_requirements=("capture_id", "frame_sha256", "card_match_key", "reward_identity"),
        failure_reason="a catalog reward is not a live task-bound reward fact",
    ),
    "fatigue_budget_unknown": _contract(
        "fatigue_budget_unknown", "FATIGUE", "explicit_fatigue_config_or_state",
        FactSourceLevel.LEVEL_0_EXISTING_CONFIG,
        observer_id="action_summary_fatigue_config_observer",
        provenance_requirements=("config_fingerprint", "fatigue_unit_id", "available_fatigue"),
        failure_reason="fatigue budget must come from explicit config or state",
    ),
    "fatigue_cost_unknown": _contract(
        "fatigue_cost_unknown", "TASK_COST", "explicit_task_fatigue_config",
        FactSourceLevel.LEVEL_0_EXISTING_CONFIG,
        observer_id="action_summary_fatigue_config_observer",
        provenance_requirements=("config_fingerprint", "fatigue_unit_id", "fatigue_cost_per_run"),
        failure_reason="fatigue cost must be independent from resource cost",
    ),
    "resource_cost_unknown": _contract(
        "resource_cost_unknown", "TASK_COST", "current_page_resource_cost",
        FactSourceLevel.LEVEL_1_CURRENT_PAGE_READ_ONLY,
        required_page="ACTION_SUMMARY_VISIBLE",
        requires_capture=True,
        observer_id="action_summary_current_page_visual_observer",
        provenance_requirements=("capture_id", "frame_sha256", "resource_identity"),
        failure_reason="resource cost requires a task-bound resource identity",
    ),
    "recommended_run_count_limit_unknown": _contract(
        "recommended_run_count_limit_unknown", "USER_POLICY", "explicit_user_policy_config",
        FactSourceLevel.LEVEL_0_EXISTING_CONFIG,
        observer_id="action_summary_policy_config_observer",
        provenance_requirements=("config_fingerprint", "maximum_task_runs"),
        failure_reason="run count limit must be explicit",
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
    payload = normalized.to_dict()
    payload.pop("config_fingerprint", None)
    return replace(normalized, config_fingerprint=_canonical_hash(payload))


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
    return AcquiredFact(
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
    )


def observe_action_summary_policy_config(
    config: ActionSummaryAcquisitionPolicyConfig,
) -> tuple[AcquiredFact, ...]:
    """Resolve only explicit objective and strategy facts from a config snapshot."""

    provenance = (config.config_id, config.config_version, config.config_fingerprint)
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
        "provenance_ids": (config.config_id, config.config_version, config.config_fingerprint),
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

    if snapshot.page_state != "ACTION_SUMMARY_VISIBLE":
        return ()
    if not _is_hash(snapshot.frame_sha256) or not _is_hash(runtime_input_fingerprint):
        raise ValueError("visual_snapshot_fingerprint_invalid")
    _aware(snapshot.captured_at, "captured_at")
    _aware(snapshot.valid_until, "valid_until")
    facts: list[AcquiredFact] = []
    common = {
        "source_level": FactSourceLevel.LEVEL_1_CURRENT_PAGE_READ_ONLY,
        "observer_id": "action_summary_current_page_visual_observer",
        "observed_at": snapshot.captured_at,
        "valid_until": snapshot.valid_until,
        "source_fingerprint": snapshot.frame_sha256,
        "policy_fingerprint": None,
        "runtime_input_fingerprint": runtime_input_fingerprint,
        "provenance_ids": (snapshot.capture_id, snapshot.frame_sha256, *snapshot.evidence_ids),
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
                    **identity,
                    "card_match_key": snapshot.card_match_key,
                    "resource_cost_per_run": snapshot.displayed_resource_cost,
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


def _validate_contract(contract: FactAcquisitionContract) -> bool:
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

    by_fact: dict[str, AcquiredFact] = {}
    for observation in observations:
        contract = FACT_ACQUISITION_CONTRACTS.get(observation.fact_id)
        if contract is None or not contract.enabled:
            continue
        if observation.policy_fingerprint not in (None, policy_fingerprint):
            continue
        if observation.runtime_input_fingerprint not in (
            None, runtime_input_fingerprint
        ):
            continue
        if not _is_hash(observation.source_fingerprint):
            continue
        if _aware(observation.valid_until, "valid_until") < generated:
            continue
        by_fact[observation.fact_id] = observation

    requested_ids = tuple(contract.fact_id for contract in contracts)
    resolved = tuple(by_fact[fact_id] for fact_id in requested_ids if fact_id in by_fact)
    unresolved = tuple(fact_id for fact_id in requested_ids if fact_id not in by_fact)

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
    zero_input = tuple(
        contract.fact_id
        for contract in contracts
        if contract.fact_id in unresolved
        and contract.enabled
        and not contract.requires_page_input
        and not contract.required_navigation_edges
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
        contract.observer_id
        for contract in contracts
        if contract.fact_id in unresolved and contract.enabled and contract.observer_id
    ))
    blocked = tuple(
        [f"CONTRACT_INVALID:{fact_id}" for fact_id in invalid]
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
        zero_input_collectable_facts=zero_input,
        navigation_only_collectable_facts=navigation_only,
        page_input_required_facts=page_input,
        unavailable_facts=unavailable,
        resolved_facts=resolved,
        unresolved_facts=unresolved,
        recommended_next_observers=observers,
        blocked_reasons=blocked,
        policy_evaluation_still_blocked=bool(unresolved),
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
    "FactAcquisitionContract",
    "FactSourceLevel",
    "MissingFactAcquisitionPlan",
    "build_missing_fact_acquisition_plan",
    "observe_action_summary_fatigue_config",
    "observe_action_summary_policy_config",
    "observe_current_action_summary_page_facts",
    "parse_action_summary_acquisition_policy_config",
    "validate_contract_registry",
]
