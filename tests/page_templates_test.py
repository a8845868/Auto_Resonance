from pathlib import Path
from types import SimpleNamespace

import numpy as np

import core.services.page_templates as page_templates
from auto.exchange_navigation import exchange_menu_matches
from core.services.city_navigation import CityNavigationState, observe_city_frame
from core.services.screen_state import (
    ResidentHomeState,
    resident_home_state,
    startup_screen_action,
)


def _items(*texts: str) -> list[dict]:
    return [{"text": text} for text in texts]


def _frame_with_template(path: Path, roi: tuple[int, int, int, int]) -> np.ndarray:
    template = page_templates._load_template(path)
    assert template is not None
    frame = np.zeros((720, 1280, 3), dtype=np.uint8)
    x1, y1, x2, y2 = roi
    height, width = template.shape[:2]
    assert height <= y2 - y1
    assert width <= x2 - x1
    frame[y1 : y1 + height, x1 : x1 + width] = template
    return frame


def _place_template(
    frame: np.ndarray,
    path: Path,
    roi: tuple[int, int, int, int],
) -> None:
    template = page_templates._load_template(path)
    assert template is not None
    x1, y1, x2, y2 = roi
    height, width = template.shape[:2]
    assert height <= y2 - y1
    assert width <= x2 - x1
    frame[y1 : y1 + height, x1 : x1 + width] = template


def test_missing_and_corrupt_templates_fail_closed(tmp_path):
    missing = tmp_path / "missing.png"
    corrupt = tmp_path / "corrupt.png"
    corrupt.write_bytes(b"not-a-png")

    assert page_templates._load_template(missing) is None
    assert page_templates._load_template(corrupt) is None
    assert not page_templates.match_page_template(
        np.zeros((720, 1280, 3), dtype=np.uint8),
        None,
        (0, 0, 100, 100),
    )


def test_home_template_first_pass_skips_empty_ocr():
    frame = _frame_with_template(
        page_templates.HOME_TEMPLATE_PATH,
        page_templates.HOME_TEMPLATE_ROI,
    )

    assert resident_home_state([], frame_img=frame) is ResidentHomeState.HOME_READY


def test_exchange_menu_template_first_pass_skips_empty_ocr():
    frame = _frame_with_template(
        page_templates.EXCHANGE_MENU_TEMPLATE_PATH,
        page_templates.EXCHANGE_MENU_TEMPLATE_ROI,
    )

    assert exchange_menu_matches([], frame_img=frame)


def test_passenger_management_requires_both_audited_templates():
    frame = np.zeros((720, 1280, 3), dtype=np.uint8)
    _place_template(
        frame,
        page_templates.PASSENGER_MANAGEMENT_TITLE_TEMPLATE_PATH,
        page_templates.PASSENGER_MANAGEMENT_TITLE_TEMPLATE_ROI,
    )

    assert not page_templates.passenger_management_matches(frame)
    assert startup_screen_action(_items("80%"), frame_img=frame) == "wait_for_game"

    _place_template(
        frame,
        page_templates.PASSENGER_MANAGEMENT_CATEGORY_CARDS_TEMPLATE_PATH,
        page_templates.PASSENGER_MANAGEMENT_CATEGORY_CARDS_TEMPLATE_ROI,
    )

    assert page_templates.passenger_management_matches(frame)
    assert startup_screen_action(_items("80%", "+2%"), frame_img=frame) is None


def test_station_detail_template_first_pass_classifies_empty_ocr():
    frame_img = _frame_with_template(
        page_templates.STATION_DETAIL_ANCHOR_STRUCTURE_TEMPLATE_PATH,
        page_templates.STATION_DETAIL_ANCHOR_STRUCTURE_TEMPLATE_ROI,
    )
    frame = SimpleNamespace(
        image=frame_img,
        raw_frame_hash="",
        source_capture_id="template-test",
        captured_at=None,
        ocr=lambda: [],
    )

    observation = observe_city_frame(frame)

    assert observation.state is CityNavigationState.CITY_DETAIL
    assert observation.reason == "station_detail_template_confirmed"
    assert "station_detail_template" in observation.evidence


def test_failed_template_match_falls_back_to_existing_ocr():
    frame = np.zeros((720, 1280, 3), dtype=np.uint8)

    assert resident_home_state(
        _items("访问城市", "启程"), frame_img=frame
    ) is ResidentHomeState.HOME_READY
    assert startup_screen_action(_items("81%"), frame_img=frame) == "wait_for_game"
    assert exchange_menu_matches(
        _items("交易所", "你想要什么", "我要买", "我要卖"),
        frame_img=frame,
    )


def test_cv_exception_fails_back_without_changing_classification(monkeypatch):
    frame = np.zeros((720, 1280, 3), dtype=np.uint8)
    template = np.ones((20, 20, 3), dtype=np.uint8)

    def fail_match(*_args, **_kwargs):
        raise RuntimeError("cv unavailable")

    monkeypatch.setattr(page_templates.cv, "matchTemplate", fail_match)
    assert not page_templates.match_page_template(
        frame, template, (0, 0, 100, 100)
    )
    assert resident_home_state(
        _items("访问城市", "作战终端"), frame_img=frame
    ) is ResidentHomeState.HOME_READY
