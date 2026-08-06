from __future__ import annotations

import json
from types import SimpleNamespace

import numpy as np
import pytest

from core.services.navigation_evidence import (
    CoordinateChain,
    ScreenToDeviceCoordinateTransform,
    NavigationAttemptEvidence,
    frame_sha256,
    record_navigation_attempt,
)


def test_same_frame_hash_cannot_prove_touch_effect_or_page_change():
    frame = np.zeros((40, 60, 3), dtype=np.uint8)
    evidence = NavigationAttemptEvidence(
        task_name="city",
        entry_name="visit_city",
        pre_state="HOME_READY",
        pre_frame_sha256=frame_sha256(frame),
        coordinate_chain=CoordinateChain.from_capture_point(
            (5, 5),
            capture_size=(60, 40),
            render_client_size=(60, 40),
            device_size=(60, 40),
        ),
        candidate_type="parent_control",
        candidate_bbox=(1, 1, 10, 10),
        candidate_score=1.0,
        candidate_count=1,
        dispatch_backend="NEMU",
    )
    evidence.add_post_observation(
        frame=frame,
        state="CITY_DETAIL_VISIBLE",
        source_base_page="HOME_READY",
        normalized_base_page="CITY_DETAIL_VISIBLE",
        target_control_disappeared=True,
        trusted_postcondition_observed=True,
        postcondition_result="blocked",
    )
    assert evidence.post_frame_changed is False
    assert evidence.target_page_changed is False
    assert evidence.target_control_disappeared is False
    assert evidence.touch_effect_observed is False
    assert evidence.detector_nondeterminism is True
    assert evidence.evidence_invariant_check == "FAIL"


def test_nemu_stale_capture_is_distinct_from_no_effect():
    from core.services.navigation_evidence import classify_native_accepted_no_effect

    assert classify_native_accepted_no_effect(
        native_accepted=True,
        nemu_frame_changed=False,
        adb_crosscheck_available=True,
        adb_frame_changed=True,
    ) == "NEMU_CAPTURE_STALE_SUSPECTED"
    assert classify_native_accepted_no_effect(
        native_accepted=True,
        nemu_frame_changed=False,
        adb_crosscheck_available=True,
        adb_frame_changed=False,
    ) == "TOUCH_NO_EFFECT_OR_TARGET_INVALID"


def test_screen_to_device_identity_mapping():
    transform = ScreenToDeviceCoordinateTransform(
        capture_width=1280,
        capture_height=720,
        device_logical_width=1280,
        device_logical_height=720,
        device_physical_width=1280,
        device_physical_height=720,
    )

    logical, physical, error = transform.map_point((141, 619))

    assert transform.mapping_status == "IDENTITY"
    assert logical == physical == (141, 619)
    assert error == 0


def test_screen_to_device_scaled_mapping():
    transform = ScreenToDeviceCoordinateTransform(
        capture_width=1280,
        capture_height=720,
        device_logical_width=1280,
        device_logical_height=720,
        device_physical_width=2560,
        device_physical_height=1440,
    )

    logical, physical, error = transform.map_point((320, 180))

    assert transform.mapping_status == "TRANSFORMED"
    assert logical == (320, 180)
    assert physical == (640, 360)
    assert error == 0


def test_screen_to_device_viewport_offset_mapping():
    transform = ScreenToDeviceCoordinateTransform(
        capture_width=1000,
        capture_height=600,
        device_logical_width=1280,
        device_logical_height=720,
        device_physical_width=1280,
        device_physical_height=720,
        viewport_offset=(140, 60),
        viewport_size=(1000, 600),
    )

    logical, physical, error = transform.map_point((500, 300))

    assert transform.mapping_status == "TRANSFORMED"
    assert logical == physical == (640, 360)
    assert error == 0


def test_screen_to_device_rotation_mismatch_blocks():
    transform = ScreenToDeviceCoordinateTransform(
        capture_width=1280,
        capture_height=720,
        device_logical_width=1280,
        device_logical_height=720,
        device_physical_width=1280,
        device_physical_height=720,
        rotation=90,
    )

    assert transform.mapping_status == "BLOCKED"
    assert transform.reason_codes == ("rotation_mismatch",)
    with pytest.raises(PermissionError, match="rotation_mismatch"):
        transform.map_point((141, 619))


def test_screen_to_device_unexplained_letterbox_blocks():
    transform = ScreenToDeviceCoordinateTransform(
        capture_width=1280,
        capture_height=720,
        device_logical_width=1280,
        device_logical_height=800,
        device_physical_width=1280,
        device_physical_height=800,
    )

    assert transform.mapping_status == "BLOCKED"
    assert "capture_viewport_aspect_mismatch" in transform.reason_codes


def _evidence(chain: CoordinateChain) -> NavigationAttemptEvidence:
    return NavigationAttemptEvidence(
        task_name="inventory_scan",
        entry_name="assets_entry",
        pre_state="home_ready",
        pre_frame_sha256="a" * 64,
        coordinate_chain=chain,
        candidate_type="template_glyph",
        candidate_bbox=(1080, 30, 1122, 72),
        candidate_score=1.0,
        candidate_count=1,
        dispatch_backend="fake",
    )


def test_coordinate_chain_is_complete_for_device_backend_without_screen_space():
    chain = CoordinateChain.from_capture_point(
        (1101, 51),
        capture_size=(1280, 720),
        render_client_size=(851, 480),
        device_size=(851, 480),
    )

    assert chain.source_point == (1101, 51)
    assert chain.render_client_point == chain.device_point == (732, 34)
    assert (chain.device_width, chain.device_height) == (851, 480)
    assert chain.screen_point is None
    assert chain.screen_coordinate_applicable is False
    assert chain.complete is True


def test_normalized_asset_point_maps_consistently_across_supported_widths():
    mapped = []
    for width, height in ((1280, 720), (851, 480), (853, 480)):
        point = (round(width * 0.858), round(height * 0.095))
        chain = CoordinateChain.from_capture_point(
            point,
            capture_size=(width, height),
            render_client_size=(1280, 720),
            device_size=(1280, 720),
        )
        mapped.append(chain.device_point)

    assert max(point[0] for point in mapped) - min(point[0] for point in mapped) <= 1
    assert max(point[1] for point in mapped) - min(point[1] for point in mapped) <= 1


def test_attempt_id_binds_dispatch_and_post_frames_and_redacts_raw_ocr():
    chain = CoordinateChain.from_capture_point(
        (1101, 51),
        capture_size=(1280, 720),
        render_client_size=(1280, 720),
    )
    evidence = _evidence(chain)
    evidence.mark_dispatch(requested=True, acknowledged=True, result="accepted")
    evidence.actual_dispatched_point = chain.device_point
    evidence.mark_station_detection(
        result="PASS",
        station_id="七号自由港",
        confidence="HIGH",
        evidence_ids=("station_name_sha256:0123456789abcdef",),
    )
    frame = np.zeros((720, 1280, 3), dtype=np.uint8)
    evidence.add_post_observation(
        frame=frame,
        state="inventory",
        positive_cues=("inventory_category_rail", "账号13800000000"),
        negative_cues=("home_anchor_absent",),
        reason_codes=("backpack_visible",),
        postcondition_result="pass",
    )

    document = evidence.to_dict()

    assert document["attempt_id"] == evidence.attempt_id
    assert document["dispatch_acknowledged"] is True
    assert document["dispatch_acknowledged_semantics"] == "COMMAND_RETURN_ONLY"
    assert document["dispatch_command_returned"] is True
    assert document["dispatch_backend_error"] is None
    assert document["evidence_schema_version"] == "2.0"
    assert document["touch_effect_observed"] is True
    assert document["post_frame_changed"] is True
    assert document["target_page_changed"] is True
    assert document["device_width"] == 1280
    assert document["device_height"] == 720
    assert document["actual_dispatched_point"] == (1101, 51)
    assert document["station_detection_result"] == "PASS"
    assert document["station_id"] == "七号自由港"
    assert document["post_observations"][0]["post_observation_index"] == 1
    assert document["post_observation_index"] == 1
    assert document["positive_cues"] == (
        "inventory_category_rail",
        "redacted_non_code",
    )
    assert "13800000000" not in json.dumps(document, ensure_ascii=False)


def test_writer_failure_does_not_change_attempt_result(tmp_path):
    chain = CoordinateChain.from_capture_point(
        (1101, 51),
        capture_size=(1280, 720),
        render_client_size=(1280, 720),
    )
    evidence = _evidence(chain)
    blocking_file = tmp_path / "not-a-directory"
    blocking_file.write_text("occupied", encoding="utf-8")

    assert record_navigation_attempt(evidence, directory=blocking_file) is False
    assert evidence.dispatch_result == "not_requested"


def test_command_return_is_not_touch_effect_until_a_frame_changes():
    chain = CoordinateChain.from_capture_point(
        (1101, 51), capture_size=(1280, 720), render_client_size=(1280, 720)
    )
    evidence = _evidence(chain)
    evidence.mark_dispatch(requested=True, acknowledged=True, result="call_returned")

    assert evidence.dispatch_command_returned is True
    assert evidence.touch_effect_observed is False
    evidence.mark_post_effect(frame_changed=True, target_page_changed=False)
    assert evidence.touch_effect_observed is True
    assert evidence.post_frame_changed is True
    assert evidence.target_page_changed is False


def test_multiframe_post_window_aggregates_effects_across_all_observations():
    chain = CoordinateChain.from_capture_point(
        (1101, 51), capture_size=(1280, 720), render_client_size=(1280, 720)
    )
    evidence = _evidence(chain)
    evidence.pre_state = "ACTION_SUMMARY_ENTRY_VISIBLE"
    evidence.pre_frame_sha256 = "pre"
    evidence.add_post_observation(
        frame=SimpleNamespace(raw_frame_hash="changed-1"),
        state="ACTION_SUMMARY_ENTRY_VISIBLE",
        postcondition_result="PENDING",
    )
    evidence.add_post_observation(
        frame=SimpleNamespace(raw_frame_hash="changed-2"),
        state="ACTION_SUMMARY_VISIBLE",
        postcondition_result="PASS",
    )

    assert evidence.post_frame_changed is True
    assert evidence.target_page_changed is True
    assert evidence.touch_effect_observed is True


def test_postcondition_pass_with_all_effect_flags_false_is_impossible():
    chain = CoordinateChain.from_capture_point(
        (1101, 51), capture_size=(1280, 720), render_client_size=(1280, 720)
    )
    evidence = _evidence(chain)
    evidence.pre_frame_sha256 = "same"

    with pytest.raises(ValueError, match="postcondition_pass_without_observed_effect"):
        evidence.add_post_observation(
            frame=SimpleNamespace(raw_frame_hash="same"),
            state=evidence.pre_state,
            postcondition_result="PASS",
        )


def test_changed_frame_same_page_pass_accepts_control_disappearance():
    chain = CoordinateChain.from_capture_point(
        (1101, 51), capture_size=(1280, 720), render_client_size=(1280, 720)
    )
    evidence = _evidence(chain)
    evidence.pre_frame_sha256 = "same"
    evidence.add_post_observation(
        frame=SimpleNamespace(raw_frame_hash="changed"),
        state=evidence.pre_state,
        postcondition_result="PASS",
        target_control_disappeared=True,
    )

    assert evidence.target_control_disappeared is True
    assert evidence.touch_effect_observed is True


def test_backend_exception_is_recorded_without_command_return():
    chain = CoordinateChain.from_capture_point(
        (1101, 51), capture_size=(1280, 720), render_client_size=(1280, 720)
    )
    evidence = _evidence(chain)
    evidence.mark_dispatch(
        requested=True, acknowledged=False, result="dispatch_exception:RuntimeError"
    )

    assert evidence.dispatch_command_returned is False
    assert evidence.dispatch_backend_error == "dispatch_exception:RuntimeError"
    assert evidence.touch_effect_observed is False


def test_writer_persists_privacy_safe_attempt(tmp_path):
    chain = CoordinateChain.from_capture_point(
        (1101, 51),
        capture_size=(1280, 720),
        render_client_size=(1280, 720),
    )
    evidence = _evidence(chain)
    evidence.mark_dispatch(requested=True, acknowledged=False, result="delivery_unknown")

    assert record_navigation_attempt(evidence, directory=tmp_path) is True
    document = json.loads((tmp_path / f"{evidence.attempt_id}.json").read_text("utf-8"))
    assert document["attempt_id"] == evidence.attempt_id
    assert document["coordinate_chain_complete"] is True
    assert document["dispatch_result"] == "delivery_unknown"
