"""Privacy-safe evidence for bounded navigation attempts.

This module records what was observed, requested, dispatched, and verified. It
does not classify pages, choose actions, send input, or retry an action.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterable

from loguru import logger


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_EVIDENCE_DIR = ROOT / "logs" / "navigation_attempts"
EVIDENCE_SCHEMA_VERSION = "2.0"
_SAFE_CODE = re.compile(r"^[a-z0-9][a-z0-9_.:-]{0,95}$")
_NORMALIZED_BASE_PAGE_ALIASES = {
    "ACTION_SUMMARY_ENTRY_VISIBLE": "GLOBAL_PREP_PAGE",
    "INVENTORY": "INVENTORY_PAGE_VISIBLE",
    "CITY_ENTRY_VISIBLE": "HOME_CITY_ENTRY_CONTROL_VISIBLE",
    "CITY_DETAIL": "CITY_DETAIL_VISIBLE",
}


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def _codes(values: Iterable[str]) -> tuple[str, ...]:
    """Keep semantic cue identifiers and discard OCR/body text."""

    result: list[str] = []
    for value in values:
        candidate = str(value).strip().casefold()
        result.append(candidate if _SAFE_CODE.fullmatch(candidate) else "redacted_non_code")
    return tuple(result)


def frame_sha256(frame) -> str:
    known = str(getattr(frame, "raw_frame_hash", "") or "").strip()
    if known:
        return known
    image = getattr(frame, "image", frame)
    if hasattr(image, "tobytes"):
        return hashlib.sha256(image.tobytes()).hexdigest()
    return ""


@dataclass(frozen=True, slots=True)
class CoordinateChain:
    source_coordinate_space: str
    source_point: tuple[int, int]
    normalized_point: tuple[float, float]
    render_client_point: tuple[int, int]
    screen_point: tuple[int, int] | None
    device_point: tuple[int, int]
    capture_width: int
    capture_height: int
    render_client_width: int
    render_client_height: int
    device_width: int
    device_height: int
    screen_width: int | None = None
    screen_height: int | None = None
    dpi: float | None = None
    scale_factor: float = 1.0
    screen_coordinate_applicable: bool = False

    @classmethod
    def from_capture_point(
        cls,
        point: tuple[int, int],
        *,
        capture_size: tuple[int, int],
        render_client_size: tuple[int, int],
        device_size: tuple[int, int] | None = None,
        screen_size: tuple[int, int] | None = None,
        dpi: float | None = None,
        source_coordinate_space: str = "CAPTURE_PIXELS",
    ) -> "CoordinateChain":
        capture_width, capture_height = map(int, capture_size)
        render_width, render_height = map(int, render_client_size)
        device_width, device_height = map(int, device_size or render_client_size)
        if min(capture_width, capture_height, render_width, render_height) <= 0:
            raise ValueError("coordinate dimensions must be positive")
        source = int(point[0]), int(point[1])
        if not (0 <= source[0] < capture_width and 0 <= source[1] < capture_height):
            raise ValueError("source point outside capture")
        normalized = source[0] / capture_width, source[1] / capture_height
        render = (
            min(render_width - 1, int(round(normalized[0] * render_width))),
            min(render_height - 1, int(round(normalized[1] * render_height))),
        )
        device = (
            min(device_width - 1, int(round(normalized[0] * device_width))),
            min(device_height - 1, int(round(normalized[1] * device_height))),
        )
        screen = None
        if screen_size is not None:
            screen_width, screen_height = map(int, screen_size)
            screen = (
                min(screen_width - 1, int(round(normalized[0] * screen_width))),
                min(screen_height - 1, int(round(normalized[1] * screen_height))),
            )
        else:
            screen_width = screen_height = None
        return cls(
            source_coordinate_space=source_coordinate_space,
            source_point=source,
            normalized_point=(round(normalized[0], 8), round(normalized[1], 8)),
            render_client_point=render,
            screen_point=screen,
            device_point=device,
            capture_width=capture_width,
            capture_height=capture_height,
            render_client_width=render_width,
            render_client_height=render_height,
            device_width=device_width,
            device_height=device_height,
            screen_width=screen_width,
            screen_height=screen_height,
            dpi=dpi,
            scale_factor=device_width / capture_width,
            screen_coordinate_applicable=screen_size is not None,
        )

    @property
    def complete(self) -> bool:
        screen_complete = (
            self.screen_point is not None or not self.screen_coordinate_applicable
        )
        return bool(
            self.source_coordinate_space
            and self.source_point
            and self.render_client_point
            and self.device_point
            and screen_complete
        )


@dataclass(frozen=True, slots=True)
class ScreenToDeviceCoordinateTransform:
    """Explicit capture-to-device mapping for a current display viewport."""

    capture_width: int
    capture_height: int
    device_logical_width: int
    device_logical_height: int
    device_physical_width: int
    device_physical_height: int
    rotation: int = 0
    viewport_offset: tuple[int, int] = (0, 0)
    viewport_size: tuple[int, int] | None = None

    def __post_init__(self) -> None:
        dimensions = (
            self.capture_width,
            self.capture_height,
            self.device_logical_width,
            self.device_logical_height,
            self.device_physical_width,
            self.device_physical_height,
        )
        if any(int(value) <= 0 for value in dimensions):
            raise ValueError("coordinate_transform_dimensions_invalid")
        viewport = self.viewport_size or (
            self.device_logical_width,
            self.device_logical_height,
        )
        if min(map(int, viewport)) <= 0:
            raise ValueError("coordinate_transform_viewport_invalid")

    @property
    def effective_viewport_size(self) -> tuple[int, int]:
        return self.viewport_size or (
            self.device_logical_width,
            self.device_logical_height,
        )

    @property
    def reason_codes(self) -> tuple[str, ...]:
        reasons: list[str] = []
        if int(self.rotation) % 360 != 0:
            reasons.append("rotation_mismatch")
        offset_x, offset_y = map(int, self.viewport_offset)
        viewport_width, viewport_height = map(int, self.effective_viewport_size)
        if (
            offset_x < 0
            or offset_y < 0
            or offset_x + viewport_width > self.device_logical_width
            or offset_y + viewport_height > self.device_logical_height
        ):
            reasons.append("viewport_outside_device_logical_bounds")
        capture_aspect = self.capture_width / self.capture_height
        viewport_aspect = viewport_width / viewport_height
        if abs(capture_aspect - viewport_aspect) > 0.005:
            reasons.append("capture_viewport_aspect_mismatch")
        return tuple(reasons)

    @property
    def mapping_status(self) -> str:
        if self.reason_codes:
            return "BLOCKED"
        if (
            self.capture_width == self.device_logical_width
            and self.capture_height == self.device_logical_height
            and self.device_logical_width == self.device_physical_width
            and self.device_logical_height == self.device_physical_height
            and self.viewport_offset == (0, 0)
            and self.effective_viewport_size
            == (self.device_logical_width, self.device_logical_height)
        ):
            return "IDENTITY"
        return "TRANSFORMED"

    def map_point(
        self, point: tuple[int, int]
    ) -> tuple[tuple[int, int], tuple[int, int], float]:
        if self.mapping_status == "BLOCKED":
            raise PermissionError(self.reason_codes[0])
        source_x, source_y = map(int, point)
        if not (
            0 <= source_x < self.capture_width
            and 0 <= source_y < self.capture_height
        ):
            raise ValueError("capture_point_outside_bounds")
        viewport_width, viewport_height = self.effective_viewport_size
        offset_x, offset_y = self.viewport_offset
        normalized_x = source_x / self.capture_width
        normalized_y = source_y / self.capture_height
        logical = (
            min(
                self.device_logical_width - 1,
                offset_x + int(round(normalized_x * viewport_width)),
            ),
            min(
                self.device_logical_height - 1,
                offset_y + int(round(normalized_y * viewport_height)),
            ),
        )
        physical = (
            min(
                self.device_physical_width - 1,
                int(round(logical[0] * self.device_physical_width /
                          self.device_logical_width)),
            ),
            min(
                self.device_physical_height - 1,
                int(round(logical[1] * self.device_physical_height /
                          self.device_logical_height)),
            ),
        )
        round_trip = (
            (logical[0] - offset_x) * self.capture_width / viewport_width,
            (logical[1] - offset_y) * self.capture_height / viewport_height,
        )
        error = max(abs(round_trip[0] - source_x), abs(round_trip[1] - source_y))
        return logical, physical, round(float(error), 6)

    def to_dict(self) -> dict[str, object]:
        return {
            "capture_width": self.capture_width,
            "capture_height": self.capture_height,
            "device_logical_width": self.device_logical_width,
            "device_logical_height": self.device_logical_height,
            "device_physical_width": self.device_physical_width,
            "device_physical_height": self.device_physical_height,
            "rotation": self.rotation,
            "viewport_offset": list(self.viewport_offset),
            "viewport_size": list(self.effective_viewport_size),
            "coordinate_mapping_status": self.mapping_status,
            "reason_codes": list(self.reason_codes),
        }

@dataclass(frozen=True, slots=True)
class PostNavigationObservation:
    post_observation_index: int
    post_frame_sha256: str
    post_state: str
    positive_cues: tuple[str, ...]
    negative_cues: tuple[str, ...]
    reason_codes: tuple[str, ...]
    postcondition_result: str
    source_capture_id: str = ""
    capture_sequence: int | None = None
    session_generation: int | None = None
    observed_at: str = field(default_factory=_now)


@dataclass(frozen=True, slots=True)
class DerivedObservationProvenance:
    source_capture_id: str
    capture_sequence: int | None
    session_generation: int | None
    valid: bool
    reason: str


class DerivedObservationProvenanceContract:
    """Resolve capture provenance without inventing freshness for derived facts."""

    @staticmethod
    def resolve(
        *,
        parent_observation: PostNavigationObservation,
        frame=None,
        new_capture_performed: bool,
    ) -> DerivedObservationProvenance:
        frame_capture_id = str(getattr(frame, "source_capture_id", "") or "")
        frame_capture_sequence = getattr(frame, "capture_sequence", None)
        frame_session_generation = getattr(
            frame,
            "session_generation",
            getattr(frame, "backend_generation", None),
        )

        if new_capture_performed:
            if not frame_capture_id:
                return DerivedObservationProvenance(
                    "", None, None, False, "new_capture_provenance_missing"
                )
            return DerivedObservationProvenance(
                frame_capture_id,
                (
                    int(frame_capture_sequence)
                    if frame_capture_sequence is not None
                    else None
                ),
                (
                    int(frame_session_generation)
                    if frame_session_generation is not None
                    else None
                ),
                True,
                "bound_to_new_capture",
            )

        parent_capture_id = str(parent_observation.source_capture_id or "")
        if not parent_capture_id:
            return DerivedObservationProvenance(
                "", None, None, False, "parent_capture_provenance_missing"
            )
        if frame_capture_id and frame_capture_id != parent_capture_id:
            return DerivedObservationProvenance(
                "", None, None, False, "derived_capture_provenance_conflict"
            )
        return DerivedObservationProvenance(
            parent_capture_id,
            parent_observation.capture_sequence,
            parent_observation.session_generation,
            True,
            "inherited_from_parent_observation",
        )


@dataclass(slots=True)
class NavigationAttemptEvidence:
    task_name: str
    entry_name: str
    pre_state: str
    pre_frame_sha256: str
    coordinate_chain: CoordinateChain
    candidate_type: str
    candidate_bbox: tuple[int, int, int, int] | None
    candidate_score: float | None
    candidate_count: int
    dispatch_backend: str
    random_offset_enabled: bool = False
    random_offset_requested: bool = False
    actual_dispatched_point: tuple[int, int] | None = None
    station_detection_result: str = "not_run"
    station_id: str | None = None
    station_confidence: str | None = None
    station_evidence_ids: tuple[str, ...] = ()
    attempt_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    dispatch_requested: bool = False
    dispatch_acknowledged: bool = False
    dispatch_acknowledged_semantics: str = "COMMAND_RETURN_ONLY"
    dispatch_command_returned: bool = False
    dispatch_backend_error: str | None = None
    touch_effect_observed: bool = False
    post_frame_changed: bool = False
    target_page_changed: bool = False
    target_control_disappeared: bool = False
    trusted_postcondition_observed: bool = False
    dispatch_result: str = "not_requested"
    dispatch_timestamp: str = ""
    detector_nondeterminism: bool = False
    evidence_invariant_check: str = "PASS"
    evidence_invariant_reasons: list[str] = field(default_factory=list)
    post_observations: list[PostNavigationObservation] = field(default_factory=list)

    def mark_dispatch(self, *, requested: bool, acknowledged: bool, result: str) -> None:
        self.dispatch_requested = bool(requested)
        # Compatibility: ``dispatch_acknowledged`` used to be interpreted as
        # device/game acknowledgement.  The backend only proves that its call
        # returned, so retain the field while publishing its exact semantics.
        self.dispatch_acknowledged = bool(acknowledged)
        self.dispatch_command_returned = bool(acknowledged)
        self.dispatch_backend_error = (
            None if acknowledged else (str(result) if requested else None)
        )
        self.dispatch_result = str(result)
        self.dispatch_timestamp = _now()

    def mark_post_effect(
        self,
        *,
        frame_changed: bool,
        target_page_changed: bool,
        target_control_disappeared: bool = False,
        trusted_postcondition_observed: bool = False,
    ) -> None:
        """Record visual effect without inferring touch delivery or hitbox cause."""

        self.post_frame_changed = self.post_frame_changed or bool(frame_changed)
        self.target_page_changed = self.target_page_changed or bool(target_page_changed)
        self.target_control_disappeared = (
            self.target_control_disappeared or bool(target_control_disappeared)
        )
        self.trusted_postcondition_observed = (
            self.trusted_postcondition_observed
            or bool(trusted_postcondition_observed)
        )
        self.touch_effect_observed = self.touch_effect_observed or any((
            frame_changed,
            target_page_changed,
            target_control_disappeared,
            trusted_postcondition_observed,
        ))

    def mark_station_detection(
        self,
        *,
        result: str,
        station_id: str | None = None,
        confidence: str | None = None,
        evidence_ids: Iterable[str] = (),
    ) -> None:
        self.station_detection_result = str(result)
        self.station_id = str(station_id) if station_id else None
        self.station_confidence = str(confidence) if confidence else None
        self.station_evidence_ids = _codes(evidence_ids)

    def add_post_observation(
        self,
        *,
        frame,
        state: str,
        positive_cues: Iterable[str] = (),
        negative_cues: Iterable[str] = (),
        reason_codes: Iterable[str] = (),
        postcondition_result: str,
        source_base_page: str | None = None,
        normalized_base_page: str | None = None,
        target_control_disappeared: bool = False,
        trusted_postcondition_observed: bool = False,
        source_capture_id: str = "",
        capture_sequence: int | None = None,
        session_generation: int | None = None,
    ) -> PostNavigationObservation:
        post_hash = frame_sha256(frame)
        source_page = _NORMALIZED_BASE_PAGE_ALIASES.get(
            str(source_base_page or self.pre_state),
            str(source_base_page or self.pre_state),
        )
        current_page = _NORMALIZED_BASE_PAGE_ALIASES.get(
            str(normalized_base_page or state),
            str(normalized_base_page or state),
        )
        frame_changed = bool(post_hash and post_hash != self.pre_frame_sha256)
        classified_page_changed = bool(
            source_page
            and current_page
            and source_page != "UNKNOWN"
            and current_page != "UNKNOWN"
            and source_page != current_page
        )
        if not frame_changed and classified_page_changed:
            self.detector_nondeterminism = True
            self.evidence_invariant_check = "FAIL"
            if "same_hash_different_classification" not in self.evidence_invariant_reasons:
                self.evidence_invariant_reasons.append("same_hash_different_classification")
        # A visual transition can never be derived from byte-identical frames.
        # The classification conflict above remains diagnostic but is not
        # allowed to become positive click-effect evidence.
        page_changed = bool(frame_changed and classified_page_changed)
        effective_target_disappeared = bool(frame_changed and target_control_disappeared)
        effective_trusted_postcondition = bool(frame_changed and trusted_postcondition_observed)
        self.mark_post_effect(
            frame_changed=frame_changed,
            target_page_changed=page_changed,
            target_control_disappeared=effective_target_disappeared,
            trusted_postcondition_observed=effective_trusted_postcondition,
        )
        if str(postcondition_result).casefold() == "pass" and not any((
            self.post_frame_changed,
            self.target_page_changed,
            self.target_control_disappeared,
            self.trusted_postcondition_observed,
        )):
            raise ValueError("postcondition_pass_without_observed_effect")
        observation = PostNavigationObservation(
            post_observation_index=len(self.post_observations) + 1,
            post_frame_sha256=post_hash,
            post_state=str(state),
            positive_cues=_codes(positive_cues),
            negative_cues=_codes(negative_cues),
            reason_codes=_codes(reason_codes),
            postcondition_result=str(postcondition_result),
            source_capture_id=str(source_capture_id),
            capture_sequence=(int(capture_sequence) if capture_sequence is not None else None),
            session_generation=(int(session_generation) if session_generation is not None else None),
        )
        self.post_observations.append(observation)
        return observation

    def to_dict(self) -> dict:
        document = asdict(self)
        latest = self.post_observations[-1] if self.post_observations else None
        document.update(
            {
                "evidence_schema_version": EVIDENCE_SCHEMA_VERSION,
                "capture_width": self.coordinate_chain.capture_width,
                "capture_height": self.coordinate_chain.capture_height,
                "render_client_width": self.coordinate_chain.render_client_width,
                "render_client_height": self.coordinate_chain.render_client_height,
                "device_width": self.coordinate_chain.device_width,
                "device_height": self.coordinate_chain.device_height,
                "screen_width": self.coordinate_chain.screen_width,
                "screen_height": self.coordinate_chain.screen_height,
                "dpi": self.coordinate_chain.dpi,
                "scale_factor": self.coordinate_chain.scale_factor,
                "source_coordinate_space": self.coordinate_chain.source_coordinate_space,
                "source_point": self.coordinate_chain.source_point,
                "normalized_point": self.coordinate_chain.normalized_point,
                "render_client_point": self.coordinate_chain.render_client_point,
                "screen_point": self.coordinate_chain.screen_point,
                "device_point": self.coordinate_chain.device_point,
                "coordinate_chain_complete": self.coordinate_chain.complete,
                "post_observation_index": latest.post_observation_index if latest else 0,
                "post_frame_sha256": latest.post_frame_sha256 if latest else "",
                "post_state": latest.post_state if latest else "not_observed",
                "positive_cues": latest.positive_cues if latest else (),
                "negative_cues": latest.negative_cues if latest else (),
                "reason_codes": latest.reason_codes if latest else (),
                "postcondition_result": latest.postcondition_result if latest else "not_observed",
                "EVIDENCE_INVARIANT_CHECK": self.evidence_invariant_check,
                "DETECTOR_NONDETERMINISM": self.detector_nondeterminism,
            }
        )
        return document


def classify_native_accepted_no_effect(
    *,
    native_accepted: bool,
    nemu_frame_changed: bool,
    adb_crosscheck_available: bool,
    adb_frame_changed: bool | None,
) -> str:
    """Separate a stale NEMU capture from a real no-effect input result."""

    if not native_accepted or nemu_frame_changed:
        return "NOT_APPLICABLE"
    if not adb_crosscheck_available or adb_frame_changed is None:
        return "CROSSCHECK_UNAVAILABLE"
    if adb_frame_changed:
        return "NEMU_CAPTURE_STALE_SUSPECTED"
    return "TOUCH_NO_EFFECT_OR_TARGET_INVALID"


def record_navigation_attempt(
    evidence: NavigationAttemptEvidence,
    *,
    directory: Path | None = None,
) -> bool:
    """Persist one complete attempt; failure is diagnostic and never a retry signal."""

    target_dir = Path(directory or DEFAULT_EVIDENCE_DIR)
    target = target_dir / f"{evidence.attempt_id}.json"
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        temporary.write_text(
            json.dumps(evidence.to_dict(), ensure_ascii=False, sort_keys=True, indent=2),
            encoding="utf-8",
        )
        os.replace(temporary, target)
        return True
    except OSError as error:
        logger.warning(f"navigation evidence write failed: {type(error).__name__}")
        return False
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
