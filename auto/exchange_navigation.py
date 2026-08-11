"""Verified navigation from the current exchange NPC menu to BUY or SELL."""

from __future__ import annotations

import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable, Iterable

import cv2 as cv
from loguru import logger

from core.control.control import input_tap, screenshot
from core.services.city_navigation import (
    CityNavigationState,
    ExchangeEntryAdapter,
    observe_city_frame,
)
from core.services.read_only_policy import ActionIntent
from core.services.runtime_control import RUNTIME_DIR
from core.services.session_evidence import capture_session_evidence


class ExchangeAction(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


@dataclass(frozen=True)
class ExchangeNavigationResult:
    success: bool
    action: ExchangeAction
    source: str = "ocr_anchor"
    reason: str = ""
    clicked: tuple[int, int] | None = None
    verified_frames: int = 0
    diagnostic_path: str = ""
    stage: str = ""
    elapsed_seconds: float = 0.0


def _capture_exchange_evidence(
    state_transition_name: str,
    result: ExchangeNavigationResult,
) -> None:
    """Record navigation evidence without changing the navigation outcome."""

    try:
        capture_session_evidence(
            state_transition_name,
            ledger_context=None,
            leg_id="",
            current_page_classification=(
                f"stage={result.stage}|reason={result.reason or 'ok'}|"
                f"clicked={result.clicked}"
            ),
        )
    except Exception as error:
        logger.warning(
            "Unable to capture exchange-navigation evidence for "
            f"{state_transition_name}: {type(error).__name__}"
        )


def _dispatch_evidence_reason(dispatch_result: object) -> str:
    """Describe the guarded dispatch receipt without interpreting success."""

    if dispatch_result is False:
        return "read_only_denied"
    receipt = getattr(dispatch_result, "receipt", None)
    delivery_status = str(
        getattr(dispatch_result, "delivery_status", "")
        or getattr(receipt, "delivery_status", "")
        or ""
    )
    release_status = str(
        getattr(dispatch_result, "release_status", "")
        or getattr(receipt, "release_status", "")
        or ""
    )
    if delivery_status or release_status:
        return (
            f"delivery_status={delivery_status or 'unknown'},"
            f"release_status={release_status or 'unknown'}"
        )
    return str(getattr(dispatch_result, "reason", "") or "call_returned")


def _action(value: ExchangeAction | str) -> ExchangeAction:
    return value if isinstance(value, ExchangeAction) else ExchangeAction(str(value).upper())


def _items(frame: object) -> list[dict]:
    if isinstance(frame, list):
        return frame
    if hasattr(frame, "ocr"):
        return list(frame.ocr())
    return []


def _text(item: dict) -> str:
    return str(item.get("text", "")).replace(" ", "")


def _center(item: dict) -> tuple[int, int]:
    points = item.get("position") or ()
    if len(points) < 3:
        raise ValueError("OCR item has no bounding box")
    return (
        int(round((float(points[0][0]) + float(points[2][0])) / 2)),
        int(round((float(points[0][1]) + float(points[2][1])) / 2)),
    )


def exchange_menu_matches(items: Iterable[dict]) -> bool:
    return observe_city_frame(list(items)).state is CityNavigationState.EXCHANGE_MENU


def exchange_page_matches(items: Iterable[dict], action: ExchangeAction | str) -> bool:
    selected = _action(action)
    observation = observe_city_frame(list(items))
    return ExchangeEntryAdapter.page_matches(observation, selected.value)


class ExchangeNavigator:
    def __init__(
        self,
        frame_provider: Callable[[], object],
        tap: Callable[[tuple[int, int]], None],
        sleep: Callable[[float], None],
        *,
        diagnostic: Callable[[object, ExchangeAction, str], str] | None = None,
        cancellation: Callable[[], bool] | None = None,
        deadline: float | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ):
        self.frame_provider = frame_provider
        self.tap = tap
        self.sleep = sleep
        self.diagnostic = diagnostic
        self.cancellation = cancellation or (lambda: False)
        self.deadline = deadline
        self.monotonic = monotonic

    def _failure(
        self,
        action: ExchangeAction,
        reason: str,
        frame: object,
        *,
        clicked: tuple[int, int] | None = None,
        verified_frames: int = 0,
    ) -> ExchangeNavigationResult:
        path = self.diagnostic(frame, action, reason) if self.diagnostic else ""
        return ExchangeNavigationResult(
            False, action, reason=reason, clicked=clicked,
            verified_frames=verified_frames, diagnostic_path=path,
        )

    def open(
        self,
        action: ExchangeAction | str,
        *,
        verify_frames: int = 2,
    ) -> ExchangeNavigationResult:
        selected = _action(action)
        if self.cancellation() or (
            self.deadline is not None and self.monotonic() >= self.deadline
        ):
            return ExchangeNavigationResult(False, selected, reason="cancelled")
        lobby_frame = self.frame_provider()
        lobby_items = _items(lobby_frame)
        if not exchange_menu_matches(lobby_items):
            return self._failure(selected, "exchange_menu_not_confirmed", lobby_frame)
        label = "我要买" if selected is ExchangeAction.BUY else "我要卖"
        anchors = [item for item in lobby_items if label in _text(item) and item.get("position")]
        if len(anchors) != 1:
            return self._failure(selected, "action_anchor_not_unique", lobby_frame)
        clicked = _center(anchors[0])
        if self.tap(clicked) is False:
            return self._failure(selected, "read_only_denied", lobby_frame, clicked=clicked)
        if verify_frames < 2:
            return self._failure(selected, "multiframe_verification_required", lobby_frame, clicked=clicked)
        verified = 0
        last = lobby_frame
        # The action panel also animates after the OCR-anchor click.  Sample a
        # bounded window and require consecutive matching frames; transition
        # frames neither count as success nor cause another tap.
        for _ in range(max(6, int(verify_frames) * 4)):
            if self.cancellation():
                return self._failure(selected, "cancelled", last, clicked=clicked, verified_frames=verified)
            if self.deadline is not None and self.monotonic() >= self.deadline:
                return self._failure(selected, "overall_deadline_exceeded", last, clicked=clicked, verified_frames=verified)
            self.sleep(0.35)
            last = self.frame_provider()
            if not exchange_page_matches(_items(last), selected):
                verified = 0
                continue
            verified += 1
            if verified >= int(verify_frames):
                return ExchangeNavigationResult(True, selected, clicked=clicked, verified_frames=verified)
        return self._failure(
            selected, "postcondition_unstable", last,
            clicked=clicked, verified_frames=verified,
        )


def _save_diagnostic(frame: object, action: ExchangeAction, reason: str) -> str:
    image = getattr(frame, "image", None)
    if image is None:
        return ""
    root = RUNTIME_DIR / "exchange-navigation"
    root.mkdir(parents=True, exist_ok=True)
    target = root / f"{action.value.lower()}-{reason}.png"
    try:
        cv.imwrite(str(target), image)
    except Exception as error:  # diagnostic failure must not change safety result
        logger.warning(f"保存交易所导航诊断截图失败: {error}")
        return ""
    return str(target)


def open_exchange_action(
    action: ExchangeAction | str,
    *,
    read_only: bool = False,
    cancellation: Callable[[], bool] | None = None,
    timeout: float = 30.0,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> ExchangeNavigationResult:
    """Open a verified action page; never clicks any transaction control."""

    selected = _action(action)
    started = monotonic()
    deadline = started + max(0.0, float(timeout))

    def deadline_failure(stage: str, reason: str = "overall_deadline_exceeded"):
        return ExchangeNavigationResult(
            False, selected, reason=reason, stage=stage,
            elapsed_seconds=max(0.0, monotonic() - started),
        )

    def safe_anchor_tap(pos: tuple[int, int]):
        action_key = (
            "exchange_buy_navigation"
            if selected is ExchangeAction.BUY
            else "exchange_sell_navigation"
        )
        target = "buy_navigation" if selected is ExchangeAction.BUY else "sell_navigation"
        label = "我要买" if selected is ExchangeAction.BUY else "我要卖"
        dispatch_result = input_tap(
            pos,
            intent=ActionIntent(
                action_key, target,
                f"exchange:{selected.value.lower()}:anchor",
            ),
        )
        _capture_exchange_evidence(
            "EXCHANGE_ANCHOR_DISPATCH",
            ExchangeNavigationResult(
                dispatch_result is not False,
                selected,
                reason=_dispatch_evidence_reason(dispatch_result),
                clicked=pos,
                stage="action_anchor_dispatch",
            ),
        )
        return dispatch_result
    if monotonic() >= deadline:
        return deadline_failure("initial_capture")
    frame = screenshot()
    if not exchange_menu_matches(_items(frame)):
        from core.preset import go_outlets
        from core.preset.control import go_home

        try:
            home_ok = go_home(deadline=deadline, cancellation=cancellation)
        except TypeError as error:
            if "unexpected keyword" not in str(error):
                raise
            home_ok = go_home()
        home_result = ExchangeNavigationResult(
            bool(home_ok),
            selected,
            reason=(
                ""
                if home_ok
                else (
                    "overall_deadline_exceeded"
                    if monotonic() >= deadline
                    else "home_navigation_failed"
                )
            ),
            stage="home_navigation",
        )
        _capture_exchange_evidence("EXCHANGE_HOME_VERIFY", home_result)
        if not home_ok:
            return deadline_failure("home_navigation", home_result.reason)
        if monotonic() >= deadline:
            return deadline_failure("home_navigation")
        try:
            outlet = go_outlets(
                "交易所", deadline=deadline, cancellation=cancellation,
                monotonic=monotonic,
            )
        except TypeError as error:
            if "unexpected keyword" not in str(error):
                raise
            outlet = go_outlets("交易所")
        outlet_result = ExchangeNavigationResult(
            bool(outlet),
            selected,
            reason=str(getattr(outlet, "reason", "") or ""),
            stage=str(getattr(outlet, "stage", "outlet_navigation")),
        )
        _capture_exchange_evidence("EXCHANGE_ANCHOR_SEARCH", outlet_result)
        if not outlet:
            return deadline_failure(
                "outlet_navigation",
                getattr(outlet, "reason", "exchange_navigation_failed"),
            )
        # ``go_outlets`` returns after selecting the NPC, while the dialogue
        # panel is still animating on slower emulators.  Wait for the actual
        # exchange menu instead of treating that transition frame as a hard
        # navigation failure.  This loop is read-only and bounded.
        frame = None
        for _ in range(12):
            if cancellation and cancellation():
                return ExchangeNavigationResult(False, selected, reason="cancelled")
            if monotonic() >= deadline:
                return deadline_failure("exchange_menu_wait")
            sleep(min(0.5, max(0.0, deadline - monotonic())))
            candidate = screenshot()
            if exchange_menu_matches(_items(candidate)):
                frame = candidate
                break
        if frame is None:
            result = ExchangeNavigator(
                lambda: candidate,
                safe_anchor_tap,
                sleep,
                diagnostic=_save_diagnostic,
                cancellation=cancellation,
                deadline=deadline,
                monotonic=monotonic,
            )._failure(selected, "exchange_menu_not_confirmed", candidate)
            menu_result = ExchangeNavigationResult(
                result.success,
                selected,
                reason=result.reason,
                clicked=result.clicked,
                verified_frames=result.verified_frames,
                diagnostic_path=result.diagnostic_path,
                stage="exchange_menu_wait",
                elapsed_seconds=max(0.0, monotonic() - started),
            )
            _capture_exchange_evidence("EXCHANGE_MENU_OBSERVE", menu_result)
            return result
    else:
        _capture_exchange_evidence(
            "EXCHANGE_HOME_VERIFY",
            ExchangeNavigationResult(
                True,
                selected,
                reason="exchange_menu_already_visible",
                stage="home_navigation",
            ),
        )
    first_frame = frame
    first_pending = True

    def next_frame():
        nonlocal first_pending
        if first_pending:
            first_pending = False
            return first_frame
        return screenshot()

    result = ExchangeNavigator(
        next_frame,
        safe_anchor_tap,
        sleep,
        diagnostic=_save_diagnostic,
        cancellation=cancellation,
        deadline=deadline,
        monotonic=monotonic,
    ).open(selected)
    _capture_exchange_evidence(
        "EXCHANGE_MENU_OBSERVE",
        ExchangeNavigationResult(
            result.success,
            selected,
            reason=result.reason,
            clicked=result.clicked,
            verified_frames=result.verified_frames,
            diagnostic_path=result.diagnostic_path,
            stage=result.stage or "exchange_action_postcondition",
            elapsed_seconds=max(0.0, monotonic() - started),
        ),
    )
    logger.info(
        "交易所导航 action={} read_only={} success={} source={} reason={}".format(
            selected.value, bool(read_only), result.success, result.source, result.reason
        )
    )
    return result
