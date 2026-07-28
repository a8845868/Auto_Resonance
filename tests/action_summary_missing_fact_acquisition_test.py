from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
import json
from pathlib import Path

import pytest

from core.services.action_summary_advisory_policy import FactAcquisitionRequest
from core.services.action_summary_missing_fact_acquisition import (
    AcquisitionPlanStatus,
    CurrentActionSummaryVisualSnapshot,
    FACT_ACQUISITION_CONTRACTS,
    FactSourceLevel,
    build_missing_fact_acquisition_plan,
    observe_action_summary_fatigue_config,
    observe_action_summary_policy_config,
    observe_current_action_summary_page_facts,
    parse_action_summary_acquisition_policy_config,
    validate_contract_registry,
)


FIXTURE = Path(
    "tests/fixtures/action_summary_missing_fact_acquisition/"
    "canonical_observe_only_config_v1.json"
)
POLICY_HASH = "a" * 64
RUNTIME_HASH = "b" * 64
FRAME_HASH = "c" * 64
NOW = "2026-07-26T12:30:00+08:00"


def config_document(**changes):
    document = json.loads(FIXTURE.read_text(encoding="utf-8"))
    document["ActionSummaryAcquisitionPolicy"].update(changes)
    return document


def config(**changes):
    return parse_action_summary_acquisition_policy_config(
        config_document(**changes)
    )


def request(fact_id: str, priority: int = 1) -> FactAcquisitionRequest:
    return FactAcquisitionRequest(
        missing_fact=fact_id,
        required_scope="TEST",
        preferred_source="test",
        requires_navigation=None,
        requires_page_input=None,
        requires_business_input=False,
        priority=priority,
        reason="test request",
    )


def visual(**changes) -> CurrentActionSummaryVisualSnapshot:
    values = {
        "capture_id": "CAPTURE-1",
        "frame_sha256": FRAME_HASH,
        "captured_at": "2026-07-26T12:20:00+08:00",
        "valid_until": "2026-07-26T12:40:00+08:00",
        "page_state": "ACTION_SUMMARY_VISIBLE",
        "card_match_key": "CARD-A",
        "resource_icon_id": "ICON-STONE",
        "resource_name": "桦石",
        "resource_available_amount": 120,
        "reward_icon_id": "ICON-REWARD",
        "reward_name": "目标奖励",
        "displayed_resource_cost": 40,
        "evidence_ids": ("ROI-1",),
    }
    values.update(changes)
    return CurrentActionSummaryVisualSnapshot(**values)


@pytest.mark.parametrize("fact_id", sorted(FACT_ACQUISITION_CONTRACTS))
def test_every_registered_fact_has_an_exact_contract(fact_id):
    contract = FACT_ACQUISITION_CONTRACTS[fact_id]
    assert contract.fact_id == fact_id
    assert contract.failure_reason
    assert contract.output_schema == "ACTION_SUMMARY_ACQUIRED_FACT_V1"


def test_contract_registry_is_safe():
    assert validate_contract_registry() == ()


def test_every_enabled_observer_is_zero_input_and_zero_dispatch():
    enabled = [value for value in FACT_ACQUISITION_CONTRACTS.values() if value.enabled]
    assert enabled
    assert all(not value.requires_page_input for value in enabled)
    assert all(not value.requires_business_input for value in enabled)
    assert all(not value.irreversible for value in enabled)
    assert all(value.maximum_dispatches == 0 for value in enabled)


def test_level_three_contract_is_disabled_and_requires_page_input():
    contract = FACT_ACQUISITION_CONTRACTS["remaining_attempts_unknown"]
    assert contract.source_level is FactSourceLevel.LEVEL_3_REVERSIBLE_DETAIL_OBSERVATION
    assert contract.enabled is False
    assert contract.requires_page_input is True
    assert contract.maximum_dispatches == 1


def test_objective_and_strategy_are_config_only():
    for fact_id in (
        "objective_missing",
        "strategy_identity_missing",
        "strategy_provenance_missing",
    ):
        contract = FACT_ACQUISITION_CONTRACTS[fact_id]
        assert contract.source_level is FactSourceLevel.LEVEL_0_EXISTING_CONFIG
        assert contract.requires_capture is False


def test_default_product_policy_is_observe_only_and_non_executing():
    value = config_document()["ActionSummaryAcquisitionPolicy"]
    value.pop("objective")
    parsed = parse_action_summary_acquisition_policy_config(
        {"ActionSummaryAcquisitionPolicy": value}
    )
    assert parsed.objective.value == "OBSERVE_ONLY"
    assert parsed.maximum_task_runs == 0
    assert parsed.maximum_cost_per_run == 0
    assert parsed.maximum_total_cost == 0


def test_config_fingerprint_is_deterministic_and_reason_bound():
    first = config()
    second = config()
    changed = config(reason="different human reason")
    assert first.config_fingerprint == second.config_fingerprint
    assert first.config_fingerprint != changed.config_fingerprint
    assert len(first.config_fingerprint) == 64


def test_policy_observer_resolves_exactly_three_level_zero_facts():
    facts = observe_action_summary_policy_config(config())
    assert {fact.fact_id for fact in facts} == {
        "objective_missing",
        "strategy_identity_missing",
        "strategy_provenance_missing",
    }
    assert all(fact.source_level is FactSourceLevel.LEVEL_0_EXISTING_CONFIG for fact in facts)
    assert all(fact.policy_fingerprint == config().config_fingerprint for fact in facts)


def test_policy_observer_never_uses_legacy_siege_tasks():
    value = config_document()
    value["SIEGE_TASKS"] = ["legacy-a", "legacy-b"]
    parsed = parse_action_summary_acquisition_policy_config(value)
    facts = observe_action_summary_policy_config(parsed)
    strategy = next(fact for fact in facts if fact.fact_id == "strategy_identity_missing")
    assert strategy.value()["strategy_id"] == "observe-only"


def test_fatigue_observer_requires_explicit_fields():
    assert observe_action_summary_fatigue_config(config()) == ()


def test_fatigue_cost_remains_independent_from_resource_cost():
    parsed = config(
        available_fatigue=80,
        fatigue_unit_id="FATIGUE",
        fatigue_cost_per_run=5,
        fatigue_cost_applicable=True,
        resource_cost_per_run=40,
    )
    facts = observe_action_summary_fatigue_config(parsed)
    cost = next(fact for fact in facts if fact.fact_id == "fatigue_cost_unknown")
    assert cost.value()["fatigue_cost_per_run"] == 5
    assert cost.value()["resource_cost_per_run"] == 40


def test_visual_resource_identity_requires_both_icon_and_name():
    for snapshot in (
        visual(resource_icon_id=None),
        visual(resource_name=None),
    ):
        facts = observe_current_action_summary_page_facts(
            snapshot, runtime_input_fingerprint=RUNTIME_HASH
        )
        assert "resource_identity_unknown" not in {fact.fact_id for fact in facts}


def test_missing_balance_stays_unknown():
    facts = observe_current_action_summary_page_facts(
        visual(resource_available_amount=None),
        runtime_input_fingerprint=RUNTIME_HASH,
    )
    assert "resource_identity_unknown" in {fact.fact_id for fact in facts}
    assert "resource_balance_unknown" not in {fact.fact_id for fact in facts}


def test_displayed_minus_40_is_resource_cost_not_fatigue():
    facts = observe_current_action_summary_page_facts(
        visual(displayed_resource_cost=40), runtime_input_fingerprint=RUNTIME_HASH
    )
    ids = {fact.fact_id for fact in facts}
    assert "resource_cost_unknown" in ids
    assert "fatigue_cost_unknown" not in ids
    assert "fatigue_budget_unknown" not in ids


def test_reward_catalog_identity_is_not_a_live_task_reward():
    facts = observe_current_action_summary_page_facts(
        visual(card_match_key=None), runtime_input_fingerprint=RUNTIME_HASH
    )
    assert "reward_target_unknown" not in {fact.fact_id for fact in facts}


def test_live_reward_requires_card_bound_icon_and_name():
    facts = observe_current_action_summary_page_facts(
        visual(), runtime_input_fingerprint=RUNTIME_HASH
    )
    reward = next(fact for fact in facts if fact.fact_id == "reward_target_unknown")
    assert reward.value()["card_match_key"] == "CARD-A"
    assert reward.runtime_input_fingerprint == RUNTIME_HASH


def test_non_action_summary_snapshot_yields_no_facts():
    assert observe_current_action_summary_page_facts(
        visual(page_state="HOME_READY"), runtime_input_fingerprint=RUNTIME_HASH
    ) == ()


def test_planner_resolves_three_config_facts_but_stays_blocked():
    parsed = config()
    facts = observe_action_summary_policy_config(parsed)
    requested = tuple(
        request(fact_id, priority=index)
        for index, fact_id in enumerate(
            (
                "objective_missing",
                "strategy_identity_missing",
                "strategy_provenance_missing",
                "remaining_attempts_unknown",
                "resource_identity_unknown",
                "resource_balance_unknown",
                "reward_target_unknown",
                "fatigue_budget_unknown",
            ),
            start=1,
        )
    )
    plan = build_missing_fact_acquisition_plan(
        requested,
        observations=facts,
        policy_fingerprint=parsed.config_fingerprint,
        runtime_input_fingerprint=RUNTIME_HASH,
        generated_at=NOW,
    )
    assert plan.status is AcquisitionPlanStatus.PASS
    assert len(plan.resolved_facts) == 3
    assert plan.policy_evaluation_still_blocked is True
    assert plan.requires_level_3 == ("remaining_attempts_unknown",)
    assert plan.page_input_required_facts == ("remaining_attempts_unknown",)
    assert set(plan.ready_level_1) == {
        "resource_identity_unknown",
        "resource_balance_unknown",
        "reward_target_unknown",
    }


def test_stale_observation_is_not_ready():
    parsed = config()
    stale = replace(
        observe_action_summary_policy_config(parsed)[0],
        valid_until="2026-07-26T12:10:00+08:00",
    )
    plan = build_missing_fact_acquisition_plan(
        (request("objective_missing"),),
        observations=(stale,),
        policy_fingerprint=parsed.config_fingerprint,
        runtime_input_fingerprint=RUNTIME_HASH,
        generated_at=NOW,
    )
    assert plan.resolved_facts == ()
    assert plan.unresolved_facts == ("objective_missing",)


def test_mismatched_policy_fingerprint_is_not_ready():
    parsed = config()
    fact = replace(
        observe_action_summary_policy_config(parsed)[0],
        policy_fingerprint=POLICY_HASH,
    )
    plan = build_missing_fact_acquisition_plan(
        (request("objective_missing"),),
        observations=(fact,),
        policy_fingerprint=parsed.config_fingerprint,
        runtime_input_fingerprint=RUNTIME_HASH,
        generated_at=NOW,
    )
    assert plan.resolved_facts == ()


def test_unknown_fact_is_level_four_unavailable():
    plan = build_missing_fact_acquisition_plan(
        (request("future_unknown_fact"),),
        policy_fingerprint=POLICY_HASH,
        runtime_input_fingerprint=RUNTIME_HASH,
        generated_at=NOW,
    )
    assert plan.unavailable_facts == ("future_unknown_fact",)
    assert plan.requests[0].enabled is False


def test_plan_never_grants_authority_or_dispatches():
    plan = build_missing_fact_acquisition_plan(
        (request("resource_identity_unknown"),),
        policy_fingerprint=POLICY_HASH,
        runtime_input_fingerprint=RUNTIME_HASH,
        generated_at=NOW,
    )
    assert plan.execution_authorized is False
    assert plan.authorization_issued is False
    assert plan.capture_calls == 0
    assert plan.page_input_dispatches == 0
    assert plan.business_dispatches == 0
    assert plan.irreversible_actions == 0


def test_plan_is_immutable_and_serializable():
    plan = build_missing_fact_acquisition_plan(
        (request("remaining_attempts_unknown"),),
        policy_fingerprint=POLICY_HASH,
        runtime_input_fingerprint=RUNTIME_HASH,
        generated_at=NOW,
    )
    with pytest.raises(FrozenInstanceError):
        plan.status = AcquisitionPlanStatus.PASS
    document = plan.to_dict()
    assert document["schema_version"] == "1.0"
    assert document["requests"][0]["source_level"] == (
        "LEVEL_3_REVERSIBLE_DETAIL_OBSERVATION"
    )


def test_planner_does_not_invoke_observers(monkeypatch):
    import core.services.action_summary_missing_fact_acquisition as acquisition

    monkeypatch.setattr(
        acquisition,
        "observe_action_summary_policy_config",
        lambda *_args, **_kwargs: pytest.fail("observer must not run"),
    )
    build_missing_fact_acquisition_plan(
        (request("objective_missing"),),
        policy_fingerprint=POLICY_HASH,
        runtime_input_fingerprint=RUNTIME_HASH,
        generated_at=NOW,
    )


def test_module_has_no_backend_executor_or_legacy_imports():
    source = Path(
        "core/services/action_summary_missing_fact_acquisition.py"
    ).read_text(encoding="utf-8")
    for forbidden in (
        "import auto",
        "from auto",
        "ADB",
        "NEMU",
        "ActionSummaryExecutionInterlock",
    ):
        assert forbidden not in source
