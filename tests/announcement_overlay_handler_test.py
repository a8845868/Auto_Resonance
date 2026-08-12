import json
import os
import subprocess
import sys

from core.services.announcement_overlay_handler import (
    AnnouncementOverlayHandler,
    AnnouncementSafeRegionSelector,
)
from core.services.personal_action_budget import EpisodeActionBudget
from core.services.personal_runtime_episode import ActionPlanner, RuntimeAction, RuntimeState, StateDetector
from tests.personal_runtime_fixtures import announcement_frame, city_frame, daily_frame, home_frame


def test_real_shape_851_frame_is_announcement_with_one_planned_safe_target():
    frame = announcement_frame()
    detected = StateDetector().detect(frame)
    plan = ActionPlanner().plan(detected, budget=EpisodeActionBudget())
    assert detected.state is RuntimeState.ANNOUNCEMENT_VISIBLE
    assert plan.action is RuntimeAction.DISMISS_ANNOUNCEMENT
    assert plan.capture_point is not None
    assert detected.dialog_bbox is not None
    x, y = plan.capture_point
    assert not any(left <= x < right and top <= y < bottom for left, top, right, bottom in detected.ocr_bboxes)
    assert not (
        detected.dialog_bbox[0] <= x < detected.dialog_bbox[2]
        and detected.dialog_bbox[1] <= y < detected.dialog_bbox[3]
    )
    selected = next(item for item in detected.announcement_candidates if item.point == plan.capture_point)
    assert selected.edge_density <= 0.02


def test_projection_resolves_non_fixed_dialog_bounds():
    bounds = AnnouncementOverlayHandler.resolve_dialog_bounds(announcement_frame())
    assert bounds is not None
    assert 60 <= bounds[0] <= 75
    assert 40 <= bounds[1] <= 55
    assert 770 <= bounds[2] <= 785
    assert 415 <= bounds[3] <= 430


def test_no_safe_region_is_announcement_but_observation_only():
    selector = AnnouncementSafeRegionSelector(minimum_region_area=10_000_000)
    detector = StateDetector(safe_region_selector=selector)
    detected = detector.detect(announcement_frame())
    plan = ActionPlanner().plan(detected, budget=EpisodeActionBudget())
    assert detected.state is RuntimeState.ANNOUNCEMENT_VISIBLE
    assert plan.action is RuntimeAction.OBSERVE_ONLY
    assert plan.reason == "announcement_safe_blank_region_unavailable"


def test_home_daily_and_city_are_not_misclassified_as_announcement():
    detector = StateDetector()
    assert detector.detect(home_frame()).state is RuntimeState.HOME_READY
    assert detector.detect(daily_frame()).state is RuntimeState.DAILY_CHECKIN
    assert detector.detect(city_frame()).state is RuntimeState.CITY_DETAIL


def test_handler_is_read_only_and_owns_no_budget_or_dispatch_counter():
    handler = AnnouncementOverlayHandler()
    assert not hasattr(handler, "dismiss_if_present")
    assert not hasattr(handler, "_attempt_count")
    assert not hasattr(handler, "_click_count")
    assert not hasattr(handler, "input_backend")


def test_import_does_not_initialize_input_backend():
    script = (
        "import json,sys; import core.services.announcement_overlay_handler; "
        "names=('adb_shell','pyautogui','pynput','win32api'); "
        "print(json.dumps({n:any(k==n or k.startswith(n+'.') for k in sys.modules) for n in names}))"
    )
    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    completed = subprocess.run(
        [sys.executable, "-c", script], check=True, capture_output=True, text=True, env=environment
    )
    assert json.loads(completed.stdout.splitlines()[-1]) == {
        "adb_shell": False,
        "pyautogui": False,
        "pynput": False,
        "win32api": False,
    }
