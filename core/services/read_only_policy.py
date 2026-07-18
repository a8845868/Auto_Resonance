"""Runtime action boundary for evidence-only emulator probes.

The hardware input surface is fail-closed while a guard is installed.  Safe
navigation must carry a short-lived, page-bound intent/permit; raw coordinates
never inherit safety from their location.
"""

from __future__ import annotations

import hashlib
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import Callable, Iterator


def _now() -> datetime:
    return datetime.now().astimezone()


@dataclass(frozen=True)
class ActionIntent:
    action_key: str
    page_id: str
    anchor_key: str
    coordinate: tuple[int, int] | None = None
    bounded_region: tuple[int, int, int, int] | None = None
    page_fingerprint: str = ""
    correlation_id: str = ""
    ttl_seconds: float = 5.0
    max_uses: int = 1


@dataclass
class ReadOnlyPermit:
    action_key: str
    page_id: str
    page_fingerprint: str
    anchor_key: str
    coordinate: tuple[int, int] | None
    bounded_region: tuple[int, int, int, int] | None
    issued_at: datetime
    expires_at: datetime
    max_uses: int
    correlation_id: str
    uses: int = 0


@dataclass(frozen=True)
class ActionJournalEntry:
    timestamp: str
    action_key: str
    page_context: str
    coordinate: tuple[int, int] | None
    allowed: bool
    reason: str
    correlation_id: str = ""
    page_id: str = ""
    page_fingerprint: str = ""
    anchor_key: str = ""


class ReadOnlyActionGuard:
    ALLOWED_ACTIONS = {
        "back", "open_tab", "scroll", "exchange_buy_anchor",
        "exchange_sell_anchor", "open_detail", "navigation_anchor",
    }
    BLOCKED_ACTIONS = {
        "transaction_buy", "transaction_sell", "all_buy", "all_sell",
        "haggle_confirm", "raise_price_confirm", "use_item", "use_book",
        "reward_claim", "fatigue_confirm", "bento_confirm", "depart",
        "account_settings",
    }

    def __init__(
        self,
        hardware_tap: Callable[[tuple[int, int]], object] | None = None,
        *,
        context_provider: Callable[[], str] | None = None,
    ):
        self.hardware_tap = hardware_tap
        self.context_provider = context_provider
        self._journal: list[ActionJournalEntry] = []

    @property
    def journal(self) -> tuple[ActionJournalEntry, ...]:
        return tuple(self._journal)

    @property
    def blocked_actions(self) -> tuple[str, ...]:
        return tuple(entry.action_key for entry in self._journal if not entry.allowed)

    def _decision(self, action_key: str) -> tuple[bool, str]:
        key = str(action_key)
        if key in self.BLOCKED_ACTIONS:
            return False, "action_prohibited_in_read_only_mode"
        if key not in self.ALLOWED_ACTIONS:
            return False, "action_not_allowlisted"
        return True, "allowlisted_read_only_action"

    @staticmethod
    def fingerprint(page_context: str) -> str:
        return hashlib.sha256(str(page_context).encode("utf-8")).hexdigest()[:16]

    def _context(self, supplied: str = "") -> str:
        if supplied:
            return str(supplied)
        if self.context_provider is None:
            return ""
        try:
            return str(self.context_provider())
        except Exception as error:
            return f"context_error:{type(error).__name__}"

    def _record(
        self,
        action_key: str,
        coordinate: tuple[int, int] | None,
        page_context: str,
        *,
        allowed: bool | None = None,
        reason: str = "",
        permit: ReadOnlyPermit | None = None,
    ) -> bool:
        if allowed is None:
            allowed, reason = self._decision(action_key)
        self._journal.append(ActionJournalEntry(
            timestamp=_now().isoformat(timespec="milliseconds"),
            action_key=str(action_key), page_context=str(page_context)[:500],
            coordinate=(int(coordinate[0]), int(coordinate[1])) if coordinate else None,
            allowed=bool(allowed), reason=str(reason),
            correlation_id=permit.correlation_id if permit else "",
            page_id=permit.page_id if permit else "",
            page_fingerprint=permit.page_fingerprint if permit else "",
            anchor_key=permit.anchor_key if permit else "",
        ))
        return bool(allowed)

    def tap(self, action_key: str, coordinate: tuple[int, int], page_context: str = "") -> bool:
        """Exercise an explicit policy action (used by canaries and unit tests)."""
        allowed = self._record(action_key, coordinate, page_context)
        if allowed and self.hardware_tap is not None:
            self.hardware_tap(coordinate)
        return allowed

    def issue_permit(
        self,
        *,
        action_key: str,
        page_id: str,
        page_fingerprint: str,
        anchor_key: str,
        coordinate: tuple[int, int] | None = None,
        bounded_region: tuple[int, int, int, int] | None = None,
        ttl: timedelta = timedelta(seconds=5),
        max_uses: int = 1,
        correlation_id: str = "",
    ) -> ReadOnlyPermit:
        allowed, reason = self._decision(action_key)
        if not allowed:
            raise PermissionError(reason)
        if not page_id or not page_fingerprint or not anchor_key:
            raise ValueError("read-only permit requires page identity, fingerprint and OCR anchor")
        if coordinate is None and bounded_region is None:
            raise ValueError("read-only permit requires a coordinate or bounded region")
        issued = _now()
        return ReadOnlyPermit(
            str(action_key), str(page_id), str(page_fingerprint), str(anchor_key),
            tuple(map(int, coordinate)) if coordinate is not None else None,
            tuple(map(int, bounded_region)) if bounded_region is not None else None,
            issued, issued + max(timedelta(milliseconds=1), ttl),
            max(1, int(max_uses)), correlation_id or uuid.uuid4().hex, 0,
        )

    def permit_for_intent(self, intent: ActionIntent, page_context: str = "") -> ReadOnlyPermit:
        context = self._context(page_context)
        fingerprint = intent.page_fingerprint or self.fingerprint(context)
        coordinate = intent.coordinate
        region = intent.bounded_region
        if coordinate is not None and region is None:
            x, y = map(int, coordinate)
            region = (x - 8, y - 8, x + 8, y + 8)
        return self.issue_permit(
            action_key=intent.action_key, page_id=intent.page_id,
            page_fingerprint=fingerprint, anchor_key=intent.anchor_key,
            coordinate=coordinate, bounded_region=region,
            ttl=timedelta(seconds=max(0.001, float(intent.ttl_seconds))),
            max_uses=intent.max_uses, correlation_id=intent.correlation_id,
        )

    def authorize_coordinate(
        self,
        coordinate: tuple[int, int],
        *,
        page_context: str = "",
        permit: ReadOnlyPermit | None = None,
        intent: ActionIntent | None = None,
        page_id: str = "",
        page_fingerprint: str = "",
        anchor_key: str = "",
    ) -> bool:
        context = self._context(page_context)
        if intent is not None:
            try:
                permit = self.permit_for_intent(intent, context)
            except (PermissionError, ValueError) as error:
                return self._record(intent.action_key, coordinate, context, allowed=False, reason=str(error))
            page_id = intent.page_id
            page_fingerprint = permit.page_fingerprint
            anchor_key = intent.anchor_key
        if permit is None:
            return self._record("unclassified_tap", coordinate, context, allowed=False, reason="read_only_permit_required")
        reason = ""
        current = _now()
        if current > permit.expires_at:
            reason = "read_only_permit_expired"
        elif permit.uses >= permit.max_uses:
            reason = "read_only_permit_exhausted"
        elif page_id != permit.page_id or page_fingerprint != permit.page_fingerprint:
            reason = "read_only_permit_page_mismatch"
        elif anchor_key != permit.anchor_key:
            reason = "read_only_permit_anchor_mismatch"
        else:
            x, y = map(int, coordinate)
            if permit.coordinate is not None and (x, y) != permit.coordinate:
                reason = "read_only_permit_coordinate_mismatch"
            if permit.bounded_region is not None:
                x1, y1, x2, y2 = permit.bounded_region
                if not (min(x1, x2) <= x <= max(x1, x2) and min(y1, y2) <= y <= max(y1, y2)):
                    reason = "read_only_permit_bounds_mismatch"
        allowed, policy_reason = self._decision(permit.action_key)
        if not allowed and not reason:
            reason = policy_reason
        if reason:
            return self._record(permit.action_key, coordinate, context, allowed=False, reason=reason, permit=permit)
        permit.uses += 1
        return self._record(permit.action_key, coordinate, context, allowed=True, reason="valid_read_only_permit", permit=permit)

    def authorize_swipe(
        self,
        start: tuple[int, int],
        end: tuple[int, int],
        *,
        page_context: str = "",
        permit: ReadOnlyPermit | None = None,
        intent: ActionIntent | None = None,
        page_id: str = "",
        page_fingerprint: str = "",
        anchor_key: str = "",
    ) -> bool:
        # A swipe is authorized by its start point and journaled with its end.
        context = f"{self._context(page_context)} to={end}".strip()
        return self.authorize_coordinate(
            start, page_context=context, permit=permit, intent=intent,
            page_id=page_id, page_fingerprint=page_fingerprint, anchor_key=anchor_key,
        )

    def report(self) -> dict:
        return {"mode": "READ_ONLY", "blocked_actions": list(self.blocked_actions),
                "journal": [asdict(entry) for entry in self.journal]}


@contextmanager
def installed_read_only_guard(guard: ReadOnlyActionGuard) -> Iterator[ReadOnlyActionGuard]:
    from core.control.control import install_action_policy
    previous = install_action_policy(guard)
    try:
        yield guard
    finally:
        install_action_policy(previous)


__all__ = ["ActionIntent", "ActionJournalEntry", "ReadOnlyActionGuard", "ReadOnlyPermit", "installed_read_only_guard"]
