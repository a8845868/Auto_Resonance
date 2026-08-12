from __future__ import annotations

import numpy as np
import pytest
import core.services.action_summary_navigation as action_nav

from core.services.action_summary_navigation import (
    ActionSummaryNavigator,
    ActionSummaryState,
    observe_action_summary,
    resolve_action_terminal_candidate,
    resolve_action_terminal_hit_target,
    resolve_action_summary_entry_target,
    resolve_global_prep_candidate,
    resolve_global_prep_hit_target,
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
    _capture_sequence = 0

    def __init__(self, labels, *, size=(1280, 720), capture_id="", draw_terminal_visual=True, draw_global_visual=True, draw_summary_visual=True, pixel=0):
        self.image = np.zeros((size[1], size[0], 3), dtype=np.uint8)
        if pixel:
            self.image[:] = pixel
        self._labels = labels
        if not capture_id:
            Frame._capture_sequence += 1
            capture_id = f"fixture-{Frame._capture_sequence}"
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
        if draw_global_visual:
            for label in labels:
                if str(label.get("text", "")).replace(" ", "") != "全域整备":
                    continue
                points = label["position"]
                left = int(min(point[0] for point in points))
                top = int(min(point[1] for point in points))
                right = int(max(point[0] for point in points))
                bottom = int(max(point[1] for point in points))
                height = max(4, bottom - top)
                card = (
                    max(0, left - round(0.2 * height)),
                    max(0, top - 3 * height),
                    min(size[0] - 1, right + round(6.3 * height)),
                    min(size[1] - 1, bottom + 1),
                )
                x0, y0, x1, y1 = card
                self.image[y0:y1 + 1, x0:x1 + 1] = 255
                self.image[y0 + 2:y1 - 1, x0 + 2:x1 - 1] = 40
        if draw_summary_visual:
            for label in labels:
                if str(label.get("text", "")).replace(" ", "") != "\u884c\u52a8\u6c47\u603b":
                    continue
                points = label["position"]
                left = int(min(point[0] for point in points))
                top = int(min(point[1] for point in points))
                right = int(max(point[0] for point in points))
                bottom = int(max(point[1] for point in points))
                height = max(4, bottom - top)
                card = (
                    max(0, left - 12 * height),
                    max(0, top - 3 * height),
                    min(size[0] - 1, right + 4 * height),
                    min(size[1] - 1, bottom + 4 * height),
                )
                x0, y0, x1, y1 = card
                self.image[y0:y1 + 1, x0:x1 + 1] = (240, 120, 30)
                self.image[y0 + 3:y1 - 2, x0 + 3:x1 - 2] = (130, 70, 15)

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
        [item("常规活动", 180, 150), item("全域整备", 60, 314, 58, 17)],
        size=size, capture_id=capture_id,
    )


def entry(capture_id="", size=(1280, 720)):
    sx, sy = size[0] / 1280, size[1] / 720
    return Frame([
        item("全域整备", round(820 * sx), round(92 * sy), round(120 * sx), round(32 * sy)),
        item("行动汇总", round(1060 * sx), round(380 * sy), round(100 * sx), round(24 * sy)),
    ], size=size, capture_id=capture_id)


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


def run_global(frames, *, timeout=1.0):
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
        stop_after_global_prep_stage=True,
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
    assert taps[1][0] != (60, 314)
    assert taps[2][0] != (1060, 380)
    assert evidence[2].candidate_type == "exact_ocr+parent_visual_action_card"
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


def test_unique_exact_global_prep_candidate_and_parent_card_resolve():
    frame = overview()
    candidate, resolution = resolve_global_prep_candidate(frame, phase="test")
    target, hit_evidence = resolve_global_prep_hit_target(frame, candidate, phase="test")

    assert candidate is not None
    assert resolution.exact_match_count == resolution.safe_candidate_count == 1
    assert resolution.broad_match_count == 0
    assert target is not None
    assert hit_evidence.visual_parent_count == 1
    assert target.hit_target_point != target.label_center
    assert target.parent_container_bbox[0] <= target.label_bbox[0]


def test_global_prep_description_substring_is_counted_but_never_authorizes():
    frame = overview()
    frame._labels.append(item("完成全域整备后领取奖励", 600, 200, 240, 24))
    candidate, resolution = resolve_global_prep_candidate(frame, phase="test")

    assert candidate is not None
    assert resolution.exact_match_count == 1
    assert resolution.broad_match_count == 1
    assert resolution.safe_candidate_count == 1


def test_two_independent_global_prep_exact_candidates_are_blocked():
    frame = Frame([
        item("全域整备", 60, 314, 58, 17),
        item("全域整备", 180, 314, 58, 17),
    ])
    candidate, resolution = resolve_global_prep_candidate(frame, phase="test")

    assert candidate is None
    assert resolution.failure_class == "OCR_MULTIPLE_MATCHES"


def test_global_prep_high_iou_duplicate_collapses():
    frame = Frame([
        item("全域整备", 60, 314, 58, 17),
        item("全域整备", 61, 314, 58, 17),
    ])
    candidate, resolution = resolve_global_prep_candidate(frame, phase="test")

    assert candidate is not None
    assert resolution.deduplicated_candidate_count == 1
    assert "duplicate_bbox_collapsed" in resolution.rejected_reasons


def test_strict_global_prep_fragments_merge():
    frame = Frame([
        item("全域", 45, 314, 28, 17), item("整备", 77, 314, 28, 17),
    ], draw_global_visual=False)
    # The visual parent remains independent of semantic fragment construction.
    frame.image[255:324, 28:197] = 255
    frame.image[257:322, 30:195] = 40
    candidate, resolution = resolve_global_prep_candidate(frame, phase="test")

    assert candidate is not None
    assert candidate.candidate_type == "merged_ocr_fragments"
    assert resolution.fragment_match_count == 2


def test_global_prep_same_text_outside_region_does_not_authorize():
    frame = Frame([item("全域整备", 700, 200, 58, 17)])
    candidate, resolution = resolve_global_prep_candidate(frame, phase="test")

    assert candidate is None
    assert resolution.failure_class == "REGION_FILTER_REJECTED"


def test_global_prep_missing_or_multiple_parent_blocks(monkeypatch):
    missing = Frame([item("全域整备", 60, 314, 58, 17)], draw_global_visual=False)
    candidate, _ = resolve_global_prep_candidate(missing, phase="test")
    target, evidence = resolve_global_prep_hit_target(missing, candidate, phase="test")
    assert target is None and evidence.visual_parent_count == 0

    multiple = overview()
    contours = [
        np.array([[[13, 13]], [[181, 13]], [[181, 82]], [[13, 82]]], dtype=np.int32),
        np.array([[[5, 3]], [[190, 3]], [[190, 86]], [[5, 86]]], dtype=np.int32),
    ]
    monkeypatch.setattr(action_nav.cv, "findContours", lambda *_args, **_kwargs: (contours, None))
    candidate, _ = resolve_global_prep_candidate(multiple, phase="test")
    target, evidence = resolve_global_prep_hit_target(multiple, candidate, phase="test")
    assert target is None and evidence.visual_parent_count > 1


def test_global_prep_overlay_occlusion_blocks_target():
    frame = overview()
    frame._labels.append(item("资讯", 110, 290, 80, 40))
    candidate, _ = resolve_global_prep_candidate(frame, phase="test")
    target, evidence = resolve_global_prep_hit_target(frame, candidate, phase="test")

    assert target is None
    assert evidence.target is not None and evidence.target.occlusion_detected


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
    missing = Frame([item("访问城市", 1172, 486), item("启程", 1190, 660)])
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
    foreign = Frame([
        item("道具", 1000, 100),
        item("材料", 1000, 200),
        item("装备", 1000, 300),
    ])
    result, taps, _ = run([home(), home(), foreign])
    assert not result.success
    assert result.reason == "unexpected_page"
    assert len(taps) == 1


def test_old_single_title_is_not_action_summary_success():
    observed = observe_action_summary(Frame([item("利刃围剿", 640, 90)]))
    assert observed.state is ActionSummaryState.UNKNOWN


def test_action_summary_entry_binds_unique_parent_and_uses_action_band():
    frame = entry("entry-target")

    target, evidence = resolve_action_summary_entry_target(frame, phase="test")

    assert target is not None
    assert evidence.exact_match_count == 1
    assert evidence.parent_container_count == 1
    assert target.candidate_count == 1
    assert target.semantic_id == "ACTION_SUMMARY_ENTRY"
    assert target.hit_target_point != (1060, 380)
    assert target.parent_bbox[0] <= target.hit_target_point[0] < target.parent_bbox[2]
    assert target.parent_bbox[1] <= target.hit_target_point[1] < target.parent_bbox[3]


def test_action_summary_entry_duplicate_semantic_anchor_has_zero_target():
    frame = Frame([
        item("\u884c\u52a8\u6c47\u603b", 1060, 380),
        item("\u884c\u52a8\u6c47\u603b", 900, 500),
    ])

    target, evidence = resolve_action_summary_entry_target(frame, phase="test")

    assert target is None
    assert evidence.failure_class == "SEMANTIC_ANCHOR_NOT_UNIQUE"


def test_full_navigation_can_start_at_global_prep_with_one_dispatch():
    result, taps, evidence = run([
        entry("entry-1"), entry("entry-2"), summary("summary-1"),
    ])

    assert result.success
    assert result.state is ActionSummaryState.ACTION_SUMMARY_VISIBLE
    assert result.dispatch_count == 1
    assert len(taps) == len(evidence) == 1
    assert taps[0][0] != (1060, 380)
    assert evidence[0].post_frame_changed is True
    assert evidence[0].target_page_changed is True
    assert evidence[0].touch_effect_observed is True


def test_full_navigation_can_start_at_activity_overview_with_two_dispatches():
    result, taps, evidence = run([
        overview("overview-1"), overview("overview-2"),
        entry("entry-1"), entry("entry-2"), summary("summary-1"),
    ])

    assert result.success
    assert result.state is ActionSummaryState.ACTION_SUMMARY_VISIBLE
    assert result.dispatch_count == 2
    assert len(taps) == len(evidence) == 2


def test_already_visible_is_zero_input_success():
    result, taps, evidence = run([summary("summary-visible")])

    assert result.success
    assert result.reason == "already_visible"
    assert result.dispatch_count == 0
    assert taps == evidence == []


def test_real_global_prep_shape_with_header_and_left_rail_is_high_confidence():
    frame = Frame([
        item("全域整备", 832, 90, 112, 35),
        item("全域整备", 58, 290, 46, 17),
        item("行动汇总", 974, 268, 104, 33),
        item("收集装备、材料等物资", 974, 315, 220, 20),
    ], capture_id="real-shape")

    observed = observe_action_summary(frame)
    state = observed.to_ui_state()

    assert observed.state is ActionSummaryState.ACTION_SUMMARY_ENTRY_VISIBLE
    assert state.base_page == "GLOBAL_PREP_PAGE"
    assert state.confidence.value == "HIGH"
    assert state.capabilities == frozenset({"OPEN_ACTION_SUMMARY"})


def test_entry_only_legacy_bridge_preserves_medium_confidence_and_denies_action():
    from core.services.runtime_navigation_kernel import PROVEN_NAVIGATION_CONTRACTS

    frame = Frame([
        item("行动汇总", 1060, 380),
    ], capture_id="entry-only")
    observed = observe_action_summary(frame)
    state = observed.to_ui_state()

    assert observed.state is ActionSummaryState.ACTION_SUMMARY_ENTRY_VISIBLE
    assert state.base_page == "GLOBAL_PREP_PAGE"
    assert state.confidence.value == "MEDIUM"
    assert not state.has_capability(
        "OPEN_ACTION_SUMMARY",
        current_capture_id="entry-only",
        current_frame_hash=state.frame_hash,
    )
    assert PROVEN_NAVIGATION_CONTRACTS["OPEN_ACTION_SUMMARY"].authorize(
        state,
        current_capture_id="entry-only",
        current_frame_hash=state.frame_hash,
    ).reason == "capability_confidence_not_high"


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
               item("全域整备", scaled(60, scale_x), scaled(314, scale_y),
                    scaled(58, scale_x), scaled(17, scale_y))], size=size),
        Frame([item("常规活动", scaled(180, scale_x), scaled(150, scale_y)),
               item("全域整备", scaled(60, scale_x), scaled(314, scale_y),
                    scaled(58, scale_x), scaled(17, scale_y))], size=size),
        Frame([
            item("全域整备", scaled(820, scale_x), scaled(92, scale_y), scaled(120, scale_x), scaled(32, scale_y)),
            item("行动汇总", scaled(1060, scale_x), scaled(380, scale_y), scaled(100, scale_x), scaled(24, scale_y)),
        ], size=size),
        Frame([
            item("全域整备", scaled(820, scale_x), scaled(92, scale_y), scaled(120, scale_x), scaled(32, scale_y)),
            item("行动汇总", scaled(1060, scale_x), scaled(380, scale_y), scaled(100, scale_x), scaled(24, scale_y)),
        ], size=size),
        summary(size=size),
    ]
    result, _, evidence = run(frames)
    assert result.success
    assert all(attempt.coordinate_chain.complete for attempt in evidence)


def test_optional_overlay_uses_computed_safe_blank_not_legacy_fixed_point():
    overlay = lambda: Frame([
        item("首次进入说明", 640, 180, 400, 80),
        item("触碰空白区域退出", 640, 680, 180, 24),
    ])
    result, taps, _ = run([
        home(), home(), overview(), overview(), overlay(), overlay(), entry(), entry(), summary(),
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
        [
            item("道具", 1000, 100),
            item("材料", 1000, 200),
            item("装备", 1000, 300),
        ],
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


def test_global_prep_expected_entry_passes_and_stops_after_one_dispatch():
    result, taps, evidence = run_global([
        overview("1"), overview("2"), entry("3"),
    ])

    assert result.success
    assert result.global_prep_stage_result == "PASS"
    assert result.state is ActionSummaryState.ACTION_SUMMARY_ENTRY_VISIBLE
    assert len(taps) == len(evidence) == result.dispatch_count == 1
    assert taps[0][0] != (60, 314)
    assert taps[0][1]["random_offset"] is False
    assert evidence[0].dispatch_command_returned is True
    assert evidence[0].touch_effect_observed is True


def test_global_prep_probe_on_home_blocks_without_replaying_first_stage():
    result, taps, evidence = run_global([home("1")])

    assert not result.success
    assert result.reason == "global_prep_precondition_failed"
    assert result.dispatch_count == 0
    assert taps == evidence == []


def test_global_prep_optional_overlay_is_reported_without_dismissal():
    overlay = Frame([
        item("首次进入说明", 640, 180, 400, 80),
        item("触碰空白区域退出", 640, 680, 180, 24),
    ], capture_id="3", pixel=19)
    result, taps, evidence = run_global([overview("1"), overview("2"), overlay])

    assert result.success
    assert result.global_prep_stage_result == "OPTIONAL_OVERLAY_REACHED"
    assert result.state is ActionSummaryState.OPTIONAL_OVERLAY_VISIBLE
    assert len(taps) == len(evidence) == 1


def test_overlay_is_orthogonal_to_visible_activity_background():
    covered = Frame([
        item("全域整备", 60, 314, 58, 17),
        item("资讯", 640, 90),
        item("触碰空白区域退出", 640, 680, 180, 24),
    ], capture_id="covered", pixel=21)

    observed = observe_action_summary(covered)

    assert observed.state is ActionSummaryState.OPTIONAL_OVERLAY_VISIBLE


def test_global_prep_direct_action_summary_is_reported_and_stops():
    result, taps, evidence = run_global([
        overview("1"), overview("2"), summary("3"),
    ])

    assert result.success
    assert result.global_prep_stage_result == "ACTION_SUMMARY_REACHED"
    assert result.state is ActionSummaryState.ACTION_SUMMARY_VISIBLE
    assert len(taps) == len(evidence) == 1


def test_global_prep_stable_changed_unknown_is_precise():
    changed = Frame([item("新页面", 640, 360)], capture_id="3", pixel=31)
    result, taps, evidence = run_global([
        overview("1"), overview("2"), changed,
        Frame([item("新页面", 640, 360)], capture_id="4", pixel=31),
    ])

    assert not result.success
    assert result.global_prep_stage_result == "STABLE_CHANGED_UNKNOWN"
    assert result.reason == "global_prep_stable_changed_unknown"
    assert len(taps) == len(evidence) == 1


def test_global_prep_no_visual_effect_is_precise():
    result, taps, evidence = run_global([
        overview("1"), overview("2"), overview("3"), overview("4"), overview("5"),
    ])

    assert not result.success
    assert result.global_prep_stage_result == "NO_TOUCH_EFFECT"
    assert result.reason == "global_prep_no_touch_effect_observed"
    assert len(taps) == len(evidence) == 1
    assert evidence[0].touch_effect_observed is False


def test_global_prep_known_foreign_page_is_precise():
    foreign = Frame(
        [
            item("道具", 1000, 100),
            item("材料", 1000, 200),
            item("装备", 1000, 300),
        ],
        capture_id="3", pixel=41,
    )
    result, taps, evidence = run_global([overview("1"), overview("2"), foreign])

    assert not result.success
    assert result.reason == "global_prep_known_foreign_page"
    assert len(taps) == len(evidence) == 1


def test_global_prep_page_description_material_does_not_override_specific_page():
    real_page_shape = Frame([
        item("全域整备", 820, 92, 120, 32),
        item("行动汇总", 980, 270, 120, 32),
        item("满足物流需求，由此收集装备、材料等物资", 940, 330, 360, 28),
    ], capture_id="3", pixel=43)

    observed = observe_action_summary(real_page_shape)

    assert observed.state is ActionSummaryState.ACTION_SUMMARY_ENTRY_VISIBLE
    assert "action_summary_entry_unique" in observed.positive_cues


def test_material_word_alone_is_unknown_not_foreign():
    observed = observe_action_summary(
        Frame([item("材料", 640, 360)], capture_id="material-only")
    )

    assert observed.state is ActionSummaryState.UNKNOWN


def test_action_summary_description_substring_is_not_a_page_signature():
    observed = observe_action_summary(
        Frame([item("行动汇总说明", 640, 360)], capture_id="description-only")
    )

    assert observed.state is ActionSummaryState.UNKNOWN


def test_global_prep_live_page_shape_passes_single_stage_without_retry():
    real_page_shape = Frame([
        item("全域整备", 820, 92, 120, 32),
        item("行动汇总", 980, 270, 120, 32),
        item("收集装备、材料等物资", 940, 330, 260, 28),
    ], capture_id="3", pixel=45)
    result, taps, evidence = run_global([
        overview("1"), overview("2"), real_page_shape,
    ])

    assert result.success
    assert result.global_prep_stage_result == "PASS"
    assert result.state is ActionSummaryState.ACTION_SUMMARY_ENTRY_VISIBLE
    assert len(taps) == len(evidence) == result.dispatch_count == 1


def test_global_prep_fresh_candidate_disappearance_has_zero_input():
    missing = Frame([item("常规活动", 180, 150)], capture_id="2")
    # Preserve the proven overview state independently while removing the target.
    missing._labels.append(item("全域整备说明", 100, 300, 120, 17))
    result, taps, evidence = run_global([overview("1"), missing])

    assert not result.success
    assert taps == evidence == []


@pytest.mark.parametrize("size", [(1280, 720), (851, 480), (853, 480)])
def test_global_prep_coordinate_mapping_is_normalized(size):
    sx, sy = size[0] / 1280, size[1] / 720
    make = lambda capture: Frame([
        item("常规活动", round(180 * sx), round(150 * sy)),
        item("全域整备", round(60 * sx), round(314 * sy),
             round(58 * sx), round(17 * sy)),
    ], size=size, capture_id=capture)
    result, taps, evidence = run_global([make("1"), make("2"), entry("3", size=size)])

    assert result.success
    assert len(taps) == len(evidence) == 1
    normalized = evidence[0].coordinate_chain.normalized_point
    assert normalized[0] == pytest.approx(0.0875, abs=0.02)
    assert normalized[1] == pytest.approx(0.403, abs=0.03)
