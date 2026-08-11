from __future__ import annotations

from types import SimpleNamespace

import auto.exchange_navigation as exchange
from core.services.dispatch_outcome import DispatchStatus, outcome_from_receipt


class _OutletResult:
    def __init__(self, success: bool, stage: str, reason: str = ""):
        self.success = success
        self.stage = stage
        self.reason = reason

    def __bool__(self) -> bool:
        return self.success


def _ocr(text: str, x: int, y: int) -> dict:
    return {
        "text": text,
        "position": [
            [x - 10, y - 10],
            [x + 10, y - 10],
            [x + 10, y + 10],
            [x - 10, y + 10],
        ],
    }


def _lobby() -> list[dict]:
    return [
        _ocr("交易所", 960, 176),
        _ocr("我要买", 805, 324),
        _ocr("我要卖", 804, 407),
        _ocr("交易品投资", 820, 489),
        _ocr("私人仓库", 814, 572),
    ]


def _buy() -> list[dict]:
    return [
        _ocr("交易品", 200, 100),
        _ocr("预计买入", 900, 500),
        _ocr("全部买入", 900, 600),
        _ocr("买入总价(含税)", 900, 550),
        _ocr("载货量", 600, 650),
    ]


def _install_success_path(monkeypatch, *, dispatch_result=True):
    frames = iter([[], _lobby(), _buy(), _buy()])
    monkeypatch.setattr(exchange, "screenshot", lambda: next(frames))
    monkeypatch.setattr(exchange, "input_tap", lambda *_args, **_kwargs: dispatch_result)
    monkeypatch.setattr("core.preset.control.go_home", lambda **_kwargs: True)
    monkeypatch.setattr(
        "core.preset.go_outlets",
        lambda *_args, **_kwargs: _OutletResult(True, "outlet_selected"),
    )


def test_evidence_disabled_keeps_open_exchange_action_behavior(monkeypatch):
    frames = iter([_lobby(), _buy(), _buy()])
    taps: list[tuple[int, int]] = []
    monkeypatch.setattr(exchange, "screenshot", lambda: next(frames))
    monkeypatch.setattr(
        exchange, "input_tap",
        lambda point, **_kwargs: taps.append(point) or True,
    )

    result = exchange.open_exchange_action(
        exchange.ExchangeAction.BUY,
        read_only=True,
        sleep=lambda _seconds: None,
    )

    assert result.success is True
    assert result.clicked == (805, 324)
    assert taps == [(805, 324)]


def test_success_path_evidence_contains_stage_reason_clicked_and_receipt(monkeypatch):
    captured: list[tuple[str, str]] = []
    receipt = SimpleNamespace(
        delivery_status="NATIVE_ACCEPTED",
        release_status="CONFIRMED",
    )
    dispatch_result = outcome_from_receipt(
        DispatchStatus.DISPATCHED_VERIFIED,
        receipt=receipt,
    )
    _install_success_path(monkeypatch, dispatch_result=dispatch_result)
    monkeypatch.setattr(
        exchange,
        "capture_session_evidence",
        lambda transition, **kwargs: captured.append(
            (transition, kwargs["current_page_classification"])
        ),
    )

    result = exchange.open_exchange_action(
        exchange.ExchangeAction.BUY,
        read_only=True,
        sleep=lambda _seconds: None,
    )

    assert result.success is True
    assert [transition for transition, _ in captured] == [
        "EXCHANGE_HOME_VERIFY",
        "EXCHANGE_ANCHOR_SEARCH",
        "EXCHANGE_ANCHOR_DISPATCH",
        "EXCHANGE_MENU_OBSERVE",
    ]
    classifications = dict(captured)
    assert classifications["EXCHANGE_HOME_VERIFY"] == (
        "stage=home_navigation|reason=ok|clicked=None"
    )
    assert classifications["EXCHANGE_ANCHOR_SEARCH"] == (
        "stage=outlet_selected|reason=ok|clicked=None"
    )
    assert classifications["EXCHANGE_ANCHOR_DISPATCH"] == (
        "stage=action_anchor_dispatch|"
        "reason=delivery_status=NATIVE_ACCEPTED,release_status=CONFIRMED|"
        "clicked=(805, 324)"
    )
    assert classifications["EXCHANGE_MENU_OBSERVE"] == (
        "stage=exchange_action_postcondition|reason=ok|clicked=(805, 324)"
    )


def test_outlet_failure_evidence_preserves_stage_and_reason(monkeypatch):
    captured: list[tuple[str, str]] = []
    monkeypatch.setattr(exchange, "screenshot", lambda: [])
    monkeypatch.setattr("core.preset.control.go_home", lambda **_kwargs: True)
    monkeypatch.setattr(
        "core.preset.go_outlets",
        lambda *_args, **_kwargs: _OutletResult(
            False, "outlet_search", "outlet_not_found"
        ),
    )
    monkeypatch.setattr(
        exchange,
        "capture_session_evidence",
        lambda transition, **kwargs: captured.append(
            (transition, kwargs["current_page_classification"])
        ),
    )

    result = exchange.open_exchange_action(
        exchange.ExchangeAction.BUY,
        read_only=True,
        sleep=lambda _seconds: None,
    )

    assert result.success is False
    assert result.reason == "outlet_not_found"
    assert dict(captured)["EXCHANGE_ANCHOR_SEARCH"] == (
        "stage=outlet_search|reason=outlet_not_found|clicked=None"
    )


def test_evidence_capture_exception_does_not_change_return_value(monkeypatch):
    frames = iter([_lobby(), _buy(), _buy()])
    taps: list[tuple[int, int]] = []
    monkeypatch.setattr(exchange, "screenshot", lambda: next(frames))
    monkeypatch.setattr(
        exchange, "input_tap",
        lambda point, **_kwargs: taps.append(point) or True,
    )
    monkeypatch.setattr(
        exchange,
        "capture_session_evidence",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk full")),
    )

    result = exchange.open_exchange_action(
        exchange.ExchangeAction.BUY,
        read_only=True,
        sleep=lambda _seconds: None,
    )

    assert result.success is True
    assert result.clicked == (805, 324)
    assert taps == [(805, 324)]
