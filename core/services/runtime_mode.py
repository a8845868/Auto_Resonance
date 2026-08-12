"""Operating-mode policy for the personal game automation runtime."""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum
from typing import Mapping


class RuntimeMode(str, Enum):
    PERSONAL_AUTOMATION = "PERSONAL_AUTOMATION"
    DEBUG = "DEBUG"
    AUDIT = "AUDIT"


DEFAULT_RUNTIME_MODE = RuntimeMode.PERSONAL_AUTOMATION


@dataclass(frozen=True)
class RuntimeModePolicy:
    mode: RuntimeMode
    authority_required: bool
    baseline_required: bool
    approval_required: bool
    occurrence_required: bool
    immutable_tree_required: bool
    persistent_ledger_required: bool
    maximum_enter_city_attempts: int
    accepted_capture_media_types: tuple[str, ...] = ("image/png", "image/jpeg")


_POLICIES = {
    RuntimeMode.PERSONAL_AUTOMATION: RuntimeModePolicy(
        mode=RuntimeMode.PERSONAL_AUTOMATION,
        authority_required=False,
        baseline_required=False,
        approval_required=False,
        occurrence_required=False,
        immutable_tree_required=False,
        persistent_ledger_required=False,
        maximum_enter_city_attempts=2,
    ),
    RuntimeMode.DEBUG: RuntimeModePolicy(
        mode=RuntimeMode.DEBUG,
        authority_required=False,
        baseline_required=False,
        approval_required=False,
        occurrence_required=False,
        immutable_tree_required=False,
        persistent_ledger_required=False,
        maximum_enter_city_attempts=2,
    ),
    RuntimeMode.AUDIT: RuntimeModePolicy(
        mode=RuntimeMode.AUDIT,
        authority_required=True,
        baseline_required=True,
        approval_required=True,
        occurrence_required=True,
        immutable_tree_required=True,
        persistent_ledger_required=True,
        maximum_enter_city_attempts=1,
    ),
}


def resolve_runtime_mode(
    value: RuntimeMode | str | None = None,
    *,
    environment: Mapping[str, str] | None = None,
) -> RuntimeMode:
    if isinstance(value, RuntimeMode):
        return value
    raw = value
    if raw is None:
        raw = (environment if environment is not None else os.environ).get(
            "HEIYUE_RUNTIME_MODE",
            DEFAULT_RUNTIME_MODE.value,
        )
    try:
        return RuntimeMode(str(raw).strip().upper())
    except ValueError as exc:
        raise ValueError(f"runtime_mode_invalid:{raw}") from exc


def runtime_policy(
    value: RuntimeMode | str | None = None,
    *,
    environment: Mapping[str, str] | None = None,
) -> RuntimeModePolicy:
    return _POLICIES[resolve_runtime_mode(value, environment=environment)]


__all__ = [
    "DEFAULT_RUNTIME_MODE",
    "RuntimeMode",
    "RuntimeModePolicy",
    "resolve_runtime_mode",
    "runtime_policy",
]
