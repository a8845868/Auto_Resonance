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
_SAFE_CODE = re.compile(r"^[a-z0-9][a-z0-9_.:-]{0,95}$")


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
            screen_width=screen_width,
            screen_height=screen_height,
            dpi=dpi,
            scale_factor=device_width / capture_width,
            screen_coordinate_applicable=screen_size is not None,
        )

    @property
    def complete(self) -> bool:
        screen_complete = self.screen_point is not None or not self.screen_coordinate_applicable
        return bool(
            self.source_coordinate_space
            and self.source_point
            and self.render_client_point
            and self.device_point
            and screen_complete
        )


@dataclass(frozen=True, slots=True)
class PostNavigationObservation:
    post_observation_index: int
    post_frame_sha256: str
    post_state: str
    positive_cues: tuple[str, ...]
    negative_cues: tuple[str, ...]
    reason_codes: tuple[str, ...]
    postcondition_result: str
    observed_at: str = field(default_factory=_now)


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
    attempt_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    dispatch_requested: bool = False
    dispatch_acknowledged: bool = False
    dispatch_result: str = "not_requested"
    dispatch_timestamp: str = ""
    post_observations: list[PostNavigationObservation] = field(default_factory=list)

    def mark_dispatch(self, *, requested: bool, acknowledged: bool, result: str) -> None:
        self.dispatch_requested = bool(requested)
        self.dispatch_acknowledged = bool(acknowledged)
        self.dispatch_result = str(result)
        self.dispatch_timestamp = _now()

    def add_post_observation(
        self,
        *,
        frame,
        state: str,
        positive_cues: Iterable[str] = (),
        negative_cues: Iterable[str] = (),
        reason_codes: Iterable[str] = (),
        postcondition_result: str,
    ) -> PostNavigationObservation:
        observation = PostNavigationObservation(
            post_observation_index=len(self.post_observations) + 1,
            post_frame_sha256=frame_sha256(frame),
            post_state=str(state),
            positive_cues=_codes(positive_cues),
            negative_cues=_codes(negative_cues),
            reason_codes=_codes(reason_codes),
            postcondition_result=str(postcondition_result),
        )
        self.post_observations.append(observation)
        return observation

    def to_dict(self) -> dict:
        document = asdict(self)
        latest = self.post_observations[-1] if self.post_observations else None
        document.update(
            {
                "capture_width": self.coordinate_chain.capture_width,
                "capture_height": self.coordinate_chain.capture_height,
                "render_client_width": self.coordinate_chain.render_client_width,
                "render_client_height": self.coordinate_chain.render_client_height,
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
            }
        )
        return document


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
