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
from core.services.runtime_control import RUNTIME_DIR


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


MENU_MARKERS = ("交易所", "我要买", "我要卖", "交易品投资", "私人仓库")
PAGE_COMMON = ("交易品", "载货量")
PAGE_MARKERS = {
    ExchangeAction.BUY: ("预计买入", "全部买入", "买入总价", "含税"),
    ExchangeAction.SELL: ("预计卖出", "全部卖出", "卖出总价", "含税", "出售"),
}


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
    texts = [_text(item) for item in items]
    return sum(any(marker in text for text in texts) for marker in MENU_MARKERS) >= 3


def exchange_page_matches(items: Iterable[dict], action: ExchangeAction | str) -> bool:
    selected = _action(action)
    texts = [_text(item) for item in items]
    common = sum(any(marker in text for text in texts) for marker in PAGE_COMMON)
    specific = sum(any(marker in text for text in texts) for marker in PAGE_MARKERS[selected])
    # The current BUY/SELL pages retain the bottom "我要买/我要卖" tabs, so
    # menu markers may coexist with the target page.  The strong page-specific
    # combination below is what distinguishes the page from the NPC menu.
    return common >= 1 and specific >= 2


class ExchangeNavigator:
    def __init__(
        self,
        frame_provider: Callable[[], object],
        tap: Callable[[tuple[int, int]], None],
        sleep: Callable[[float], None],
        *,
        diagnostic: Callable[[object, ExchangeAction, str], str] | None = None,
        cancellation: Callable[[], bool] | None = None,
    ):
        self.frame_provider = frame_provider
        self.tap = tap
        self.sleep = sleep
        self.diagnostic = diagnostic
        self.cancellation = cancellation or (lambda: False)

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
        if self.cancellation():
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
        self.tap(clicked)
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
) -> ExchangeNavigationResult:
    """Open a verified action page; never clicks any transaction control."""

    selected = _action(action)
    frame = screenshot()
    if not exchange_menu_matches(_items(frame)):
        from core.preset import go_outlets
        from core.preset.control import go_home

        if not go_home() or not go_outlets("交易所"):
            return ExchangeNavigationResult(False, selected, reason="exchange_navigation_failed")
        # ``go_outlets`` returns after selecting the NPC, while the dialogue
        # panel is still animating on slower emulators.  Wait for the actual
        # exchange menu instead of treating that transition frame as a hard
        # navigation failure.  This loop is read-only and bounded.
        frame = None
        for _ in range(12):
            if cancellation and cancellation():
                return ExchangeNavigationResult(False, selected, reason="cancelled")
            time.sleep(0.5)
            candidate = screenshot()
            if exchange_menu_matches(_items(candidate)):
                frame = candidate
                break
        if frame is None:
            return ExchangeNavigator(
                lambda: candidate,
                input_tap,
                time.sleep,
                diagnostic=_save_diagnostic,
                cancellation=cancellation,
            )._failure(selected, "exchange_menu_not_confirmed", candidate)
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
        input_tap,
        time.sleep,
        diagnostic=_save_diagnostic,
        cancellation=cancellation,
    ).open(selected)
    logger.info(
        "交易所导航 action={} read_only={} success={} source={} reason={}".format(
            selected.value, bool(read_only), result.success, result.source, result.reason
        )
    )
    return result
