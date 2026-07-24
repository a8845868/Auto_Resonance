"""Replay nine generated runtime scenarios without emulator or input access."""

from __future__ import annotations

import argparse
import base64
import io
import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

import cv2 as cv
import numpy as np
from PIL import Image as PilImage


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.services.announcement_overlay_handler import (  # noqa: E402
    AnnouncementSafeRegionSelector,
)
from core.services.capture_media import decode_capture_data_url  # noqa: E402
from core.services.personal_action_budget import EpisodeActionBudget  # noqa: E402
from core.services.personal_city_target import PersonalCityTarget  # noqa: E402
from core.services.personal_runtime_episode import (  # noqa: E402
    ActionPlanner,
    StateDetector,
)


class ScenarioFrame:
    def __init__(self, image: object, ocr: list[dict], metadata: dict | None = None) -> None:
        self.image = image
        self._ocr = list(ocr)
        self.scenario_metadata = metadata or {}
        self.announcement_dialog_bbox = self.scenario_metadata.get("dialog_bounds")

    def ocr(self) -> list[dict]:
        return list(self._ocr)


@dataclass(frozen=True)
class ScenarioSpec:
    name: str
    frame: ScenarioFrame
    expected_state: str
    expected_action: str
    force_no_safe_region: bool = False


def _item(text: str, bbox: tuple[int, int, int, int]) -> dict:
    return {"text": text, "bbox": list(bbox), "score": 0.99}


def _announcement_pixels(width: int = 851, *, block_safe_band: bool = False) -> np.ndarray:
    image = np.full((480, width, 3), 28, dtype=np.uint8)
    scale = width / 851
    left, right = round(68 * scale), round(778 * scale)
    cv.rectangle(image, (left, 47), (right, 422), (180, 180, 180), 2)
    cv.line(image, (left, 93), (right, 93), (190, 190, 190), 2)
    if block_safe_band:
        for y in range(424, 469, 4):
            cv.line(image, (0, y), (width - 1, y), (255, 255, 255), 1)
    return image


def _encoded_frame(media_type: str, width: int = 851) -> ScenarioFrame:
    image = _announcement_pixels(width)
    rgb = cv.cvtColor(image, cv.COLOR_BGR2RGB)
    buffer = io.BytesIO()
    PilImage.fromarray(rgb).save(
        buffer,
        format="PNG" if media_type == "image/png" else "JPEG",
        quality=84,
    )
    payload = base64.b64encode(buffer.getvalue()).decode("ascii")
    decoded = decode_capture_data_url(f"data:{media_type};base64,{payload}")
    scale = width / 851
    return ScenarioFrame(
        decoded.image,
        [
            _item("公告", (round(566 * scale), 64, round(619 * scale), 86)),
            _item("触碰空白区域退出", (round(398 * scale), 440, round(471 * scale), 453)),
        ],
    )


def generated_scenarios() -> tuple[ScenarioSpec, ...]:
    resource_complete = ScenarioFrame(
        np.full((720, 1280, 3), 20, dtype=np.uint8),
        [
            _item("下载已经完成", (468, 546, 630, 574)),
            _item("点击任意位置进入游戏", (620, 546, 820, 574)),
        ],
    )
    daily = ScenarioFrame(
        np.full((720, 1280, 3), 35, dtype=np.uint8),
        [
            _item("每日签到奖励", (311, 78, 560, 122)),
            _item("已领取", (1012, 584, 1071, 608)),
            _item("触碰空白区域退出", (604, 675, 712, 692)),
            _item("今日奖励", (847, 486, 933, 504)),
        ],
        {"overlay_bounds": [0, 0, 1280, 720], "dialog_bounds": [174, 64, 1105, 644]},
    )
    home = ScenarioFrame(
        np.full((720, 1280, 3), 35, dtype=np.uint8),
        [
            _item("作战终端", (1148, 395, 1233, 423)),
            _item("访问城市", (1129, 474, 1216, 500)),
            _item("岚心城", (1128, 499, 1175, 518)),
            _item("启程", (1173, 652, 1209, 672)),
        ],
    )
    city = ScenarioFrame(
        np.full((720, 1280, 3), 35, dtype=np.uint8),
        [
            _item("岚心城", (176, 535, 279, 572)),
            _item("市政厅", (279, 23, 352, 49)),
            _item("休息区", (632, 167, 716, 193)),
            _item("交易所", (855, 267, 952, 294)),
        ],
    )
    blocked_announcement = ScenarioFrame(
        _announcement_pixels(block_safe_band=True),
        [_item("公告", (566, 64, 619, 86)), _item("触碰空白区域退出", (398, 440, 471, 453))],
    )
    return (
        ScenarioSpec("announcement_png_851", _encoded_frame("image/png"), "ANNOUNCEMENT_VISIBLE", "DISMISS_ANNOUNCEMENT"),
        ScenarioSpec("announcement_jpeg_851", _encoded_frame("image/jpeg"), "ANNOUNCEMENT_VISIBLE", "DISMISS_ANNOUNCEMENT"),
        ScenarioSpec("announcement_png_853", _encoded_frame("image/png", 853), "ANNOUNCEMENT_VISIBLE", "DISMISS_ANNOUNCEMENT"),
        ScenarioSpec("daily_checkin_claimed", daily, "DAILY_CHECKIN", "DISMISS_DAILY_CHECKIN"),
        ScenarioSpec("home_ready", home, "HOME_READY", "ENTER_CITY"),
        ScenarioSpec("city_detail", city, "CITY_DETAIL", "STOP"),
        ScenarioSpec("transition_unknown", ScenarioFrame(np.zeros((720, 1280, 3), dtype=np.uint8), []), "UNKNOWN", "OBSERVE_ONLY"),
        ScenarioSpec("announcement_no_safe_region", blocked_announcement, "ANNOUNCEMENT_VISIBLE", "OBSERVE_ONLY", True),
        ScenarioSpec(
            "resource_update_complete_tap_to_enter",
            resource_complete,
            "RESOURCE_UPDATE_COMPLETE_TAP_TO_ENTER",
            "ENTER_AFTER_RESOURCE_UPDATE",
        ),
    )


def replay_spec(spec: ScenarioSpec) -> dict:
    detector = StateDetector(
        safe_region_selector=(
            AnnouncementSafeRegionSelector(minimum_region_area=10_000_000)
            if spec.force_no_safe_region
            else None
        ),
        city_target=PersonalCityTarget(city_id="岚心城"),
    )
    detected = detector.detect(spec.frame)
    planned = ActionPlanner(target_city_id="岚心城").plan(
        detected, budget=EpisodeActionBudget()
    )
    passed = detected.state.value == spec.expected_state and planned.action.value == spec.expected_action
    return {
        "scenario": spec.name,
        "detected_state": detected.state.value,
        "planned_action": planned.action.value,
        "target_bbox": list(planned.target_bbox) if planned.target_bbox else None,
        "target_point": list(planned.capture_point) if planned.capture_point else None,
        "postcondition_expectation": planned.expected_postcondition.value if planned.expected_postcondition else None,
        "safe_target_count": 1 if planned.capture_point is not None else 0,
        "candidate_count": len(detected.announcement_candidates),
        "announcement_candidates": [asdict(item) for item in detected.announcement_candidates],
        "download_complete_text": detected.download_complete_text,
        "tap_to_enter_text": detected.tap_to_enter_text,
        "confidence": detected.confidence,
        "reason_codes": list(detected.reason_codes),
        "expected_state": spec.expected_state,
        "expected_planned_action": spec.expected_action,
        "result": "PASS" if passed else "FAIL",
    }


def replay_all() -> list[dict]:
    return [replay_spec(spec) for spec in generated_scenarios()]


def replay_private_announcement(image_path: Path, analysis_path: Path) -> dict:
    image = cv.imread(str(image_path), cv.IMREAD_COLOR)
    if image is None:
        raise RuntimeError("private_replay_image_unreadable")
    analysis = json.loads(analysis_path.read_text(encoding="utf-8"))
    frame = ScenarioFrame(image, list(analysis.get("ocr_tokens", ())))
    detected = StateDetector().detect(frame)
    planned = ActionPlanner().plan(detected, budget=EpisodeActionBudget())
    target = planned.capture_point
    button = tuple(analysis.get("announcement_target", {}).get("bbox", ()))
    target_not_inside_ocr = bool(target) and not any(
        left <= target[0] < right and top <= target[1] < bottom
        for left, top, right, bottom in detected.ocr_bboxes
    )
    target_not_inside_button = bool(target) and (
        len(button) != 4 or not (button[0] <= target[0] < button[2] and button[1] <= target[1] < button[3])
    )
    selected = next((item for item in detected.announcement_candidates if item.point == target), None)
    passed = (
        detected.state.value == "ANNOUNCEMENT_VISIBLE"
        and planned.action.value == "DISMISS_ANNOUNCEMENT"
        and target_not_inside_ocr
        and target_not_inside_button
        and selected is not None
        and selected.edge_density <= 0.02
    )
    return {
        "scenario": "private_real_announcement",
        "detected_state": detected.state.value,
        "planned_action": planned.action.value,
        "safe_target_count": 1 if target is not None else 0,
        "target_point": list(target) if target else None,
        "target_not_inside_ocr": "PASS" if target_not_inside_ocr else "FAIL",
        "target_not_inside_button": "PASS" if target_not_inside_button else "FAIL",
        "edge_density": selected.edge_density if selected else None,
        "result": "PASS" if passed else "FAIL",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--private-image", type=Path)
    parser.add_argument("--private-analysis", type=Path)
    args = parser.parse_args()
    if args.all:
        results = replay_all()
    elif args.private_image and args.private_analysis:
        results = [replay_private_announcement(args.private_image, args.private_analysis)]
    else:
        parser.error("provide --all or both --private-image and --private-analysis")
    for result in results:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if all(item["result"] == "PASS" for item in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
