"""Issuer-authenticated, observation-backed boundary for read-only probes."""

from __future__ import annotations

import hashlib
import secrets
import threading
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from enum import Enum
from types import MappingProxyType
from typing import Callable, Iterator


LOGICAL_WIDTH = 1280
LOGICAL_HEIGHT = 720


def _now() -> datetime:
    return datetime.now().astimezone()


def _point(value: tuple[int, int]) -> tuple[int, int]:
    return int(value[0]), int(value[1])


def _inside(point: tuple[int, int], bounds: tuple[int, int, int, int]) -> bool:
    x, y = _point(point)
    x1, y1, x2, y2 = map(int, bounds)
    return min(x1, x2) <= x <= max(x1, x2) and min(y1, y2) <= y <= max(y1, y2)


class CoordinateSpace(str, Enum):
    LOGICAL_1280X720 = "LOGICAL_1280x720"
    DEVICE_PHYSICAL = "DEVICE_PHYSICAL"


@dataclass(frozen=True)
class DisplayGeometry:
    logical_width: int = LOGICAL_WIDTH
    logical_height: int = LOGICAL_HEIGHT
    physical_width: int = LOGICAL_WIDTH
    physical_height: int = LOGICAL_HEIGHT
    geometry_revision: str = ""
    captured_at: datetime | None = None

    def __post_init__(self) -> None:
        dimensions = (
            int(self.logical_width), int(self.logical_height),
            int(self.physical_width), int(self.physical_height),
        )
        if any(value <= 0 for value in dimensions):
            raise ValueError("display geometry dimensions must be positive")
        if not self.geometry_revision:
            source = "x".join(map(str, dimensions))
            object.__setattr__(
                self,
                "geometry_revision",
                hashlib.sha256(source.encode("ascii")).hexdigest()[:16],
            )

    @property
    def scale_x(self) -> float:
        return self.physical_width / self.logical_width

    @property
    def scale_y(self) -> float:
        return self.physical_height / self.logical_height

    @property
    def is_uniform(self) -> bool:
        return abs(self.scale_x - self.scale_y) <= 1e-9

    @classmethod
    def from_ratio(
        cls,
        ratio: float,
        *,
        geometry_revision: str = "",
        captured_at: datetime | None = None,
    ) -> "DisplayGeometry":
        ratio = float(ratio)
        if ratio <= 0:
            raise ValueError("display ratio must be positive")
        return cls(
            physical_width=int(round(LOGICAL_WIDTH * ratio)),
            physical_height=int(round(LOGICAL_HEIGHT * ratio)),
            geometry_revision=geometry_revision,
            captured_at=captured_at,
        )

    def logical_to_physical(self, point: tuple[int, int]) -> tuple[int, int]:
        if not self.is_uniform:
            raise PermissionError("non_uniform_display_geometry")
        logical = _point(point)
        if not _inside(logical, (0, 0, self.logical_width - 1, self.logical_height - 1)):
            raise PermissionError("logical_trajectory_outside_display")
        physical = (
            min(self.physical_width - 1, int(round(logical[0] * self.scale_x))),
            min(self.physical_height - 1, int(round(logical[1] * self.scale_y))),
        )
        if not _inside(physical, (0, 0, self.physical_width - 1, self.physical_height - 1)):
            raise PermissionError("physical_trajectory_outside_display")
        return physical


@dataclass(frozen=True)
class ActionIntent:
    """A caller request. It contains no page, OCR, or coordinate trust claims."""

    action_key: str
    requested_target: str
    correlation_id: str = ""


@dataclass(frozen=True)
class OcrObservedAnchor:
    anchor_id: str
    text: str
    bbox: tuple[int, int, int, int]


# Backward-compatible name. The concrete type remains explicitly OCR sourced.
ObservedAnchor = OcrObservedAnchor


@dataclass(frozen=True)
class CalibratedStaticRegion:
    anchor_id: str
    bbox: tuple[int, int, int, int]
    page_classifier: str
    allowed_action: str
    postcondition: str
    logical_resolution: tuple[int, int] = (LOGICAL_WIDTH, LOGICAL_HEIGHT)
    ui_layout_version: str = "legacy-1280x720-v1"
    geometry_revision: str = ""


@dataclass(frozen=True)
class PageObservation:
    observation_id: str
    screenshot_hash: str
    page_type: str
    markers: tuple[str, ...]
    anchors: tuple[OcrObservedAnchor, ...]
    captured_at: datetime
    anchors_are_logical: bool = True
    display_geometry: DisplayGeometry | None = None
    static_regions: tuple[CalibratedStaticRegion, ...] = ()
    ui_layout_version: str = "legacy-1280x720-v1"

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
        if observation.anchors_are_logical is not True:
            raise PermissionError("page anchors must use logical coordinates")
        return observation


class AnchorResolver:
    def resolve_with_source(
        self,
        observation: PageObservation,
        anchor_id: str,
        *,
        action_key: str = "",
        postcondition: str = "",
    ) -> tuple[OcrObservedAnchor | CalibratedStaticRegion, str]:
        ocr_matches = [
            anchor for anchor in observation.anchors if anchor.anchor_id == anchor_id
        ]
        static_matches = [
            region for region in observation.static_regions
            if region.anchor_id == anchor_id
            and region.page_classifier == observation.page_type
            and region.logical_resolution == (LOGICAL_WIDTH, LOGICAL_HEIGHT)
            and region.ui_layout_version == observation.ui_layout_version
            and (not region.geometry_revision or (
                observation.display_geometry is not None
                and region.geometry_revision == observation.display_geometry.geometry_revision
            ))
            and (not action_key or region.allowed_action == action_key)
            and (not postcondition or region.postcondition == postcondition)
        ]
        if len(ocr_matches) + len(static_matches) != 1:
            raise PermissionError("trusted anchor or calibrated region was not uniquely available")
        if ocr_matches:
            return ocr_matches[0], "OCR_OBSERVED"
        return static_matches[0], "CALIBRATED_STATIC"

    def resolve(
        self, observation: PageObservation, anchor_id: str
    ) -> OcrObservedAnchor | CalibratedStaticRegion:
        return self.resolve_with_source(observation, anchor_id)[0]


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


@dataclass(frozen=True)
class ReadOnlyPermit:
    """Immutable capability handle; the issuer registry is the authority."""

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
    coordinate_space: str = CoordinateSpace.LOGICAL_1280X720.value
    geometry_revision: str = ""
    physical_width: int = LOGICAL_WIDTH
    physical_height: int = LOGICAL_HEIGHT
    anchor_source: str = "OCR_OBSERVED"


@dataclass(frozen=True)
class _PermitRecord:
    permit: ReadOnlyPermit
    geometry: DisplayGeometry


@dataclass
class _RegistryEntry:
    record: _PermitRecord
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
        self._policies = MappingProxyType(dict(policies or DEFAULT_POLICY_SPECS))
        self.now = now
        self.observation_ttl = observation_ttl
        self._registry: dict[str, _RegistryEntry] = {}
        self._registry_lock = threading.RLock()
        self._before_consume: Callable[[], object] | None = None

    def _fresh_observation(self) -> PageObservation:
        observation = self.observer.observe()
        current = self.now()
        if current - observation.captured_at > self.observation_ttl or observation.captured_at > current + timedelta(seconds=1):
            raise PermissionError("trusted page observation is stale")
        return observation

    @staticmethod
    def _marker_text(observation: PageObservation) -> str:
        return "|".join(observation.markers).casefold()

    def _validate_spec(
        self,
        spec: ReadOnlyPolicySpec,
        observation: PageObservation,
        requested_target: str,
        trajectory: tuple[tuple[int, int], ...],
    ) -> tuple[OcrObservedAnchor | CalibratedStaticRegion, str, tuple[int, int, int, int]]:
        if requested_target != spec.anchor_id:
            raise PermissionError("requested target does not match the static policy anchor")
        if observation.page_type not in spec.allowed_page_types:
            raise PermissionError("observed page type is not allowed for this action")
        marker_text = self._marker_text(observation)
        if any(marker.casefold() not in marker_text for marker in spec.required_markers):
            raise PermissionError("required observed page marker is missing")
        if any(marker.casefold() in marker_text for marker in spec.forbidden_markers):
            raise PermissionError("forbidden observed page marker is present")
        anchor, anchor_source = self.resolver.resolve_with_source(
            observation,
            spec.anchor_id,
            action_key=spec.action_key,
            postcondition=spec.postcondition,
        )
        allowed_region = tuple(map(int, spec.allowed_region or anchor.bbox))
        if len(trajectory) == 1 and not _inside(trajectory[0], anchor.bbox):
            raise PermissionError("authorized logical coordinate is outside the trusted anchor bbox")
        if any(not _inside(point, allowed_region) for point in trajectory):
            raise PermissionError("final logical trajectory leaves the safe region")
        return anchor, anchor_source, allowed_region

    def issue(
        self,
        intent: ActionIntent,
        final_trajectory: tuple[tuple[int, int], ...],
        *,
        geometry: DisplayGeometry | None = None,
    ) -> ReadOnlyPermit:
        spec = self._policies.get(str(intent.action_key))
        if spec is None:
            raise PermissionError("action is not a specific read-only policy")
        trajectory = tuple(_point(point) for point in final_trajectory)
        if not trajectory:
            raise PermissionError("final logical trajectory is required")
        observation = self._fresh_observation()
        active_geometry = geometry or observation.display_geometry or DisplayGeometry()
        if not active_geometry.is_uniform:
            raise PermissionError("non_uniform_display_geometry")
        if (
            observation.display_geometry is not None
            and observation.display_geometry.geometry_revision != active_geometry.geometry_revision
        ):
            raise PermissionError("display_geometry_revision_changed")
        anchor, anchor_source, allowed_region = self._validate_spec(
            spec, observation, str(intent.requested_target), trajectory
        )
        for point in trajectory:
            active_geometry.logical_to_physical(point)
        issued = self.now()
        permit = ReadOnlyPermit(
            permit_id=secrets.token_urlsafe(32),
            action_key=spec.action_key,
            requested_target=str(intent.requested_target),
            observation_id=observation.observation_id,
            screenshot_hash=observation.screenshot_hash,
            page_classifier=observation.page_type,
            page_fingerprint=observation.page_fingerprint,
            anchor_id=anchor.anchor_id,
            anchor_text_hash=hashlib.sha256(
                (anchor.text if isinstance(anchor, OcrObservedAnchor) else anchor.anchor_id).encode("utf-8")
            ).hexdigest()[:16],
            anchor_bbox=tuple(map(int, anchor.bbox)),
            allowed_region=allowed_region,
            final_trajectory=trajectory,
            issued_at=issued,
            expires_at=issued + timedelta(seconds=5),
            max_uses=1,
            correlation_id=intent.correlation_id or uuid.uuid4().hex,
            postcondition=spec.postcondition,
            coordinate_space=CoordinateSpace.LOGICAL_1280X720.value,
            geometry_revision=active_geometry.geometry_revision,
            physical_width=active_geometry.physical_width,
            physical_height=active_geometry.physical_height,
            anchor_source=anchor_source,
        )
        with self._registry_lock:
            self._registry[permit.permit_id] = _RegistryEntry(
                _PermitRecord(permit=permit, geometry=active_geometry)
            )
        return permit

    @staticmethod
    def permit_digest(permit: ReadOnlyPermit | None) -> str:
        if permit is None or not permit.permit_id:
            return ""
        return hashlib.sha256(permit.permit_id.encode("utf-8")).hexdigest()[:16]

    def registry_contains(self, permit: ReadOnlyPermit) -> bool:
        with self._registry_lock:
            return permit.permit_id in self._registry

    def permit_status(self, permit: ReadOnlyPermit) -> dict[str, object]:
        """Return non-sensitive proof without exporting registry contents or token."""

        with self._registry_lock:
            entry = self._registry.get(permit.permit_id)
            return {
                "permit_digest": self.permit_digest(permit),
                "registered": entry is not None,
                "uses": entry.uses if entry is not None else 0,
                "max_uses": entry.record.permit.max_uses if entry is not None else 0,
            }

    def _observation_matches(
        self, observation: PageObservation, record: _PermitRecord
    ) -> bool:
        permit = record.permit
        return bool(
            observation.observation_id == permit.observation_id
            and observation.screenshot_hash == permit.screenshot_hash
            and observation.page_type == permit.page_classifier
            and observation.page_fingerprint == permit.page_fingerprint
            and (
                observation.display_geometry is None
                or observation.display_geometry.geometry_revision == permit.geometry_revision
            )
        )

    def revalidate(self, permit: ReadOnlyPermit) -> bool:
        """Compatibility check; authorization uses atomic ``consume`` below."""

        with self._registry_lock:
            entry = self._registry.get(permit.permit_id)
            if entry is None or entry.record.permit != permit:
                return False
            try:
                return self._observation_matches(self._fresh_observation(), entry.record)
            except PermissionError:
                return False

    def consume(
        self,
        permit: ReadOnlyPermit,
        trajectory: tuple[tuple[int, int], ...],
        *,
        geometry: DisplayGeometry | None = None,
    ) -> tuple[bool, str, _PermitRecord | None, tuple[tuple[int, int], ...]]:
        """Validate and consume a capability in one registry critical section."""

        hook = self._before_consume
        if hook is not None:
            hook()
        logical = tuple(_point(point) for point in trajectory)
        with self._registry_lock:
            entry = self._registry.get(str(getattr(permit, "permit_id", "")))
            if entry is None:
                return False, "permit_not_issued_by_registry", None, ()
            record = entry.record
            authoritative = record.permit
            if permit != authoritative:
                return False, "permit_registry_record_mismatch", record, ()
            spec = self._policies.get(authoritative.action_key)
            if spec is None:
                return False, "policy_no_longer_allows_action", record, ()
            if authoritative.requested_target != spec.anchor_id:
                return False, "policy_anchor_changed", record, ()
            if authoritative.postcondition != spec.postcondition:
                return False, "policy_postcondition_changed", record, ()
            current = self.now()
            if current > authoritative.expires_at:
                return False, "read_only_permit_expired", record, ()
            if entry.uses >= authoritative.max_uses:
                return False, "permit_already_consumed", record, ()
            if logical != authoritative.final_trajectory:
                return False, "final_logical_trajectory_mismatch", record, ()
            if any(not _inside(point, authoritative.allowed_region) for point in logical):
                return False, "final_logical_trajectory_outside_bounds", record, ()
            active_geometry = geometry or record.geometry
            if not active_geometry.is_uniform:
                return False, "non_uniform_display_geometry", record, ()
            if active_geometry.geometry_revision != authoritative.geometry_revision:
                return False, "display_geometry_revision_changed", record, ()
            if (
                active_geometry.physical_width != authoritative.physical_width
                or active_geometry.physical_height != authoritative.physical_height
            ):
                return False, "display_geometry_dimensions_changed", record, ()
            try:
                observation = self._fresh_observation()
                if not self._observation_matches(observation, record):
                    return False, "page_observation_changed_or_stale", record, ()
                anchor, source, allowed_region = self._validate_spec(
                    spec, observation, authoritative.requested_target, logical
                )
                anchor_hash = hashlib.sha256(
                    (anchor.text if isinstance(anchor, OcrObservedAnchor) else anchor.anchor_id).encode("utf-8")
                ).hexdigest()[:16]
                if (
                    source != authoritative.anchor_source
                    or tuple(map(int, anchor.bbox)) != authoritative.anchor_bbox
                    or anchor_hash != authoritative.anchor_text_hash
                    or allowed_region != authoritative.allowed_region
                ):
                    return False, "trusted_anchor_or_policy_changed", record, ()
                physical = tuple(active_geometry.logical_to_physical(point) for point in logical)
            except PermissionError as error:
                return False, str(error), record, ()
            entry.uses += 1
            return True, "valid_trusted_read_only_permit", record, physical


@dataclass(frozen=True)
class ActionJournalEntry:
    timestamp: str
    action_key: str
    allowed: bool
    reason: str
    correlation_id: str = ""
    permit_id: str = ""
    permit_digest: str = ""
    observation_id: str = ""
    screenshot_hash: str = ""
    page_classifier: str = ""
    marker_hash: str = ""
    anchor_key: str = ""
    anchor_bbox: tuple[int, int, int, int] | None = None
    anchor_source: str = ""
    coordinate: tuple[int, int] | None = None
    final_trajectory: tuple[tuple[int, int], ...] = ()
    logical_trajectory: tuple[tuple[int, int], ...] = ()
    physical_trajectory: tuple[tuple[int, int], ...] = ()
    coordinate_space: str = CoordinateSpace.LOGICAL_1280X720.value
    geometry_revision: str = ""
    registry_registered: bool = False
    permit_uses: int = 0
    permit_max_uses: int = 0


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
        self._journal_lock = threading.Lock()

    @property
    def journal(self) -> tuple[ActionJournalEntry, ...]:
        with self._journal_lock:
            return tuple(self._journal)

    @property
    def blocked_actions(self) -> tuple[str, ...]:
        return tuple(entry.action_key for entry in self.journal if not entry.allowed)

    def _record(
        self,
        action_key: str,
        logical_trajectory: tuple[tuple[int, int], ...],
        *,
        physical_trajectory: tuple[tuple[int, int], ...] = (),
        allowed: bool,
        reason: str,
        permit: ReadOnlyPermit | None = None,
        record: _PermitRecord | None = None,
    ) -> bool:
        authoritative = record.permit if record is not None else None
        digest = (
            self.permit_issuer.permit_digest(permit)
            if self.permit_issuer is not None
            else ReadOnlyPermitIssuer.permit_digest(permit)
        )
        marker_hash = ""
        permit_status: dict[str, object] = {}
        if authoritative is not None:
            marker_hash = hashlib.sha256(
                f"{authoritative.page_classifier}|{authoritative.anchor_text_hash}".encode("utf-8")
            ).hexdigest()[:16]
            if self.permit_issuer is not None and permit is not None:
                permit_status = self.permit_issuer.permit_status(permit)
        entry = ActionJournalEntry(
            timestamp=self.now().isoformat(timespec="milliseconds"),
            action_key=str(authoritative.action_key if authoritative else action_key),
            allowed=bool(allowed),
            reason=str(reason),
            correlation_id=authoritative.correlation_id if authoritative else "",
            permit_id=digest,
            permit_digest=digest,
            observation_id=authoritative.observation_id if authoritative else "",
            screenshot_hash=authoritative.screenshot_hash if authoritative else "",
            page_classifier=authoritative.page_classifier if authoritative else "",
            marker_hash=marker_hash,
            anchor_key=authoritative.anchor_id if authoritative else "",
            anchor_bbox=authoritative.anchor_bbox if authoritative else None,
            anchor_source=authoritative.anchor_source if authoritative else "",
            coordinate=logical_trajectory[0] if len(logical_trajectory) == 1 else None,
            final_trajectory=logical_trajectory,
            logical_trajectory=logical_trajectory,
            physical_trajectory=physical_trajectory,
            geometry_revision=authoritative.geometry_revision if authoritative else "",
            registry_registered=bool(permit_status.get("registered", False)),
            permit_uses=int(permit_status.get("uses", 0)),
            permit_max_uses=int(permit_status.get("max_uses", 0)),
        )
        with self._journal_lock:
            self._journal.append(entry)
        return bool(allowed)

    def tap(self, action_key: str, coordinate: tuple[int, int], page_context: str = "") -> bool:
        key = str(action_key)
        reason = "action_prohibited_in_read_only_mode" if key in self.BLOCKED_ACTIONS else "trusted_read_only_permit_required"
        return self._record(key, (_point(coordinate),), allowed=False, reason=reason)

    def issue_permit(self, **_kwargs) -> ReadOnlyPermit:
        raise PermissionError("caller cannot self-issue a permit; trusted issuer required")

    def _authorize(
        self,
        trajectory: tuple[tuple[int, int], ...],
        *,
        permit: ReadOnlyPermit | None,
        intent: ActionIntent | None,
        geometry: DisplayGeometry | None,
        execute: Callable[[tuple[tuple[int, int], ...]], object] | None,
    ) -> bool:
        key = str(intent.action_key if intent is not None else permit.action_key if permit is not None else "unclassified_tap")
        if key in self.BLOCKED_ACTIONS:
            return self._record(key, trajectory, allowed=False, reason="action_prohibited_in_read_only_mode", permit=permit)
        if permit is None and intent is not None:
            if self.permit_issuer is None:
                return self._record(key, trajectory, allowed=False, reason="trusted_permit_issuer_unavailable")
            try:
                permit = self.permit_issuer.issue(intent, trajectory, geometry=geometry)
            except PermissionError as error:
                return self._record(key, trajectory, allowed=False, reason=str(error))
        if permit is None:
            return self._record(key, trajectory, allowed=False, reason="read_only_permit_required")
        if self.permit_issuer is None:
            return self._record(key, trajectory, allowed=False, reason="trusted_permit_issuer_unavailable", permit=permit)
        allowed, reason, record, physical = self.permit_issuer.consume(
            permit, trajectory, geometry=geometry
        )
        self._record(
            key,
            trajectory,
            physical_trajectory=physical,
            allowed=allowed,
            reason=reason,
            permit=permit,
            record=record,
        )
        if not allowed:
            return False
        if execute is not None:
            execute(physical)
        elif self.hardware_tap is not None and len(physical) == 1:
            self.hardware_tap(physical[0])
        return True

    def authorize_coordinate(
        self,
        coordinate: tuple[int, int],
        *,
        permit: ReadOnlyPermit | None = None,
        intent: ActionIntent | None = None,
        geometry: DisplayGeometry | None = None,
        _execute: Callable[[tuple[tuple[int, int], ...]], object] | None = None,
        **caller_claims,
    ) -> bool:
        logical = (_point(coordinate),)
        if any(caller_claims.values()) and permit is None and intent is None:
            return self._record("unclassified_tap", logical, allowed=False, reason="caller_page_claims_are_untrusted")
        return self._authorize(
            logical, permit=permit, intent=intent, geometry=geometry, execute=_execute
        )

    def authorize_swipe(
        self,
        start: tuple[int, int],
        end: tuple[int, int],
        *,
        trajectory: tuple[tuple[int, int], ...] | None = None,
        permit: ReadOnlyPermit | None = None,
        intent: ActionIntent | None = None,
        geometry: DisplayGeometry | None = None,
        _execute: Callable[[tuple[tuple[int, int], ...]], object] | None = None,
        **_caller_claims,
    ) -> bool:
        path = tuple(_point(point) for point in (trajectory or (start, end)))
        if not path or path[0] != _point(start) or path[-1] != _point(end):
            return self._record("unclassified_swipe", path, allowed=False, reason="invalid_final_swipe_trajectory")
        return self._authorize(
            path, permit=permit, intent=intent, geometry=geometry, execute=_execute
        )

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
    "ActionIntent", "ActionJournalEntry", "AnchorResolver",
    "CalibratedStaticRegion", "CoordinateSpace", "DisplayGeometry",
    "ObservedAnchor", "OcrObservedAnchor", "PageObservation", "PageObserver",
    "ReadOnlyActionGuard", "ReadOnlyPermit", "ReadOnlyPermitIssuer",
    "ReadOnlyPolicySpec", "installed_read_only_guard",
]
