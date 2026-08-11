from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import numpy as np

import core.services.city_navigation as city_navigation_module
from core.services.city_navigation import CityNavigationAdapter
from core.services.navigation_parent_control import (
    NavigationParentControlObservation,
)
from core.services.session_evidence import _SESSION_EVIDENCE


NOW = datetime(2026, 8, 12, 10, 0, tzinfo=timezone(timedelta(hours=8)))


def _item(text: str, x: int, y: int) -> dict:
    return {
        "text": text,
        "position": [
            [x - 30, y - 12],
            [x + 30, y - 12],
            [x + 30, y + 12],
            [x - 30, y + 12],
        ],
    }


class _Frame:
    def __init__(self, *, pixel: int, capture_id: str):
        self.image = np.full((720, 1280, 3), pixel, dtype=np.uint8)
        self.raw_frame_hash = f"{pixel + 1:064x}"
        self.source_capture_id = capture_id
        self.captured_at = NOW
        self._items = [
            _item("访问城市", 1100, 480),
            _item("作战终端", 500, 220),
            _item("启程", 700, 220),
        ]

    def ocr(self):
        return list(self._items)


class _Clock:
    def __init__(self):
        self.value = 100.0

    def monotonic(self):
        return self.value

    def sleep(self, seconds: float):
        self.value += float(seconds)


def _parent(capture_id: str, x: int) -> NavigationParentControlObservation:
    return NavigationParentControlObservation(
        semantic_id="visit_city",
        anchor_bbox=(1070, 468, 1130, 492),
        parent_control_bbox=(1000, 440, 1280, 520),
        safe_hit_bbox=(x - 12, 468, x + 12, 492),
        safe_hit_point=(x, 480),
        parent_detection_method="SEMANTIC_CONTROL_SLOT",
        candidate_count=1,
        confidence="HIGH",
        source_capture_id=capture_id,
        source_frame_sha256=capture_id.encode("utf-8").hex().ljust(64, "0")[:64],
        reason_codes=(),
        evidence_ids=("parent_control_unique",),
    )


def _unstable_adapter(monkeypatch, *, capture):
    parents = iter(
        (
            _parent("home-1", 1017),
            _parent("home-2", 1037),
            _parent("home-3", 1057),
        )
    )
    monkeypatch.setattr(
        city_navigation_module,
        "resolve_navigation_parent_control",
        lambda *_args, **_kwargs: next(parents),
    )
    monkeypatch.setattr(
        city_navigation_module, "capture_session_evidence", capture
    )
    frames = iter(
        (
            _Frame(pixel=1, capture_id="home-1"),
            _Frame(pixel=2, capture_id="home-2"),
            _Frame(pixel=3, capture_id="home-3"),
        )
    )
    clock = _Clock()
    taps = []
    adapter = CityNavigationAdapter(
        frame_provider=lambda: next(frames),
        tap=lambda *args, **kwargs: taps.append((args, kwargs)) or True,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
        now=lambda: NOW,
        timeout=10.0,
        max_attempts=3,
        stall_frames=5,
        correlation_id="CITY-EVIDENCE-TEST",
        evidence_recorder=lambda _evidence: True,
    )
    return adapter, taps


def test_evidence_disabled_keeps_parent_control_result_unchanged(monkeypatch):
    token = _SESSION_EVIDENCE.set(None)
    try:
        adapter, taps = _unstable_adapter(
            monkeypatch,
            capture=city_navigation_module.capture_session_evidence,
        )
        result = adapter.enter_city()
    finally:
        _SESSION_EVIDENCE.reset(token)

    assert result.status == "BLOCKED"
    assert result.reason == "city_parent_control_unstable"
    assert result.dispatch_count == 0
    assert taps == []


def test_internal_fresh_frames_emit_parent_control_observation_evidence(
    monkeypatch,
):
    captured = []

    def capture(name, **kwargs):
        captured.append((name, kwargs))

    adapter, taps = _unstable_adapter(monkeypatch, capture=capture)
    result = adapter.enter_city()

    assert result.reason == "city_parent_control_unstable"
    assert taps == []
    assert [name for name, _kwargs in captured] == [
        "CITY_PARENT_CONTROL_OBSERVE",
        "CITY_PARENT_CONTROL_OBSERVE",
    ]
    first = json.loads(captured[0][1]["current_page_classification"])
    assert first == {
        "observation_index": 1,
        "anchor_bbox": [1070, 468, 1130, 492],
        "parent_bbox": [1000, 440, 1280, 520],
        "safe_bbox": [1025, 468, 1049, 492],
        "safe_hit_point": [1037, 480],
        "confirm_matched": False,
        "failure_reason": "prior[0]=safe_bbox_iou_below_0_75",
        "comparisons": [
            {
                "prior_observation_index": 0,
                "matched": False,
                "reason": "safe_bbox_iou_below_0_75",
            }
        ],
    }
    assert captured[0][1]["ledger_context"] is None
    assert captured[0][1]["leg_id"] == ""


def test_evidence_capture_failure_does_not_change_parent_control_result(
    monkeypatch,
):
    calls = []

    def capture(*_args, **_kwargs):
        calls.append(True)
        raise OSError("evidence unavailable")

    adapter, taps = _unstable_adapter(monkeypatch, capture=capture)
    result = adapter.enter_city()

    assert len(calls) == 2
    assert result.status == "BLOCKED"
    assert result.reason == "city_parent_control_unstable"
    assert result.dispatch_count == 0
    assert result.physical_input_count == 0
    assert taps == []
