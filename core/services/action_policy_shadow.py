"""Metadata-only shadow journal for legacy (policy-less) physical input.

Stage 0 of the F-02 action-policy activation plan.  When the operator sets
``HEIYUE_ACTION_POLICY_SHADOW=1`` this module appends one JSON line per legacy
dispatch *attempt* immediately before the physical call, classifying what the
read-only policy gate would have decided.  It never blocks, delays, or alters
a dispatch, never captures a frame, never issues a permit, and never runs a
postcondition check.

Privacy contract: rows contain action-key/coordinate metadata only — no OCR
text, no screenshots, no frame hashes, no account, asset, or reward content.
``caller_hint`` records only repository-relative dotted module names, never
filesystem paths.
"""

from __future__ import annotations

import json
import os
import sys
import threading
from datetime import datetime

from loguru import logger


SHADOW_ENV_VAR = "HEIYUE_ACTION_POLICY_SHADOW"
SHADOW_DIR_ENV_VAR = "HEIYUE_ACTION_POLICY_SHADOW_DIR"
DEFAULT_SHADOW_DIR = os.path.join("logs", "action_policy_shadow")
MAX_JOURNAL_BYTES = 64 * 1024 * 1024

BLOCKED_TIER_NO_INTENT = "NO_INTENT"
BLOCKED_TIER_NO_SPEC = "NO_SPEC"
BLOCKED_TIER_BUSINESS = "BLOCKED_BUSINESS"
BLOCKED_TIER_READ_ONLY = "READ_ONLY_CANDIDATE"

_VERDICTS = {
    BLOCKED_TIER_NO_INTENT: "WOULD_DENY_NO_INTENT",
    BLOCKED_TIER_NO_SPEC: "WOULD_DENY_NO_SPEC",
    BLOCKED_TIER_BUSINESS: "WOULD_DENY_BLOCKED",
    BLOCKED_TIER_READ_ONLY: "WOULD_AUTHORIZE_PENDING_OBSERVATION",
}

_WRITE_LOCK = threading.Lock()
_NOTICE_LOCK = threading.Lock()
_ENABLED_ANNOUNCED = False
_WRITE_FAILURE_WARNED = False
_SIZE_CAP_WARNED = False
_WRITE_DISABLED = False


def shadow_enabled(environ=None) -> bool:
    values = os.environ if environ is None else environ
    return str(values.get(SHADOW_ENV_VAR, "")).strip() in {"1", "2"}


def _policy_vocabulary():
    """Lazy import to avoid a module-level control<->policy import cycle."""

    from core.services.read_only_policy import PRODUCTION_POLICY_SPECS
    from core.services.read_only_policy import ReadOnlyActionGuard

    return PRODUCTION_POLICY_SPECS, ReadOnlyActionGuard.BLOCKED_ACTIONS


def classify_shadow_verdict(intent) -> tuple[str, str]:
    """Classify one intent against the gate vocabulary; no observation is run."""

    action_key = str(getattr(intent, "action_key", "") or "")
    if intent is None or not action_key:
        tier = BLOCKED_TIER_NO_INTENT
    else:
        specs, blocked = _policy_vocabulary()
        if action_key in blocked:
            tier = BLOCKED_TIER_BUSINESS
        elif action_key in specs:
            tier = BLOCKED_TIER_READ_ONLY
        else:
            tier = BLOCKED_TIER_NO_SPEC
    return tier, _VERDICTS[tier]


def _caller_hint() -> str:
    """Dotted module:function:line of the nearest non-infrastructure caller."""

    frame = sys._getframe(1)
    skipped_prefixes = (__name__, "core.control.control")
    while frame is not None:
        module = str(frame.f_globals.get("__name__", ""))
        if module and not module.startswith(skipped_prefixes):
            return f"{module}:{frame.f_code.co_name}:{frame.f_lineno}"
        frame = frame.f_back
    return "unknown"


def _journal_path(now: datetime) -> str:
    root = os.environ.get(SHADOW_DIR_ENV_VAR, "").strip() or DEFAULT_SHADOW_DIR
    return os.path.join(root, f"{now:%Y%m%d}.jsonl")


def record_shadow_dispatch(
    *,
    api: str,
    intent,
    logical_points,
    random_offset: bool | None = None,
    duration_ms: int | None = None,
) -> None:
    """Append one shadow row; every failure is contained and warned once."""

    global _ENABLED_ANNOUNCED, _WRITE_FAILURE_WARNED, _SIZE_CAP_WARNED
    global _WRITE_DISABLED
    if not shadow_enabled() or _WRITE_DISABLED:
        return
    try:
        tier, verdict = classify_shadow_verdict(intent)
        now = datetime.now().astimezone()
        row = {
            "ts": now.isoformat(timespec="milliseconds"),
            "pid": os.getpid(),
            "queue_run_id": None,
            "api": str(api),
            "logical_points": [
                [int(point[0]), int(point[1])] for point in logical_points
            ],
            "random_offset": random_offset,
            "duration_ms": duration_ms,
            "intent_present": intent is not None,
            "action_key": str(getattr(intent, "action_key", "") or ""),
            "anchor_key": str(getattr(intent, "requested_target", "") or ""),
            "correlation_id": str(getattr(intent, "correlation_id", "") or ""),
            "spec_exists": tier == BLOCKED_TIER_READ_ONLY,
            "blocked_tier": tier,
            "would_verdict": verdict,
            "caller_hint": _caller_hint(),
        }
        line = json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n"
        path = _journal_path(now)
        with _WRITE_LOCK:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            try:
                current_size = os.path.getsize(path)
            except OSError:
                current_size = 0  # file does not exist yet
            # Hard cap: the file must never exceed MAX_JOURNAL_BYTES, so the
            # candidate line's own size is part of the check.
            if current_size + len(line.encode("utf-8")) > MAX_JOURNAL_BYTES:
                with _NOTICE_LOCK:
                    first_cap_notice = not _SIZE_CAP_WARNED
                    _SIZE_CAP_WARNED = True
                if first_cap_notice:
                    logger.warning(
                        "action policy shadow journal reached its size cap; "
                        "appends suspended for this file"
                    )
                return
            with open(path, "a", encoding="utf-8") as stream:
                stream.write(line)
        with _NOTICE_LOCK:
            first_enable_notice = not _ENABLED_ANNOUNCED
            _ENABLED_ANNOUNCED = True
        if first_enable_notice:
            logger.info(
                "action policy shadow journal enabled (metadata only, "
                "no enforcement)"
            )
    except Exception as error:  # noqa: BLE001 - must never block a dispatch
        with _NOTICE_LOCK:
            first_failure_notice = not _WRITE_FAILURE_WARNED
            _WRITE_FAILURE_WARNED = True
            _WRITE_DISABLED = True
        if first_failure_notice:
            logger.warning(
                "action policy shadow journal disabled after write failure: "
                f"{type(error).__name__}"
            )


__all__ = [
    "BLOCKED_TIER_BUSINESS",
    "BLOCKED_TIER_NO_INTENT",
    "BLOCKED_TIER_NO_SPEC",
    "BLOCKED_TIER_READ_ONLY",
    "MAX_JOURNAL_BYTES",
    "SHADOW_DIR_ENV_VAR",
    "SHADOW_ENV_VAR",
    "classify_shadow_verdict",
    "record_shadow_dispatch",
    "shadow_enabled",
]
