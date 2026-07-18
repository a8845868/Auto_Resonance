"""Trusted, observation-backed action boundary for read-only emulator probes."""

from __future__ import annotations

import hashlib
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import Callable, Iterator


def _now() -> datetime:
    return datetime.now().astimezone()


def _point(value: tuple[int, int]) -> tuple[int, int]:
    return int(value[0]), int(value[1])


def _inside(point: tuple[int, int], bounds: tuple[int, int, int, int]) -> bool:
    x, y = _point(point)
    x1, y1, x2, y2 = map(int, bounds)
    return min(x1, x2) <= x <= max(x1, x2) and min(y1, y2) <= y <= max(y1, y2)


@dataclass(frozen=True)
class ActionIntent:
    """A caller request.  It contains no page or OCR trust assertions."""

    action_key: str
    requested_target: str
    correlation_id: str = ""


@dataclass(frozen=True)
class ObservedAnchor:
    anchor_id: str
    text: str
    bbox: tuple[int, int, int, int]


@dataclass(frozen=True)
class PageObservation:
    observation_id: str
    screenshot_hash: str
    page_type: str
    markers: tuple[str, ...]
    anchors: tuple[ObservedAnchor, ...]
    captured_at: datetime

    @property
    def page_fingerprint(self) -> str:
        source = "|".join((self.screenshot_hash, self.page_type, *sorted(self.markers)))
        return hashlib.sha256(source.encode("utf-8")).hexdigest()[:16]


class PageObserver:
    """Reads a fresh screenshot/OCR-derived observation from a trusted adapter."""

    def __init__(self, observe: Callable[[], PageObservation]):
        self._observe = observe

    def observe(self) -> PageObservation:
        observation = self._observe()
        if not isinstance(observation, PageObservation):
            raise PermissionError("trusted page observation unavailable")
        if not observation.observation_id or not observation.screenshot_hash or not observation.page_type:
            raise PermissionError("trusted page observation is incomplete")
        if observation.captured_at.tzinfo is None or observation.captured_at.utcoffset() is None:
            raise PermissionError("trusted page observation timestamp must be timezone-aware")
        return observation


class AnchorResolver:
    def resolve(self, observation: PageObservation, anchor_id: str) -> ObservedAnchor:
        matches = [anchor for anchor in observation.anchors if anchor.anchor_id == anchor_id]
        if len(matches) != 1:
            raise PermissionError("trusted OCR anchor was not uniquely observed")
        return matches[0]


@dataclass(frozen=True)
class ReadOnlyPolicySpec:
    action_key: str
    allowed_page_types: frozenset[str]
    anchor_id: str
    required_markers: tuple[str, ...] = ()
    forbidden_markers: tuple[str, ...] = ("注销", "退出登录", "account_logout")
    allowed_region: tuple[int, int, int, int] | None = None
    postcondition: str = "page_identity_must_change_or_remain_safe"


DEFAULT_POLICY_SPECS = {
    "reward_back": ReadOnlyPolicySpec(
        "reward_back", frozenset({"daily_activity", "travel_manual", "manual_tasks", "manual_track"}),
        "top_left_back",
    ),
    "page_back": ReadOnlyPolicySpec(
        "page_back",
        frozenset({"home", "daily_activity", "travel_manual", "manual_tasks", "manual_track", "exchange_buy", "exchange_sell", "inventory", "fatigue_info"}),
        "top_left_back",
    ),
    "manual_tab": ReadOnlyPolicySpec(
        "manual_tab", frozenset({"travel_manual", "manual_tasks", "manual_track"}), "manual_tab",
    ),
    "manual_tasks_tab": ReadOnlyPolicySpec(
        "manual_tasks_tab", frozenset({"travel_manual", "manual_tasks", "manual_track"}), "manual_tasks_tab",
    ),
    "manual_track_tab": ReadOnlyPolicySpec(
        "manual_track_tab", frozenset({"travel_manual", "manual_tasks"}), "manual_track_tab",
    ),
    "daily_page_open": ReadOnlyPolicySpec(
        "daily_page_open", frozenset({"home"}), "daily_shortcut",
    ),
    "manual_page_open": ReadOnlyPolicySpec(
        "manual_page_open", frozenset({"home"}), "manual_shortcut",
    ),
    "daily_horizontal_scroll": ReadOnlyPolicySpec(
        "daily_horizontal_scroll", frozenset({"daily_activity"}), "daily_content",
        allowed_region=(150, 180, 1180, 650), postcondition="daily_anchor_remains_valid",
    ),
    "manual_horizontal_scroll": ReadOnlyPolicySpec(
        "manual_horizontal_scroll", frozenset({"manual_tasks", "manual_track"}), "manual_content",
        allowed_region=(150, 100, 1180, 650), postcondition="manual_anchor_remains_valid",
    ),
    "exchange_buy_navigation": ReadOnlyPolicySpec(
        "exchange_buy_navigation", frozenset({"station", "exchange"}), "buy_navigation",
    ),
    "exchange_sell_navigation": ReadOnlyPolicySpec(
        "exchange_sell_navigation", frozenset({"station", "exchange"}), "sell_navigation",
    ),
    "fatigue_info_open": ReadOnlyPolicySpec(
        "fatigue_info_open", frozenset({"home", "hud"}), "fatigue_value",
    ),
    "dialog_cancel": ReadOnlyPolicySpec(
        "dialog_cancel", frozenset({"clarity_dialog", "resource_repair", "startup_overlay"}), "cancel",
    ),
    "enter_game": ReadOnlyPolicySpec(
        "enter_game", frozenset({"login"}), "enter_game",
        postcondition="top_level_hud_or_safe_startup_transition",
    ),
}


@dataclass
class ReadOnlyPermit:
    permit_id: str
    action_key: str
    requested_target: str
    observation_id: str
    screenshot_hash: str
    page_classifier: str
    page_fingerprint: str
    anchor_id: str
    anchor_text_hash: str
    anchor_bbox: tuple[int, int, int, int]
    allowed_region: tuple[int, int, int, int]
    final_trajectory: tuple[tuple[int, int], ...]
    issued_at: datetime
    expires_at: datetime
    max_uses: int
    correlation_id: str
    postcondition: str
    uses: int = 0


class ReadOnlyPermitIssuer:
    def __init__(
        self,
        observer: PageObserver,
        resolver: AnchorResolver,
        *,
        policies: dict[str, ReadOnlyPolicySpec] | None = None,
        now: Callable[[], datetime] = _now,
        observation_ttl: timedelta = timedelta(seconds=5),
    ):
        self.observer = observer
        self.resolver = resolver
        self.policies = dict(policies or DEFAULT_POLICY_SPECS)
        self.now = now
        self.observation_ttl = observation_ttl

    def _fresh_observation(self) -> PageObservation:
        observation = self.observer.observe()
        current = self.now()
        if current - observation.captured_at > self.observation_ttl or observation.captured_at > current + timedelta(seconds=1):
            raise PermissionError("trusted page observation is stale")
        return observation

    def issue(
        self,
        intent: ActionIntent,
        final_trajectory: tuple[tuple[int, int], ...],
    ) -> ReadOnlyPermit:
        spec = self.policies.get(str(intent.action_key))
        if spec is None:
            raise PermissionError("action is not a specific read-only policy")
        if str(intent.requested_target) != spec.anchor_id:
            raise PermissionError("requested target does not match the static policy anchor")
        trajectory = tuple(_point(point) for point in final_trajectory)
        if not trajectory:
            raise PermissionError("final physical trajectory is required")
        observation = self._fresh_observation()
        if observation.page_type not in spec.allowed_page_types:
            raise PermissionError("observed page type is not allowed for this action")
        marker_text = "|".join(observation.markers).casefold()
        if any(marker.casefold() not in marker_text for marker in spec.required_markers):
            raise PermissionError("required observed page marker is missing")
        if any(marker.casefold() in marker_text for marker in spec.forbidden_markers):
            raise PermissionError("forbidden observed page marker is present")
        anchor = self.resolver.resolve(observation, spec.anchor_id)
        allowed_region = spec.allowed_region or anchor.bbox
        if len(trajectory) == 1 and not _inside(trajectory[0], anchor.bbox):
            raise PermissionError("authorized coordinate is outside the observed anchor bbox")
        if any(not _inside(point, allowed_region) for point in trajectory):
            raise PermissionError("final physical trajectory leaves the safe region")
        issued = self.now()
        return ReadOnlyPermit(
            permit_id=uuid.uuid4().hex,
            action_key=spec.action_key,
            requested_target=intent.requested_target,
            observation_id=observation.observation_id,
            screenshot_hash=observation.screenshot_hash,
            page_classifier=observation.page_type,
            page_fingerprint=observation.page_fingerprint,
            anchor_id=anchor.anchor_id,
            anchor_text_hash=hashlib.sha256(anchor.text.encode("utf-8")).hexdigest()[:16],
            anchor_bbox=tuple(map(int, anchor.bbox)),
            allowed_region=tuple(map(int, allowed_region)),
            final_trajectory=trajectory,
            issued_at=issued,
            expires_at=issued + timedelta(seconds=5),
            max_uses=1,
            correlation_id=intent.correlation_id or uuid.uuid4().hex,
            postcondition=spec.postcondition,
        )

    def revalidate(self, permit: ReadOnlyPermit) -> bool:
        try:
            observation = self._fresh_observation()
        except PermissionError:
            return False
        return bool(
            observation.observation_id == permit.observation_id
            and observation.screenshot_hash == permit.screenshot_hash
            and observation.page_type == permit.page_classifier
            and observation.page_fingerprint == permit.page_fingerprint
        )


@dataclass(frozen=True)
class ActionJournalEntry:
    timestamp: str
    action_key: str
    allowed: bool
    reason: str
    correlation_id: str = ""
    permit_id: str = ""
    observation_id: str = ""
    screenshot_hash: str = ""
    page_classifier: str = ""
    marker_hash: str = ""
    anchor_key: str = ""
    anchor_bbox: tuple[int, int, int, int] | None = None
    coordinate: tuple[int, int] | None = None
    final_trajectory: tuple[tuple[int, int], ...] = ()


class ReadOnlyActionGuard:
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
        permit_issuer: ReadOnlyPermitIssuer | None = None,
        now: Callable[[], datetime] = _now,
        context_provider: Callable[[], str] | None = None,
    ):
        self.hardware_tap = hardware_tap
        self.permit_issuer = permit_issuer
        self.now = now
        self._journal: list[ActionJournalEntry] = []

    @property
    def journal(self) -> tuple[ActionJournalEntry, ...]:
        return tuple(self._journal)

    @property
    def blocked_actions(self) -> tuple[str, ...]:
        return tuple(entry.action_key for entry in self._journal if not entry.allowed)

    def _record(
        self,
        action_key: str,
        trajectory: tuple[tuple[int, int], ...],
        *,
        allowed: bool,
        reason: str,
        permit: ReadOnlyPermit | None = None,
    ) -> bool:
        marker_hash = ""
        if permit is not None:
            marker_hash = hashlib.sha256(
                f"{permit.page_classifier}|{permit.anchor_text_hash}".encode("utf-8")
            ).hexdigest()[:16]
        self._journal.append(ActionJournalEntry(
            timestamp=self.now().isoformat(timespec="milliseconds"),
            action_key=str(action_key), allowed=bool(allowed), reason=str(reason),
            correlation_id=permit.correlation_id if permit else "",
            permit_id=permit.permit_id if permit else "",
            observation_id=permit.observation_id if permit else "",
            screenshot_hash=permit.screenshot_hash if permit else "",
            page_classifier=permit.page_classifier if permit else "",
            marker_hash=marker_hash,
            anchor_key=permit.anchor_id if permit else "",
            anchor_bbox=permit.anchor_bbox if permit else None,
            coordinate=trajectory[0] if len(trajectory) == 1 else None,
            final_trajectory=trajectory,
        ))
        return bool(allowed)

    def tap(self, action_key: str, coordinate: tuple[int, int], page_context: str = "") -> bool:
        key = str(action_key)
        allowed = False
        reason = "action_prohibited_in_read_only_mode" if key in self.BLOCKED_ACTIONS else "trusted_read_only_permit_required"
        return self._record(key, (_point(coordinate),), allowed=allowed, reason=reason)

    def issue_permit(self, **_kwargs) -> ReadOnlyPermit:
        raise PermissionError("caller cannot self-issue a permit; trusted issuer required")

    def _authorize(
        self,
        trajectory: tuple[tuple[int, int], ...],
        *,
        permit: ReadOnlyPermit | None,
        intent: ActionIntent | None,
    ) -> bool:
        key = str(intent.action_key if intent is not None else permit.action_key if permit is not None else "unclassified_tap")
        if key in self.BLOCKED_ACTIONS:
            return self._record(key, trajectory, allowed=False, reason="action_prohibited_in_read_only_mode", permit=permit)
        if permit is None and intent is not None:
            if self.permit_issuer is None:
                return self._record(key, trajectory, allowed=False, reason="trusted_permit_issuer_unavailable")
            try:
                permit = self.permit_issuer.issue(intent, trajectory)
            except PermissionError as error:
                return self._record(key, trajectory, allowed=False, reason=str(error))
        if permit is None:
            return self._record(key, trajectory, allowed=False, reason="read_only_permit_required")
        current = self.now()
        reason = ""
        if current > permit.expires_at:
            reason = "read_only_permit_expired"
        elif permit.uses >= permit.max_uses:
            reason = "read_only_permit_exhausted"
        elif trajectory != permit.final_trajectory:
            reason = "final_physical_trajectory_mismatch"
        elif any(not _inside(point, permit.allowed_region) for point in trajectory):
            reason = "final_physical_trajectory_outside_bounds"
        elif self.permit_issuer is None or not self.permit_issuer.revalidate(permit):
            reason = "page_observation_changed_or_stale"
        if reason:
            return self._record(permit.action_key, trajectory, allowed=False, reason=reason, permit=permit)
        permit.uses += 1
        return self._record(permit.action_key, trajectory, allowed=True, reason="valid_trusted_read_only_permit", permit=permit)

    def authorize_coordinate(
        self,
        coordinate: tuple[int, int],
        *,
        permit: ReadOnlyPermit | None = None,
        intent: ActionIntent | None = None,
        **caller_claims,
    ) -> bool:
        if any(caller_claims.values()) and permit is None and intent is None:
            return self._record("unclassified_tap", (_point(coordinate),), allowed=False, reason="caller_page_claims_are_untrusted")
        return self._authorize((_point(coordinate),), permit=permit, intent=intent)

    def authorize_swipe(
        self,
        start: tuple[int, int],
        end: tuple[int, int],
        *,
        trajectory: tuple[tuple[int, int], ...] | None = None,
        permit: ReadOnlyPermit | None = None,
        intent: ActionIntent | None = None,
        **_caller_claims,
    ) -> bool:
        path = tuple(_point(point) for point in (trajectory or (start, end)))
        if not path or path[0] != _point(start) or path[-1] != _point(end):
            return self._record("unclassified_swipe", path, allowed=False, reason="invalid_final_swipe_trajectory")
        return self._authorize(path, permit=permit, intent=intent)

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
    "ActionIntent", "ActionJournalEntry", "AnchorResolver", "ObservedAnchor",
    "PageObservation", "PageObserver", "ReadOnlyActionGuard", "ReadOnlyPermit",
    "ReadOnlyPermitIssuer", "ReadOnlyPolicySpec", "installed_read_only_guard",
]
