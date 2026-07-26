from __future__ import annotations

import numpy as np
import pytest

from core.services.action_summary_navigation import (
    ActionSummaryNavigator,
    ActionSummaryState,
    observe_action_summary,
)


def item(text, x, y, width=100, height=24):
    return {
        "text": text,
        "position": [
            [x - width // 2, y - height // 2],
            [x + width // 2, y - height // 2],
            [x + width // 2, y + height // 2],
            [x - width // 2, y + height // 2],
        ],
    }


class Frame:
    def __init__(self, labels, *, size=(1280, 720), capture_id=""):
        self.image = np.zeros((size[1], size[0], 3), dtype=np.uint8)
        self._labels = labels
        self.source_capture_id = capture_id

    def ocr(self):
        return list(self._labels)


class Clock:
    def __init__(self):
        self.value = 0.0

    def __call__(self):
        return self.value

    def sleep(self, seconds):
        self.value += seconds


class Frames:
    def __init__(self, frames):
        self.frames = list(frames)
        self.index = 0

    def __call__(self):
        frame = self.frames[min(self.index, len(self.frames) - 1)]
        self.index += 1
        return frame


def home(capture_id="", size=(1280, 720), *, duplicate=False):
    labels = [item("访问城市", 1172, 486), item("作战终端", 1191, 410)]
    if duplicate:
        labels.append(item("作战终端", 600, 300))
    return Frame(labels, size=size, capture_id=capture_id)


def overview(capture_id="", size=(1280, 720)):
    return Frame(
        [item("常规活动", 180, 150), item("全域整备", 110, 285)],
        size=size, capture_id=capture_id,
    )


def entry(capture_id="", size=(1280, 720)):
    return Frame([item("行动汇总", 1060, 380)], size=size, capture_id=capture_id)


def summary(capture_id="", size=(1280, 720)):
    return Frame(
        [
            item("利刃围剿", 640, 90),
            item("特殊订单", 280, 300),
            item("利刃行动", 640, 300),
            item("进入挑战", 280, 610),
            item("进入挑战", 640, 610),
        ],
        size=size, capture_id=capture_id,
    )


def run(frames):
    clock = Clock()
    taps = []
    evidence = []

    def tap(point, **kwargs):
        taps.append((point, kwargs))
        return True

    result = ActionSummaryNavigator(
        frame_provider=Frames(frames), tap=tap,
        evidence_recorder=lambda attempt: evidence.append(attempt),
        monotonic=clock, sleep=clock.sleep, postcondition_timeout=1.0,
        poll_interval=0.2,
    ).navigate()
    return result, taps, evidence


def test_complete_navigation_has_three_single_dispatch_stages_and_stops():
    result, taps, evidence = run([
        home("1"), home("2"), overview("3"), overview("4"),
        entry("5"), entry("6"), summary("7"),
    ])
    assert result.success
    assert result.state is ActionSummaryState.ACTION_SUMMARY_VISIBLE
    assert result.dispatch_count == result.stage_count == 3
    assert len(taps) == len(evidence) == 3
    assert [call[0] for call in taps] == [(1191, 410), (110, 285), (1060, 380)]
    assert all(call[1]["random_offset"] is False for call in taps)
    assert all(attempt.coordinate_chain.complete for attempt in evidence)
    assert all(attempt.dispatch_acknowledged for attempt in evidence)


def test_first_postcondition_timeout_stops_all_later_stages():
    result, taps, evidence = run([home("1"), home("2"), home("3"), home("4")])
    assert not result.success
    assert result.reason == "postcondition_timeout"
    assert result.dispatch_count == 1
    assert len(taps) == len(evidence) == 1


def test_stale_fresh_confirmation_has_zero_input():
    result, taps, evidence = run([home("same"), home("same")])
    assert not result.success
    assert result.reason == "stale_frame_action"
    assert taps == evidence == []


def test_multiple_home_candidates_have_zero_input():
    result, taps, evidence = run([home("1"), home("2", duplicate=True)])
    assert not result.success
    assert result.reason == "candidate_not_unique_or_safe"
    assert taps == evidence == []


def test_nonempty_unknown_waits_until_deadline_without_more_input():
    unknown = Frame([item("加载中", 640, 360)])
    result, taps, evidence = run([home(), home(), unknown, unknown, unknown])
    assert not result.success
    assert result.reason == "postcondition_timeout"
    assert len(taps) == len(evidence) == 1


def test_stale_post_frame_is_ignored_until_fresh_postcondition():
    result, taps, evidence = run([
        home("1"), home("2"), home("2"), overview("3"), overview("4"),
        entry("5"), entry("6"), summary("7"),
    ])
    assert result.success
    assert len(taps) == len(evidence) == 3
    assert result.timeline[0]["transition_classification"] == "STALE"


def test_foreign_inventory_page_fails_immediately():
    foreign = Frame([item("道具", 1000, 100), item("材料", 1000, 200)])
    result, taps, _ = run([home(), home(), foreign])
    assert not result.success
    assert result.reason == "unexpected_page"
    assert len(taps) == 1


def test_old_single_title_is_not_action_summary_success():
    observed = observe_action_summary(Frame([item("利刃围剿", 640, 90)]))
    assert observed.state is ActionSummaryState.UNKNOWN


@pytest.mark.parametrize("size", [(1280, 720), (851, 480), (853, 480)])
def test_coordinate_chain_is_complete_for_current_capture_sizes(size):
    scale_x, scale_y = size[0] / 1280, size[1] / 720
    scaled = lambda value, scale: round(value * scale)
    frames = [
        Frame([item("访问城市", scaled(1172, scale_x), scaled(486, scale_y)),
               item("作战终端", scaled(1191, scale_x), scaled(410, scale_y))], size=size),
        Frame([item("访问城市", scaled(1172, scale_x), scaled(486, scale_y)),
               item("作战终端", scaled(1191, scale_x), scaled(410, scale_y))], size=size),
        Frame([item("常规活动", scaled(180, scale_x), scaled(150, scale_y)),
               item("全域整备", scaled(110, scale_x), scaled(285, scale_y))], size=size),
        Frame([item("常规活动", scaled(180, scale_x), scaled(150, scale_y)),
               item("全域整备", scaled(110, scale_x), scaled(285, scale_y))], size=size),
        Frame([item("行动汇总", scaled(1060, scale_x), scaled(380, scale_y))], size=size),
        Frame([item("行动汇总", scaled(1060, scale_x), scaled(380, scale_y))], size=size),
        summary(size=size),
    ]
    result, _, evidence = run(frames)
    assert result.success
    assert all(attempt.coordinate_chain.complete for attempt in evidence)


def test_optional_overlay_uses_computed_safe_blank_not_legacy_fixed_point():
    overlay = Frame([
        item("首次进入说明", 640, 180, 400, 80),
        item("触碰空白区域退出", 640, 680, 180, 24),
    ])
    result, taps, _ = run([
        home(), home(), overview(), overview(), overlay, overlay, entry(), entry(), summary(),
    ])
    assert result.success
    assert result.dispatch_count == 4
    assert taps[2][0] != (800, 100)


def test_import_does_not_initialize_backend():
    # Reaching this module and constructing pure frames must not connect ADB.
    assert observe_action_summary(home()).state is ActionSummaryState.HOME_READY
