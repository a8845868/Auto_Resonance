from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import subprocess
import sys

import pytest

from core.services.action_summary_policy_prerequisites import (
    FactSource,
    PrerequisiteModelStatus,
    StrategyObjective,
)
from core.services.action_summary_policy_runtime_inputs import (
    ActionSummaryRuntimeResourceObservation,
    AssemblyIntegrityStatus,
    PolicyInputReadiness,
    PolicyTargetMatchStatus,
    RuntimeInputAssemblyStatus,
    SourceRelationship,
    action_summary_policy_config_fingerprint,
    assemble_action_summary_policy_runtime_inputs,
    load_action_summary_user_policy_config,
    parse_action_summary_user_policy_config,
)
from core.services.action_summary_product_model import (
    TaskCardState,
    action_summary_title_hash,
    observe_action_summary_page,
)
from tests.action_summary_policy_prerequisite_model_test import card, page_model
from tests.action_summary_product_model_test import Frame, item, trusted_items


def policy_document(*, rewards: list[dict] | None = None) -> dict:
    configured_rewards = rewards if rewards is not None else [
        {
            "task_semantic_id": "TASK_A",
            "reward_amount_per_execution": 30,
        },
        {
            "task_semantic_id": "TASK_B",
            "reward_amount_per_execution": 25,
        },
    ]
    return {
        "ActionSummaryPolicy": {
            "schema_version": "1.0",
            "policy_id": "personal-siege-policy",
            "policy_version": "1",
            "revision": "user-config-rev-7",
            "effective_at": "2026-07-26T11:50:00+08:00",
            "valid_until": "2026-07-26T12:10:00+08:00",
            "objective": "MAXIMIZE_TARGET_REWARD",
            "allowed_action_types": ["CHALLENGE", "SWEEP"],
            "max_task_executions": 2,
            "reward_target_id": "SIEGE_PROGRESS",
            "reward_current_amount": 20,
            "reward_target_amount": 100,
            "reserved_fatigue": 20,
            "max_policy_spend": 80,
            "candidate_rewards": configured_rewards,
            "candidate_fatigue_costs": [
                {
                    "task_semantic_id": item["task_semantic_id"],
                    "fatigue_unit_id": "FATIGUE",
                    "fatigue_cost_per_run": 10 + index,
                    "fatigue_cost_applicable": True,
                }
                for index, item in enumerate(configured_rewards)
            ],
        }
    }


def observation(**changes) -> ActionSummaryRuntimeResourceObservation:
    values = {
        "source_capture_id": "resource-capture-9",
        "source_frame_sha256": "9" * 64,
        "revision": "resource-observation-9",
        "observed_at": "2026-07-26T11:59:00+08:00",
        "valid_until": "2026-07-26T12:05:00+08:00",
        "resource_id": "FATIGUE",
        "available_amount": 120,
        "fatigue_unit_id": "FATIGUE",
        "available_fatigue": 120,
        "evidence_ids": ("resource-hud-9",),
    }
    values.update(changes)
    return ActionSummaryRuntimeResourceObservation(**values)


def runtime_page():
    first = replace(
        card(match_key="MATCH_A", semantic_id="TASK_A", cost=40),
        remaining_attempts=3,
        total_attempts=5,
    )
    second = replace(
        card(match_key="MATCH_B", semantic_id="TASK_B", cost=20),
        remaining_attempts=2,
        total_attempts=5,
        available_actions=frozenset({
            "CARD_SELECTABLE",
            "SWEEP_AVAILABLE",
            "TASK_EXECUTION_AVAILABLE",
        }),
    )
    return page_model(first, second)


def assemble(*, page=None, policy=None, resource=None):
    return assemble_action_summary_policy_runtime_inputs(
        page or runtime_page(),
        policy or policy_document(),
        resource_observation=(resource if resource is not None else observation()),
    )


def test_real_page_facts_and_user_policy_assemble_all_candidates():
    result = assemble()

    assert result.status is RuntimeInputAssemblyStatus.READY_FOR_POLICY_EVALUATION
    assert result.source_policy_candidate_count == 2
    assert len(result.candidate_prerequisites) == 2
    assert all(
        candidate.status is PrerequisiteModelStatus.READY_FOR_POLICY_EVALUATION
        for candidate in result.candidate_prerequisites
    )
    first, second = result.candidate_prerequisites
    assert first.remaining_attempts.remaining_attempts == 3
    assert first.remaining_attempts.total_attempts == 5
    assert first.remaining_attempts.provenance.source is FactSource.PAGE_MODEL
    assert first.resource_balance.unit_cost == 40
    assert second.resource_balance.unit_cost == 20
    assert first.reward_target.candidate_reward_amount == 30
    assert second.reward_target.candidate_reward_amount == 25
    assert first.fatigue_budget.provenance.source is FactSource.RUNTIME_ASSEMBLED
    assert first.fatigue_budget.fatigue_cost_per_run == 10
    assert second.fatigue_budget.fatigue_cost_per_run == 11


def test_assembly_stops_before_business_policy_or_execution():
    result = assemble()

    assert result.policy_evaluation_allowed is True
    assert result.business_policy_evaluated is False
    assert result.executor_connected is False
    assert result.execution_authorized is False
    assert result.authorization_issued is False
    assert result.business_dispatches == 0
    assert result.irreversible_actions == 0
    assert not any(
        hasattr(candidate, name)
        for candidate in result.candidate_prerequisites
        for name in ("tap", "click", "dispatch", "executor")
    )


def test_page_attempt_fraction_preserves_remaining_and_total():
    labels = trusted_items() + [item("3/5", 580, 540, 55, 20)]
    model = observe_action_summary_page(Frame(labels))

    assert model.task_cards[0].remaining_attempts == 3
    assert model.task_cards[0].total_attempts == 5
    assert model.to_dict()["task_cards"][0]["total_attempts"] == 5


def test_observed_frame_model_flows_directly_into_runtime_assembly():
    labels = trusted_items() + [
        item("3/5", 580, 540, 55, 20),
        item("2/5", 856, 540, 55, 20),
        item("1/5", 1138, 540, 55, 20),
    ]
    page = observe_action_summary_page(Frame(
        labels,
        "runtime-frame-17",
        raw_frame_hash="7" * 64,
        captured_at="2026-07-26T12:00:00+08:00",
    ))
    rewards = [
        {
            "task_semantic_id": card.semantic_id,
            "reward_amount_per_execution": 10 + index,
        }
        for index, card in enumerate(page.task_cards)
    ]

    result = assemble_action_summary_policy_runtime_inputs(
        page,
        policy_document(rewards=rewards),
        resource_observation=observation(),
    )

    assert result.status is RuntimeInputAssemblyStatus.READY_FOR_POLICY_EVALUATION
    assert result.source_capture_id == "runtime-frame-17"
    assert result.source_frame_sha256 == "7" * 64
    assert result.source_policy_candidate_count == 3
    assert [
        candidate.remaining_attempts.remaining_attempts
        for candidate in result.candidate_prerequisites
    ] == [3, 2, 1]


def test_missing_page_attempt_total_is_not_invented():
    first, second = runtime_page().task_cards
    page = page_model(replace(first, total_attempts=None), second)
    result = assemble(page=page)

    assert result.status is RuntimeInputAssemblyStatus.BLOCKED_RUNTIME_FACTS
    assert "remaining_attempts_unknown" in result.reason_codes
    assert result.policy_evaluation_allowed is False


def test_missing_resource_observation_is_not_filled_from_user_config():
    result = assemble_action_summary_policy_runtime_inputs(
        runtime_page(), policy_document(), resource_observation=None
    )

    assert result.status is RuntimeInputAssemblyStatus.BLOCKED_RUNTIME_FACTS
    assert "runtime_resource_observation_missing" in result.reason_codes
    assert "resource_balance_unknown" in result.reason_codes
    assert "fatigue_budget_unknown" in result.reason_codes
    assert all(
        candidate.resource_balance.provenance is None
        for candidate in result.candidate_prerequisites
    )


def test_candidate_reward_must_be_configured_for_every_visible_candidate():
    policy = policy_document(rewards=[{
        "task_semantic_id": "TASK_A",
        "reward_amount_per_execution": 30,
    }])
    result = assemble(policy=policy)

    assert result.status is RuntimeInputAssemblyStatus.BLOCKED_USER_CONFIG
    assert "reward_target_unknown" in result.reason_codes
    assert result.candidate_prerequisites[1].reward_target.status.value == "UNKNOWN"


def test_missing_candidate_fatigue_cost_remains_an_explicit_unknown_fact():
    policy = policy_document()
    policy["ActionSummaryPolicy"].pop("candidate_fatigue_costs")

    result = assemble(policy=policy)

    assert result.status is RuntimeInputAssemblyStatus.BLOCKED_RUNTIME_FACTS
    assert "fatigue_cost_unknown" in result.missing_runtime_inputs
    assert all(
        candidate.fatigue_budget.fatigue_cost_per_run is None
        for candidate in result.candidate_prerequisites
    )


def test_resource_and_fatigue_unit_identities_are_assembled_independently():
    result = assemble(resource=observation(
        resource_id="ACTIVITY_TOKEN",
        fatigue_unit_id="STAMINA",
    ))

    assert result.status is RuntimeInputAssemblyStatus.READY_FOR_POLICY_EVALUATION
    assert all(
        candidate.resource_balance.resource_id == "ACTIVITY_TOKEN"
        and candidate.fatigue_budget.fatigue_unit_id == "STAMINA"
        for candidate in result.candidate_prerequisites
    )


def test_invalid_non_applicable_fatigue_cost_contract_is_rejected():
    policy = policy_document()
    entry = policy["ActionSummaryPolicy"]["candidate_fatigue_costs"][0]
    entry["fatigue_cost_applicable"] = False
    entry["fatigue_cost_per_run"] = 1

    with pytest.raises(ValueError, match="fatigue_cost_contract_invalid"):
        parse_action_summary_user_policy_config(policy)


def test_overlay_or_unknown_page_blocks_before_policy_evaluation():
    overlay = replace(runtime_page(), overlay_states=("DAILY_CHECKIN",))
    result = assemble(page=overlay)

    assert result.status is RuntimeInputAssemblyStatus.BLOCKED_SOURCE_PAGE
    assert "source_page_not_runtime_eligible" in result.reason_codes
    assert result.business_dispatches == 0


def test_no_executable_cards_is_a_source_page_block():
    locked = replace(card(), state=TaskCardState.LOCKED)
    result = assemble(page=page_model(locked))

    assert result.status is RuntimeInputAssemblyStatus.BLOCKED_SOURCE_PAGE
    assert result.reason_codes == ("no_policy_candidates",)
    assert result.candidate_prerequisites == ()


def test_resource_and_fatigue_mismatch_remains_conflicting():
    result = assemble(resource=observation(available_fatigue=100))

    assert result.status is RuntimeInputAssemblyStatus.BLOCKED_RUNTIME_FACTS
    assert "fatigue_resource_balance_mismatch" in result.reason_codes


def test_untraceable_resource_observation_is_not_promoted_to_known_fact():
    result = assemble(resource=observation(
        source_capture_id="",
        source_frame_sha256="not-a-frame-hash",
        evidence_ids=(),
    ))

    assert result.status is RuntimeInputAssemblyStatus.BLOCKED_RUNTIME_FACTS
    assert "runtime_resource_capture_id_missing" in result.reason_codes
    assert "runtime_resource_frame_hash_invalid" in result.reason_codes
    assert "runtime_resource_evidence_missing" in result.reason_codes
    assert result.candidate_prerequisites[0].resource_balance.status.value == "UNKNOWN"


def test_policy_hash_is_deterministic_and_changes_with_user_policy():
    first = assemble()
    second = assemble()
    changed = policy_document()
    changed["ActionSummaryPolicy"]["max_task_executions"] = 1
    third = assemble(policy=changed)

    assert first.policy_config_sha256 == second.policy_config_sha256
    assert first.policy_config_sha256 != third.policy_config_sha256


def test_different_capture_within_freshness_window_is_explicitly_bound():
    result = assemble()

    assert result.assembly_status is AssemblyIntegrityStatus.PASS
    assert result.policy_input_readiness is PolicyInputReadiness.READY_FOR_POLICY_EVALUATION
    assert result.source_relationship is SourceRelationship.DIFFERENT_CAPTURE_WITHIN_WINDOW
    assert result.source_age_seconds == 60.0
    assert result.stale_resource_input_accepted is False


def test_same_capture_is_not_inferred_from_timestamp_only():
    page = runtime_page()
    resource = observation(
        source_capture_id=page.source_capture_id,
        source_frame_sha256=page.source_frame_sha256,
        observed_at=page.captured_at,
    )
    result = assemble(page=page, resource=resource)

    assert result.source_relationship is SourceRelationship.SAME_CAPTURE
    assert result.source_age_seconds == 0.0


def test_stale_resource_observation_is_preserved_but_not_used_for_readiness():
    resource = observation(
        observed_at="2026-07-26T11:00:00+08:00",
        valid_until="2026-07-26T11:05:00+08:00",
    )
    result = assemble(resource=resource)

    assert result.assembly_status is AssemblyIntegrityStatus.PASS
    assert result.policy_input_readiness is PolicyInputReadiness.BLOCKED_MISSING_FACTS
    assert result.source_relationship is SourceRelationship.DIFFERENT_CAPTURE_STALE
    assert result.source_age_seconds == 3600.0
    assert result.resource_observation_capture_id == "resource-capture-9"
    assert result.stale_resource_input_accepted is False
    assert "runtime_resource_observation_stale" in result.reason_codes
    assert all(
        candidate.resource_balance.status.value == "UNKNOWN"
        for candidate in result.candidate_prerequisites
    )


def test_missing_resource_capture_id_remains_none_and_relationship_unknown():
    result = assemble(resource=observation(source_capture_id=""))

    assert result.resource_observation_capture_id == ""
    assert result.source_relationship is SourceRelationship.UNKNOWN
    assert result.policy_input_readiness is PolicyInputReadiness.BLOCKED_MISSING_FACTS
    assert result.stale_resource_input_accepted is False


def test_assembled_at_and_policy_snapshot_fingerprint_are_frozen():
    policy = policy_document()
    assembled_at = "2026-07-26T12:00:01+08:00"
    result = assemble_action_summary_policy_runtime_inputs(
        runtime_page(),
        policy,
        resource_observation=observation(),
        assembled_at=assembled_at,
    )

    assert result.assembled_at == assembled_at
    assert result.policy_config_fingerprint_bound is True
    assert result.policy_config_sha256 == action_summary_policy_config_fingerprint(policy)
    assert result.matches_policy_config(policy) is True
    changed = policy_document()
    changed["ActionSummaryPolicy"]["max_task_executions"] = 1
    assert result.matches_policy_config(changed) is False


def test_partial_policy_snapshot_is_valid_assembly_but_missing_facts():
    partial = {
        "ActionSummaryPolicy": {
            "schema_version": "1.0",
            "policy_id": "gui-snapshot",
            "policy_version": "config-v1",
            "captured_at": "2026-07-26T12:00:00+08:00",
        }
    }
    result = assemble_action_summary_policy_runtime_inputs(
        runtime_page(), partial, resource_observation=None
    )

    assert result.assembly_status is AssemblyIntegrityStatus.PASS
    assert result.policy_input_readiness is PolicyInputReadiness.BLOCKED_MISSING_FACTS
    assert result.policy_config_id == "gui-snapshot"
    assert result.policy_config_version == "config-v1"
    assert result.policy_config_captured_at == "2026-07-26T12:00:00+08:00"
    assert "objective_missing" in result.missing_runtime_inputs
    assert "runtime_resource_observation_missing" in result.missing_runtime_inputs


def test_malformed_policy_section_fails_closed_without_snapshot_reuse():
    malformed = {"ActionSummaryPolicy": []}
    result = assemble(policy=malformed)

    assert result.assembly_status is AssemblyIntegrityStatus.FAIL
    assert result.policy_input_readiness is PolicyInputReadiness.BLOCKED_CONFLICTING_INPUTS
    assert result.matches_policy_config({"not": {"json": {1, 2}}}) is False


def test_unique_policy_target_binds_current_fresh_card_match_key():
    title_hash = action_summary_title_hash("特殊订单")
    first, second = runtime_page().task_cards
    page = page_model(replace(first, title_hash=title_hash), second)
    policy = policy_document()
    policy["ActionSummaryPolicy"].update({
        "requested_known_task_id": "TASK_A",
        "requested_task_title_hash": title_hash,
    })
    result = assemble(page=page, policy=policy)

    assert result.policy_target_match_status is PolicyTargetMatchStatus.UNIQUE
    assert result.policy_target_card_match_key == "MATCH_A"
    assert result.assembly_status is AssemblyIntegrityStatus.PASS


def test_duplicate_semantic_target_is_ambiguous_not_index_bound():
    title_hash = action_summary_title_hash("特殊订单")
    first, second = runtime_page().task_cards
    page = page_model(
        replace(first, title_hash=title_hash),
        replace(second, semantic_id="TASK_A", title_hash=title_hash),
    )
    policy = policy_document()
    policy["ActionSummaryPolicy"].update({
        "requested_known_task_id": "TASK_A",
        "requested_task_title_hash": title_hash,
    })
    result = assemble(page=page, policy=policy)

    assert result.policy_target_match_status is PolicyTargetMatchStatus.AMBIGUOUS
    assert result.policy_target_card_match_key is None
    assert result.assembly_status is AssemblyIntegrityStatus.FAIL
    assert result.policy_input_readiness is PolicyInputReadiness.BLOCKED_CONFLICTING_INPUTS


def test_unknown_activity_cannot_bind_known_policy_target():
    page = replace(runtime_page(), activity_family="UNKNOWN")
    policy = policy_document()
    policy["ActionSummaryPolicy"]["requested_known_task_id"] = "TASK_A"
    result = assemble(page=page, policy=policy)

    assert result.policy_target_match_status is PolicyTargetMatchStatus.UNSUPPORTED
    assert result.policy_target_card_match_key is None
    assert result.assembly_status is AssemblyIntegrityStatus.FAIL


def test_parser_accepts_direct_or_app_section_documents():
    nested = parse_action_summary_user_policy_config(policy_document())
    direct = parse_action_summary_user_policy_config(
        policy_document()["ActionSummaryPolicy"]
    )

    assert nested == direct
    assert nested.objective is StrategyObjective.MAXIMIZE_TARGET_REWARD
    assert nested.allowed_action_types == frozenset({"CHALLENGE", "SWEEP"})


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("schema_version", "2.0", "policy_schema_unsupported"),
        ("max_task_executions", True, "max_task_executions_invalid"),
        ("max_task_executions", 11, "max_task_executions_invalid"),
        ("objective", "DO_ANYTHING", "strategy_objective_invalid"),
        ("allowed_action_types", ["CLAIM"], "strategy_action_types_invalid"),
        ("reward_current_amount", -1, "reward_current_amount_invalid"),
    ],
)
def test_invalid_user_policy_is_rejected_without_defaults(field, value, reason):
    document = policy_document()
    document["ActionSummaryPolicy"][field] = value

    with pytest.raises(ValueError, match=reason):
        parse_action_summary_user_policy_config(document)


def test_duplicate_candidate_reward_config_is_rejected():
    duplicate = {
        "task_semantic_id": "TASK_A",
        "reward_amount_per_execution": 10,
    }
    document = policy_document(rewards=[duplicate, dict(duplicate)])

    with pytest.raises(ValueError, match="candidate_reward_duplicate"):
        parse_action_summary_user_policy_config(document)


def test_invalid_mapping_returns_blocked_result_instead_of_throwing():
    document = policy_document()
    document["ActionSummaryPolicy"]["allowed_action_types"] = ["CLAIM"]
    result = assemble(policy=document)

    assert result.status is RuntimeInputAssemblyStatus.BLOCKED_USER_CONFIG
    assert result.reason_codes[0] == "strategy_action_types_invalid"
    assert result.execution_authorized is False


def test_explicit_policy_file_loader_is_read_only(tmp_path: Path):
    path = tmp_path / "action-summary-policy.json"
    path.write_text(json.dumps(policy_document(), ensure_ascii=False), encoding="utf-8")
    before = path.read_bytes()

    config = load_action_summary_user_policy_config(path)

    assert config.policy_id == "personal-siege-policy"
    assert path.read_bytes() == before


def test_corrupt_or_non_object_policy_file_is_rejected(tmp_path: Path):
    corrupt = tmp_path / "corrupt.json"
    corrupt.write_text("{", encoding="utf-8")
    array = tmp_path / "array.json"
    array.write_text("[]", encoding="utf-8")

    with pytest.raises(ValueError, match="policy_config_unreadable"):
        load_action_summary_user_policy_config(corrupt)
    with pytest.raises(ValueError, match="policy_document_invalid"):
        load_action_summary_user_policy_config(array)


def test_serialization_contains_only_data_and_zero_authority():
    document = assemble().to_dict()

    assert document["status"] == "READY_FOR_POLICY_EVALUATION"
    assert document["business_policy_evaluated"] is False
    assert document["executor_connected"] is False
    assert document["execution_authorized"] is False
    assert document["authorization_issued"] is False
    assert document["business_dispatches"] == 0
    assert document["irreversible_actions"] == 0
    assert len(document["candidate_prerequisites"]) == 2


def test_module_has_no_executor_control_or_automation_dependency():
    source = Path(
        "core/services/action_summary_policy_runtime_inputs.py"
    ).read_text(encoding="utf-8")

    assert "core.control" not in source
    assert "auto." not in source
    assert "evaluate_action_summary_business_policy" not in source
    assert "input_tap" not in source
    assert "input_swipe" not in source


def test_import_does_not_initialize_real_backend():
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; "
                "import core.services.action_summary_policy_runtime_inputs; "
                "assert 'core.control.control' not in sys.modules; "
                "assert 'auto' not in sys.modules"
            ),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr


def test_live_gate_import_does_not_load_runtime_backend_or_automation():
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; "
                "import tools.action_summary_policy_runtime_input_live_gate; "
                "assert 'core.control.control' not in sys.modules; "
                "assert 'auto.resident_activity' not in sys.modules"
            ),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
