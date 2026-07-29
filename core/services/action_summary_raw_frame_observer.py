"""Pure raw-frame observer for current Action Summary visual facts.

The observer consumes one frame that was captured by its caller.  It may read
the frame pixels and OCR once, but it cannot capture, navigate, dispatch input,
retry, persist, evaluate policy, or issue authorization.  Raw OCR text is never
included in its output.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta
from typing import Mapping, Sequence

from core.services.action_summary_missing_fact_acquisition import (
    AcquiredFact,
    CurrentActionSummaryVisualSnapshot,
    observe_current_action_summary_page_facts,
)
from core.services.action_summary_product_model import (
    ActionSummaryPageModel,
    PageConfidence,
    observe_action_summary_page,
)
from core.services.navigation_evidence import frame_sha256


RAW_OBSERVER_ID = "action_summary_current_page_raw_frame_observer"
RAW_OBSERVER_CALLABLE_ID = (
    "core.services.action_summary_raw_frame_observer."
    "observe_action_summary_current_page_visuals"
)
_SCHEMA_VERSION = "1.0"
_COST_PATTERN = re.compile(r"-\s*(\d+)")


@dataclass(frozen=True, slots=True)
class RawVisualFactObservation:
    card_match_key: str
    fact_type: str
    bbox: tuple[int, int, int, int]
    semantic_hash: str
    confidence: str
    reason_codes: tuple[str, ...]
    evidence_ids: tuple[str, ...]
    numeric_value: int | None = None
    semantic_id: str | None = None

    def to_dict(self) -> dict[str, object]:
        document = asdict(self)
        document["bbox"] = list(self.bbox)
        document["reason_codes"] = list(self.reason_codes)
        document["evidence_ids"] = list(self.evidence_ids)
        return document


@dataclass(frozen=True, slots=True)
class RawVisualRejectedCandidate:
    fact_type: str
    bbox: tuple[int, int, int, int] | None
    semantic_hash: str
    reason_codes: tuple[str, ...]
    evidence_ids: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        document = asdict(self)
        document["bbox"] = list(self.bbox) if self.bbox is not None else None
        document["reason_codes"] = list(self.reason_codes)
        document["evidence_ids"] = list(self.evidence_ids)
        return document


@dataclass(frozen=True, slots=True)
class RawActionSummaryVisualObservation:
    schema_version: str
    observer_id: str
    observation_status: str
    source_capture_id: str | None
    source_frame_sha256: str | None
    captured_at: str | None
    page_state: str
    page_confidence: str
    resource_cost_observations: tuple[RawVisualFactObservation, ...]
    resource_identity_observations: tuple[RawVisualFactObservation, ...]
    resource_balance_observations: tuple[RawVisualFactObservation, ...]
    reward_target_observations: tuple[RawVisualFactObservation, ...]
    rejected_candidates: tuple[RawVisualRejectedCandidate, ...]
    ambiguous_candidates: tuple[RawVisualRejectedCandidate, ...]
    evidence_ids: tuple[str, ...]
    normalization_snapshots: tuple[CurrentActionSummaryVisualSnapshot, ...]
    ocr_calls: int
    capture_calls: int = 0
    page_input_dispatches: int = 0
    business_dispatches: int = 0
    irreversible_actions: int = 0

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "observer_id": self.observer_id,
            "observation_status": self.observation_status,
            "source_capture_id": self.source_capture_id,
            "source_frame_sha256": self.source_frame_sha256,
            "captured_at": self.captured_at,
            "page_state": self.page_state,
            "page_confidence": self.page_confidence,
            "resource_cost_observations": [
                item.to_dict() for item in self.resource_cost_observations
            ],
            "resource_identity_observations": [
                item.to_dict() for item in self.resource_identity_observations
            ],
            "resource_balance_observations": [
                item.to_dict() for item in self.resource_balance_observations
            ],
            "reward_target_observations": [
                item.to_dict() for item in self.reward_target_observations
            ],
            "rejected_candidates": [
                item.to_dict() for item in self.rejected_candidates
            ],
            "ambiguous_candidates": [
                item.to_dict() for item in self.ambiguous_candidates
            ],
            "evidence_ids": list(self.evidence_ids),
            "normalization_snapshots": [
                asdict(item) for item in self.normalization_snapshots
            ],
            "ocr_calls": self.ocr_calls,
            "capture_calls": self.capture_calls,
            "page_input_dispatches": self.page_input_dispatches,
            "business_dispatches": self.business_dispatches,
            "irreversible_actions": self.irreversible_actions,
        }


class _CachedFrame:
    def __init__(self, frame: object, items: Sequence[object]) -> None:
        self.image = getattr(frame, "image", None)
        self.source_capture_id = getattr(frame, "source_capture_id", None)
        self.raw_frame_hash = getattr(frame, "raw_frame_hash", "")
        self.captured_at = getattr(frame, "captured_at", None)
        self._items = tuple(items)

    def ocr(self) -> list[object]:
        return list(self._items)


def _normalize(value: object) -> str:
    return "".join(str(value or "").replace("：", ":").split())


def _is_sha256(value: object) -> bool:
    return bool(re.fullmatch(r"[0-9a-fA-F]{64}", str(value or "")))


def _bbox(item: Mapping[str, object]) -> tuple[int, int, int, int] | None:
    points = item.get("position")
    if not isinstance(points, Sequence) or isinstance(points, (str, bytes)):
        return None
    try:
        xs = [int(float(point[0])) for point in points]
        ys = [int(float(point[1])) for point in points]
    except (IndexError, TypeError, ValueError):
        return None
    if not xs or not ys:
        return None
    bounds = min(xs), min(ys), max(xs), max(ys)
    if bounds[2] <= bounds[0] or bounds[3] <= bounds[1]:
        return None
    return bounds


def _center(bounds: tuple[int, int, int, int]) -> tuple[int, int]:
    return (bounds[0] + bounds[2]) // 2, (bounds[1] + bounds[3]) // 2


def _inside(
    point: tuple[int, int], bounds: tuple[int, int, int, int]
) -> bool:
    return bounds[0] <= point[0] <= bounds[2] and bounds[1] <= point[1] <= bounds[3]


def _semantic_hash(kind: str, normalized_text: str) -> str:
    return hashlib.sha256(
        f"{kind}|{normalized_text}".encode("utf-8")
    ).hexdigest()


def _evidence_id(
    kind: str,
    semantic_hash: str,
    bounds: tuple[int, int, int, int] | None,
    card_match_key: str | None = None,
) -> str:
    payload = "|".join((
        kind,
        semantic_hash,
        ",".join(map(str, bounds)) if bounds is not None else "NO_BBOX",
        card_match_key or "NO_CARD",
    ))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def _captured_at(frame: object) -> tuple[str | None, datetime | None]:
    value = getattr(frame, "captured_at", None)
    text = value.isoformat() if hasattr(value, "isoformat") else str(value or "").strip()
    if not text:
        return None, None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return text, None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return text, None
    return text, parsed


def _empty(
    *,
    status: str,
    capture_id: str | None,
    frame_hash: str | None,
    captured_at: str | None,
    page_state: str,
    page_confidence: str,
    ocr_calls: int,
) -> RawActionSummaryVisualObservation:
    return RawActionSummaryVisualObservation(
        schema_version=_SCHEMA_VERSION,
        observer_id=RAW_OBSERVER_ID,
        observation_status=status,
        source_capture_id=capture_id,
        source_frame_sha256=frame_hash,
        captured_at=captured_at,
        page_state=page_state,
        page_confidence=page_confidence,
        resource_cost_observations=(),
        resource_identity_observations=(),
        resource_balance_observations=(),
        reward_target_observations=(),
        rejected_candidates=(),
        ambiguous_candidates=(),
        evidence_ids=(),
        normalization_snapshots=(),
        ocr_calls=ocr_calls,
    )


def observe_action_summary_current_page_visuals(
    frame: object,
    page_model: ActionSummaryPageModel | None = None,
) -> RawActionSummaryVisualObservation:
    """Observe current-page facts from one supplied frame with zero authority."""

    ocr_calls = 1
    try:
        raw_items = tuple(frame.ocr())
    except Exception:
        raw_items = ()
    cached = _CachedFrame(frame, raw_items)
    model = page_model or observe_action_summary_page(cached)
    capture_id = str(getattr(frame, "source_capture_id", "") or "").strip() or None
    frame_hash = frame_sha256(cached) or None
    captured_text, captured_time = _captured_at(frame)
    confidence = (
        model.page_confidence.value
        if isinstance(model.page_confidence, PageConfidence)
        else str(model.page_confidence)
    )
    if (
        not capture_id
        or not frame_hash
        or not _is_sha256(frame_hash)
        or captured_time is None
    ):
        return _empty(
            status="BLOCKED_SOURCE_PROVENANCE",
            capture_id=capture_id,
            frame_hash=frame_hash,
            captured_at=captured_text,
            page_state=model.page_state,
            page_confidence=confidence,
            ocr_calls=ocr_calls,
        )
    if (
        model.source_capture_id != capture_id
        or model.source_frame_sha256 != frame_hash
    ):
        return _empty(
            status="BLOCKED_FRAME_MISMATCH",
            capture_id=capture_id,
            frame_hash=frame_hash,
            captured_at=captured_text,
            page_state=model.page_state,
            page_confidence=confidence,
            ocr_calls=ocr_calls,
        )
    if model.page_state != "ACTION_SUMMARY_VISIBLE" or model.overlay_states:
        return _empty(
            status="BLOCKED_PAGE_NOT_TRUSTED",
            capture_id=capture_id,
            frame_hash=frame_hash,
            captured_at=captured_text,
            page_state=model.page_state,
            page_confidence=confidence,
            ocr_calls=ocr_calls,
        )

    parsed: list[tuple[str, tuple[int, int, int, int]]] = []
    rejected: list[RawVisualRejectedCandidate] = []
    for item in raw_items:
        if not isinstance(item, Mapping):
            continue
        text = _normalize(item.get("text", ""))
        bounds = _bbox(item)
        if not text or bounds is None:
            continue
        parsed.append((text, bounds))

    cost_candidates: dict[
        str, list[tuple[int, str, tuple[int, int, int, int]]]
    ] = {card.card_match_key: [] for card in model.task_cards}
    for text, bounds in parsed:
        match = _COST_PATTERN.fullmatch(text)
        if match is None:
            continue
        cost = int(match.group(1))
        semantic_hash = _semantic_hash("resource_cost", text)
        containing = [
            card for card in model.task_cards if _inside(_center(bounds), card.bbox)
        ]
        evidence = _evidence_id("resource_cost_candidate", semantic_hash, bounds)
        if len(containing) != 1:
            rejected.append(RawVisualRejectedCandidate(
                fact_type="RESOURCE_COST",
                bbox=bounds,
                semantic_hash=semantic_hash,
                reason_codes=((
                    "UNBOUND_PAGE_LEVEL_NEGATIVE"
                    if not containing
                    else "MULTIPLE_CARD_CONTAINERS"
                ),),
                evidence_ids=(evidence,),
            ))
            continue
        card = containing[0]
        cost_candidates[card.card_match_key].append((cost, semantic_hash, bounds))

    observations: list[RawVisualFactObservation] = []
    ambiguous: list[RawVisualRejectedCandidate] = []
    snapshots: list[CurrentActionSummaryVisualSnapshot] = []
    valid_until = (captured_time + timedelta(seconds=300)).isoformat()
    for card in model.task_cards:
        unique = sorted(set(cost_candidates[card.card_match_key]))
        if not unique:
            continue
        if len(unique) != 1:
            for _cost, semantic_hash, bounds in unique:
                ambiguous.append(RawVisualRejectedCandidate(
                    fact_type="RESOURCE_COST",
                    bbox=bounds,
                    semantic_hash=semantic_hash,
                    reason_codes=("MULTIPLE_COST_CANDIDATES_FOR_CARD",),
                    evidence_ids=(
                        _evidence_id(
                            "ambiguous_resource_cost",
                            semantic_hash,
                            bounds,
                            card.card_match_key,
                        ),
                    ),
                ))
            continue
        cost, semantic_hash, bounds = unique[0]
        if type(cost) is not int or cost < 0 or card.cost != cost:
            rejected.append(RawVisualRejectedCandidate(
                fact_type="RESOURCE_COST",
                bbox=bounds,
                semantic_hash=semantic_hash,
                reason_codes=("PAGE_MODEL_COST_MISMATCH",),
                evidence_ids=(
                    _evidence_id(
                        "rejected_resource_cost",
                        semantic_hash,
                        bounds,
                        card.card_match_key,
                    ),
                ),
            ))
            continue
        evidence = _evidence_id(
            "resource_cost", semantic_hash, bounds, card.card_match_key
        )
        observation = RawVisualFactObservation(
            card_match_key=card.card_match_key,
            fact_type="RESOURCE_COST",
            bbox=bounds,
            semantic_hash=semantic_hash,
            confidence=(
                "HIGH" if card.confidence is PageConfidence.HIGH else "MEDIUM"
            ),
            reason_codes=(
                "EXACT_NEGATIVE_INTEGER",
                "UNIQUE_CARD_CONTAINER",
                "FRESH_CARD_MATCH_KEY",
                "PAGE_MODEL_COST_MATCH",
                "RESOURCE_IDENTITY_UNKNOWN",
            ),
            evidence_ids=(evidence, *card.evidence_ids),
            numeric_value=cost,
            semantic_id="UNKNOWN",
        )
        observations.append(observation)
        snapshots.append(CurrentActionSummaryVisualSnapshot(
            capture_id=capture_id,
            frame_sha256=frame_hash,
            captured_at=captured_text,
            valid_until=valid_until,
            page_state=model.page_state,
            card_match_key=card.card_match_key,
            displayed_resource_cost=cost,
            evidence_ids=observation.evidence_ids,
        ))

    page_evidence = tuple(sorted({
        *model.evidence_ids,
        *(item for observation in observations for item in observation.evidence_ids),
    }))
    return RawActionSummaryVisualObservation(
        schema_version=_SCHEMA_VERSION,
        observer_id=RAW_OBSERVER_ID,
        observation_status=(
            "PASS_WITH_AMBIGUOUS_FACTS" if ambiguous else "PASS"
        ),
        source_capture_id=capture_id,
        source_frame_sha256=frame_hash,
        captured_at=captured_text,
        page_state=model.page_state,
        page_confidence=confidence,
        resource_cost_observations=tuple(observations),
        resource_identity_observations=(),
        resource_balance_observations=(),
        reward_target_observations=(),
        rejected_candidates=tuple(rejected),
        ambiguous_candidates=tuple(ambiguous),
        evidence_ids=page_evidence,
        normalization_snapshots=tuple(snapshots),
        ocr_calls=ocr_calls,
    )


def normalize_raw_action_summary_visual_observation(
    observation: RawActionSummaryVisualObservation,
    *,
    runtime_input_fingerprint: str,
) -> tuple[AcquiredFact, ...]:
    """Run the existing normalizer and bind emitted cost facts to this observer."""

    if not observation.observation_status.startswith("PASS"):
        return ()
    facts: list[AcquiredFact] = []
    for snapshot in observation.normalization_snapshots:
        normalized = observe_current_action_summary_page_facts(
            snapshot,
            runtime_input_fingerprint=runtime_input_fingerprint,
        )
        facts.extend(
            replace(fact, observer_id=RAW_OBSERVER_ID)
            for fact in normalized
            if fact.fact_id == "resource_cost_unknown"
        )
    return tuple(facts)


def raw_observation_fingerprint(
    observation: RawActionSummaryVisualObservation,
) -> str:
    payload = json.dumps(
        observation.to_dict(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


__all__ = [
    "RAW_OBSERVER_CALLABLE_ID",
    "RAW_OBSERVER_ID",
    "RawActionSummaryVisualObservation",
    "RawVisualFactObservation",
    "RawVisualRejectedCandidate",
    "normalize_raw_action_summary_visual_observation",
    "observe_action_summary_current_page_visuals",
    "raw_observation_fingerprint",
]
