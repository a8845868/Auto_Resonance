"""Runtime action boundary for evidence-only emulator probes."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Callable, Iterator


@dataclass(frozen=True)
class ActionJournalEntry:
    timestamp: str
    action_key: str
    page_context: str
    coordinate: tuple[int, int] | None
    allowed: bool
    reason: str


class ReadOnlyActionGuard:
    ALLOWED_ACTIONS = {
        "back", "open_tab", "scroll", "exchange_buy_anchor",
        "exchange_sell_anchor", "open_detail", "navigation_anchor",
        "unclassified_tap",
    }
    BLOCKED_ACTIONS = {
        "transaction_buy", "transaction_sell", "all_buy", "all_sell",
        "haggle_confirm", "raise_price_confirm", "use_item", "use_book",
        "reward_claim", "fatigue_confirm", "bento_confirm", "depart",
        "account_settings",
    }
    TRANSACTION_MARKERS = (
        "预计买入", "买入总价", "预计卖出", "卖出总价", "全部买入",
        "全部卖出", "讨价还价", "抬价",
    )

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

    def _decision(self, action_key: str, page_context: str) -> tuple[bool, str]:
        key = str(action_key)
        if key in self.BLOCKED_ACTIONS:
            return False, "action_prohibited_in_read_only_mode"
        if key not in self.ALLOWED_ACTIONS:
            return False, "action_not_allowlisted"
        if key == "unclassified_tap" and any(
            marker in page_context for marker in self.TRANSACTION_MARKERS
        ):
            return False, "transaction_page_requires_explicit_safe_anchor"
        return True, "allowlisted_read_only_action"

    def _record(
        self,
        action_key: str,
        coordinate: tuple[int, int] | None,
        page_context: str,
    ) -> bool:
        allowed, reason = self._decision(action_key, page_context)
        self._journal.append(
            ActionJournalEntry(
                timestamp=datetime.now().astimezone().isoformat(timespec="milliseconds"),
                action_key=str(action_key),
                page_context=str(page_context)[:500],
                coordinate=(int(coordinate[0]), int(coordinate[1])) if coordinate else None,
                allowed=allowed,
                reason=reason,
            )
        )
        return allowed

    def tap(
        self,
        action_key: str,
        coordinate: tuple[int, int],
        page_context: str = "",
    ) -> bool:
        allowed = self._record(action_key, coordinate, page_context)
        if allowed and self.hardware_tap is not None:
            self.hardware_tap(coordinate)
        return allowed

    def authorize_coordinate(
        self,
        coordinate: tuple[int, int],
        *,
        page_context: str = "",
    ) -> bool:
        context = page_context
        if not context and self.context_provider is not None:
            try:
                context = str(self.context_provider())
            except Exception as error:
                self._record("unclassified_tap", coordinate, f"context_error:{type(error).__name__}")
                return False
        x, y = int(coordinate[0]), int(coordinate[1])
        # The normalized game back button is a navigation invariant.  It must
        # remain usable even when the current page contains transaction text.
        if 0 <= x <= 180 and 0 <= y <= 120:
            return self._record("back", coordinate, context)
        return self._record("unclassified_tap", coordinate, context)

    def authorize_swipe(
        self,
        start: tuple[int, int],
        end: tuple[int, int],
    ) -> bool:
        return self._record("scroll", start, f"to={end}")

    def report(self) -> dict:
        return {
            "mode": "READ_ONLY",
            "blocked_actions": list(self.blocked_actions),
            "journal": [asdict(entry) for entry in self.journal],
        }


@contextmanager
def installed_read_only_guard(guard: ReadOnlyActionGuard) -> Iterator[ReadOnlyActionGuard]:
    from core.control.control import install_action_policy

    previous = install_action_policy(guard)
    try:
        yield guard
    finally:
        install_action_policy(previous)


__all__ = [
    "ActionJournalEntry",
    "ReadOnlyActionGuard",
    "installed_read_only_guard",
]
