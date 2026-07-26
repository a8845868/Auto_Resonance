from __future__ import annotations

import numpy as np
import pytest

from core.services.action_summary_navigation import (
    ActionSummaryNavigator,
    ActionSummaryState,
    observe_action_summary,
    resolve_action_terminal_candidate,
    resolve_action_terminal_hit_target,
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
    def __init__(self, labels, *, size=(1280, 720), capture_id="", draw_terminal_visual=True, pixel=0):
        self.image = np.zeros((size[1], size[0], 3), dtype=np.uint8)
        if pixel:
            self.image[:] = pixel
        self._labels = labels
        self.source_capture_id = capture_id
        if draw_terminal_visual:
            for label in labels:
                if str(label.get("text", "")).replace(" ", "") != "作战终端":
                    continue
                points = label["position"]
                left = int(min(point[0] for point in points))
                top = int(min(point[1] for point in points))
                bottom = int(max(point[1] for point in points))
                height = max(4, bottom - top)
                icon_size = max(8, round(height * 1.4))
                right = max(1, left - max(2, round(height * 0.2)))
                x0 = max(0, right - icon_size)
                y0 = max(0, (top + bottom - icon_size) // 2)
                x1 = min(size[0], right)
                y1 = min(size[1], y0 + icon_size)
                self.image[y0:y1, x0:x1] = 255
                inset = max(2, icon_size // 4)
                self.image[y0 + inset:y1 - inset, x0 + inset:x1 - inset] = 0

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
        labels.append(item("作战终端", 1120, 400))
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


def run_first(frames, *, timeout=1.0):
    clock = Clock()
    taps = []
    evidence = []

    def tap(point, **kwargs):
        taps.append((point, kwargs))
        return True

    result = ActionSummaryNavigator(
        frame_provider=Frames(frames), tap=tap,
        evidence_recorder=evidence.append,
        monotonic=clock, sleep=clock.sleep,
        postcondition_timeout=timeout, poll_interval=0.2,
        stop_after_first_stage=True,
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
    assert taps[0][0] != (1191, 410)
    assert [call[0] for call in taps[1:]] == [(110, 285), (1060, 380)]
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
    assert result.reason == "action_terminal_fresh_confirmation_failed"
    assert taps == evidence == []


def test_unique_exact_terminal_ocr_resolves_inside_target_region():
    candidate, evidence = resolve_action_terminal_candidate(home(), phase="test")
    assert candidate is not None
    assert candidate.candidate_type == "exact_ocr"
    assert evidence.exact_match_count == 1
    assert evidence.safe_candidate_count == 1


def test_label_binds_to_unique_left_visual_parent_and_safe_point_differs():
    frame = home()
    candidate, _ = resolve_action_terminal_candidate(frame, phase="test")
    target, evidence = resolve_action_terminal_hit_target(frame, candidate, phase="test")

    assert target is not None
    assert evidence.parent_container_count == 1
    assert target.label_center == candidate.point
    assert target.hit_target_point != target.label_center
    assert target.icon_bbox[2] <= target.label_bbox[0]
    assert target.hit_target_bbox[0] < target.hit_target_point[0] < target.hit_target_bbox[2]
    assert target.container_bbox[0] <= target.icon_bbox[0]
    assert target.container_bbox[2] >= target.label_bbox[2]


def test_missing_visual_parent_blocks_hit_target():
    frame = Frame(
        [item("访问城市", 1172, 486), item("作战终端", 1191, 410)],
        draw_terminal_visual=False,
    )
    candidate, _ = resolve_action_terminal_candidate(frame, phase="test")
    target, evidence = resolve_action_terminal_hit_target(frame, candidate, phase="test")

    assert target is None
    assert evidence.parent_container_count == 0
    assert "visual_parent_missing" in evidence.rejected_reasons


def test_multiple_visual_parents_block_hit_target():
    frame = home()
    # A second independent square component in the label-relative search band.
    frame.image[398:422, 1079:1103] = 255
    candidate, _ = resolve_action_terminal_candidate(frame, phase="test")
    target, evidence = resolve_action_terminal_hit_target(frame, candidate, phase="test")

    assert target is None
    assert evidence.parent_container_count > 1
    assert "visual_parent_not_unique" in evidence.rejected_reasons


def test_recognized_overlay_covering_icon_blocks_hit_target():
    frame = home()
    frame._labels.append(item("资讯", 1115, 410, 45, 45))
    candidate, _ = resolve_action_terminal_candidate(frame, phase="test")
    target, evidence = resolve_action_terminal_hit_target(frame, candidate, phase="test")

    assert target is None
    assert evidence.target is not None
    assert evidence.target.occlusion_detected
    assert "known_modal_present" in evidence.rejected_reasons


def test_icon_outside_trusted_parent_region_cannot_back_label():
    frame = Frame(
        [item("访问城市", 1172, 486), item("作战终端", 1191, 410)],
        draw_terminal_visual=False,
    )
    frame.image[396:424, 1040:1068] = 255
    candidate, _ = resolve_action_terminal_candidate(frame, phase="test")
    target, evidence = resolve_action_terminal_hit_target(frame, candidate, phase="test")

    assert target is None
    assert evidence.parent_container_count == 0


@pytest.mark.parametrize("size", [(1280, 720), (851, 480), (853, 480)])
def test_hit_target_normalizes_consistently_for_supported_capture_sizes(size):
    sx, sy = size[0] / 1280, size[1] / 720
    frame = Frame([
        item("访问城市", round(1172 * sx), round(486 * sy), round(100 * sx), round(24 * sy)),
        item("作战终端", round(1191 * sx), round(410 * sy), round(100 * sx), round(24 * sy)),
    ], size=size)
    candidate, _ = resolve_action_terminal_candidate(frame, phase="test")
    target, _ = resolve_action_terminal_hit_target(frame, candidate, phase="test")

    assert target is not None
    assert target.hit_target_point[0] / size[0] == pytest.approx(0.87, abs=0.02)
    assert target.hit_target_point[1] / size[1] == pytest.approx(0.57, abs=0.02)


def test_nfkc_whitespace_and_ocr_separator_normalization_remains_exact():
    frame = Frame([
        item("访问城市", 1172, 486),
        item("作 战｜终端", 1191, 410),
    ])
    candidate, evidence = resolve_action_terminal_candidate(frame, phase="test")
    assert candidate is not None
    assert evidence.exact_match_count == 1


def test_two_exact_matches_inside_target_region_are_blocked():
    frame = Frame([
        item("访问城市", 1172, 486),
        item("作战终端", 1120, 400, 70),
        item("作战终端", 1230, 420, 70),
    ])
    candidate, evidence = resolve_action_terminal_candidate(frame, phase="test")
    assert candidate is None
    assert evidence.failure_class == "OCR_MULTIPLE_MATCHES"
    assert evidence.safe_candidate_count == 2


def test_same_text_outside_terminal_region_does_not_compete():
    frame = Frame([
        item("访问城市", 1172, 486),
        item("作战终端", 1191, 410),
        item("作战终端", 700, 200),
    ])
    candidate, evidence = resolve_action_terminal_candidate(frame, phase="test")
    assert candidate is not None
    assert evidence.deduplicated_candidate_count == 2
    assert evidence.region_filtered_candidate_count == 1


def test_high_iou_duplicate_boxes_collapse_to_one_candidate():
    frame = Frame([
        item("访问城市", 1172, 486),
        item("作战终端", 1190, 408),
        item("作战终端", 1192, 410),
    ])
    candidate, evidence = resolve_action_terminal_candidate(frame, phase="test")
    assert candidate is not None
    assert evidence.exact_match_count == 2
    assert evidence.deduplicated_candidate_count == 1
    assert "duplicate_bbox_collapsed" in evidence.rejected_candidate_reasons


def test_adjacent_same_line_fragments_merge_exactly():
    frame = Frame([
        item("访问城市", 1172, 486),
        item("作战", 1160, 410, 40),
        item("终端", 1205, 410, 40),
    ])
    candidate, evidence = resolve_action_terminal_candidate(frame, phase="test")
    assert candidate is not None
    assert candidate.candidate_type == "merged_ocr_fragments"
    assert evidence.fragment_match_count == 2
    assert evidence.merged_candidate_count == 1
    assert len(candidate.evidence_ids) == 2


def test_fragments_in_different_regions_do_not_merge():
    frame = Frame([item("访问城市", 1172, 486), item("作战", 1160, 410), item("终端", 700, 200)])
    candidate, evidence = resolve_action_terminal_candidate(frame, phase="test")
    assert candidate is None
    assert evidence.failure_class == "OCR_FRAGMENTED"


def test_intervening_text_blocks_fragment_merge():
    frame = Frame([
        item("访问城市", 1172, 486),
        item("作战", 1150, 410, 40), item("其他", 1185, 410, 20),
        item("终端", 1210, 410, 40),
    ])
    candidate, evidence = resolve_action_terminal_candidate(frame, phase="test")
    assert candidate is None
    assert "fragment_crosses_other_item" in evidence.rejected_candidate_reasons


def test_reversed_fragment_order_is_blocked():
    frame = Frame([item("访问城市", 1172, 486), item("终端", 1160, 410), item("作战", 1205, 410)])
    candidate, evidence = resolve_action_terminal_candidate(frame, phase="test")
    assert candidate is None
    assert evidence.failure_class == "OCR_FRAGMENTED"


def test_nonexact_fragment_text_is_not_widened_into_match():
    frame = Frame([item("访问城市", 1172, 486), item("作站", 1160, 410), item("终端", 1205, 410)])
    candidate, evidence = resolve_action_terminal_candidate(frame, phase="test")
    assert candidate is None
    assert evidence.failure_class in {"OCR_NO_MATCH", "OCR_FRAGMENTED"}


def test_multiple_mergeable_fragment_groups_are_blocked():
    frame = Frame([
        item("访问城市", 1172, 486),
        item("作战", 1115, 395, 30), item("终端", 1150, 395, 30),
        item("作战", 1190, 430, 30), item("终端", 1225, 430, 30),
    ])
    candidate, evidence = resolve_action_terminal_candidate(frame, phase="test")
    assert candidate is None
    assert evidence.failure_class == "OCR_MULTIPLE_MATCHES"


def test_out_of_bounds_bbox_is_blocked():
    frame = home()
    frame._labels[1]["position"] = [[1180, 390], [1300, 390], [1300, 425], [1180, 425]]
    candidate, evidence = resolve_action_terminal_candidate(frame, phase="test")
    assert candidate is None
    assert evidence.failure_class == "BBOX_INVALID"


def test_abnormal_bbox_area_is_blocked():
    frame = Frame([item("访问城市", 1172, 486), item("作战终端", 1190, 410, 2, 2)])
    candidate, evidence = resolve_action_terminal_candidate(frame, phase="test")
    assert candidate is None
    assert evidence.failure_class == "BBOX_INVALID"


def test_center_overlapping_other_ocr_is_blocked():
    frame = Frame([
        item("访问城市", 1172, 486),
        item("作战终端", 1190, 410),
        item("其他按钮", 1190, 410, 140, 50),
    ])
    candidate, evidence = resolve_action_terminal_candidate(frame, phase="test")
    assert candidate is None
    assert evidence.failure_class == "SAFE_POINT_REJECTED"


def test_quest_copy_containing_target_does_not_create_second_candidate():
    frame = Frame([
        item("访问城市", 1172, 486),
        item("前往作战终端", 1097, 285, 112, 27),
        item("作战终端", 1190, 408, 83, 28),
    ])
    candidate, evidence = resolve_action_terminal_candidate(frame, phase="test")
    assert candidate is not None
    assert evidence.exact_match_count == 1
    assert evidence.safe_candidate_count == 1


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


def test_fresh_terminal_bbox_jitter_preserves_semantic_identity():
    jittered = Frame([item("访问城市", 1172, 486), item("作战终端", 1188, 407)])
    result, taps, _ = run([
        home("1"), jittered, overview("3"), overview("4"),
        entry("5"), entry("6"), summary("7"),
    ])
    assert result.success
    assert taps[0][0] != (1188, 407)


def test_fresh_terminal_disappearance_has_zero_input():
    missing = Frame([item("访问城市", 1172, 486)])
    result, taps, evidence = run([home("1"), missing])
    assert not result.success
    assert result.reason == "action_terminal_fresh_confirmation_failed"
    assert taps == evidence == []


def test_fresh_visual_parent_disappearance_has_zero_input():
    fresh = Frame(
        [item("访问城市", 1172, 486), item("作战终端", 1191, 410)],
        capture_id="2", draw_terminal_visual=False,
    )
    result, taps, evidence = run([home("1"), fresh])

    assert not result.success
    assert result.reason == "action_terminal_fresh_confirmation_failed"
    assert taps == evidence == []


def test_fresh_target_occlusion_has_zero_input():
    fresh = home("2")
    fresh._labels.append(item("资讯", 1115, 410, 45, 45))
    result, taps, evidence = run([home("1"), fresh])

    assert not result.success
    assert result.reason == "action_terminal_target_occluded"
    assert taps == evidence == []


def test_fresh_terminal_becoming_multiple_has_zero_input():
    result, taps, evidence = run([home("1"), home("2", duplicate=True)])
    assert not result.success
    assert result.reason == "action_terminal_fresh_confirmation_failed"
    assert taps == evidence == []


def test_fresh_state_change_has_zero_input():
    result, taps, evidence = run([home("1"), overview("2")])
    assert not result.success
    assert result.reason == "fresh_confirmation_state_changed"
    assert taps == evidence == []


def test_fresh_semantic_identity_large_position_change_has_zero_input():
    initial = Frame([item("访问城市", 1172, 486), item("作战终端", 1220, 410, 60)])
    fresh = Frame([item("访问城市", 1172, 486), item("作战终端", 1110, 410, 60)])
    result, taps, evidence = run([initial, fresh])
    assert not result.success
    assert result.reason == "action_terminal_fresh_confirmation_failed"
    assert taps == evidence == []


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
               item("作战终端", scaled(1191, scale_x), scaled(410, scale_y),
                    scaled(100, scale_x), scaled(24, scale_y))], size=size),
        Frame([item("访问城市", scaled(1172, scale_x), scaled(486, scale_y)),
               item("作战终端", scaled(1191, scale_x), scaled(410, scale_y),
                    scaled(100, scale_x), scaled(24, scale_y))], size=size),
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


def test_first_stage_expected_overview_stops_after_one_dispatch():
    result, taps, evidence = run_first([
        home("1"), home("2"), overview("3",),
    ])

    assert result.success
    assert result.first_stage_result == "PASS"
    assert result.state is ActionSummaryState.ACTIVITY_OVERVIEW_VISIBLE
    assert len(taps) == len(evidence) == result.dispatch_count == 1
    assert evidence[0].dispatch_command_returned is True
    assert evidence[0].touch_effect_observed is True
    assert evidence[0].post_frame_changed is True
    assert evidence[0].target_page_changed is True


def test_first_stage_stable_changed_unknown_is_precise_and_stops():
    changed = Frame([item("新页面", 640, 360)], capture_id="3", pixel=17)
    result, taps, evidence = run_first([
        home("1"), home("2"), changed,
        Frame([item("新页面", 640, 360)], capture_id="4", pixel=17),
    ])

    assert not result.success
    assert result.first_stage_result == "STABLE_CHANGED_UNKNOWN"
    assert result.reason == "action_terminal_stable_changed_unknown"
    assert len(taps) == len(evidence) == 1
    assert evidence[0].touch_effect_observed is True


def test_first_stage_three_equivalent_home_frames_are_no_touch_effect():
    result, taps, evidence = run_first([
        home("1"), home("2"), home("3"), home("4"), home("5"), home("6"),
    ])

    assert not result.success
    assert result.first_stage_result == "NO_TOUCH_EFFECT_OBSERVED"
    assert result.reason == "action_terminal_no_touch_effect_observed"
    assert len(taps) == len(evidence) == 1
    assert evidence[0].dispatch_command_returned is True
    assert evidence[0].touch_effect_observed is False
    assert evidence[0].post_frame_changed is False


def test_first_stage_known_foreign_page_is_precise():
    foreign = Frame(
        [item("道具", 1000, 100), item("材料", 1000, 200)],
        capture_id="3", pixel=23,
    )
    result, taps, evidence = run_first([home("1"), home("2"), foreign])

    assert not result.success
    assert result.first_stage_result == "KNOWN_FOREIGN_PAGE"
    assert result.reason == "action_terminal_known_foreign_page"
    assert len(taps) == len(evidence) == 1


def test_first_stage_changing_transition_times_out_without_later_dispatch():
    frames = [home("1"), home("2")]
    frames.extend(
        Frame([item("加载中", 640, 360)], capture_id=str(index), pixel=index)
        for index in range(3, 10)
    )
    result, taps, evidence = run_first(frames, timeout=0.8)

    assert not result.success
    assert result.first_stage_result == "TRANSITION_TIMEOUT"
    assert result.reason == "action_terminal_transition_timeout"
    assert len(taps) == len(evidence) == result.dispatch_count == 1
