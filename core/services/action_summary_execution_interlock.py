"""Default-deny execution contracts for the action-summary product flow.

This module is deliberately pure data and validation.  It cannot capture a
frame, issue an authorization, dispatch input, persist evidence, or retry.
Legacy execution remains outside this module and must be selected explicitly
by the caller.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from core.services.action_summary_product_model import (
    ActionSummaryDecision,
    ActionSummaryDecisionType,
    ActionSummaryPageModel,
)


class ActionSummaryExecutionMode(str, Enum):
    READ_ONLY = "READ_ONLY"
    POLICY_GATED = "POLICY_GATED"
    LEGACY_COMPATIBILITY = "LEGACY_COMPATIBILITY"


@dataclass(frozen=True, slots=True)
class ActionSummaryExecutionAuthorization:
    schema_version: str
    authorization_id: str
    model_scope: str
    activity_family: str
    model_freshness_token: str
    source_capture_id: str
    source_frame_sha256: str
    selected_card_match_key: str
    allowed_action: str
    max_dispatches: int
    issued_reason: str
    policy_id: str
    policy_version: str


@dataclass(frozen=True, slots=True)
class AuthorizationValidation:
    valid: bool
    reason: str
    matched_fresh_card_key: str | None = None


@dataclass(frozen=True, slots=True)
class ActionSummaryExecutionResult:
    success: bool
    terminal: bool
    execution_mode: ActionSummaryExecutionMode
    page_model: ActionSummaryPageModel
    decision: ActionSummaryDecision
    execution_status: str
    execution_authorized: bool
    authorization_valid: bool
    selected_card_match_key: str | None
    requested_action: str | None
    business_dispatches: int
    irreversible_actions: int
    reason: str
    reason_codes: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "success": self.success,
            "terminal": self.terminal,
            "execution_mode": self.execution_mode.value,
            "page_model": self.page_model.to_dict(),
            "decision": self.decision.to_dict(),
            "execution_status": self.execution_status,
            "execution_authorized": self.execution_authorized,
            "authorization_valid": self.authorization_valid,
            "selected_card_match_key": self.selected_card_match_key,
            "requested_action": self.requested_action,
            "business_dispatches": self.business_dispatches,
            "irreversible_actions": self.irreversible_actions,
            "reason": self.reason,
            "reason_codes": list(self.reason_codes),
        }


def validate_execution_authorization(
    authorization: ActionSummaryExecutionAuthorization | None,
    *,
    source_model: ActionSummaryPageModel,
    fresh_model: ActionSummaryPageModel | None,
    requested_action: str | None,
) -> AuthorizationValidation:
    """Validate contract binding but never grant execution in V1."""

    if authorization is None:
        return AuthorizationValidation(False, "execution_authorization_required")
    if source_model.activity_family == "UNKNOWN":
        return AuthorizationValidation(False, "authorization_activity_mismatch")
    if not source_model.model_freshness_token:
        return AuthorizationValidation(False, "authorization_stale")
    if authorization.model_scope != source_model.model_scope:
        return AuthorizationValidation(False, "authorization_model_mismatch")
    if authorization.model_freshness_token != source_model.model_freshness_token:
        return AuthorizationValidation(False, "authorization_stale")
    if authorization.source_capture_id != source_model.source_capture_id:
        return AuthorizationValidation(False, "authorization_model_mismatch")
    if authorization.source_frame_sha256 != source_model.source_frame_sha256:
        return AuthorizationValidation(False, "authorization_model_mismatch")
    if authorization.activity_family != source_model.activity_family:
        return AuthorizationValidation(False, "authorization_activity_mismatch")
    if not requested_action or authorization.allowed_action != requested_action:
        return AuthorizationValidation(False, "authorization_action_mismatch")
    if authorization.max_dispatches != 1:
        return AuthorizationValidation(False, "authorization_dispatch_limit_invalid")
    source_matches = [
        card
        for card in source_model.task_cards
        if card.card_match_key == authorization.selected_card_match_key
    ]
    if len(source_matches) != 1:
        return AuthorizationValidation(False, "authorization_model_mismatch")
    if fresh_model is None:
        return AuthorizationValidation(False, "fresh_model_required")
    if fresh_model.source_capture_id == source_model.source_capture_id:
        return AuthorizationValidation(False, "authorization_stale")
    if fresh_model.activity_family != authorization.activity_family:
        return AuthorizationValidation(False, "authorization_activity_mismatch")
    matches = [
        card
        for card in fresh_model.task_cards
        if card.card_match_key == authorization.selected_card_match_key
    ]
    if not matches:
        return AuthorizationValidation(False, "fresh_card_not_found")
    if len(matches) != 1:
        return AuthorizationValidation(False, "fresh_card_ambiguous")
    # No trusted issuer or execution policy exists in V1.  Passing structural
    # binding therefore still cannot become physical-input authority.
    return AuthorizationValidation(
        False,
        "execution_authority_not_implemented",
        matches[0].card_match_key,
    )


def evaluate_execution_interlock(
    model: ActionSummaryPageModel,
    decision: ActionSummaryDecision,
    *,
    mode: ActionSummaryExecutionMode,
    authorization: ActionSummaryExecutionAuthorization | None = None,
    requested_action: str | None = None,
    fresh_model: ActionSummaryPageModel | None = None,
) -> ActionSummaryExecutionResult:
    """Return a terminal recommendation without dispatching business input."""

    def result(
        *,
        success: bool,
        status: str,
        reason: str,
        reason_codes: tuple[str, ...],
        validation: AuthorizationValidation | None = None,
    ) -> ActionSummaryExecutionResult:
        return ActionSummaryExecutionResult(
            success=success,
            terminal=True,
            execution_mode=mode,
            page_model=model,
            decision=decision,
            execution_status=status,
            execution_authorized=False,
            authorization_valid=bool(validation and validation.valid),
            selected_card_match_key=(
                validation.matched_fresh_card_key if validation else None
            ),
            requested_action=requested_action,
            business_dispatches=0,
            irreversible_actions=0,
            reason=reason,
            reason_codes=reason_codes,
        )

    if mode is ActionSummaryExecutionMode.LEGACY_COMPATIBILITY:
        return result(
            success=False,
            status="BLOCKED",
            reason="legacy_compatibility_requires_explicit_legacy_route",
            reason_codes=("legacy_compatibility_disabled_in_interlock",),
        )
    if decision.decision is ActionSummaryDecisionType.TASK_AVAILABLE_NEEDS_POLICY:
        return result(
            success=False,
            status="BLOCKED",
            reason="business_policy_required",
            reason_codes=("business_policy_not_started",),
        )
    if decision.decision is ActionSummaryDecisionType.COMPLETED_REWARD_AVAILABLE:
        return result(
            success=False,
            status="BLOCKED",
            reason="business_policy_required",
            reason_codes=("reward_policy_not_started",),
        )
    if mode is ActionSummaryExecutionMode.POLICY_GATED:
        validation = validate_execution_authorization(
            authorization,
            source_model=model,
            fresh_model=fresh_model,
            requested_action=requested_action,
        )
        return result(
            success=False,
            status="BLOCKED",
            reason=validation.reason,
            reason_codes=(validation.reason,),
            validation=validation,
        )
    if decision.decision in {
        ActionSummaryDecisionType.AMBIGUOUS_PAGE,
        ActionSummaryDecisionType.UNSUPPORTED_TASK,
        ActionSummaryDecisionType.UNKNOWN,
    }:
        return result(
            success=False,
            status="BLOCKED",
            reason="read_only_page_not_actionable",
            reason_codes=decision.reason_codes,
        )
    return result(
        success=True,
        status="READ_ONLY_COMPLETE",
        reason="read_only_complete",
        reason_codes=decision.reason_codes,
    )


__all__ = [
    "ActionSummaryExecutionAuthorization",
    "ActionSummaryExecutionMode",
    "ActionSummaryExecutionResult",
    "AuthorizationValidation",
    "evaluate_execution_interlock",
    "validate_execution_authorization",
]
