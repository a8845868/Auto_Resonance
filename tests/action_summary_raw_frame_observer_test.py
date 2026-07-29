from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from core.services.action_summary_advisory_policy import FactAcquisitionRequest
from core.services.action_summary_missing_fact_acquisition import (
    FACT_ACQUISITION_CONTRACTS,
    CurrentActionSummaryVisualSnapshot,
    ObserverImplementationStatus,
    build_missing_fact_acquisition_plan,
    observe_current_action_summary_page_facts,
    validate_acquired_fact,
    validate_contract_registry,
)
from core.services.action_summary_product_model import observe_action_summary_page
from core.services.action_summary_raw_frame_observer import (
    RAW_OBSERVER_CALLABLE_ID,
    RAW_OBSERVER_ID,
    normalize_raw_action_summary_visual_observation,
    observe_action_summary_current_page_visuals,
    raw_observation_fingerprint,
)
from tests.action_summary_product_model_test import Frame, item, trusted_items
from tools.action_summary_current_page_raw_frame_observer_live_gate import (
    _SingleOcrFrame,
    _blocked,
)


FRAME_HASH = "a" * 64
OTHER_HASH = "b" * 64
POLICY_HASH = "c" * 64
RUNTIME_HASH = "d" * 64
CAPTURED_AT = datetime(2026, 7, 29, 10, 0, tzinfo=timezone.utc)


def frame(
    labels: list[dict] | None = None,
    *,
    capture_id: str | None = "RAW-CAPTURE-1",
    frame_hash: str = FRAME_HASH,
    captured_at: object = CAPTURED_AT,
) -> Frame:
    return Frame(
        labels or trusted_items(),
        capture_id=capture_id,
        raw_frame_hash=frame_hash,
        captured_at=captured_at,
    )


def request(fact_id: str) -> FactAcquisitionRequest:
    return FactAcquisitionRequest(
        missing_fact=fact_id,
        required_scope="TEST",
        preferred_source="raw frame",
        requires_navigation=None,
        requires_page_input=False,
        requires_business_input=False,
        priority=1,
        reason="raw observer regression",
    )


def test_three_costs_bind_to_three_fresh_card_match_keys():
    observation = observe_action_summary_current_page_visuals(frame())

    assert observation.observation_status == "PASS"
    assert [value.numeric_value for value in observation.resource_cost_observations] == [
        40,
        40,
        40,
    ]
    assert len({value.card_match_key for value in observation.resource_cost_observations}) == 3
    assert all(value.semantic_id == "UNKNOWN" for value in observation.resource_cost_observations)
    assert all(value.bbox for value in observation.resource_cost_observations)


def test_page_level_negative_is_rejected_without_card_binding():
    observation = observe_action_summary_current_page_visuals(
        frame(trusted_items() + [item("-999", 80, 120, 50, 20)])
    )

    assert len(observation.resource_cost_observations) == 3
    assert any(
        value.reason_codes == ("UNBOUND_PAGE_LEVEL_NEGATIVE",)
        for value in observation.rejected_candidates
    )


def test_multiple_cost_candidates_in_one_card_are_ambiguous():
    observation = observe_action_summary_current_page_visuals(
        frame(trusted_items() + [item("-41", 580, 650, 55, 20)])
    )

    assert observation.observation_status == "PASS_WITH_AMBIGUOUS_FACTS"
    assert len(observation.resource_cost_observations) == 2
    assert len(observation.ambiguous_candidates) == 2
    assert {
        value.reason_codes for value in observation.ambiguous_candidates
    } == {("MULTIPLE_COST_CANDIDATES_FOR_CARD",)}


@pytest.mark.parametrize("text", ["-True", "--40", "-4.5", "+40", "-1e2"])
def test_malformed_or_non_integer_cost_text_never_becomes_a_fact(text):
    labels = [value for value in trusted_items() if value["text"] != "-40"]
    labels.append(item(text, 580, 636, 55, 20))
    observation = observe_action_summary_current_page_visuals(frame(labels))

    assert all(value.numeric_value == 40 for value in observation.resource_cost_observations)
    assert len(observation.resource_cost_observations) == 0


def test_missing_icon_and_name_leave_resource_identity_unknown():
    observation = observe_action_summary_current_page_visuals(frame())

    assert observation.resource_identity_observations == ()
    assert all(value.semantic_id == "UNKNOWN" for value in observation.resource_cost_observations)


def test_name_without_a_catalog_bound_icon_does_not_identify_resource():
    observation = observe_action_summary_current_page_visuals(
        frame(trusted_items() + [item("RESOURCE-NAME", 580, 660, 100, 20)])
    )

    assert observation.resource_identity_observations == ()


def test_existing_normalizer_requires_and_accepts_icon_plus_name_identity():
    snapshot = CurrentActionSummaryVisualSnapshot(
        capture_id="RAW-CAPTURE-1",
        frame_sha256=FRAME_HASH,
        captured_at=CAPTURED_AT.isoformat(),
        valid_until="2026-07-29T10:05:00+00:00",
        page_state="ACTION_SUMMARY_VISIBLE",
        card_match_key="CARD-A",
        resource_icon_id="CATALOG-ICON-A",
        resource_name="RESOURCE-A",
        displayed_resource_cost=40,
        evidence_ids=("strict-icon-name-contract",),
    )
    facts = observe_current_action_summary_page_facts(
        snapshot, runtime_input_fingerprint=RUNTIME_HASH
    )

    assert "resource_identity_unknown" in {value.fact_id for value in facts}


def test_cost_is_not_promoted_to_balance_or_sufficiency():
    observation = observe_action_summary_current_page_visuals(frame())

    assert observation.resource_balance_observations == ()
    serialized = observation.to_dict()
    assert "sufficient" not in str(serialized).lower()


def test_generic_page_reward_is_not_bound_to_every_card():
    observation = observe_action_summary_current_page_visuals(
        frame(trusted_items() + [item("REWARD-CATALOG", 90, 120, 120, 20)])
    )

    assert observation.reward_target_observations == ()


def test_existing_normalizer_accepts_only_card_bound_reward_identity():
    snapshot = CurrentActionSummaryVisualSnapshot(
        capture_id="RAW-CAPTURE-1",
        frame_sha256=FRAME_HASH,
        captured_at=CAPTURED_AT.isoformat(),
        valid_until="2026-07-29T10:05:00+00:00",
        page_state="ACTION_SUMMARY_VISIBLE",
        card_match_key="CARD-A",
        reward_icon_id="CATALOG-REWARD-A",
        reward_name="REWARD-A",
        evidence_ids=("strict-card-reward-contract",),
    )
    facts = observe_current_action_summary_page_facts(
        snapshot, runtime_input_fingerprint=RUNTIME_HASH
    )

    assert "reward_target_unknown" in {value.fact_id for value in facts}
    assert observe_current_action_summary_page_facts(
        replace(snapshot, card_match_key=None),
        runtime_input_fingerprint=RUNTIME_HASH,
    ) == ()


def test_duplicate_card_titles_remain_separated_by_match_key():
    observation = observe_action_summary_current_page_visuals(frame())
    keys = [value.card_match_key for value in observation.resource_cost_observations]

    assert len(keys) == len(set(keys)) == 3


def test_supplied_page_model_must_match_frame_hash():
    model = observe_action_summary_page(frame(frame_hash=OTHER_HASH))
    observation = observe_action_summary_current_page_visuals(frame(), page_model=model)

    assert observation.observation_status == "BLOCKED_FRAME_MISMATCH"
    assert observation.resource_cost_observations == ()


def test_supplied_page_model_must_match_capture_identity():
    model = observe_action_summary_page(frame(capture_id="OTHER-CAPTURE"))
    observation = observe_action_summary_current_page_visuals(frame(), page_model=model)

    assert observation.observation_status == "BLOCKED_FRAME_MISMATCH"


@pytest.mark.parametrize(
    ("capture_id", "frame_hash", "captured_at"),
    [
        (None, FRAME_HASH, CAPTURED_AT),
        ("RAW-CAPTURE-1", "not-a-sha", CAPTURED_AT),
        ("RAW-CAPTURE-1", FRAME_HASH, "2026-07-29T10:00:00"),
    ],
)
def test_invalid_source_provenance_blocks_observation(
    capture_id, frame_hash, captured_at
):
    observation = observe_action_summary_current_page_visuals(
        frame(
            capture_id=capture_id,
            frame_hash=frame_hash,
            captured_at=captured_at,
        )
    )

    assert observation.observation_status == "BLOCKED_SOURCE_PROVENANCE"
    assert observation.normalization_snapshots == ()


def test_observer_calls_ocr_once_and_never_calls_capture():
    class CountingFrame(Frame):
        def __init__(self):
            super().__init__(
                trusted_items(),
                capture_id="COUNTING-CAPTURE",
                raw_frame_hash=FRAME_HASH,
                captured_at=CAPTURED_AT,
            )
            self.calls = 0

        def ocr(self):
            self.calls += 1
            return super().ocr()

        def capture_frame(self):
            raise AssertionError("observer has no capture authority")

    supplied = CountingFrame()
    observation = observe_action_summary_current_page_visuals(supplied)

    assert supplied.calls == observation.ocr_calls == 1
    assert observation.capture_calls == 0


def test_live_gate_frame_wrapper_reuses_one_underlying_ocr_result():
    supplied = frame()
    supplied.calls = 0
    original = supplied.ocr

    def counted_ocr():
        supplied.calls += 1
        return original()

    supplied.ocr = counted_ocr
    cached = _SingleOcrFrame(supplied)

    assert cached.ocr() == cached.ocr()
    assert supplied.calls == cached.underlying_ocr_calls == 1


def test_live_gate_blocked_result_has_zero_page_and_business_authority():
    document = _blocked("test_precondition")

    assert document["LIVE_READ_ONLY_OBSERVATIONS"] == 0
    assert document["ACTION_SUMMARY_PAGE_INPUTS"] == 0
    assert document["BUSINESS_ACTIONS"] == 0
    assert document["IRREVERSIBLE_ACTIONS"] == 0
    assert document["POLICY_EVALUATOR_CALLED"] == "NO"
    assert document["EXECUTOR_CONNECTED"] == "NO"


def test_observer_has_no_input_persistence_execution_or_backend_imports():
    source = Path(
        "core/services/action_summary_raw_frame_observer.py"
    ).read_text(encoding="utf-8")
    for forbidden in (
        "from auto",
        "import auto",
        "ADB",
        "MuMu",
        ".tap(",
        ".swipe(",
        "capture_frame(",
        "Path(",
        "open(",
        "ActionSummaryExecutionInterlock",
    ):
        assert forbidden not in source


def test_raw_cost_normalizes_to_valid_unknown_identity_acquired_facts():
    observation = observe_action_summary_current_page_visuals(frame())
    facts = normalize_raw_action_summary_visual_observation(
        observation, runtime_input_fingerprint=RUNTIME_HASH
    )

    assert len(facts) == 3
    assert all(value.observer_id == RAW_OBSERVER_ID for value in facts)
    assert all(value.value()["resource_id"] == "UNKNOWN" for value in facts)
    assert all(
        validate_acquired_fact(
            value,
            FACT_ACQUISITION_CONTRACTS["resource_cost_unknown"],
            POLICY_HASH,
            RUNTIME_HASH,
            CAPTURED_AT.isoformat(),
        )
        is None
        for value in facts
    )


def test_resource_cost_contract_is_callable_not_normalizer_only():
    contract = FACT_ACQUISITION_CONTRACTS["resource_cost_unknown"]

    assert contract.observer_id == RAW_OBSERVER_ID
    assert contract.observer_callable_id == RAW_OBSERVER_CALLABLE_ID
    assert contract.observer_status is ObserverImplementationStatus.OFFLINE_PROVEN
    assert contract.normalizer_only is False
    assert validate_contract_registry() == ()


def test_unproven_visual_fact_contracts_remain_normalizer_only():
    for fact_id in (
        "resource_identity_unknown",
        "resource_balance_unknown",
        "reward_target_unknown",
    ):
        contract = FACT_ACQUISITION_CONTRACTS[fact_id]
        assert contract.observer_status is ObserverImplementationStatus.OFFLINE_PROVEN
        assert contract.normalizer_only is True


def test_remaining_attempt_level_three_contract_stays_disabled():
    contract = FACT_ACQUISITION_CONTRACTS["remaining_attempts_unknown"]

    assert contract.enabled is False
    assert contract.requires_page_input is True
    assert contract.observer_status is ObserverImplementationStatus.NOT_IMPLEMENTED


def test_acquisition_conflict_regression_remains_fail_closed():
    first = normalize_raw_action_summary_visual_observation(
        observe_action_summary_current_page_visuals(frame()),
        runtime_input_fingerprint=RUNTIME_HASH,
    )[0]
    changed_labels = [
        ({**value, "text": "-41"} if value["text"] == "-40" else value)
        for value in trusted_items()
    ]
    second = normalize_raw_action_summary_visual_observation(
        observe_action_summary_current_page_visuals(
            frame(changed_labels, capture_id="RAW-CAPTURE-2", frame_hash=OTHER_HASH)
        ),
        runtime_input_fingerprint=RUNTIME_HASH,
    )[0]
    plan = build_missing_fact_acquisition_plan(
        (request("resource_cost_unknown"),),
        observations=(first, second),
        policy_fingerprint=POLICY_HASH,
        runtime_input_fingerprint=RUNTIME_HASH,
        generated_at=CAPTURED_AT.isoformat(),
    )

    assert plan.resolved_facts == ()
    assert plan.conflicting_facts == ("resource_cost_unknown",)
    assert plan.policy_evaluation_still_blocked is True


def test_serialized_observation_has_hashes_and_no_raw_ocr_text():
    observation = observe_action_summary_current_page_visuals(frame())
    document = observation.to_dict()
    serialized = str(document)

    assert raw_observation_fingerprint(observation)
    assert document["source_frame_sha256"] == FRAME_HASH
    assert "-40" not in serialized
    assert "raw_ocr" not in serialized.lower()
    assert observation.page_input_dispatches == 0
    assert observation.business_dispatches == 0
    assert observation.irreversible_actions == 0
