from dataclasses import replace
from types import SimpleNamespace

import cv2 as cv
import numpy as np

from core.services.navigation_parent_control import (
    confirm_fresh_parent_control,
    resolve_navigation_parent_control,
)


def _frame_control(anchor=(90, 45, 150, 65)):
    image = np.zeros((160, 260, 3), dtype=np.uint8)
    cv.rectangle(image, (50, 25), (220, 95), (255, 255, 255), 3)
    cv.putText(image, "ENTRY", (anchor[0], anchor[3]), cv.FONT_HERSHEY_SIMPLEX, 0.5, (220, 220, 220), 1)
    return image


def _semantic_slot_frame(
    *,
    width=320,
    height=180,
    parent=(70, 60, 320, 130),
    anchor=(150, 82, 215, 105),
    chevrons=((270, 93),),
    icons=(),
):
    image = np.zeros((height, width, 3), dtype=np.uint8)
    x0, y0, x1, y1 = parent
    image[y0:y1, x0:x1] = 235
    cv.line(image, (x0, y0), (x1 - 1, y0), (20, 20, 20), 2)
    cv.line(image, (x0, y1 - 1), (x1 - 1, y1 - 1), (20, 20, 20), 2)
    cv.putText(
        image, "ENTRY", (anchor[0], anchor[3] - 2),
        cv.FONT_HERSHEY_SIMPLEX, 0.48, (30, 30, 30), 1,
    )
    for center_x, center_y in chevrons:
        cv.line(image, (center_x - 11, center_y), (center_x + 7, center_y), (10, 10, 10), 3)
        cv.line(image, (center_x + 7, center_y), (center_x - 2, center_y - 8), (10, 10, 10), 3)
        cv.line(image, (center_x + 7, center_y), (center_x - 2, center_y + 8), (10, 10, 10), 3)
    for center_x, center_y in icons:
        cv.circle(image, (center_x, center_y), 11, (15, 15, 15), 3)
    return image, anchor


def _resolve_slot(image, anchor, capture_id="capture-1"):
    return resolve_navigation_parent_control(
        image,
        semantic_id="visit_city",
        anchor_bbox=anchor,
        source_capture_id=capture_id,
        source_frame_sha256=(capture_id[-1] if capture_id[-1].isdigit() else "a") * 64,
    )


def test_city_semantic_anchor_binds_unique_visual_parent_and_fresh_frame():
    first = resolve_navigation_parent_control(
        _frame_control(), semantic_id="visit_city", anchor_bbox=(90, 45, 150, 65),
        source_capture_id="capture-1", source_frame_sha256="a" * 64,
    )
    fresh = resolve_navigation_parent_control(
        _frame_control(), semantic_id="visit_city", anchor_bbox=(90, 45, 150, 65),
        source_capture_id="capture-2", source_frame_sha256="a" * 64,
    )
    confirmed = confirm_fresh_parent_control(first, fresh)
    assert confirmed is fresh
    assert confirmed.resolved
    assert confirmed.parent_control_bbox != confirmed.anchor_bbox
    assert confirmed.safe_hit_point != ((90 + 150) // 2, (45 + 65) // 2)


def test_text_without_visual_parent_is_rejected():
    image = np.zeros((160, 260, 3), dtype=np.uint8)
    observed = resolve_navigation_parent_control(
        image, semantic_id="visit_city", anchor_bbox=(90, 45, 150, 65),
        source_capture_id="capture-1", source_frame_sha256="a" * 64,
    )
    assert observed.resolved is False
    assert observed.safe_hit_point is None


def test_inventory_anchor_uses_same_parent_contract():
    observed = resolve_navigation_parent_control(
        _frame_control((170, 45, 190, 65)), semantic_id="open_inventory",
        anchor_bbox=(170, 45, 190, 65), source_capture_id="capture-3",
        source_frame_sha256="b" * 64,
    )
    assert observed.resolved
    assert observed.semantic_id == "open_inventory"


def test_unoutlined_anchor_chevron_and_row_resolve_semantic_control_slot():
    image, anchor = _semantic_slot_frame()
    observed = _resolve_slot(image, anchor)
    assert observed.resolved
    assert observed.parent_detection_method == "SEMANTIC_CONTROL_SLOT"
    assert observed.associated_chevron_bbox is not None
    assert observed.associated_icon_bbox is None


def test_unoutlined_anchor_icon_and_row_resolve_without_chevron():
    image, anchor = _semantic_slot_frame(chevrons=(), icons=((110, 94),))
    observed = _resolve_slot(image, anchor)
    assert observed.resolved
    assert observed.parent_detection_method == "SEMANTIC_CONTROL_SLOT"
    assert observed.associated_icon_bbox is not None
    assert observed.associated_chevron_bbox is None


def test_only_ocr_anchor_without_independent_geometry_is_blocked():
    image = np.zeros((180, 320, 3), dtype=np.uint8)
    anchor = (150, 82, 215, 105)
    cv.putText(image, "ENTRY", (150, 103), cv.FONT_HERSHEY_SIMPLEX, 0.48, (235, 235, 235), 1)
    observed = _resolve_slot(image, anchor)
    assert observed.resolved is False
    assert observed.parent_control_bbox is None


def test_ocr_bbox_padding_never_creates_parent_authority():
    image = np.zeros((180, 320, 3), dtype=np.uint8)
    anchor = (150, 82, 215, 105)
    observed = _resolve_slot(image, anchor)
    assert observed.resolved is False
    assert observed.parent_control_bbox != (130, 62, 235, 125)
    assert "OCR" not in observed.parent_detection_method


def test_two_associated_chevrons_are_ambiguous_and_blocked():
    image, anchor = _semantic_slot_frame(chevrons=((230, 82), (265, 108)))
    observed = _resolve_slot(image, anchor)
    assert observed.resolved is False
    assert "ambiguous" in observed.reason_codes[0]


def test_two_associated_icons_without_chevron_are_ambiguous_and_blocked():
    image, anchor = _semantic_slot_frame(chevrons=(), icons=((100, 84), (125, 108)))
    observed = _resolve_slot(image, anchor)
    assert observed.resolved is False
    assert "ambiguous" in observed.reason_codes[0]


def test_two_action_glyphs_do_not_select_first_or_nearest_candidate():
    image, anchor = _semantic_slot_frame(chevrons=((230, 94), (265, 94)))
    observed = _resolve_slot(image, anchor)
    assert observed.resolved is False
    assert observed.candidate_count >= 2


def test_overlapping_adjacent_parent_contours_are_ambiguous_and_blocked():
    image = np.zeros((180, 320, 3), dtype=np.uint8)
    anchor = (150, 82, 200, 104)
    cv.line(image, (70, 50), (245, 50), (255, 255, 255), 2)
    cv.line(image, (70, 50), (70, 125), (255, 255, 255), 2)
    cv.line(image, (70, 125), (245, 125), (255, 255, 255), 2)
    cv.line(image, (105, 58), (285, 58), (255, 255, 255), 2)
    cv.line(image, (285, 58), (285, 135), (255, 255, 255), 2)
    cv.line(image, (105, 135), (285, 135), (255, 255, 255), 2)
    cv.putText(image, "ENTRY", (150, 102), cv.FONT_HERSHEY_SIMPLEX, 0.48, (220, 220, 220), 1)
    observed = _resolve_slot(image, anchor)
    assert observed.resolved is False
    assert observed.candidate_count >= 2
    assert observed.reason_codes == ("parent_control_ambiguous",)


def test_safe_hit_point_is_inside_parent_inset_and_outside_text_anchor():
    image, anchor = _semantic_slot_frame()
    observed = _resolve_slot(image, anchor)
    assert observed.parent_control_bbox and observed.safe_hit_bbox and observed.safe_hit_point
    px, py = observed.safe_hit_point
    parent = observed.parent_control_bbox
    safe = observed.safe_hit_bbox
    assert parent[0] < safe[0] <= px < safe[2] < parent[2]
    assert parent[1] < safe[1] <= py < safe[3] < parent[3]
    assert not (anchor[0] <= px < anchor[2] and anchor[1] <= py < anchor[3])


def test_safe_hit_region_is_bound_to_non_text_control_glyph():
    image, anchor = _semantic_slot_frame()
    observed = _resolve_slot(image, anchor)
    assert observed.associated_chevron_bbox is not None
    assert observed.safe_hit_point == (
        (observed.associated_chevron_bbox[0] + observed.associated_chevron_bbox[2]) // 2,
        (observed.associated_chevron_bbox[1] + observed.associated_chevron_bbox[3]) // 2,
    )


def test_two_fresh_semantic_slots_confirm_when_geometry_is_stable():
    image, anchor = _semantic_slot_frame()
    initial = _resolve_slot(image, anchor, "capture-1")
    fresh = _resolve_slot(image.copy(), anchor, "capture-2")
    assert confirm_fresh_parent_control(initial, fresh) is fresh


def test_safe_point_drift_over_eight_pixels_blocks_fresh_confirmation():
    image, anchor = _semantic_slot_frame()
    initial = _resolve_slot(image, anchor, "capture-1")
    fresh = _resolve_slot(image.copy(), anchor, "capture-2")
    assert fresh.safe_hit_point is not None
    unstable = replace(fresh, safe_hit_point=(fresh.safe_hit_point[0] + 9, fresh.safe_hit_point[1]))
    assert confirm_fresh_parent_control(initial, unstable) is None


def test_final_target_is_the_second_fresh_frame_observation():
    image, anchor = _semantic_slot_frame()
    initial = _resolve_slot(image, anchor, "capture-1")
    fresh = _resolve_slot(image.copy(), anchor, "capture-2")
    confirmed = confirm_fresh_parent_control(initial, fresh)
    assert confirmed is fresh
    assert confirmed.source_capture_id == "capture-2"


def test_historical_city_coordinate_is_never_a_fixed_fallback():
    image = np.zeros((720, 1280, 3), dtype=np.uint8)
    parent = (1007, 468, 1280, 526)
    anchor = (1129, 472, 1216, 499)
    image[parent[1]:parent[3], parent[0]:parent[2]] = 235
    cv.line(image, (parent[0], parent[1]), (parent[2] - 1, parent[1]), (20, 20, 20), 2)
    cv.line(image, (parent[0], parent[3] - 1), (parent[2] - 1, parent[3] - 1), (20, 20, 20), 2)
    cv.line(image, (1228, 495), (1250, 495), (10, 10, 10), 3)
    cv.line(image, (1250, 495), (1240, 486), (10, 10, 10), 3)
    cv.line(image, (1250, 495), (1240, 504), (10, 10, 10), 3)
    observed = _resolve_slot(image, anchor)
    assert observed.resolved
    assert observed.safe_hit_point != (1172, 486)


def test_parent_diagnostic_reports_candidates_and_privacy_safe_local_roi():
    image = np.zeros((720, 1280, 3), dtype=np.uint8)
    parent = (1007, 468, 1280, 526)
    anchor = (1129, 472, 1216, 499)
    image[parent[1]:parent[3], parent[0]:parent[2]] = 235
    cv.line(image, (parent[0], parent[1]), (parent[2] - 1, parent[1]), (20, 20, 20), 2)
    cv.line(image, (parent[0], parent[3] - 1), (parent[2] - 1, parent[3] - 1), (20, 20, 20), 2)
    cv.line(image, (1228, 495), (1250, 495), (10, 10, 10), 3)
    cv.line(image, (1250, 495), (1240, 486), (10, 10, 10), 3)
    cv.line(image, (1250, 495), (1240, 504), (10, 10, 10), 3)
    observed = _resolve_slot(image, anchor)
    diagnostic = observed.diagnostic
    assert diagnostic is not None
    assert diagnostic.visual_component_count >= 1
    assert diagnostic.accepted_parent_candidate_count == 1
    assert diagnostic.resolution_status == "PASS"
    x0, y0, x1, y1 = diagnostic.parent_search_roi
    assert ((x1 - x0) * (y1 - y0)) / (1280 * 720) <= 0.15
    assert y0 > 80
    assert y1 < 620
