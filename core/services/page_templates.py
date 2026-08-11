"""Fail-safe, ROI-bound template anchors for core page classifiers."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import cv2 as cv
import numpy as np


TEMPLATE_DIR = Path(__file__).resolve().parents[2] / "resources" / "templates"
HOME_TEMPLATE_PATH = TEMPLATE_DIR / "home_button.png"
EXCHANGE_MENU_TEMPLATE_PATH = TEMPLATE_DIR / "exchange_menu.png"

# Runtime frames are normalized to 1280x720.  Matching is restricted to these
# fixed UI regions; templates are never searched over the full frame.
HOME_TEMPLATE_ROI = (1050, 445, 1280, 530)
EXCHANGE_MENU_TEMPLATE_ROI = (710, 270, 1210, 455)


@lru_cache(maxsize=8)
def _load_template(path: str | Path) -> cv.typing.MatLike | None:
    """Load a template without making missing/corrupt resources fatal."""

    try:
        resolved = Path(path)
        if not resolved.is_file() or resolved.stat().st_size <= 0:
            return None
        data = np.fromfile(resolved, dtype=np.uint8)
        if data.size == 0:
            return None
        image = cv.imdecode(data, cv.IMREAD_COLOR)
        if image is None or image.size == 0:
            return None
        return image
    except Exception:
        return None


def match_page_template(
    frame_img: Any,
    template: cv.typing.MatLike | None,
    roi: tuple[int, int, int, int],
    threshold: float = 0.85,
) -> bool:
    """Match ``template`` only inside ``roi`` and fail back to OCR safely."""

    try:
        if frame_img is None or template is None:
            return False
        frame = np.asarray(frame_img)
        candidate = np.asarray(template)
        if frame.ndim not in (2, 3) or candidate.ndim not in (2, 3):
            return False
        x1, y1, x2, y2 = (int(value) for value in roi)
        height, width = frame.shape[:2]
        if not (0 <= x1 < x2 <= width and 0 <= y1 < y2 <= height):
            return False
        cropped = frame[y1:y2, x1:x2]
        if cropped.size == 0:
            return False
        if cropped.ndim == 3:
            cropped = cv.cvtColor(cropped, cv.COLOR_BGR2GRAY)
        if candidate.ndim == 3:
            candidate = cv.cvtColor(candidate, cv.COLOR_BGR2GRAY)
        if (
            candidate.shape[0] > cropped.shape[0]
            or candidate.shape[1] > cropped.shape[1]
        ):
            return False
        result = cv.matchTemplate(cropped, candidate, cv.TM_CCOEFF_NORMED)
        if result is None or result.size == 0:
            return False
        score = float(cv.minMaxLoc(result)[1])
        return bool(np.isfinite(score) and score >= float(threshold))
    except Exception:
        return False
