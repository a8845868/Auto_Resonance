import hashlib
import json
from types import SimpleNamespace

import cv2 as cv
import numpy as np
import pytest

import core.services.home_backpack_cube as backpack
from core.services.home_backpack_cube import (
    HomeAssetsBalanceDisplay,
    HomeBackpackCubeCandidate,
    backpack_cube_template,
    confirm_fresh_home_backpack_cube,
    find_home_backpack_cube_candidates,
    resolve_home_backpack_cube_control,
)


def _frame(capture_id="capture-1", *, width=1280, height=720, pixel=0):
    return SimpleNamespace(
        image=np.full((height, width, 3), pixel, dtype=np.uint8),
        source_capture_id=capture_id,
    )


def _candidate(
    center=(1101, 48),
    *,
    size=(44, 44),
    score=0.98,
):
    x, y = center
    width, height = size
    return HomeBackpackCubeCandidate(
        point=center,
        bbox=(x - width // 2, y - height // 2, x + width // 2, y + height // 2),
        score=score,
    )


def _resolved(capture_id="capture-1", *, candidate=None):
    frame = _frame(capture_id)
    value = candidate or _candidate()
    return resolve_home_backpack_cube_control(
        frame,
        candidate_resolver=lambda _image: [value],
    )


def _synthetic_cube_frame(*, width=1280, height=720, left=None, top=15):
    frame = _frame(width=width, height=height)
    template = backpack_cube_template().pixels
    if left is None:
        left = int(round(width * 0.85))
    glyph = cv.cvtColor(template, cv.COLOR_GRAY2BGR)
    frame.image[top:top + glyph.shape[0], left:left + glyph.shape[1]] = glyph
    return frame


def test_assets_label_is_iron_coin_balance_display_only():
    semantics = HomeAssetsBalanceDisplay()
    assert semantics.semantic_id == "iron_coin_balance_display"
    assert semantics.currency_id == "iron_coin"
    assert semantics.can_open_inventory is False


def test_template_source_payload_is_hash_bound():
    document = json.loads(backpack._TEMPLATE_PATH.read_text(encoding="utf-8"))
    source = backpack._TEMPLATE_PATH.with_name(document["source_filename"])
    assert hashlib.sha256(source.read_bytes()).hexdigest() == document["source_payload_sha256"]


def test_template_normalized_pixels_are_hash_bound():
    document = json.loads(backpack._TEMPLATE_PATH.read_text(encoding="utf-8"))
    template = backpack_cube_template()
    assert hashlib.sha256(template.pixels.tobytes()).hexdigest() == document["normalized_sha256"]


def test_template_is_privacy_minimized():
    document = json.loads(backpack._TEMPLATE_PATH.read_text(encoding="utf-8"))
    assert document["privacy"] == "MINIMAL_UI_ICON_CROP_NO_ACCOUNT_DATA"
    assert document["source_dimensions"] == [93, 90]
    assert document["normalized_dimensions"] == [40, 46]


def test_template_contract_rejects_source_hash_mismatch(tmp_path, monkeypatch):
    original = json.loads(backpack._TEMPLATE_PATH.read_text(encoding="utf-8"))
    source = backpack._TEMPLATE_PATH.with_name(original["source_filename"])
    (tmp_path / source.name).write_bytes(source.read_bytes())
    original["source_payload_sha256"] = "0" * 64
    metadata = tmp_path / backpack._TEMPLATE_PATH.name
    metadata.write_text(json.dumps(original), encoding="utf-8")
    monkeypatch.setattr(backpack, "_TEMPLATE_PATH", metadata)
    backpack.backpack_cube_template.cache_clear()
    with pytest.raises(ValueError, match="source_hash_mismatch"):
        backpack.backpack_cube_template()
    backpack.backpack_cube_template.cache_clear()


def test_synthetic_cube_produces_one_candidate():
    candidates = find_home_backpack_cube_candidates(_synthetic_cube_frame().image)
    assert len(candidates) == 1
    assert candidates[0].score >= backpack_cube_template().minimum_score


def test_search_roi_is_local_to_top_right():
    ratios = backpack_cube_template().roi_ratios
    assert ratios[0] >= 0.75
    assert ratios[1] == 0.0
    assert ratios[2] <= 1.0
    assert ratios[3] <= 0.2
    assert (ratios[2] - ratios[0]) * (ratios[3] - ratios[1]) <= 0.05


def test_same_glyph_outside_top_right_roi_is_not_a_candidate():
    frame = _synthetic_cube_frame(left=200, top=300)
    assert find_home_backpack_cube_candidates(frame.image) == []


def test_unique_cube_resolves():
    observed = _resolved()
    assert observed.resolved
    assert observed.semantic_id == "home_backpack_cube_control"
    assert observed.candidate_count == 1


def test_missing_cube_is_fail_closed():
    observed = resolve_home_backpack_cube_control(
        _frame(), candidate_resolver=lambda _image: [],
    )
    assert not observed.resolved
    assert observed.reason_codes == ("backpack_cube_missing",)


def test_multiple_cubes_are_ambiguous():
    observed = resolve_home_backpack_cube_control(
        _frame(), candidate_resolver=lambda _image: [_candidate(), _candidate((1220, 48))],
    )
    assert not observed.resolved
    assert observed.candidate_count == 2
    assert observed.reason_codes == ("backpack_cube_ambiguous",)


def test_invalid_frame_pixels_are_rejected():
    frame = SimpleNamespace(image=None, source_capture_id="capture-1")
    observed = resolve_home_backpack_cube_control(frame)
    assert not observed.resolved
    assert observed.reason_codes == ("frame_pixels_invalid",)


def test_missing_capture_id_is_rejected():
    observed = resolve_home_backpack_cube_control(
        _frame(""), candidate_resolver=lambda _image: [_candidate()],
    )
    assert not observed.resolved
    assert observed.reason_codes == ("capture_provenance_missing",)


def test_below_threshold_candidate_is_rejected():
    observed = resolve_home_backpack_cube_control(
        _frame(), candidate_resolver=lambda _image: [_candidate(score=0.85)],
    )
    assert not observed.resolved
    assert observed.candidate_count == 0


def test_candidate_outside_render_client_is_rejected():
    invalid = HomeBackpackCubeCandidate((1275, 48), (1250, 20, 1300, 75), 0.99)
    observed = resolve_home_backpack_cube_control(
        _frame(), candidate_resolver=lambda _image: [invalid],
    )
    assert not observed.resolved


def test_toolbar_sized_candidate_is_never_accepted_as_cube():
    toolbar = HomeBackpackCubeCandidate((1138, 94), (997, 17, 1280, 172), 0.99)
    observed = resolve_home_backpack_cube_control(
        _frame(), candidate_resolver=lambda _image: [toolbar],
    )
    assert not observed.resolved
    assert observed.safe_hit_point is None


def test_safe_hit_bbox_is_strictly_inside_dynamic_cube():
    observed = _resolved()
    cube = observed.cube_bbox
    safe = observed.safe_hit_bbox
    assert cube is not None and safe is not None
    assert cube[0] < safe[0] < safe[2] < cube[2]
    assert cube[1] < safe[1] < safe[3] < cube[3]


def test_safe_hit_point_is_inside_safe_bbox_and_cube():
    observed = _resolved()
    point = observed.safe_hit_point
    safe = observed.safe_hit_bbox
    cube = observed.cube_bbox
    assert point is not None and safe is not None and cube is not None
    assert safe[0] <= point[0] < safe[2] and safe[1] <= point[1] < safe[3]
    assert cube[0] <= point[0] < cube[2] and cube[1] <= point[1] < cube[3]


def test_safe_hit_point_is_derived_from_dynamic_bbox():
    observed = _resolved(candidate=_candidate((1092, 44)))
    assert observed.safe_hit_point == (1092, 44)
    assert observed.safe_hit_point != (1101, 48)


def test_forbidden_legacy_point_is_never_authorized():
    observed = _resolved(candidate=_candidate((1138, 94), size=(72, 86)))
    assert not observed.resolved
    assert observed.safe_hit_point is None


def test_known_live_cube_bbox_yields_calibration_center_only_dynamically():
    live = HomeBackpackCubeCandidate((1101, 48), (1065, 5, 1137, 92), 0.974025)
    observed = _resolved(candidate=live)
    assert observed.resolved
    assert observed.safe_hit_point == (1101, 48)
    assert observed.cube_bbox == live.bbox


def test_two_fresh_stable_frames_confirm():
    initial = _resolved("capture-1")
    fresh = _resolved("capture-2")
    assert confirm_fresh_home_backpack_cube(initial, fresh) is fresh


def test_final_target_is_bound_to_second_fresh_frame():
    initial = _resolved("capture-1", candidate=_candidate((1100, 47)))
    fresh = _resolved("capture-2", candidate=_candidate((1103, 50)))
    confirmed = confirm_fresh_home_backpack_cube(initial, fresh)
    assert confirmed is fresh
    assert confirmed.source_capture_id == "capture-2"
    assert confirmed.safe_hit_point == (1103, 50)


def test_reused_capture_id_does_not_count_as_fresh():
    initial = _resolved("capture-1")
    duplicate = _resolved("capture-1")
    assert confirm_fresh_home_backpack_cube(initial, duplicate) is None


def test_center_drift_over_limit_blocks_confirmation():
    initial = _resolved("capture-1")
    fresh = _resolved("capture-2", candidate=_candidate((1110, 48)))
    assert confirm_fresh_home_backpack_cube(initial, fresh) is None


def test_size_drift_over_limit_blocks_confirmation():
    initial = _resolved("capture-1", candidate=_candidate(size=(44, 44)))
    fresh = _resolved("capture-2", candidate=_candidate(size=(54, 44)))
    assert confirm_fresh_home_backpack_cube(initial, fresh) is None


def test_capture_dimension_change_blocks_confirmation():
    initial = _resolved("capture-1")
    frame = _frame("capture-2", width=851, height=480)
    center = (int(851 * 0.86), int(480 * 0.07))
    fresh = resolve_home_backpack_cube_control(
        frame,
        candidate_resolver=lambda _image: [_candidate(center, size=(36, 36))],
    )
    assert fresh.resolved
    assert confirm_fresh_home_backpack_cube(initial, fresh) is None


def test_unresolved_initial_blocks_confirmation():
    initial = resolve_home_backpack_cube_control(
        _frame("capture-1"), candidate_resolver=lambda _image: [],
    )
    assert confirm_fresh_home_backpack_cube(initial, _resolved("capture-2")) is None


def test_unresolved_fresh_blocks_confirmation():
    fresh = resolve_home_backpack_cube_control(
        _frame("capture-2"), candidate_resolver=lambda _image: [],
    )
    assert confirm_fresh_home_backpack_cube(_resolved("capture-1"), fresh) is None


def test_actual_capture_dimensions_drive_roi_and_target():
    width, height = 851, 480
    center = (int(width * 0.86), int(height * 0.07))
    observed = resolve_home_backpack_cube_control(
        _frame(width=width, height=height),
        candidate_resolver=lambda _image: [_candidate(center, size=(36, 36))],
    )
    assert observed.resolved
    assert observed.safe_hit_point == center


def test_control_diagnostic_contains_no_raw_pixels_or_ocr():
    document = repr(_resolved())
    assert "image=" not in document
    assert "ocr" not in document.lower()
    assert "资产" not in document
