"""Narrow business policy for a single, verified shop confirmation.

This module deliberately does not relax ``ReadOnlyActionGuard``.  Business
sessions are separately registered by ``core.control`` and their issuer owns
the allow-list; V1 contains exactly ``shop_confirm``.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from types import MappingProxyType
from typing import Callable

from core.services.read_only_policy import (
    PageObservation,
    ReadOnlyActionGuard,
    ReadOnlyPermitIssuer,
    ReadOnlyPolicySpec,
)


@dataclass(frozen=True)
class BusinessActionSnapshot:
    """Immutable facts independently revalidated before the final tap."""

    item_id: str
    shop_id: str
    currency: str
    quantity_mode: str
    quantity: int
    total_cost: int
    catalog_digest: str
    plan_digest: str
    ledger_digest: str


BUSINESS_POLICY_SPECS = MappingProxyType({
    "shop_confirm": ReadOnlyPolicySpec(
        action_key="shop_confirm",
        allowed_page_types=frozenset({"shop_quantity_dialog"}),
        anchor_id="shop_confirm_button",
        required_markers=("shop_quantity_dialog",),
        # Static region is a current-frame calibrated control container, not a
        # coordinate fallback.  The resolver must find it uniquely on both
        # independent source frames before dispatch.
        allowed_region=(800, 480, 1120, 590),
        postcondition="shop_confirmation_resolved",
        allowed_post_page_types=frozenset({"shop_purchase_result"}),
        postcondition_attempts=3,
        postcondition_interval_seconds=0.5,
    ),
})
BUSINESS_POLICY_REVISION = hashlib.sha256(
    repr(tuple(sorted(BUSINESS_POLICY_SPECS.items()))).encode("utf-8")
).hexdigest()[:16]


class BusinessPermitIssuer(ReadOnlyPermitIssuer):
    """The proven permit mechanics plus an independently trusted context gate."""

    def __init__(self, *args, snapshot: BusinessActionSnapshot,
                 context_validator: Callable[[PageObservation, BusinessActionSnapshot], None],
                 **kwargs):
        super().__init__(*args, policies=BUSINESS_POLICY_SPECS, **kwargs)
        self._snapshot = snapshot
        self._context_validator = context_validator

    def _validate_spec(self, spec, observation, requested_target, trajectory):
        result = super()._validate_spec(spec, observation, requested_target, trajectory)
        # Called at both issuance and fresh consume.  Caller claims never enter
        # this validation path.
        self._context_validator(observation, self._snapshot)
        return result


class BusinessActionSession(ReadOnlyActionGuard):
    """A sealed session whose business authority is the issuer allow-list."""

    BLOCKED_ACTIONS = frozenset()

    def report(self) -> dict:
        result = super().report()
        result["mode"] = "BUSINESS"
        return result


class ProductionBusinessActionSession(BusinessActionSession):
    """Production-shaped session; control registration remains mandatory."""


__all__ = [
    "BUSINESS_POLICY_REVISION", "BUSINESS_POLICY_SPECS", "BusinessActionSession",
    "BusinessActionSnapshot", "BusinessPermitIssuer", "ProductionBusinessActionSession",
]
