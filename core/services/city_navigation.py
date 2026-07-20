"""Bounded, evidence-driven city and exchange read-only navigation."""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Callable, Iterable

from core.services.read_only_policy import ActionIntent


class CityNavigationState(str, Enum):
    HOME_READY = "HOME_READY"
    CITY_ENTRY_VISIBLE = "CITY_ENTRY_VISIBLE"
    CITY_TRANSITION = "CITY_TRANSITION"
    CITY_MAP = "CITY_MAP"
    CITY_DETAIL = "CITY_DETAIL"
    NPC_DIALOG = "NPC_DIALOG"
    EXCHANGE_NPC_VISIBLE = "EXCHANGE_NPC_VISIBLE"
    EXCHANGE_MENU = "EXCHANGE_MENU"
    EXCHANGE_BUY = "EXCHANGE_BUY"
    EXCHANGE_SELL = "EXCHANGE_SELL"
    UNKNOWN = "UNKNOWN"
    TIMEOUT = "TIMEOUT"
    FAILED = "FAILED"


@dataclass(frozen=True)
class CityEntryObservation:
    visible: bool
    anchor_bbox: tuple[int, int, int, int] | None
    confidence: str
    page_fingerprint: str
    source_capture_id: str


@dataclass(frozen=True)
class CityPageObservation:
    state: CityNavigationState
    page_fingerprint: str
    screenshot_hash: str
    confidence: str
    timestamp: str
    source_capture_id: str
    city_entry: CityEntryObservation
    exchange_anchor_bbox: tuple[int, int, int, int] | None
    buy_anchor_bbox: tuple[int, int, int, int] | None
    sell_anchor_bbox: tuple[int, int, int, int] | None
    evidence: tuple[str, ...]
    text_count: int
    reason: str


@dataclass(frozen=True)
class CityNavigationEvent:
    correlation_id: str
    timestamp: str
    state: CityNavigationState
    confidence: str
    page_fingerprint: str
    screenshot_hash: str
    action: str
    guard_result: str
    postcondition: str
    status: str
    reason: str


@dataclass(frozen=True)
class CityNavigationResult:
    state: CityNavigationState
    status: str
    reason: str
    attempt_count: int
    trace: tuple[CityNavigationEvent, ...]
    irreversible_actions: int = 0


def _items(frame: object) -> list[dict]:
    if isinstance(frame, list):
        return list(frame)
    if hasattr(frame, "ocr"):
        return list(frame.ocr())
    return []


def _text(item: dict) -> str:
    return str(item.get("text", "")).replace(" ", "")


def _bbox(item: dict) -> tuple[int, int, int, int] | None:
    points = item.get("position") or ()
    if len(points) < 3:
        return None
    xs = [int(point[0]) for point in points]
    ys = [int(point[1]) for point in points]
    return min(xs), min(ys), max(xs), max(ys)


def _unique_bbox(items: Iterable[dict], predicate: Callable[[str], bool]):
    candidates = [
        bounds for item in items
        if predicate(_text(item)) and (bounds := _bbox(item)) is not None
    ]
    return candidates[0] if len(candidates) == 1 else None


def _center(bounds: tuple[int, int, int, int]) -> tuple[int, int]:
    x1, y1, x2, y2 = bounds
    return int(round((x1 + x2) / 2)), int(round((y1 + y2) / 2))


def _frame_hash(frame: object) -> str:
    supplied = str(getattr(frame, "raw_frame_hash", ""))
    if supplied:
        return supplied
    image = getattr(frame, "image", None)
    return hashlib.sha256(image.tobytes()).hexdigest() if image is not None else ""


def _contains(texts: list[str], markers: Iterable[str]) -> bool:
    return any(marker in text for marker in markers for text in texts)


def _exchange_evidence(texts: list[str], side: str) -> tuple[str, ...]:
    if side == "BUY":
        checks = (
            ("buy_title", _contains(texts, ("预计买入", "买入标题")) or any(text == "买入" for text in texts)),
            ("goods_list", _contains(texts, ("商品列表", "交易品列表", "交易品", "载货量"))),
            ("buy_button", _contains(texts, ("买入按钮", "全部买入")) or any(text == "买入" for text in texts)),
            ("buy_price", _contains(texts, ("购买价格", "买入价格", "买入总价", "含税"))),
        )
    else:
        checks = (
            ("sell_title", _contains(texts, ("预计卖出", "卖出标题")) or any(text == "卖出" for text in texts)),
            ("goods_area", _contains(texts, ("商品出售区域", "交易品列表", "交易品", "载货量"))),
            ("sell_button", _contains(texts, ("卖出按钮", "全部卖出")) or any(text == "卖出" for text in texts)),
            ("sell_price", _contains(texts, ("出售价格", "卖出价格", "卖出总价", "含税"))),
        )
    return tuple(name for name, present in checks if present)


def observe_city_frame(
    frame: object,
    *,
    now: Callable[[], datetime] = lambda: datetime.now().astimezone(),
) -> CityPageObservation:
    items = _items(frame)
    texts = [_text(item) for item in items]
    joined = "|".join(texts)
    screenshot_hash = _frame_hash(frame)
    source_capture_id = str(getattr(frame, "source_capture_id", ""))
    captured_at = getattr(frame, "captured_at", None) or now()
    timestamp = captured_at.isoformat(timespec="milliseconds")
    evidence: list[str] = []

    city_entry_bbox = _unique_bbox(items, lambda text: "访问城市" in text)
    exchange_bbox = _unique_bbox(items, lambda text: "交易所" in text)
    buy_bbox = _unique_bbox(items, lambda text: "我要买" in text)
    sell_bbox = _unique_bbox(items, lambda text: "我要卖" in text)

    buy_evidence = _exchange_evidence(texts, "BUY")
    sell_evidence = _exchange_evidence(texts, "SELL")
    menu_evidence = tuple(
        marker for marker in ("交易所", "你想要什么", "我要买", "我要卖", "交易品投资", "私人仓库")
        if marker in joined
    )
    home_evidence = tuple(
        marker for marker in ("访问城市", "作战终端", "启程", "整备列车")
        if marker in joined
    )
    city_title = _contains(texts, ("当前城市", "城市详情", "城市设施", "城市手册", "城市发展度", "CITY"))
    city_map = _contains(texts, ("城市设施", "城市手册", "交易所", "商会", "休息区"))
    npc_area = _contains(texts, ("NPC", "交流", "对话", "进入", "交易所"))
    city_categories = tuple(
        name for name, present in (
            ("city_title", city_title), ("city_map", city_map), ("npc_area", npc_area),
        ) if present
    )

    if len(buy_evidence) == 4 and len(sell_evidence) == 4:
        state = CityNavigationState.UNKNOWN
        reason = "conflicting_exchange_page_evidence"
        evidence.extend(("buy_sell_conflict",))
    elif len(buy_evidence) == 4:
        state = CityNavigationState.EXCHANGE_BUY
        reason = "buy_page_evidence_confirmed"
        evidence.extend(buy_evidence)
    elif len(sell_evidence) == 4:
        state = CityNavigationState.EXCHANGE_SELL
        reason = "sell_page_evidence_confirmed"
        evidence.extend(sell_evidence)
    elif len(menu_evidence) >= 3 and "我要买" in joined and "我要卖" in joined:
        state = CityNavigationState.EXCHANGE_MENU
        reason = "exchange_menu_evidence_confirmed"
        evidence.extend(menu_evidence)
    elif _contains(texts, ("你想要什么", "研究报告", "什么都行")):
        state = CityNavigationState.NPC_DIALOG
        reason = "npc_dialog_evidence_confirmed"
        evidence.append("npc_dialog")
    elif city_title and _contains(texts, ("城市地图", "地图")):
        state = CityNavigationState.CITY_MAP
        reason = "city_map_evidence_confirmed"
        evidence.extend(("city_title", "map_element"))
    elif len(city_categories) >= 2 and exchange_bbox is not None:
        state = CityNavigationState.EXCHANGE_NPC_VISIBLE
        reason = "city_exchange_anchor_confirmed"
        evidence.extend(city_categories)
    elif len(city_categories) >= 2:
        state = CityNavigationState.CITY_DETAIL
        reason = "city_detail_evidence_confirmed"
        evidence.extend(city_categories)
    elif len(home_evidence) >= 2 and city_entry_bbox is not None:
        state = CityNavigationState.CITY_ENTRY_VISIBLE
        reason = "home_ready_city_entry_visible"
        evidence.extend(home_evidence)
    elif len(home_evidence) >= 2:
        state = CityNavigationState.HOME_READY
        reason = "home_ready_without_unique_city_entry"
        evidence.extend(home_evidence)
    else:
        state = CityNavigationState.UNKNOWN
        reason = "page_evidence_unknown"

    fingerprint = hashlib.sha256(
        json.dumps(
            {"state": state.value, "evidence": sorted(set(evidence))},
            ensure_ascii=False,
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()[:24]
    confidence = "HIGH" if state is not CityNavigationState.UNKNOWN else "UNKNOWN"
    city_entry = CityEntryObservation(
        visible=city_entry_bbox is not None,
        anchor_bbox=city_entry_bbox,
        confidence="HIGH" if city_entry_bbox is not None else "UNKNOWN",
        page_fingerprint=fingerprint,
        source_capture_id=source_capture_id,
    )
    return CityPageObservation(
        state=state,
        page_fingerprint=fingerprint,
        screenshot_hash=screenshot_hash,
        confidence=confidence,
        timestamp=timestamp,
        source_capture_id=source_capture_id,
        city_entry=city_entry,
        exchange_anchor_bbox=exchange_bbox,
        buy_anchor_bbox=buy_bbox,
        sell_anchor_bbox=sell_bbox,
        evidence=tuple(evidence),
        text_count=len(texts),
        reason=reason,
    )


class CityNavigationAdapter:
    def __init__(
        self,
        *,
        frame_provider: Callable[[], object],
        tap: Callable[..., object],
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = lambda: datetime.now().astimezone(),
        timeout: float = 30.0,
        max_attempts: int = 10,
        stall_frames: int = 5,
        cancellation: Callable[[], bool] | None = None,
        correlation_id: str | None = None,
    ):
        self.frame_provider = frame_provider
        self.tap = tap
        self.sleep = sleep
        self.monotonic = monotonic
        self.now = now
        self.timeout = max(0.0, float(timeout))
        self.max_attempts = max(1, int(max_attempts))
        self.stall_frames = max(2, int(stall_frames))
        self.cancellation = cancellation or (lambda: False)
        self.correlation_id = correlation_id or now().strftime("CITYNAV-%Y%m%d-%H%M%S")

    def enter_city(self) -> CityNavigationResult:
        deadline = self.monotonic() + self.timeout
        attempts = 0
        trace: list[CityNavigationEvent] = []

        def record(observation, action, guard, postcondition, status, reason, *, state=None):
            trace.append(CityNavigationEvent(
                correlation_id=self.correlation_id,
                timestamp=observation.timestamp,
                state=state or observation.state,
                confidence=observation.confidence,
                page_fingerprint=observation.page_fingerprint,
                screenshot_hash=observation.screenshot_hash,
                action=action,
                guard_result=guard,
                postcondition=postcondition,
                status=status,
                reason=reason,
            ))

        def finish(state, status, reason):
            return CityNavigationResult(
                state, status, reason, attempts, tuple(trace), 0,
            )

        if self.cancellation():
            return finish(CityNavigationState.FAILED, "BLOCKED", "cancelled")
        if self.monotonic() >= deadline:
            return finish(CityNavigationState.TIMEOUT, "BLOCKED", "navigation_deadline_or_attempt_limit")
        before = observe_city_frame(self.frame_provider(), now=self.now)
        attempts += 1
        if before.state is CityNavigationState.HOME_READY:
            record(before, "OBSERVE_CITY_ENTRY", "NOT_REQUESTED", "NOT_CHECKED", "BLOCKED", "city_entry_anchor_missing")
            return finish(CityNavigationState.HOME_READY, "BLOCKED", "city_entry_anchor_missing")
        if before.state is not CityNavigationState.CITY_ENTRY_VISIBLE:
            record(before, "OBSERVE_CITY_ENTRY", "NOT_REQUESTED", "NOT_CHECKED", "BLOCKED", "unknown_page_blocks_action")
            return finish(CityNavigationState.UNKNOWN, "BLOCKED", "unknown_page_blocks_action")

        bounds = before.city_entry.anchor_bbox
        if bounds is None:
            return finish(CityNavigationState.HOME_READY, "BLOCKED", "city_entry_anchor_missing")
        point = _center(bounds)
        record(before, "enter_city", "PENDING", "city_evidence_categories_at_least_2", "PENDING", "city_entry_observed")
        try:
            allowed = self.tap(
                point,
                intent=ActionIntent(
                    "city_entry_navigation", "city_entry", f"{self.correlation_id}:enter-city",
                ),
            )
        except (PermissionError, RuntimeError) as error:
            record(before, "enter_city", type(error).__name__, "NOT_CHECKED", "BLOCKED", "guard_denied_city_entry")
            return finish(CityNavigationState.FAILED, "BLOCKED", "guard_denied_city_entry")
        if allowed is False:
            record(before, "enter_city", "DENIED", "NOT_CHECKED", "BLOCKED", "guard_denied_city_entry")
            return finish(CityNavigationState.FAILED, "BLOCKED", "guard_denied_city_entry")
        record(before, "enter_city", "ALLOWED", "PENDING", "PENDING", "city_entry_action_executed")

        last_hash = before.screenshot_hash
        last_fingerprint = before.page_fingerprint
        stable = 0
        for _ in range(max(0, self.max_attempts - attempts)):
            if self.cancellation():
                return finish(CityNavigationState.FAILED, "BLOCKED", "cancelled")
            if self.monotonic() >= deadline:
                break
            self.sleep(min(0.5, max(0.0, deadline - self.monotonic())))
            if self.monotonic() >= deadline:
                break
            observation = observe_city_frame(self.frame_provider(), now=self.now)
            attempts += 1
            same_frame = bool(observation.screenshot_hash and observation.screenshot_hash == last_hash)
            same_page = observation.page_fingerprint == last_fingerprint
            stable = stable + 1 if (same_frame or same_page) else 1
            last_hash = observation.screenshot_hash
            last_fingerprint = observation.page_fingerprint

            if observation.state in {
                CityNavigationState.CITY_MAP,
                CityNavigationState.CITY_DETAIL,
                CityNavigationState.EXCHANGE_NPC_VISIBLE,
            }:
                record(observation, "OBSERVE_CITY_POSTCONDITION", "ALLOWED", "VERIFIED", "PASS", "city_postcondition_verified")
                return finish(observation.state, "PASS", "city_postcondition_verified")
            if observation.state is CityNavigationState.UNKNOWN and observation.text_count == 0:
                record(
                    observation, "WAIT_CITY_TRANSITION", "ALLOWED", "PENDING", "PENDING",
                    "city_transition_without_committed_page", state=CityNavigationState.CITY_TRANSITION,
                )
            elif observation.state is CityNavigationState.UNKNOWN:
                record(observation, "OBSERVE_CITY_POSTCONDITION", "ALLOWED", "FAILED", "FAILED", "NAVIGATION_POSTCONDITION_FAILED")
                return finish(CityNavigationState.FAILED, "FAILED", "NAVIGATION_POSTCONDITION_FAILED")
            else:
                record(observation, "OBSERVE_CITY_POSTCONDITION", "ALLOWED", "FAILED", "FAILED", "NAVIGATION_POSTCONDITION_FAILED")
                return finish(CityNavigationState.FAILED, "FAILED", "NAVIGATION_POSTCONDITION_FAILED")
            if stable >= self.stall_frames:
                record(observation, "WAIT_CITY_TRANSITION", "ALLOWED", "FAILED", "FAILED", "NAVIGATION_STALLED", state=CityNavigationState.FAILED)
                return finish(CityNavigationState.FAILED, "FAILED", "NAVIGATION_STALLED")

        return finish(CityNavigationState.TIMEOUT, "BLOCKED", "navigation_deadline_or_attempt_limit")


class ExchangeEntryAdapter:
    def __init__(
        self,
        *,
        frame_provider: Callable[[], object] | None = None,
        tap: Callable[..., object] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = lambda: datetime.now().astimezone(),
        timeout: float = 30.0,
        max_attempts: int = 10,
        stall_frames: int = 5,
        cancellation: Callable[[], bool] | None = None,
        correlation_id: str | None = None,
    ):
        self.frame_provider = frame_provider
        self.tap = tap
        self.sleep = sleep
        self.monotonic = monotonic
        self.now = now
        self.timeout = max(0.0, float(timeout))
        self.max_attempts = max(1, int(max_attempts))
        self.stall_frames = max(2, int(stall_frames))
        self.cancellation = cancellation or (lambda: False)
        self.correlation_id = correlation_id or now().strftime("CITYNAV-%Y%m%d-%H%M%S")

    @staticmethod
    def page_matches(observation: CityPageObservation, action: str) -> bool:
        selected = str(action).upper()
        return observation.state is (
            CityNavigationState.EXCHANGE_BUY
            if selected == "BUY" else CityNavigationState.EXCHANGE_SELL
        )

    def _navigate(
        self,
        *,
        source_states: frozenset[CityNavigationState],
        target_state: CityNavigationState,
        anchor_name: str,
        action_key: str,
        action_name: str,
    ) -> CityNavigationResult:
        if self.frame_provider is None or self.tap is None:
            raise RuntimeError("exchange_adapter_runtime_not_configured")
        deadline = self.monotonic() + self.timeout
        attempts = 0
        trace: list[CityNavigationEvent] = []

        def record(observation, action, guard, postcondition, status, reason, *, state=None):
            trace.append(CityNavigationEvent(
                self.correlation_id, observation.timestamp,
                state or observation.state, observation.confidence,
                observation.page_fingerprint, observation.screenshot_hash,
                action, guard, postcondition, status, reason,
            ))

        def finish(state, status, reason):
            return CityNavigationResult(state, status, reason, attempts, tuple(trace), 0)

        if self.cancellation():
            return finish(CityNavigationState.FAILED, "BLOCKED", "cancelled")
        if self.monotonic() >= deadline:
            return finish(CityNavigationState.TIMEOUT, "BLOCKED", "navigation_deadline_or_attempt_limit")
        before = observe_city_frame(self.frame_provider(), now=self.now)
        attempts += 1
        if before.state not in source_states:
            record(before, action_name, "NOT_REQUESTED", "NOT_CHECKED", "BLOCKED", "navigation_source_not_confirmed")
            return finish(before.state, "BLOCKED", "navigation_source_not_confirmed")
        bounds = {
            "交易所": before.exchange_anchor_bbox,
            "buy_navigation": before.buy_anchor_bbox,
            "sell_navigation": before.sell_anchor_bbox,
        }[anchor_name]
        if bounds is None:
            record(before, action_name, "NOT_REQUESTED", "NOT_CHECKED", "BLOCKED", "navigation_anchor_not_unique")
            return finish(before.state, "BLOCKED", "navigation_anchor_not_unique")
        point = _center(bounds)
        record(before, action_name, "PENDING", target_state.value, "PENDING", "navigation_anchor_observed")
        try:
            allowed = self.tap(
                point,
                intent=ActionIntent(
                    action_key, anchor_name, f"{self.correlation_id}:{action_name}",
                ),
            )
        except (PermissionError, RuntimeError) as error:
            record(before, action_name, type(error).__name__, "NOT_CHECKED", "BLOCKED", "guard_denied_navigation")
            return finish(CityNavigationState.FAILED, "BLOCKED", "guard_denied_navigation")
        if allowed is False:
            record(before, action_name, "DENIED", "NOT_CHECKED", "BLOCKED", "guard_denied_navigation")
            return finish(CityNavigationState.FAILED, "BLOCKED", "guard_denied_navigation")
        record(before, action_name, "ALLOWED", "PENDING", "PENDING", "navigation_action_executed")

        last_hash = before.screenshot_hash
        last_fingerprint = before.page_fingerprint
        stable = 0
        for _ in range(max(0, self.max_attempts - attempts)):
            if self.cancellation():
                return finish(CityNavigationState.FAILED, "BLOCKED", "cancelled")
            if self.monotonic() >= deadline:
                break
            self.sleep(min(0.5, max(0.0, deadline - self.monotonic())))
            if self.monotonic() >= deadline:
                break
            observation = observe_city_frame(self.frame_provider(), now=self.now)
            attempts += 1
            same_frame = bool(observation.screenshot_hash and observation.screenshot_hash == last_hash)
            same_page = observation.page_fingerprint == last_fingerprint
            stable = stable + 1 if (same_frame or same_page) else 1
            last_hash = observation.screenshot_hash
            last_fingerprint = observation.page_fingerprint

            if observation.state is target_state:
                record(observation, "OBSERVE_NAVIGATION_POSTCONDITION", "ALLOWED", "VERIFIED", "PASS", "navigation_postcondition_verified")
                return finish(target_state, "PASS", "navigation_postcondition_verified")
            transitional = (
                observation.state is CityNavigationState.NPC_DIALOG
                and target_state is CityNavigationState.EXCHANGE_MENU
            ) or (
                observation.state is CityNavigationState.UNKNOWN
                and observation.text_count == 0
            )
            if transitional:
                transition_state = (
                    observation.state
                    if observation.state is CityNavigationState.NPC_DIALOG
                    else CityNavigationState.CITY_TRANSITION
                )
                record(observation, "WAIT_NAVIGATION_TRANSITION", "ALLOWED", "PENDING", "PENDING", "navigation_transition", state=transition_state)
            else:
                record(observation, "OBSERVE_NAVIGATION_POSTCONDITION", "ALLOWED", "FAILED", "FAILED", "NAVIGATION_POSTCONDITION_FAILED")
                return finish(CityNavigationState.FAILED, "FAILED", "NAVIGATION_POSTCONDITION_FAILED")
            if stable >= self.stall_frames:
                record(observation, "WAIT_NAVIGATION_TRANSITION", "ALLOWED", "FAILED", "FAILED", "NAVIGATION_STALLED", state=CityNavigationState.FAILED)
                return finish(CityNavigationState.FAILED, "FAILED", "NAVIGATION_STALLED")
        return finish(CityNavigationState.TIMEOUT, "BLOCKED", "navigation_deadline_or_attempt_limit")

    def open_menu(self) -> CityNavigationResult:
        return self._navigate(
            source_states=frozenset({
                CityNavigationState.CITY_MAP,
                CityNavigationState.CITY_DETAIL,
                CityNavigationState.EXCHANGE_NPC_VISIBLE,
            }),
            target_state=CityNavigationState.EXCHANGE_MENU,
            anchor_name="交易所",
            action_key="navigation_anchor",
            action_name="enter_exchange",
        )

    def open_action(self, action: str) -> CityNavigationResult:
        selected = str(action).upper()
        if selected not in {"BUY", "SELL"}:
            raise ValueError(f"unsupported exchange action: {action}")
        return self._navigate(
            source_states=frozenset({CityNavigationState.EXCHANGE_MENU}),
            target_state=(
                CityNavigationState.EXCHANGE_BUY
                if selected == "BUY" else CityNavigationState.EXCHANGE_SELL
            ),
            anchor_name="buy_navigation" if selected == "BUY" else "sell_navigation",
            action_key=(
                "exchange_buy_navigation"
                if selected == "BUY" else "exchange_sell_navigation"
            ),
            action_name=f"open_exchange_{selected.lower()}",
        )


__all__ = [
    "CityEntryObservation",
    "CityNavigationAdapter",
    "CityNavigationEvent",
    "CityNavigationResult",
    "CityNavigationState",
    "CityPageObservation",
    "ExchangeEntryAdapter",
    "observe_city_frame",
]
