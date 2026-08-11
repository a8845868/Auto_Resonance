from __future__ import annotations

import json
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


class _StructuredOutletResult(_OutletResult):
    def __init__(self):
        super().__init__(True, "outlet_selected")
        self.city = SimpleNamespace(station_id="岚心城")


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


def _city() -> list[dict]:
    return [
        _ocr("岚心城", 200, 200),
        _ocr("交易所", 900, 276),
        _ocr("商会", 800, 350),
        _ocr("城市发展度", 300, 400),
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


def test_city_signature_ignores_multichar_latin_ocr_noise():
    stable_city_texts = [
        _ocr("岚心城", 200, 200),
        _ocr("交易所", 900, 276),
        _ocr("商会", 800, 350),
        _ocr("前往作战终端", 500, 400),
    ]
    signatures = [
        exchange._semantic_page_texts(stable_city_texts + [_ocr(noise, 50, 50)])
        for noise in ("SHN", "SHHN", "D.U.N")
    ]

    assert signatures == [
        frozenset({"岚心城", "交易所", "商会", "前往作战终端"})
    ] * 3
    assert exchange._stable_city_signatures(signatures) is True


def test_city_signature_still_rejects_material_chinese_page_change():
    signatures = [
        frozenset({"岚心城", "交易所", "商会", "前往作战终端"}),
        frozenset({"岚心城", "交易所", "商会", "前往作战终端"}),
        frozenset({"总部商店", "黑月商店", "特惠礼包"}),
    ]

    assert exchange._stable_city_signatures(signatures) is False


def test_city_anchor_observation_evidence_contains_each_gate_value(monkeypatch):
    captured: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        exchange,
        "capture_session_evidence",
        lambda transition, **kwargs: captured.append(
            (
                transition,
                json.loads(kwargs["current_page_classification"]),
            )
        ),
    )

    exchange._capture_city_anchor_observation(
        observation_index=4,
        items=_city(),
        city_marker="岚心城",
        rejection_reason="semantic_signature_unstable",
        retryable=True,
        jaccard_to_previous=0.81234567,
        stable_window_size=2,
        signature_stable=False,
        cooldown_elapsed=1.25,
        physical_dispatches=1,
    )

    assert captured[0][0] == "EXCHANGE_CITY_ANCHOR_OBSERVE"
    payload = captured[0][1]
    assert payload["observation_index"] == 4
    assert payload["city_marker"] == "岚心城"
    assert payload["city_marker_present"] is True
    assert payload["page_state"] == "EXCHANGE_NPC_VISIBLE"
    assert payload["anchor_count"] == 1
    assert payload["anchor_coordinate"] == [900, 276]
    assert payload["rejection_reason"] == "semantic_signature_unstable"
    assert payload["jaccard_to_previous"] == 0.812346
    assert payload["stable_window_size"] == 2
    assert payload["signature_stable"] is False
    assert payload["cooldown_elapsed"] == 1.25
    assert payload["physical_dispatches"] == 1
    assert payload["dispatch_limit"] == 3
    assert set(payload["page_texts"]) == {
        "岚心城",
        "交易所",
        "商会",
        "城市发展度",
    }


def test_city_anchor_observation_capture_failure_is_non_blocking(monkeypatch):
    monkeypatch.setattr(
        exchange,
        "capture_session_evidence",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk full")),
    )

    exchange._capture_city_anchor_observation(
        observation_index=0,
        items=_city(),
        city_marker="岚心城",
        rejection_reason="semantic_signature_unstable",
        retryable=True,
        jaccard_to_previous=None,
        stable_window_size=1,
        signature_stable=False,
        cooldown_elapsed=0.0,
        physical_dispatches=1,
    )


def test_city_anchor_observation_derivation_failure_is_non_blocking(monkeypatch):
    monkeypatch.setattr(
        exchange,
        "observe_city_frame",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("bad frame")),
    )

    exchange._capture_city_anchor_observation(
        observation_index=0,
        items=_city(),
        city_marker="岚心城",
        rejection_reason="semantic_signature_unstable",
        retryable=True,
        jaccard_to_previous=None,
        stable_window_size=1,
        signature_stable=False,
        cooldown_elapsed=0.0,
        physical_dispatches=1,
    )


def test_city_anchor_observation_traces_stability_cooldown_and_redispatch(
    monkeypatch,
):
    frames = iter(
        [
            [],
            _city(),
            _city(),
            _city(),
            _city(),
            _lobby(),
            _buy(),
            _buy(),
        ]
    )
    clock = [0.0]
    taps: list[tuple[int, int]] = []
    captured: list[tuple[str, str]] = []
    monkeypatch.setattr(exchange, "screenshot", lambda: next(frames))
    monkeypatch.setattr(
        exchange,
        "input_tap",
        lambda point, **_kwargs: taps.append(point) or True,
    )
    monkeypatch.setattr("core.preset.control.go_home", lambda **_kwargs: True)
    monkeypatch.setattr(
        "core.preset.go_outlets",
        lambda *_args, **_kwargs: _StructuredOutletResult(),
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
        monotonic=lambda: clock[0],
        sleep=lambda seconds: clock.__setitem__(0, clock[0] + seconds),
    )

    assert result.success is True
    assert taps == [(900, 276), (805, 324)]
    observations = [
        json.loads(classification)
        for transition, classification in captured
        if transition == "EXCHANGE_CITY_ANCHOR_OBSERVE"
    ]
    assert [item["rejection_reason"] for item in observations] == [
        "semantic_signature_unstable",
        "semantic_signature_unstable",
        "cooldown_pending",
        "redispatch_authorized",
        "exchange_menu_visible",
    ]
    assert observations[2]["jaccard_to_previous"] == 1.0
    assert observations[2]["signature_stable"] is True
    assert observations[3]["physical_dispatches"] == 2
