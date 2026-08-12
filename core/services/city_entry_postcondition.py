"""Shared, side-effect-free postcondition policy for city entry.

The exact page classifier remains authoritative for the canonical leaf state.
This module only answers whether that leaf, together with fresh transition
evidence, proves the higher-level ``CITY_CONTEXT_VISIBLE`` capability.
"""

from __future__ import annotations

from dataclasses import dataclass
CITY_CONTEXT_VISIBLE = "CITY_CONTEXT_VISIBLE"
CITY_ENTRY_VERIFIED = "CITY_ENTRY_VERIFIED"
POSTCONDITION_TAXONOMY_MISMATCH = "POSTCONDITION_TAXONOMY_MISMATCH"


def _state_value(state: object) -> str:
    value = str(getattr(state, "value", state))
    return {
        "CITY_DETAIL": "CITY_DETAIL_VISIBLE",
    }.get(value, value)


@dataclass(frozen=True, slots=True)
class CityEntryPostconditionDecision:
    policy_id: str
    post_canonical_leaf_state: str
    post_context_state: str
    required_capability: str
    city_entry_verified: bool
    exact_expected_leaf_match: bool
    frame_is_fresh: bool
    frame_changed: bool
    evidence_invariant_check: str
    reason_codes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CityEntryPostconditionPolicy:
    policy_id: str
    accepted_leaf_states: frozenset[str]
    required_capability: str
    forbidden_states: frozenset[str]
    evidence_requirements: tuple[str, ...]
    reason_codes: tuple[str, ...]

    def evaluate(
        self,
        canonical_leaf_state: object,
        *,
        frame_is_fresh: bool,
        frame_changed: bool,
        evidence_invariant_check: str,
        exact_expected_leaf_state: object = "CITY_DETAIL_VISIBLE",
    ) -> CityEntryPostconditionDecision:
        leaf = _state_value(canonical_leaf_state)
        expected = _state_value(exact_expected_leaf_state)
        invariant = str(evidence_invariant_check)
        trusted_leaf = leaf in self.accepted_leaf_states
        forbidden = leaf in self.forbidden_states
        verified = bool(
            trusted_leaf
            and not forbidden
            and frame_is_fresh
            and frame_changed
            and invariant == "PASS"
        )
        reasons: list[str] = []
        if forbidden:
            reasons.append("forbidden_city_entry_leaf_state")
        elif not trusted_leaf:
            reasons.append("untrusted_city_entry_leaf_state")
        if not frame_is_fresh:
            reasons.append("post_frame_not_fresh")
        if not frame_changed:
            reasons.append("post_frame_unchanged")
        if invariant != "PASS":
            reasons.append("evidence_invariant_failed")
        if verified:
            reasons.extend(self.reason_codes)
        return CityEntryPostconditionDecision(
            policy_id=self.policy_id,
            post_canonical_leaf_state=leaf,
            post_context_state=(CITY_CONTEXT_VISIBLE if trusted_leaf else "UNKNOWN"),
            required_capability=self.required_capability,
            city_entry_verified=verified,
            exact_expected_leaf_match=leaf == expected,
            frame_is_fresh=bool(frame_is_fresh),
            frame_changed=bool(frame_changed),
            evidence_invariant_check=invariant,
            reason_codes=tuple(dict.fromkeys(reasons)),
        )


CITY_ENTRY_POSTCONDITION_POLICY = CityEntryPostconditionPolicy(
    policy_id="CITY_ENTRY_TRUSTED_CONTEXT_V1",
    accepted_leaf_states=frozenset({
        "CITY_DETAIL_VISIBLE",
        "EXCHANGE_NPC_VISIBLE",
    }),
    required_capability=CITY_CONTEXT_VISIBLE,
    forbidden_states=frozenset({
        "HOME_READY",
        "HOME_CITY_ENTRY_CONTROL_VISIBLE",
        "CITY_ENTRY_VISIBLE",
        "UNKNOWN",
        "INVENTORY_PAGE_VISIBLE",
        "MAILBOX_VISIBLE",
        "ACTION_SUMMARY_VISIBLE",
        "LOGIN_PAGE",
    }),
    evidence_requirements=(
        "fresh_post_frame",
        "frame_changed",
        "evidence_invariant_pass",
        "trusted_canonical_leaf_state",
    ),
    reason_codes=(
        "trusted_city_entry_leaf_state",
        "city_context_visible",
        "city_entry_verified",
    ),
)


@dataclass(frozen=True, slots=True)
class HistoricalCityEntryInterpretation:
    historical_status: str
    historical_status_preserved: bool
    block_classification: str
    decision: CityEntryPostconditionDecision


def interpret_historical_gate_result(
    *,
    historical_status: str,
    post_canonical_leaf_state: object,
    expected_leaf_state: object,
    frame_is_fresh: bool,
    frame_changed: bool,
    evidence_invariant_check: str,
    policy: CityEntryPostconditionPolicy = CITY_ENTRY_POSTCONDITION_POLICY,
) -> HistoricalCityEntryInterpretation:
    """Explain an immutable historical result without rewriting its status."""

    decision = policy.evaluate(
        post_canonical_leaf_state,
        frame_is_fresh=frame_is_fresh,
        frame_changed=frame_changed,
        evidence_invariant_check=evidence_invariant_check,
        exact_expected_leaf_state=expected_leaf_state,
    )
    taxonomy_mismatch = bool(
        str(historical_status) == "BLOCKED"
        and decision.city_entry_verified
        and not decision.exact_expected_leaf_match
    )
    return HistoricalCityEntryInterpretation(
        historical_status=str(historical_status),
        historical_status_preserved=True,
        block_classification=(
            POSTCONDITION_TAXONOMY_MISMATCH
            if taxonomy_mismatch else "NOT_APPLICABLE"
        ),
        decision=decision,
    )


def accepted_city_entry_leaf_states(
    policy: CityEntryPostconditionPolicy = CITY_ENTRY_POSTCONDITION_POLICY,
) -> frozenset[str]:
    return policy.accepted_leaf_states


__all__ = [
    "CITY_CONTEXT_VISIBLE",
    "CITY_ENTRY_POSTCONDITION_POLICY",
    "CITY_ENTRY_VERIFIED",
    "POSTCONDITION_TAXONOMY_MISMATCH",
    "CityEntryPostconditionDecision",
    "CityEntryPostconditionPolicy",
    "HistoricalCityEntryInterpretation",
    "accepted_city_entry_leaf_states",
    "interpret_historical_gate_result",
]
