"""Composable, side-effect-free contracts for runtime UI navigation.

The kernel deliberately owns no capture or input backend.  Feature extractors
publish privacy-safe semantic facts, page signatures compose those facts into a
normalized :class:`UiState`, and action/transition contracts decide whether a
caller may proceed.  Existing adapters remain responsible for OCR/CV, physical
dispatch and evidence persistence while they migrate incrementally.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable, Mapping


UNKNOWN_PAGE = "UNKNOWN"


class PageKind(str, Enum):
    TRUSTED = "TRUSTED"
    OVERLAY = "OVERLAY"
    FOREIGN = "FOREIGN"


class Confidence(str, Enum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    UNKNOWN = "UNKNOWN"


class ActionPrimitive(str, Enum):
    OPEN_ENTRY = "OPEN_ENTRY"
    SELECT_CARD = "SELECT_CARD"
    DISMISS_CLAIMED_OVERLAY = "DISMISS_CLAIMED_OVERLAY"
    OPEN_TAB = "OPEN_TAB"
    OPEN_MENU = "OPEN_MENU"
    GO_BACK = "GO_BACK"
    CONFIRM_SAFE = "CONFIRM_SAFE"


class TransitionClassification(str, Enum):
    EXPECTED_POST_STATE = "EXPECTED_POST_STATE"
    OPTIONAL_OVERLAY_REACHED = "OPTIONAL_OVERLAY_REACHED"
    KNOWN_TRANSITION = "KNOWN_TRANSITION"
    SOURCE_STATE_RETAINED = "SOURCE_STATE_RETAINED"
    UNKNOWN_RECOVERABLE = "UNKNOWN_RECOVERABLE"
    STABLE_CHANGED_UNKNOWN = "STABLE_CHANGED_UNKNOWN"
    KNOWN_FOREIGN_PAGE = "KNOWN_FOREIGN_PAGE"
    STALE = "STALE"
    TRANSITION_TIMEOUT = "TRANSITION_TIMEOUT"
    NO_TOUCH_EFFECT_OBSERVED = "NO_TOUCH_EFFECT_OBSERVED"


@dataclass(frozen=True, slots=True)
class PagePerception:
    """Privacy-safe facts derived from one capture.

    ``facts`` contains semantic/layout cue identifiers, never raw OCR bodies.
    ``frame_hash`` and ``capture_id`` let transition observers reject stale
    samples without coupling the kernel to one capture implementation.
    """

    facts: frozenset[str]
    frame_hash: str = ""
    capture_id: str = ""

    @classmethod
    def from_facts(
        cls,
        facts: Iterable[str],
        *,
        frame_hash: str = "",
        capture_id: str = "",
    ) -> "PagePerception":
        return cls(
            frozenset(str(fact).strip() for fact in facts if str(fact).strip()),
            str(frame_hash),
            str(capture_id),
        )


@dataclass(frozen=True, slots=True)
class PageSignature:
    signature_id: str
    base_page: str
    kind: PageKind
    required_all: frozenset[str] = field(default_factory=frozenset)
    required_any: frozenset[str] = field(default_factory=frozenset)
    forbidden: frozenset[str] = field(default_factory=frozenset)
    capabilities: frozenset[str] = field(default_factory=frozenset)
    priority: int = 0
    confidence: Confidence = Confidence.HIGH

    def matched_evidence(self, perception: PagePerception) -> tuple[str, ...] | None:
        facts = perception.facts
        if self.forbidden & facts:
            return None
        if not self.required_all.issubset(facts):
            return None
        any_evidence = self.required_any & facts
        if self.required_any and not any_evidence:
            return None
        evidence = self.required_all | any_evidence
        if not evidence:
            raise ValueError(f"page_signature_without_evidence:{self.signature_id}")
        return tuple(sorted(evidence))


@dataclass(frozen=True, slots=True)
class UiState:
    base_page: str
    overlays: tuple[str, ...] = ()
    phase: str = ""
    capabilities: frozenset[str] = field(default_factory=frozenset)
    confidence: Confidence = Confidence.UNKNOWN
    evidence: tuple[str, ...] = ()
    signature_id: str = ""
    frame_hash: str = ""
    capture_id: str = ""
    reason: str = ""

    @property
    def is_unknown(self) -> bool:
        return self.base_page == UNKNOWN_PAGE

    def has_capability(self, capability: str) -> bool:
        return str(capability) in self.capabilities and not self.overlays


@dataclass(frozen=True, slots=True)
class _SignatureMatch:
    signature: PageSignature
    evidence: tuple[str, ...]


class RuntimeNavigationKernel:
    """Normalize page facts and evaluate reusable navigation contracts."""

    def __init__(self, signatures: Iterable[PageSignature]) -> None:
        self.signatures = tuple(signatures)
        ids = [signature.signature_id for signature in self.signatures]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate_page_signature_id")

    @staticmethod
    def _best(matches: list[_SignatureMatch]) -> _SignatureMatch | None:
        if not matches:
            return None
        ranked = sorted(
            matches,
            key=lambda match: (
                match.signature.priority,
                len(match.evidence),
                match.signature.signature_id,
            ),
            reverse=True,
        )
        best = ranked[0]
        tied_pages = {
            match.signature.base_page
            for match in ranked
            if match.signature.priority == best.signature.priority
            and len(match.evidence) == len(best.evidence)
        }
        return None if len(tied_pages) > 1 else best

    def classify(self, perception: PagePerception, *, phase: str = "") -> UiState:
        matches: dict[PageKind, list[_SignatureMatch]] = {
            kind: [] for kind in PageKind
        }
        for signature in self.signatures:
            evidence = signature.matched_evidence(perception)
            if evidence is not None:
                matches[signature.kind].append(_SignatureMatch(signature, evidence))

        trusted = self._best(matches[PageKind.TRUSTED])
        overlays = sorted(
            {
                match.signature.base_page
                for match in matches[PageKind.OVERLAY]
            }
        )
        selected = trusted
        if selected is None and not overlays:
            selected = self._best(matches[PageKind.FOREIGN])

        if selected is None:
            reason = "overlay_without_committed_base" if overlays else (
                "ambiguous_or_unknown_page_signature"
                if matches[PageKind.TRUSTED] or matches[PageKind.FOREIGN]
                else "page_signature_absent"
            )
            return UiState(
                base_page=UNKNOWN_PAGE,
                overlays=tuple(overlays),
                phase=str(phase),
                confidence=Confidence.MEDIUM if overlays else Confidence.UNKNOWN,
                evidence=tuple(
                    sorted(
                        fact
                        for match in matches[PageKind.OVERLAY]
                        for fact in match.evidence
                    )
                ),
                frame_hash=perception.frame_hash,
                capture_id=perception.capture_id,
                reason=reason,
            )

        signature = selected.signature
        return UiState(
            base_page=signature.base_page,
            overlays=tuple(overlays),
            phase=str(phase),
            capabilities=signature.capabilities,
            confidence=signature.confidence,
            evidence=selected.evidence,
            signature_id=signature.signature_id,
            frame_hash=perception.frame_hash,
            capture_id=perception.capture_id,
            reason="page_signature_matched",
        )


@dataclass(frozen=True, slots=True)
class ContractDecision:
    allowed: bool
    reason: str


@dataclass(frozen=True, slots=True)
class ActionContract:
    action_id: str
    primitive: ActionPrimitive
    allowed_pre_pages: frozenset[str]
    required_capabilities: frozenset[str] = field(default_factory=frozenset)
    candidate_policy: str = "UNIQUE_SEMANTIC_ANCHOR"
    hit_target_policy: str = "UNIQUE_PARENT_SAFE_REGION"
    max_dispatches: int = 1
    random_offset: bool = False
    allowed_post_pages: frozenset[str] = field(default_factory=frozenset)
    transitional_post_pages: frozenset[str] = field(default_factory=frozenset)
    optional_post_overlays: frozenset[str] = field(default_factory=frozenset)
    forbidden_post_pages: frozenset[str] = field(default_factory=frozenset)
    transition_timeout_seconds: float = 30.0
    irreversible: bool = False

    def authorize(self, state: UiState, *, dispatch_count: int = 0) -> ContractDecision:
        if state.is_unknown:
            return ContractDecision(False, "unknown_pre_state")
        if state.overlays:
            return ContractDecision(False, "overlay_blocks_action")
        if state.base_page not in self.allowed_pre_pages:
            return ContractDecision(False, "pre_state_not_allowed")
        missing = self.required_capabilities - state.capabilities
        if missing:
            return ContractDecision(False, "required_capability_absent")
        if dispatch_count >= self.max_dispatches:
            return ContractDecision(False, "action_dispatch_budget_exhausted")
        if self.random_offset:
            return ContractDecision(False, "random_offset_contract_forbidden")
        return ContractDecision(True, "authorized")


@dataclass(frozen=True, slots=True)
class InteractionTarget:
    semantic_id: str
    candidate_count: int
    label_bbox: tuple[int, int, int, int]
    parent_bbox: tuple[int, int, int, int]
    hit_target_bbox: tuple[int, int, int, int]
    hit_target_point: tuple[int, int]
    capture_size: tuple[int, int]
    method: str
    occluded: bool = False


def confirm_fresh_target(
    initial: InteractionTarget,
    fresh: InteractionTarget,
    *,
    normalized_tolerance: float = 0.03,
) -> ContractDecision:
    """Confirm semantic identity and geometry without requiring equal frames."""

    if initial.candidate_count != 1 or fresh.candidate_count != 1:
        return ContractDecision(False, "candidate_not_unique")
    if initial.semantic_id != fresh.semantic_id:
        return ContractDecision(False, "semantic_identity_changed")
    if initial.method != fresh.method:
        return ContractDecision(False, "hit_target_method_changed")
    if initial.occluded or fresh.occluded:
        return ContractDecision(False, "target_occluded")
    if min(*initial.capture_size, *fresh.capture_size) <= 0:
        return ContractDecision(False, "capture_geometry_invalid")
    ix = initial.hit_target_point[0] / initial.capture_size[0]
    iy = initial.hit_target_point[1] / initial.capture_size[1]
    fx = fresh.hit_target_point[0] / fresh.capture_size[0]
    fy = fresh.hit_target_point[1] / fresh.capture_size[1]
    if abs(ix - fx) > normalized_tolerance or abs(iy - fy) > normalized_tolerance:
        return ContractDecision(False, "normalized_target_moved")
    x1, y1, x2, y2 = fresh.parent_bbox
    x, y = fresh.hit_target_point
    if not (x1 <= x <= x2 and y1 <= y <= y2):
        return ContractDecision(False, "target_outside_parent")
    return ContractDecision(True, "fresh_target_confirmed")


@dataclass(frozen=True, slots=True)
class TransitionDecision:
    classification: TransitionClassification
    terminal: bool
    success: bool
    reason: str


class TransitionClassifier:
    """Classify bounded post-dispatch observations without sending input."""

    def __init__(
        self,
        contract: ActionContract,
        *,
        source_state: UiState,
        stable_unknown_frames: int = 2,
    ) -> None:
        self.contract = contract
        self.source_state = source_state
        self.stable_unknown_frames = max(2, int(stable_unknown_frames))
        self._unknown_hash = ""
        self._unknown_count = 0
        self.any_changed = False

    def observe(self, state: UiState) -> TransitionDecision:
        if state.capture_id and state.capture_id == self.source_state.capture_id:
            return TransitionDecision(
                TransitionClassification.STALE, False, False, "stale_capture_ignored"
            )
        frame_changed = bool(
            state.frame_hash and state.frame_hash != self.source_state.frame_hash
        )
        self.any_changed = self.any_changed or frame_changed
        if state.base_page in self.contract.allowed_post_pages:
            return TransitionDecision(
                TransitionClassification.EXPECTED_POST_STATE,
                True,
                True,
                "allowed_post_state_reached",
            )
        if state.base_page in self.contract.transitional_post_pages:
            return TransitionDecision(
                TransitionClassification.KNOWN_TRANSITION,
                False,
                False,
                "known_transition_pending",
            )
        if set(state.overlays) & self.contract.optional_post_overlays:
            return TransitionDecision(
                TransitionClassification.OPTIONAL_OVERLAY_REACHED,
                True,
                True,
                "optional_post_overlay_reached",
            )
        if state.base_page in self.contract.forbidden_post_pages:
            return TransitionDecision(
                TransitionClassification.KNOWN_FOREIGN_PAGE,
                True,
                False,
                "forbidden_post_state_reached",
            )
        if state.is_unknown:
            if frame_changed and state.frame_hash:
                if state.frame_hash == self._unknown_hash:
                    self._unknown_count += 1
                else:
                    self._unknown_hash = state.frame_hash
                    self._unknown_count = 1
                if self._unknown_count >= self.stable_unknown_frames:
                    return TransitionDecision(
                        TransitionClassification.STABLE_CHANGED_UNKNOWN,
                        True,
                        False,
                        "stable_changed_unknown_reached",
                    )
            return TransitionDecision(
                TransitionClassification.UNKNOWN_RECOVERABLE,
                False,
                False,
                "unknown_transition_pending",
            )
        self._unknown_hash = ""
        self._unknown_count = 0
        if state.base_page == self.source_state.base_page:
            return TransitionDecision(
                TransitionClassification.SOURCE_STATE_RETAINED,
                False,
                False,
                "source_state_still_visible",
            )
        return TransitionDecision(
            TransitionClassification.KNOWN_FOREIGN_PAGE,
            True,
            False,
            "known_unexpected_post_state",
        )

    def timeout(self) -> TransitionDecision:
        if not self.any_changed:
            return TransitionDecision(
                TransitionClassification.NO_TOUCH_EFFECT_OBSERVED,
                True,
                False,
                "no_touch_effect_observed",
            )
        return TransitionDecision(
            TransitionClassification.TRANSITION_TIMEOUT,
            True,
            False,
            "transition_timeout",
        )


DEFAULT_CAPABILITIES: Mapping[str, frozenset[str]] = {
    "HOME_READY": frozenset({"OPEN_CITY", "OPEN_ACTION_TERMINAL", "OPEN_INVENTORY"}),
    "ACTIVITY_OVERVIEW_VISIBLE": frozenset({"OPEN_GLOBAL_PREP"}),
    "GLOBAL_PREP_PAGE": frozenset({"OPEN_ACTION_SUMMARY"}),
    "ACTION_SUMMARY_ENTRY_VISIBLE": frozenset({"OPEN_ACTION_SUMMARY"}),
    "ACTION_SUMMARY_VISIBLE": frozenset({"SELECT_ACTION_TASK"}),
    "INVENTORY": frozenset({"READ_INVENTORY", "OPEN_ITEM_DETAIL"}),
    "CITY_ENTRY_VISIBLE": frozenset({"ENTER_CITY"}),
    "CITY_DETAIL": frozenset({"OPEN_CITY_FACILITY"}),
}


PROVEN_NAVIGATION_CONTRACTS: Mapping[str, ActionContract] = {
    "OPEN_INVENTORY": ActionContract(
        action_id="open_inventory",
        primitive=ActionPrimitive.OPEN_ENTRY,
        allowed_pre_pages=frozenset({"HOME_READY"}),
        required_capabilities=frozenset({"OPEN_INVENTORY"}),
        candidate_policy="UNIQUE_TOP_RIGHT_ASSETS_GLYPH",
        hit_target_policy="TEMPLATE_PARENT_SAFE_REGION",
        max_dispatches=1,
        random_offset=False,
        allowed_post_pages=frozenset({"INVENTORY"}),
        forbidden_post_pages=frozenset({"EXTERNAL_BROWSER", "LOGIN_PAGE"}),
        transition_timeout_seconds=6.0,
        irreversible=False,
    ),
    "ENTER_CITY": ActionContract(
        action_id="enter_city",
        primitive=ActionPrimitive.OPEN_ENTRY,
        allowed_pre_pages=frozenset({"CITY_ENTRY_VISIBLE"}),
        required_capabilities=frozenset({"ENTER_CITY"}),
        candidate_policy="UNIQUE_CITY_SEMANTIC_ANCHOR",
        hit_target_policy="RESOLVER_BOUND_TARGET",
        max_dispatches=1,
        random_offset=False,
        allowed_post_pages=frozenset({"CITY_DETAIL", "CITY_MAP"}),
        transitional_post_pages=frozenset({"CITY_TRANSITION"}),
        forbidden_post_pages=frozenset({
            "INVENTORY", "ACTION_SUMMARY_VISIBLE", "EXTERNAL_BROWSER", "LOGIN_PAGE",
        }),
        transition_timeout_seconds=30.0,
        irreversible=False,
    ),
    "OPEN_ACTION_TERMINAL": ActionContract(
        action_id="open_action_terminal",
        primitive=ActionPrimitive.OPEN_ENTRY,
        allowed_pre_pages=frozenset({"HOME_READY"}),
        required_capabilities=frozenset({"OPEN_ACTION_TERMINAL"}),
        candidate_policy="UNIQUE_EXACT_ACTION_TERMINAL_ANCHOR",
        hit_target_policy="UNIQUE_PARENT_ICON_SAFE_REGION",
        max_dispatches=1,
        random_offset=False,
        allowed_post_pages=frozenset({"ACTIVITY_OVERVIEW_VISIBLE"}),
        forbidden_post_pages=frozenset({
            "INVENTORY", "EXTERNAL_BROWSER", "LOGIN_PAGE",
        }),
        transition_timeout_seconds=30.0,
        irreversible=False,
    ),
    "OPEN_GLOBAL_PREP": ActionContract(
        action_id="open_global_prep",
        primitive=ActionPrimitive.SELECT_CARD,
        allowed_pre_pages=frozenset({"ACTIVITY_OVERVIEW_VISIBLE"}),
        required_capabilities=frozenset({"OPEN_GLOBAL_PREP"}),
        candidate_policy="UNIQUE_EXACT_GLOBAL_PREP_OR_STRICT_FRAGMENT_MERGE",
        hit_target_policy="UNIQUE_PARENT_CARD_SAFE_REGION",
        max_dispatches=1,
        random_offset=False,
        allowed_post_pages=frozenset({"GLOBAL_PREP_PAGE", "ACTION_SUMMARY_VISIBLE"}),
        optional_post_overlays=frozenset({"OPTIONAL_OVERLAY_VISIBLE"}),
        forbidden_post_pages=frozenset({
            "INVENTORY", "EXCHANGE_PAGE", "EXTERNAL_BROWSER", "LOGIN_PAGE",
            "FOREIGN_PAGE",
        }),
        transition_timeout_seconds=30.0,
        irreversible=False,
    ),
}


def normalize_legacy_state(
    base_page: str,
    *,
    overlays: Iterable[str] = (),
    phase: str = "",
    confidence: str | Confidence = Confidence.HIGH,
    evidence: Iterable[str] = (),
    frame_hash: str = "",
    capture_id: str = "",
) -> UiState:
    """Bridge an already-proven adapter state into the shared state model."""

    page = str(base_page)
    try:
        normalized_confidence = (
            confidence if isinstance(confidence, Confidence) else Confidence(str(confidence))
        )
    except ValueError:
        normalized_confidence = Confidence.UNKNOWN
    return UiState(
        base_page=page,
        overlays=tuple(str(value) for value in overlays),
        phase=str(phase),
        capabilities=DEFAULT_CAPABILITIES.get(page, frozenset()),
        confidence=normalized_confidence,
        evidence=tuple(str(value) for value in evidence),
        frame_hash=str(frame_hash),
        capture_id=str(capture_id),
        reason="legacy_state_normalized",
    )


__all__ = [
    "ActionContract",
    "ActionPrimitive",
    "Confidence",
    "ContractDecision",
    "DEFAULT_CAPABILITIES",
    "InteractionTarget",
    "PageKind",
    "PagePerception",
    "PageSignature",
    "PROVEN_NAVIGATION_CONTRACTS",
    "RuntimeNavigationKernel",
    "TransitionClassification",
    "TransitionClassifier",
    "TransitionDecision",
    "UiState",
    "UNKNOWN_PAGE",
    "confirm_fresh_target",
    "normalize_legacy_state",
]
