from __future__ import annotations

import json

import numpy as np

from core.services.navigation_evidence import (
    CoordinateChain,
    NavigationAttemptEvidence,
    record_navigation_attempt,
)


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
