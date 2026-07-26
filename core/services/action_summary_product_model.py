"""Read-only product facts and decisions for ACTION_SUMMARY_VISIBLE.

This module has no control/backend dependency and cannot dispatch input.  It
turns one captured frame into conservative facts; missing or ambiguous OCR is
kept UNKNOWN instead of being promoted into business authority.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass
from enum import Enum
from typing import Mapping, Sequence

from core.services.action_summary_navigation import (
    ActionSummaryState,
    observe_action_summary,
)


class PageConfidence(str, Enum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    UNKNOWN = "UNKNOWN"


class TaskCardState(str, Enum):
    AVAILABLE = "available"
    COMPLETED = "completed"
    LOCKED = "locked"
    IN_PROGRESS = "in_progress"
    UNKNOWN = "unknown"


class RewardState(str, Enum):
    AVAILABLE = "available"
    CLAIMED = "claimed"
    LOCKED = "locked"
    UNKNOWN = "unknown"


class ActionSummaryDecisionType(str, Enum):
    NO_ACTION_REQUIRED = "NO_ACTION_REQUIRED"
    TASK_AVAILABLE_NEEDS_POLICY = "TASK_AVAILABLE_NEEDS_POLICY"
    COMPLETED_REWARD_AVAILABLE = "COMPLETED_REWARD_AVAILABLE"
    RESOURCE_INSUFFICIENT = "RESOURCE_INSUFFICIENT"
    ATTEMPTS_EXHAUSTED = "ATTEMPTS_EXHAUSTED"
    LOCKED = "LOCKED"
    AMBIGUOUS_PAGE = "AMBIGUOUS_PAGE"
    UNSUPPORTED_TASK = "UNSUPPORTED_TASK"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class ActionSummaryPageActions:
    can_open_task: bool = False
    can_sweep: bool = False
    can_challenge: bool = False
    can_claim: bool = False
    can_scroll: bool = False


@dataclass(frozen=True, slots=True)
class ActionSummaryTaskCard:
    semantic_id: str
    title_hash: str
    bbox: tuple[int, int, int, int]
    state: TaskCardState
    available_actions: frozenset[str]
    remaining_attempts: int | None
    cost: int | None
    cost_resource_id: str | None
    reward_state: RewardState
    confidence: PageConfidence
    evidence_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ActionSummaryPageModel:
    page_state: str
    page_confidence: PageConfidence
    overlay_states: tuple[str, ...]
    visible_task_cards: int
    selected_task_id: str | None
    task_cards: tuple[ActionSummaryTaskCard, ...]
    page_actions: ActionSummaryPageActions
    page_capabilities: frozenset[str]
    attempts_exhausted: bool | None
    resource_insufficient: bool | None
    evidence_ids: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        document = asdict(self)
        document["page_confidence"] = self.page_confidence.value
        document["page_capabilities"] = sorted(self.page_capabilities)
        cards = []
        for card in self.task_cards:
            item = asdict(card)
            item["state"] = card.state.value
            item["reward_state"] = card.reward_state.value
            item["confidence"] = card.confidence.value
            item["available_actions"] = sorted(card.available_actions)
            cards.append(item)
        document["task_cards"] = cards
        return document


@dataclass(frozen=True, slots=True)
class ActionSummaryDecision:
    decision: ActionSummaryDecisionType
    reason_codes: tuple[str, ...]
    evidence_ids: tuple[str, ...]
    confidence: PageConfidence
    required_future_authorization: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "decision": self.decision.value,
            "reason_codes": list(self.reason_codes),
            "evidence_ids": list(self.evidence_ids),
            "confidence": self.confidence.value,
            "required_future_authorization": list(
                self.required_future_authorization
            ),
        }


_PAGE_TITLE = "利刃围剿"
_CHALLENGE = "进入挑战"
_SWEEP = "扫荡"
_CLAIM_MARKERS = ("领取奖励", "可领取")
_COMPLETED_MARKERS = ("已完成", "完成")
_LOCKED_MARKERS = ("未解锁", "锁定")
_IN_PROGRESS_MARKERS = ("进行中",)
_OVERLAY_MARKERS = {
    "每日签到奖励": "DAILY_CHECKIN",
    "触碰空白区域退出": "MODAL_OVERLAY",
    "资讯": "ANNOUNCEMENT",
    "公告": "ANNOUNCEMENT",
}
_NON_TITLE_MARKERS = {
    _PAGE_TITLE,
    _CHALLENGE,
    _SWEEP,
    "REWARD",
    "REWARDS",
    "奖励",
    "奖励预览",
    "剩余时间",
    "今日可获取作战奖励",
    "难度选择",
    "查看详情",
    *_CLAIM_MARKERS,
    *_COMPLETED_MARKERS,
    *_LOCKED_MARKERS,
    *_IN_PROGRESS_MARKERS,
}
_CARD_ANCHOR_MARKERS = (
    _CHALLENGE,
    _SWEEP,
    "查看详情",
    *_CLAIM_MARKERS,
    *_COMPLETED_MARKERS,
    *_LOCKED_MARKERS,
    *_IN_PROGRESS_MARKERS,
)


def _normalize(value: object) -> str:
    return "".join(str(value or "").replace("：", ":").split())


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
    left, top, right, bottom = min(xs), min(ys), max(xs), max(ys)
    if right <= left or bottom <= top:
        return None
    return left, top, right, bottom


def _center(bounds: tuple[int, int, int, int]) -> tuple[int, int]:
    return (bounds[0] + bounds[2]) // 2, (bounds[1] + bounds[3]) // 2


def _hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _evidence_id(kind: str, text_hash: str, bounds: tuple[int, ...]) -> str:
    payload = f"{kind}|{text_hash}|{','.join(str(value) for value in bounds)}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def _is_title_candidate(text: str) -> bool:
    if not text or text in _NON_TITLE_MARKERS:
        return False
    if any(marker in text for marker in _CARD_ANCHOR_MARKERS):
        return False
    if re.fullmatch(r"[-+]?\d+(?:/\d+)?", text):
        return False
    if re.fullmatch(r"[A-Za-z0-9_./:-]+", text):
        return False
    return len(text) >= 2


def _card_state(texts: Sequence[str]) -> TaskCardState:
    joined = "|".join(texts)
    if any(marker in joined for marker in _LOCKED_MARKERS):
        return TaskCardState.LOCKED
    if any(marker in joined for marker in _IN_PROGRESS_MARKERS):
        return TaskCardState.IN_PROGRESS
    if any(marker in joined for marker in _COMPLETED_MARKERS):
        return TaskCardState.COMPLETED
    if _CHALLENGE in joined:
        return TaskCardState.AVAILABLE
    return TaskCardState.UNKNOWN


def _reward_state(texts: Sequence[str]) -> RewardState:
    joined = "|".join(texts)
    if "已领取" in joined:
        return RewardState.CLAIMED
    if any(marker in joined for marker in _LOCKED_MARKERS):
        return RewardState.LOCKED
    if any(marker in joined for marker in _CLAIM_MARKERS):
        return RewardState.AVAILABLE
    return RewardState.UNKNOWN


def _available_actions(texts: Sequence[str], state: TaskCardState) -> frozenset[str]:
    joined = "|".join(texts)
    actions: set[str] = set()
    if state is not TaskCardState.LOCKED:
        actions.add("SELECT_TASK_AVAILABLE")
    if _CHALLENGE in joined:
        actions.add("CHALLENGE_AVAILABLE")
    if _SWEEP in joined:
        actions.add("SWEEP_AVAILABLE")
    if any(marker in joined for marker in _CLAIM_MARKERS):
        actions.add("CLAIM_AVAILABLE")
    return frozenset(actions)


def _parse_quantity(texts: Sequence[str]) -> int | None:
    for text in texts:
        match = re.fullmatch(r"(\d+)\s*/\s*\d+", text)
        if match:
            return int(match.group(1))
    return None


def _parse_cost(texts: Sequence[str]) -> int | None:
    for text in texts:
        match = re.fullmatch(r"-\s*(\d+)", text)
        if match:
            return int(match.group(1))
    return None


class _FrameProxy:
    def __init__(self, frame: object, items: Sequence[Mapping[str, object]]) -> None:
        self.image = getattr(frame, "image", None)
        self.source_capture_id = getattr(frame, "source_capture_id", "")
        self._items = tuple(items)

    def ocr(self) -> list[Mapping[str, object]]:
        return list(self._items)


def observe_action_summary_page(frame: object) -> ActionSummaryPageModel:
    """Build one immutable model from one captured frame without input."""

    try:
        raw_items = tuple(frame.ocr())
    except Exception:
        raw_items = ()
    items: list[tuple[str, tuple[int, int, int, int], Mapping[str, object]]] = []
    for item in raw_items:
        if not isinstance(item, Mapping):
            continue
        bounds = _bbox(item)
        if bounds is None:
            continue
        items.append((_normalize(item.get("text", "")), bounds, item))

    try:
        navigation_observation = observe_action_summary(_FrameProxy(frame, raw_items))
    except Exception:
        navigation_observation = None

    overlays = tuple(sorted({
        overlay
        for text, _bounds, _item in items
        for marker, overlay in _OVERLAY_MARKERS.items()
        if marker in text
    }))
    title_items = [(text, bounds) for text, bounds, _item in items if _PAGE_TITLE in text]
    anchors = [
        (text, bounds)
        for text, bounds, _item in items
        if any(marker in text for marker in _CARD_ANCHOR_MARKERS)
        and _center(bounds)[1] >= 250
    ]

    cards: list[ActionSummaryTaskCard] = []
    used_titles: set[str] = set()
    for anchor_text, anchor_bbox in sorted(anchors, key=lambda item: _center(item[1])[0]):
        anchor_x, _anchor_y = _center(anchor_bbox)
        candidates = [
            (text, bounds)
            for text, bounds, _item in items
            if _is_title_candidate(text)
            and abs(_center(bounds)[0] - anchor_x) <= 175
            and bounds[3] < anchor_bbox[1]
            and anchor_bbox[1] - bounds[3] <= 260
            and bounds[1] >= 240
        ]
        if not candidates:
            continue
        title, title_bbox = max(candidates, key=lambda item: item[1][3])
        title_hash = _hash_text(title)
        if title_hash in used_titles:
            continue
        used_titles.add(title_hash)
        band_items = [
            (text, bounds)
            for text, bounds, _item in items
            if abs(_center(bounds)[0] - anchor_x) <= 175
            and title_bbox[1] <= bounds[1] <= anchor_bbox[3] + 60
        ]
        card_texts = [text for text, _bounds in band_items]
        all_bounds = [bounds for _text, bounds in band_items] or [title_bbox, anchor_bbox]
        card_bbox = (
            min(bounds[0] for bounds in all_bounds),
            min(bounds[1] for bounds in all_bounds),
            max(bounds[2] for bounds in all_bounds),
            max(bounds[3] for bounds in all_bounds),
        )
        state = _card_state(card_texts)
        evidence_ids = (
            _evidence_id("task_title", title_hash, title_bbox),
            _evidence_id("task_anchor", _hash_text(anchor_text), anchor_bbox),
        )
        cost = _parse_cost(card_texts)
        cards.append(ActionSummaryTaskCard(
            semantic_id=f"TASK_{title_hash[:16].upper()}",
            title_hash=title_hash,
            bbox=card_bbox,
            state=state,
            available_actions=_available_actions(card_texts, state),
            remaining_attempts=_parse_quantity(card_texts),
            cost=cost,
            cost_resource_id="UNKNOWN" if cost is not None else None,
            reward_state=_reward_state(card_texts),
            confidence=(
                PageConfidence.HIGH
                if state is not TaskCardState.UNKNOWN
                else PageConfidence.MEDIUM
            ),
            evidence_ids=evidence_ids,
        ))

    navigation_visible = bool(
        navigation_observation
        and navigation_observation.state is ActionSummaryState.ACTION_SUMMARY_VISIBLE
    )
    structurally_visible = bool(title_items and len(cards) >= 2)
    if navigation_visible and structurally_visible:
        page_state = "ACTION_SUMMARY_VISIBLE"
        page_confidence = PageConfidence.HIGH
    elif structurally_visible:
        page_state = "ACTION_SUMMARY_VISIBLE"
        page_confidence = PageConfidence.MEDIUM
    else:
        page_state = "UNKNOWN"
        page_confidence = PageConfidence.UNKNOWN

    capabilities: set[str] = set()
    if page_state == "ACTION_SUMMARY_VISIBLE":
        capabilities.add("VIEW_TASK_LIST")
    if any("SELECT_TASK_AVAILABLE" in card.available_actions for card in cards):
        capabilities.add("SELECT_TASK_AVAILABLE")
    if any("CHALLENGE_AVAILABLE" in card.available_actions for card in cards):
        capabilities.add("CHALLENGE_AVAILABLE")
    if any("SWEEP_AVAILABLE" in card.available_actions for card in cards):
        capabilities.add("SWEEP_AVAILABLE")
    if any("CLAIM_AVAILABLE" in card.available_actions for card in cards):
        capabilities.add("CLAIM_AVAILABLE")
    joined = "|".join(text for text, _bounds, _item in items)
    if any(marker in joined for marker in ("下一页", "分页", "向左滑动", "向右滑动")):
        capabilities.add("SCROLL_AVAILABLE")

    page_evidence = tuple(
        _evidence_id("page_title", _hash_text(text), bounds)
        for text, bounds in title_items
    )
    attempts_exhausted = True if "次数已用完" in joined else None
    resource_insufficient = True if any(
        marker in joined for marker in ("资源不足", "体力不足", "疲劳不足", "澄清度不足")
    ) else None
    return ActionSummaryPageModel(
        page_state=page_state,
        page_confidence=page_confidence,
        overlay_states=overlays,
        visible_task_cards=len(cards),
        selected_task_id=None,
        task_cards=tuple(cards),
        page_actions=ActionSummaryPageActions(
            can_open_task="SELECT_TASK_AVAILABLE" in capabilities,
            can_sweep="SWEEP_AVAILABLE" in capabilities,
            can_challenge="CHALLENGE_AVAILABLE" in capabilities,
            can_claim="CLAIM_AVAILABLE" in capabilities,
            can_scroll="SCROLL_AVAILABLE" in capabilities,
        ),
        page_capabilities=frozenset(capabilities),
        attempts_exhausted=attempts_exhausted,
        resource_insufficient=resource_insufficient,
        evidence_ids=page_evidence + tuple(
            evidence_id for card in cards for evidence_id in card.evidence_ids
        ),
    )


def decide_action_summary(model: ActionSummaryPageModel) -> ActionSummaryDecision:
    """Return a recommendation only; never authorize or dispatch input."""

    def decision(
        value: ActionSummaryDecisionType,
        *reason_codes: str,
        confidence: PageConfidence | None = None,
        future_authorization: bool = False,
    ) -> ActionSummaryDecision:
        return ActionSummaryDecision(
            value,
            tuple(reason_codes),
            model.evidence_ids,
            confidence or model.page_confidence,
            (
                ("BUSINESS_POLICY", "EXPLICIT_ACTION_AUTHORIZATION")
                if future_authorization
                else ()
            ),
        )

    if model.page_state != "ACTION_SUMMARY_VISIBLE" or model.overlay_states:
        return decision(
            ActionSummaryDecisionType.AMBIGUOUS_PAGE,
            "page_not_trusted" if model.page_state != "ACTION_SUMMARY_VISIBLE" else "overlay_present",
            confidence=PageConfidence.UNKNOWN,
        )
    if model.resource_insufficient:
        return decision(
            ActionSummaryDecisionType.RESOURCE_INSUFFICIENT,
            "explicit_resource_insufficient_cue",
        )
    if model.attempts_exhausted:
        return decision(
            ActionSummaryDecisionType.ATTEMPTS_EXHAUSTED,
            "explicit_attempts_exhausted_cue",
        )
    if any(card.reward_state is RewardState.AVAILABLE for card in model.task_cards):
        return decision(
            ActionSummaryDecisionType.COMPLETED_REWARD_AVAILABLE,
            "claimable_reward_observed",
            future_authorization=True,
        )
    if model.task_cards and all(
        card.state is TaskCardState.LOCKED for card in model.task_cards
    ):
        return decision(ActionSummaryDecisionType.LOCKED, "all_visible_tasks_locked")
    if model.task_cards and all(
        card.state is TaskCardState.COMPLETED for card in model.task_cards
    ):
        return decision(
            ActionSummaryDecisionType.NO_ACTION_REQUIRED,
            "all_visible_tasks_completed_without_claimable_reward",
        )
    if any(card.state is TaskCardState.AVAILABLE for card in model.task_cards):
        return decision(
            ActionSummaryDecisionType.TASK_AVAILABLE_NEEDS_POLICY,
            "available_task_requires_business_policy",
            future_authorization=True,
        )
    if any(
        card.state is TaskCardState.UNKNOWN
        and "SELECT_TASK_AVAILABLE" in card.available_actions
        for card in model.task_cards
    ):
        return decision(
            ActionSummaryDecisionType.UNSUPPORTED_TASK,
            "visible_task_has_no_supported_state_contract",
            confidence=PageConfidence.UNKNOWN,
        )
    if model.task_cards:
        return decision(
            ActionSummaryDecisionType.UNKNOWN,
            "task_state_not_actionable_with_current_evidence",
            confidence=PageConfidence.UNKNOWN,
        )
    return decision(
        ActionSummaryDecisionType.UNKNOWN,
        "no_task_cards_resolved",
        confidence=PageConfidence.UNKNOWN,
    )


__all__ = [
    "ActionSummaryDecision",
    "ActionSummaryDecisionType",
    "ActionSummaryPageActions",
    "ActionSummaryPageModel",
    "ActionSummaryTaskCard",
    "PageConfidence",
    "RewardState",
    "TaskCardState",
    "decide_action_summary",
    "observe_action_summary_page",
]
