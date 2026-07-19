"""Issuer-authenticated, observation-backed boundary for read-only probes."""

from __future__ import annotations

import hashlib
import os
import secrets
import threading
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta
from enum import Enum
from types import MappingProxyType
from typing import Callable, Iterator, Protocol


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


class ActionKind(str, Enum):
    TAP = "TAP"
    SWIPE = "SWIPE"


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
    action_kind: str = ""


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
    capture_sequence: int = 0
    capture_nonce: str = ""

    @property
    def page_fingerprint(self) -> str:
        source = "|".join((self.screenshot_hash, self.page_type, *sorted(self.markers)))
        return hashlib.sha256(source.encode("utf-8")).hexdigest()[:16]


class PageObserver:
    """Reads a fresh screenshot/OCR-derived observation from a trusted adapter."""

    def __init__(self, observe: Callable[[], PageObservation]):
        self._observe = observe
        self._sequence = 0
        self._lock = threading.Lock()

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
        # The adapter may reuse immutable values, but every trusted capture call
        # receives an issuer-local identity. This prevents a caller cache from
        # making issue, consume and postcondition validation the same capture.
        with self._lock:
            self._sequence += 1
            sequence = self._sequence
        return replace(
            observation,
            capture_sequence=sequence,
            capture_nonce=secrets.token_hex(16),
        )


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
    action_kind: ActionKind = ActionKind.TAP
    required_markers: tuple[str, ...] = ()
    forbidden_markers: tuple[str, ...] = ("注销", "退出登录", "account_logout")
    allowed_region: tuple[int, int, int, int] | None = None
    postcondition: str = "page_identity_must_change_or_remain_safe"
    minimum_displacement: int = 40
    maximum_vertical_ratio: float = 0.20
    allowed_swipe_directions: frozenset[str] = frozenset({"LEFT", "RIGHT"})
    minimum_duration_ms: int = 100
    maximum_duration_ms: int = 2000


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
        action_kind=ActionKind.SWIPE,
        allowed_region=(150, 180, 1180, 650), postcondition="daily_anchor_remains_valid",
    ),
    "manual_horizontal_scroll": ReadOnlyPolicySpec(
        "manual_horizontal_scroll", frozenset({"manual_tasks", "manual_track"}), "manual_content",
        action_kind=ActionKind.SWIPE,
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


@dataclass(frozen=True, slots=True)
class ReadOnlyPermitHandle:
    """Opaque, non-authoritative reference to an issuer-owned record."""

    opaque_token: str


# Compatibility import name. The public value is still only an opaque handle.
ReadOnlyPermit = ReadOnlyPermitHandle


@dataclass(frozen=True, slots=True)
class _PermitRecord:
    action_key: str
    action_kind: ActionKind
    policy_revision: str
    requested_target: str
    observation_id: str
    screenshot_hash: str
    page_classifier: str
    markers: tuple[str, ...]
    issue_capture_sequence: int
    anchor_id: str
    anchor_text_hash: str
    anchor_bbox: tuple[int, int, int, int]
    anchor_source: str
    allowed_region: tuple[int, int, int, int]
    final_trajectory: tuple[tuple[int, int], ...]
    duration_ms: int
    issued_at: datetime
    expires_at: datetime
    max_uses: int
    correlation_id: str
    postcondition: str
    coordinate_space: str
    geometry_revision: str
    logical_width: int
    logical_height: int
    physical_width: int
    physical_height: int
    process_id: int
    session_id: str


@dataclass
class _RegistryEntry:
    record: _PermitRecord
    uses: int = 0
    consume_capture_sequence: int = 0


class ReadOnlyPermitIssuer:
    def __init__(
        self,
        observer: PageObserver,
        resolver: AnchorResolver,
        *,
        policies: dict[str, ReadOnlyPolicySpec] | None = None,
        now: Callable[[], datetime] = _now,
        observation_ttl: timedelta = timedelta(seconds=5),
        registry_limit: int = 256,
    ):
        self.observer = observer
        self.resolver = resolver
        source = policies or DEFAULT_POLICY_SPECS
        self._policies = MappingProxyType({
            str(key): replace(
                value,
                allowed_page_types=frozenset(value.allowed_page_types),
                required_markers=tuple(value.required_markers),
                forbidden_markers=tuple(value.forbidden_markers),
                allowed_region=(tuple(value.allowed_region) if value.allowed_region else None),
                allowed_swipe_directions=frozenset(value.allowed_swipe_directions),
            )
            for key, value in source.items()
        })
        self.now = now
        self.observation_ttl = observation_ttl
        self._registry: dict[str, _RegistryEntry] = {}
        self._registry_lock = threading.RLock()
        self._before_consume: Callable[[], object] | None = None
        self._registry_limit = max(1, int(registry_limit))
        self._process_id = os.getpid()
        self._session_id = secrets.token_hex(16)

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

    @staticmethod
    def _policy_revision(spec: ReadOnlyPolicySpec) -> str:
        payload = repr((
            spec.action_key, spec.action_kind.value, sorted(spec.allowed_page_types),
            spec.anchor_id, spec.required_markers, spec.forbidden_markers,
            spec.allowed_region, spec.postcondition, spec.minimum_displacement,
            spec.maximum_vertical_ratio, sorted(spec.allowed_swipe_directions),
            spec.minimum_duration_ms, spec.maximum_duration_ms,
        ))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

    @staticmethod
    def _validate_modality(
        spec: ReadOnlyPolicySpec,
        trajectory: tuple[tuple[int, int], ...],
        duration_ms: int,
    ) -> None:
        if spec.action_kind is ActionKind.TAP:
            if len(trajectory) != 1:
                raise PermissionError("action_kind_mismatch")
            return
        if len(set(trajectory)) < 2:
            raise PermissionError("scroll_trajectory_requires_distinct_points")
        dx = trajectory[-1][0] - trajectory[0][0]
        dy = trajectory[-1][1] - trajectory[0][1]
        if abs(dx) < int(spec.minimum_displacement):
            raise PermissionError("scroll_minimum_displacement_not_met")
        if abs(dy) > max(2, int(round(abs(dx) * float(spec.maximum_vertical_ratio)))):
            raise PermissionError("scroll_must_be_near_horizontal")
        direction = "RIGHT" if dx > 0 else "LEFT"
        if direction not in spec.allowed_swipe_directions:
            raise PermissionError("scroll_direction_not_allowed")
        if not (spec.minimum_duration_ms <= int(duration_ms) <= spec.maximum_duration_ms):
            raise PermissionError("scroll_duration_out_of_bounds")

    def issue(
        self,
        intent: ActionIntent,
        final_trajectory: tuple[tuple[int, int], ...],
        *,
        geometry: DisplayGeometry | None = None,
        duration_ms: int = 0,
        requested_action_kind: ActionKind | None = None,
    ) -> ReadOnlyPermitHandle:
        spec = self._policies.get(str(intent.action_key))
        if spec is None:
            raise PermissionError("action is not a specific read-only policy")
        if requested_action_kind is not None and requested_action_kind is not spec.action_kind:
            raise PermissionError("action_kind_mismatch")
        trajectory = tuple(_point(point) for point in final_trajectory)
        if not trajectory:
            raise PermissionError("final logical trajectory is required")
        self._validate_modality(spec, trajectory, int(duration_ms))
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
        token = secrets.token_urlsafe(32)
        record = _PermitRecord(
            action_key=spec.action_key,
            action_kind=spec.action_kind,
            policy_revision=self._policy_revision(spec),
            requested_target=str(intent.requested_target),
            observation_id=observation.observation_id,
            screenshot_hash=observation.screenshot_hash,
            page_classifier=observation.page_type,
            markers=tuple(str(marker) for marker in observation.markers),
            issue_capture_sequence=int(observation.capture_sequence),
            anchor_id=anchor.anchor_id,
            anchor_text_hash=hashlib.sha256(
                (anchor.text if isinstance(anchor, OcrObservedAnchor) else anchor.anchor_id).encode("utf-8")
            ).hexdigest()[:16],
            anchor_bbox=tuple(map(int, anchor.bbox)),
            allowed_region=allowed_region,
            final_trajectory=trajectory,
            duration_ms=int(duration_ms),
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
            logical_width=active_geometry.logical_width,
            logical_height=active_geometry.logical_height,
            process_id=self._process_id,
            session_id=self._session_id,
        )
        with self._registry_lock:
            self.cleanup_expired()
            if len(self._registry) >= self._registry_limit:
                raise PermissionError("permit_registry_capacity_exceeded")
            self._registry[token] = _RegistryEntry(record)
        return ReadOnlyPermitHandle(token)

    @staticmethod
    def permit_digest(permit: ReadOnlyPermitHandle | None) -> str:
        token = str(getattr(permit, "opaque_token", ""))
        if not token:
            return ""
        return hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]

    def registry_contains(self, permit: ReadOnlyPermitHandle) -> bool:
        with self._registry_lock:
            return str(getattr(permit, "opaque_token", "")) in self._registry

    def permit_status(self, permit: ReadOnlyPermitHandle) -> dict[str, object]:
        """Return non-sensitive proof without exporting registry contents or token."""

        with self._registry_lock:
            entry = self._registry.get(str(getattr(permit, "opaque_token", "")))
            return {
                "permit_digest": self.permit_digest(permit),
                "registered": entry is not None,
                "uses": entry.uses if entry is not None else 0,
                "max_uses": entry.record.max_uses if entry is not None else 0,
            }

    def action_key(self, permit: ReadOnlyPermitHandle | None) -> str:
        with self._registry_lock:
            entry = self._registry.get(str(getattr(permit, "opaque_token", "")))
            return entry.record.action_key if entry else "unclassified_action"

    def cleanup_expired(self) -> int:
        current = self.now()
        removed = 0
        with self._registry_lock:
            for token in tuple(self._registry):
                if current > self._registry[token].record.expires_at:
                    del self._registry[token]
                    removed += 1
        return removed

    def revoke(self, permit: ReadOnlyPermitHandle) -> bool:
        with self._registry_lock:
            return self._registry.pop(str(getattr(permit, "opaque_token", "")), None) is not None

    def _observation_matches(
        self, observation: PageObservation, record: _PermitRecord
    ) -> bool:
        if observation.page_type != record.page_classifier:
            return False
        if observation.capture_sequence == record.issue_capture_sequence:
            return False
        marker_text = self._marker_text(observation)
        spec = self._policies.get(record.action_key)
        if spec is None or any(marker.casefold() in marker_text for marker in spec.forbidden_markers):
            return False
        if observation.display_geometry is not None and observation.display_geometry.geometry_revision != record.geometry_revision:
            return False
        try:
            anchor, source, region = self._validate_spec(
                spec, observation, record.requested_target, record.final_trajectory
            )
        except PermissionError:
            return False
        anchor_hash = hashlib.sha256(
            (anchor.text if isinstance(anchor, OcrObservedAnchor) else anchor.anchor_id).encode("utf-8")
        ).hexdigest()[:16]
        return bool(
            source == record.anchor_source
            and tuple(map(int, anchor.bbox)) == record.anchor_bbox
            and anchor_hash == record.anchor_text_hash
            and region == record.allowed_region
        )

    def revalidate(self, permit: ReadOnlyPermitHandle) -> bool:
        """Compatibility check; authorization uses atomic ``consume`` below."""

        with self._registry_lock:
            entry = self._registry.get(str(getattr(permit, "opaque_token", "")))
            if entry is None:
                return False
            try:
                return self._observation_matches(self._fresh_observation(), entry.record)
            except PermissionError:
                return False

    def consume(
        self,
        permit: ReadOnlyPermitHandle,
        trajectory: tuple[tuple[int, int], ...],
        *,
        geometry: DisplayGeometry | None = None,
        action_kind: ActionKind,
        duration_ms: int = 0,
    ) -> tuple[bool, str, _PermitRecord | None, tuple[tuple[int, int], ...]]:
        """Validate and consume a capability in one registry critical section."""

        hook = self._before_consume
        if hook is not None:
            hook()
        logical = tuple(_point(point) for point in trajectory)
        with self._registry_lock:
            entry = self._registry.get(str(getattr(permit, "opaque_token", "")))
            if entry is None:
                return False, "permit_not_issued_by_registry", None, ()
            record = entry.record
            if record.process_id != os.getpid() or record.session_id != self._session_id:
                return False, "permit_process_or_session_mismatch", record, ()
            if record.action_kind is not action_kind:
                return False, "action_kind_mismatch", record, ()
            spec = self._policies.get(record.action_key)
            if spec is None:
                return False, "policy_no_longer_allows_action", record, ()
            if self._policy_revision(spec) != record.policy_revision:
                return False, "policy_revision_changed", record, ()
            if record.requested_target != spec.anchor_id:
                return False, "policy_anchor_changed", record, ()
            if record.postcondition != spec.postcondition:
                return False, "policy_postcondition_changed", record, ()
            current = self.now()
            if current > record.expires_at:
                return False, "read_only_permit_expired", record, ()
            if entry.uses >= record.max_uses:
                return False, "permit_already_consumed", record, ()
            if logical != record.final_trajectory:
                return False, "final_logical_trajectory_mismatch", record, ()
            if int(duration_ms) != record.duration_ms:
                return False, "swipe_duration_mismatch", record, ()
            if any(not _inside(point, record.allowed_region) for point in logical):
                return False, "final_logical_trajectory_outside_bounds", record, ()
            active_geometry = geometry or DisplayGeometry(
                logical_width=record.logical_width,
                logical_height=record.logical_height,
                physical_width=record.physical_width,
                physical_height=record.physical_height,
                geometry_revision=record.geometry_revision,
            )
            if not active_geometry.is_uniform:
                return False, "non_uniform_display_geometry", record, ()
            if active_geometry.geometry_revision != record.geometry_revision:
                return False, "display_geometry_revision_changed", record, ()
            if (
                active_geometry.physical_width != record.physical_width
                or active_geometry.physical_height != record.physical_height
            ):
                return False, "display_geometry_dimensions_changed", record, ()
            try:
                observation = self._fresh_observation()
                if not self._observation_matches(observation, record):
                    return False, "page_observation_changed_or_stale", record, ()
                anchor, source, allowed_region = self._validate_spec(
                    spec, observation, record.requested_target, logical
                )
                anchor_hash = hashlib.sha256(
                    (anchor.text if isinstance(anchor, OcrObservedAnchor) else anchor.anchor_id).encode("utf-8")
                ).hexdigest()[:16]
                if (
                    source != record.anchor_source
                    or tuple(map(int, anchor.bbox)) != record.anchor_bbox
                    or anchor_hash != record.anchor_text_hash
                    or allowed_region != record.allowed_region
                ):
                    return False, "trusted_anchor_or_policy_changed", record, ()
                physical = tuple(active_geometry.logical_to_physical(point) for point in logical)
            except PermissionError as error:
                return False, str(error), record, ()
            entry.uses += 1
            entry.consume_capture_sequence = observation.capture_sequence
            return True, "valid_trusted_read_only_permit", record, physical

    def verify_postcondition(
        self, permit: ReadOnlyPermitHandle, record: _PermitRecord
    ) -> tuple[bool, str]:
        with self._registry_lock:
            entry = self._registry.get(str(getattr(permit, "opaque_token", "")))
            if entry is None:
                return False, "postcondition_permit_missing"
            try:
                observation = self._fresh_observation()
            except PermissionError as error:
                return False, str(error)
            if observation.capture_sequence <= entry.consume_capture_sequence:
                return False, "postcondition_capture_not_independent"
            marker_text = self._marker_text(observation)
            spec = self._policies.get(record.action_key)
            if spec is None or any(marker.casefold() in marker_text for marker in spec.forbidden_markers):
                return False, "postcondition_forbidden_marker"
            predicates = {
                "daily_anchor_remains_valid": lambda: self._observation_matches(observation, record),
                "manual_anchor_remains_valid": lambda: self._observation_matches(observation, record),
                "page_identity_must_change_or_remain_safe": lambda: observation.page_type != "unknown",
                "top_level_hud_or_safe_startup_transition": lambda: observation.page_type in {"login", "home", "hud", "startup_overlay"},
            }
            predicate = predicates.get(record.postcondition)
            if predicate is None:
                return False, "postcondition_predicate_unregistered"
            return (True, "postcondition_verified") if predicate() else (False, "postcondition_failed")


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
    stage: str = "DENIED"
    action_kind: str = ""


class JournalStage(str, Enum):
    AUTHORIZED = "AUTHORIZED"
    EXECUTION_STARTED = "EXECUTION_STARTED"
    EXECUTED = "EXECUTED"
    POSTCONDITION_VERIFIED = "POSTCONDITION_VERIFIED"
    EXECUTION_FAILED = "EXECUTION_FAILED"
    POSTCONDITION_FAILED = "POSTCONDITION_FAILED"
    DENIED = "DENIED"


class TrustedInputExecutor(Protocol):
    def tap(self, physical_point: tuple[int, int]) -> object: ...

    def swipe(
        self, physical_trajectory: tuple[tuple[int, int], ...], duration_ms: int
    ) -> object: ...


class _CallableTapExecutor:
    def __init__(self, tap: Callable[[tuple[int, int]], object]):
        self._tap = tap

    def tap(self, physical_point: tuple[int, int]) -> object:
        return self._tap(physical_point)

    def swipe(self, _trajectory, _duration_ms):
        raise RuntimeError("trusted swipe executor unavailable")


class ReadOnlyActionGuard:
    BLOCKED_ACTIONS = {
        "transaction_buy", "transaction_sell", "all_buy", "all_sell",
        "haggle_confirm", "raise_price_confirm", "use_item", "use_book",
        "reward_claim", "fatigue_confirm", "bento_confirm", "depart",
        "account_settings",
    }

    def __setattr__(self, name, value):
        if name == "_executor" and "_executor" in self.__dict__:
            raise AttributeError("trusted executor is immutable after guard initialization")
        super().__setattr__(name, value)

    def __init__(
        self,
        trusted_executor: TrustedInputExecutor | Callable[[tuple[int, int]], object] | None = None,
        *,
        permit_issuer: ReadOnlyPermitIssuer | None = None,
        now: Callable[[], datetime] = _now,
        context_provider: Callable[[], str] | None = None,
    ):
        if trusted_executor is not None and callable(trusted_executor) and not hasattr(trusted_executor, "tap"):
            trusted_executor = _CallableTapExecutor(trusted_executor)
        self._executor = trusted_executor
        self.permit_issuer = permit_issuer
        self.now = now
        self._journal: list[ActionJournalEntry] = []
        self._journal_lock = threading.Lock()

    @property
    def trusted_executor(self) -> TrustedInputExecutor | None:
        return self._executor

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
        stage: JournalStage = JournalStage.DENIED,
        action_kind: ActionKind | None = None,
        permit: ReadOnlyPermitHandle | None = None,
        record: _PermitRecord | None = None,
    ) -> bool:
        authoritative = record
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
            stage=stage.value,
            action_kind=(authoritative.action_kind.value if authoritative else (action_kind.value if action_kind else "")),
        )
        with self._journal_lock:
            self._journal.append(entry)
        return bool(allowed)

    def tap(self, action_key: str, coordinate: tuple[int, int], page_context: str = "") -> bool:
        key = str(action_key)
        reason = "action_prohibited_in_read_only_mode" if key in self.BLOCKED_ACTIONS else "trusted_read_only_permit_required"
        return self._record(key, (_point(coordinate),), allowed=False, reason=reason, stage=JournalStage.DENIED, action_kind=ActionKind.TAP)

    def issue_permit(self, **_kwargs) -> ReadOnlyPermitHandle:
        raise PermissionError("caller cannot self-issue a permit; trusted issuer required")

    def _authorize(
        self,
        trajectory: tuple[tuple[int, int], ...],
        *,
        permit: ReadOnlyPermit | None,
        intent: ActionIntent | None,
        geometry: DisplayGeometry | None,
        action_kind: ActionKind,
        duration_ms: int,
    ) -> bool:
        key = str(intent.action_key if intent is not None else self.permit_issuer.action_key(permit) if self.permit_issuer is not None else "unclassified_action")
        if key in self.BLOCKED_ACTIONS:
            return self._record(key, trajectory, allowed=False, reason="action_prohibited_in_read_only_mode", stage=JournalStage.DENIED, action_kind=action_kind, permit=permit)
        if self._executor is None:
            return self._record(key, trajectory, allowed=False, reason="trusted_executor_unavailable", stage=JournalStage.DENIED, action_kind=action_kind, permit=permit)
        if permit is None and intent is not None:
            if self.permit_issuer is None:
                return self._record(key, trajectory, allowed=False, reason="trusted_permit_issuer_unavailable")
            try:
                permit = self.permit_issuer.issue(
                    intent, trajectory, geometry=geometry, duration_ms=duration_ms,
                    requested_action_kind=action_kind,
                )
            except PermissionError as error:
                return self._record(key, trajectory, allowed=False, reason=str(error), stage=JournalStage.DENIED, action_kind=action_kind)
        if permit is None:
            return self._record(key, trajectory, allowed=False, reason="read_only_permit_required", stage=JournalStage.DENIED, action_kind=action_kind)
        if self.permit_issuer is None:
            return self._record(key, trajectory, allowed=False, reason="trusted_permit_issuer_unavailable", stage=JournalStage.DENIED, action_kind=action_kind, permit=permit)
        allowed, reason, record, physical = self.permit_issuer.consume(
            permit, trajectory, geometry=geometry, action_kind=action_kind, duration_ms=duration_ms
        )
        self._record(
            key,
            trajectory,
            physical_trajectory=physical,
            allowed=allowed,
            reason=reason,
            stage=JournalStage.AUTHORIZED if allowed else JournalStage.DENIED,
            action_kind=action_kind,
            permit=permit,
            record=record,
        )
        if not allowed:
            return False
        self._record(key, trajectory, physical_trajectory=physical, allowed=True, reason="execution_started", stage=JournalStage.EXECUTION_STARTED, action_kind=action_kind, permit=permit, record=record)
        try:
            if action_kind is ActionKind.TAP:
                self._executor.tap(physical[0])
            else:
                self._executor.swipe(physical, duration_ms)
        except Exception:
            self._record(key, trajectory, physical_trajectory=physical, allowed=False, reason="execution_failed", stage=JournalStage.EXECUTION_FAILED, action_kind=action_kind, permit=permit, record=record)
            raise
        self._record(key, trajectory, physical_trajectory=physical, allowed=True, reason="hardware_execution_completed", stage=JournalStage.EXECUTED, action_kind=action_kind, permit=permit, record=record)
        verified, post_reason = self.permit_issuer.verify_postcondition(permit, record)
        if not verified:
            self._record(key, trajectory, physical_trajectory=physical, allowed=False, reason=post_reason, stage=JournalStage.POSTCONDITION_FAILED, action_kind=action_kind, permit=permit, record=record)
            return False
        self._record(key, trajectory, physical_trajectory=physical, allowed=True, reason=post_reason, stage=JournalStage.POSTCONDITION_VERIFIED, action_kind=action_kind, permit=permit, record=record)
        return True

    def authorize_coordinate(
        self,
        coordinate: tuple[int, int],
        *,
        permit: ReadOnlyPermitHandle | None = None,
        intent: ActionIntent | None = None,
        geometry: DisplayGeometry | None = None,
        **caller_claims,
    ) -> bool:
        logical = (_point(coordinate),)
        if any(caller_claims.values()) and permit is None and intent is None:
            return self._record("unclassified_tap", logical, allowed=False, reason="caller_page_claims_are_untrusted")
        return self._authorize(
            logical, permit=permit, intent=intent, geometry=geometry,
            action_kind=ActionKind.TAP, duration_ms=0,
        )

    def authorize_swipe(
        self,
        start: tuple[int, int],
        end: tuple[int, int],
        *,
        trajectory: tuple[tuple[int, int], ...] | None = None,
        permit: ReadOnlyPermitHandle | None = None,
        intent: ActionIntent | None = None,
        geometry: DisplayGeometry | None = None,
        duration_ms: int = 100,
        **_caller_claims,
    ) -> bool:
        path = tuple(_point(point) for point in (trajectory or (start, end)))
        if not path or path[0] != _point(start) or path[-1] != _point(end):
            return self._record("unclassified_swipe", path, allowed=False, reason="invalid_final_swipe_trajectory")
        return self._authorize(
            path, permit=permit, intent=intent, geometry=geometry,
            action_kind=ActionKind.SWIPE, duration_ms=int(duration_ms),
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
    "ActionIntent", "ActionJournalEntry", "ActionKind", "AnchorResolver",
    "CalibratedStaticRegion", "CoordinateSpace", "DisplayGeometry",
    "ObservedAnchor", "OcrObservedAnchor", "PageObservation", "PageObserver",
    "JournalStage", "ReadOnlyActionGuard", "ReadOnlyPermit", "ReadOnlyPermitHandle",
    "ReadOnlyPermitIssuer", "ReadOnlyPolicySpec", "TrustedInputExecutor",
    "installed_read_only_guard",
]
