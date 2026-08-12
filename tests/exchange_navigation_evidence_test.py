from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import auto.exchange_navigation as exchange
import numpy as np
from core.services.dispatch_outcome import DispatchStatus, outcome_from_receipt


PARENT_CONTROL_EVIDENCE = (
    Path(__file__).parent / "fixtures" / "exchange_city_parent_control"
)


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


def test_exchange_city_parent_control_resolves_unique_circle_below_anchor(
    monkeypatch,
):
    image = np.zeros((720, 1280, 3), dtype=np.uint8)
    anchor = _ocr("交易所", 900, 276)
    monkeypatch.setattr(
        exchange.cv,
        "HoughCircles",
        lambda *_args, **_kwargs: np.array(
            [[[74.0, 69.0, 45.0]]], dtype=np.float32
        ),
    )

    assert exchange._exchange_city_parent_control(image, anchor) == (
        (900, 350),
        45,
    )


def test_exchange_city_parent_control_rejects_ambiguous_circles(monkeypatch):
    image = np.zeros((720, 1280, 3), dtype=np.uint8)
    anchor = _ocr("交易所", 900, 276)
    monkeypatch.setattr(
        exchange.cv,
        "HoughCircles",
        lambda *_args, **_kwargs: np.array(
            [[[74.0, 69.0, 45.0], [92.0, 76.0, 42.0]]],
            dtype=np.float32,
        ),
    )

    assert exchange._exchange_city_parent_control(image, anchor) is None


def test_real_exchange_city_frames_resolve_stable_npc_control_below_text():
    """The guarded target is the NPC portrait, never the OCR label center."""

    anchor = {
        "text": "交易所",
        "position": [[854, 263], [950, 263], [950, 291], [854, 291]],
    }
    anchor_center = exchange._center(anchor)
    controls: list[tuple[tuple[int, int], int]] = []
    for evidence_path in sorted(PARENT_CONTROL_EVIDENCE.glob("*.png")):
        crop = exchange.cv.imread(str(evidence_path))
        assert crop is not None
        frame = np.zeros((720, 1280, 3), dtype=np.uint8)
        frame[275:470, 760:1040] = crop
        resolved = exchange._exchange_city_parent_control(frame, anchor)
        assert resolved is not None
        controls.append(resolved)

    assert len(controls) == 3
    assert exchange._stable_exchange_city_parent_controls(controls) is True
    for (center_x, center_y), radius in controls:
        assert abs(center_x - 900) <= 4
        assert 335 <= center_y <= 355
        assert 40 <= radius <= 55
        assert center_y - anchor_center[1] >= 55


def test_initial_exchange_outlet_dispatch_uses_visual_parent_control(
    monkeypatch,
):
    city_frame = SimpleNamespace(
        image=np.zeros((720, 1280, 3), dtype=np.uint8),
        ocr=lambda: _city(),
    )
    frames = iter([[], city_frame, _lobby(), _buy(), _buy()])
    taps: list[tuple[tuple[int, int], dict]] = []
    captured: list[tuple[str, str]] = []

    def fake_go_outlets(_name, *, ocr_click, swipe, **_kwargs):
        assert ocr_click("交易所") is True
        return _StructuredOutletResult()

    monkeypatch.setattr(exchange, "screenshot", lambda: next(frames))
    monkeypatch.setattr(
        exchange,
        "input_tap",
        lambda point, **kwargs: taps.append((point, kwargs)) or True,
    )
    monkeypatch.setattr(
        exchange,
        "_exchange_city_parent_control",
        lambda _frame, _anchor: ((900, 350), 45),
    )
    monkeypatch.setattr("core.preset.control.go_home", lambda **_kwargs: True)
    monkeypatch.setattr("core.preset.go_outlets", fake_go_outlets)
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
    assert [point for point, _kwargs in taps] == [(900, 350), (805, 324)]
    assert taps[0][1]["random_offset"] is False
    assert taps[0][1]["intent"].action_key == "navigation_anchor"
    dispatches = [
        json.loads(classification)
        for transition, classification in captured
        if transition == "EXCHANGE_CITY_ANCHOR_REDISPATCH"
    ]
    assert len(dispatches) == 1
    assert dispatches[0]["anchor_coordinate"] == [900, 350]
    assert dispatches[0]["attempt"] == 1
    assert dispatches[0]["total"] == 3
    assert dispatches[0]["dispatch_result"] == "call_returned"
    assert set(dispatches[0]["page_texts"]) == {
        "交易所", "城市发展度", "商会", "岚心城",
    }
    assert dispatches[0]["stage"] == "city_anchor_redispatch"


def test_initial_exchange_outlet_blocks_when_parent_control_is_unresolved(
    monkeypatch,
):
    city_frame = SimpleNamespace(
        image=np.zeros((720, 1280, 3), dtype=np.uint8),
        ocr=lambda: _city(),
    )
    frames = iter([[], city_frame])
    taps: list[tuple[int, int]] = []

    def fake_go_outlets(_name, *, ocr_click, swipe, **_kwargs):
        assert ocr_click("交易所") is False
        assert swipe((1, 2), (3, 4)) is False
        return _OutletResult(False, "outlet_scroll", "read_only_denied")

    monkeypatch.setattr(exchange, "screenshot", lambda: next(frames))
    monkeypatch.setattr(
        exchange,
        "input_tap",
        lambda point, **_kwargs: taps.append(point) or True,
    )
    monkeypatch.setattr(
        exchange,
        "input_swipe",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("terminal parent-control block must not swipe")
        ),
    )
    monkeypatch.setattr(
        exchange, "_exchange_city_parent_control", lambda _frame, _anchor: None
    )
    monkeypatch.setattr("core.preset.control.go_home", lambda **_kwargs: True)
    monkeypatch.setattr("core.preset.go_outlets", fake_go_outlets)

    result = exchange.open_exchange_action(
        exchange.ExchangeAction.BUY,
        read_only=True,
        sleep=lambda _seconds: None,
    )

    assert result.success is False
    assert result.reason == "visual_parent_control_unresolved"
    assert taps == []


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


def test_city_marker_prefers_exact_city_label_over_station_in_mission_text():
    items = [
        _ocr("岚心城", 226, 552),
        _ocr("于汇流塔完成10个作战计划", 1159, 239),
        _ocr("交易所", 901, 274),
    ]

    marker = exchange._resolve_city_marker(
        items,
        SimpleNamespace(city=SimpleNamespace(station_id="")),
    )

    assert marker == "岚心城"


def test_city_marker_rejects_two_exact_station_labels_as_ambiguous():
    items = [
        _ocr("岚心城", 226, 552),
        _ocr("汇流塔", 1159, 239),
        _ocr("交易所", 901, 274),
    ]

    marker = exchange._resolve_city_marker(
        items,
        SimpleNamespace(city=SimpleNamespace(station_id="")),
    )

    assert marker == ""


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
    monkeypatch.setattr(
        exchange,
        "_exchange_city_parent_control",
        lambda _frame, _anchor: ((900, 350), 45),
    )

    result = exchange.open_exchange_action(
        exchange.ExchangeAction.BUY,
        read_only=True,
        monotonic=lambda: clock[0],
        sleep=lambda seconds: clock.__setitem__(0, clock[0] + seconds),
    )

    assert result.success is True
    assert taps == [(900, 350), (805, 324)]
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
    assert observations[2]["parent_control_coordinate"] == [900, 350]
    assert observations[2]["parent_control_stable"] is True
    assert observations[3]["physical_dispatches"] == 2
