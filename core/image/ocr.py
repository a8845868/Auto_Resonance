"""
Author: Night-stars-1 nujj1042633805@gmail.com
Date: 2024-04-01 21:40:57
LastEditTime: 2025-02-04 23:40:25
LastEditors: Night-stars-1 nujj1042633805@gmail.com
"""

import os
import re
import threading
from pathlib import Path
from typing import Tuple, Union

import cv2 as cv
from loguru import logger

_UID_PREFIX = re.compile(r"(?i)\b(?:uid|u1d)\s*[:：]?\s*\d{6,12}\b")
_BARE_SENSITIVE_DIGITS = re.compile(r"^\s*\d{6,12}\s*$")
_REDACTED_UID = "<redacted_home_profile_id>"

# ---------------------------------------------------------------------------
# Pluggable backend singleton
# ---------------------------------------------------------------------------

_backend = None
_backend_lock = threading.Lock()


def _get_backend():
    """Lazily initialise the configured OCR backend (thread-safe).

    Any error during provider resolution or backend construction is
    wrapped in ``RuntimeError`` so that every caller sees the same
    exception class regardless of which backend is active.  The
    ``_backend`` global is reset on failure — no half-initialised
    instance is ever cached.
    """
    global _backend
    if _backend is not None:
        return _backend
    with _backend_lock:
        if _backend is not None:
            return _backend
        from core.image.ocr_backend import OnnxPpocrV4Backend

        try:
            provider = os.getenv("AUTO_RESONANCE_OCR_PROVIDER", "auto")
            _backend = OnnxPpocrV4Backend(provider=provider)
            logger.info(
                f"OCR backend initialised: {_backend.name} "
                f"provider={_backend.provider}"
            )
        except Exception as exc:
            _backend = None
            raise RuntimeError(
                f"ocr_model_initialization_failed: "
                f"{type(exc).__name__}: {exc}"
            ) from exc
        return _backend


def _reset_ocr_backend_for_tests() -> None:
    global _backend
    with _backend_lock:
        _backend = None


# ---------------------------------------------------------------------------
# Public API — signatures are frozen; callers must not depend on the backend
# ---------------------------------------------------------------------------


def predict(
    image: Union[str, Path, cv.typing.MatLike],
    cropped_pos1: Tuple[int, int] = (0, 0),
    cropped_pos2: Tuple[int, int] = (0, 0),
    no_crop: bool = False,
):
    """
    说明：
        OCR识别图片上的文字
    参数：
        :param img_fp: 图片
        :param cropped_pos1: 切剪区域 (x1, y1)
        :param cropped_pos2: 切剪区域 (x2, y2)
    """
    if isinstance(image, Path):
        image = str(image)
    if isinstance(image, str):
        image = cv.imread(image)
    frame_height, frame_width = image.shape[:2]
    result = _get_backend().predict(
        image, cropped_pos1, cropped_pos2, no_crop,
    )
    logger.debug(
        redact_ocr_result_for_log(
            result, frame_size=(frame_width, frame_height),
        )
    )
    return result


def number_predict(
    image: Union[str, Path, cv.typing.MatLike],
    cropped_pos1: Tuple[int, int] = (0, 0),
    cropped_pos2: Tuple[int, int] = (0, 0),
    no_crop: bool = False,
):
    """
    说明：
        OCR识别图片上的文字（数字专用 — 当前与 predict 共享同一后端）
    参数：
        :param img_fp: 图片
        :param cropped_pos: 切剪区域 (x1, x2, y1, y2)
    """
    if isinstance(image, Path):
        image = str(image)
    if isinstance(image, str):
        image = cv.imread(image)
    frame_height, frame_width = image.shape[:2]
    result = _get_backend().predict(
        image, cropped_pos1, cropped_pos2, no_crop,
    )
    logger.debug(
        redact_ocr_result_for_log(
            result, frame_size=(frame_width, frame_height),
        )
    )
    return result


# ---------------------------------------------------------------------------
# Log redaction — model-independent post-processing
# ---------------------------------------------------------------------------


def _log_bbox(item: dict) -> tuple[int, int, int, int] | None:
    points = item.get("position")
    if not isinstance(points, (list, tuple)) or len(points) < 2:
        return None
    try:
        xs = [int(round(float(point[0]))) for point in points]
        ys = [int(round(float(point[1]))) for point in points]
    except (TypeError, ValueError, IndexError):
        return None
    return min(xs), min(ys), max(xs), max(ys)


def redact_ocr_result_for_log(
    result: list[dict], *, frame_size: tuple[int, int]
) -> list[dict]:
    """Redact HOME profile identifiers without mutating OCR return values."""

    width, height = map(int, frame_size)
    redacted: list[dict] = []
    for original in result:
        item = dict(original)
        text = str(item.get("text", ""))
        bounds = _log_bbox(item)
        in_home_profile_roi = bool(
            bounds is not None
            and bounds[0] <= int(width * 0.28)
            and bounds[1] >= int(height * 0.68)
        )
        if _UID_PREFIX.search(text) or (
            in_home_profile_roi and _BARE_SENSITIVE_DIGITS.fullmatch(text)
        ):
            item["text"] = _REDACTED_UID
        redacted.append(item)
    return redacted


# ---------------------------------------------------------------------------
# Test helpers — kept for backward compatibility with existing test suites
# ---------------------------------------------------------------------------


def _reset_ocr_model_for_tests() -> None:
    """Deprecated alias — prefer :func:`_reset_ocr_backend_for_tests`."""
    _reset_ocr_backend_for_tests()


def get_ocr_model():
    """Deprecated — returns the current backend for tests that still inspect
    internal model state.  New code should use :func:`_get_backend`."""
    backend = _get_backend()
    # Tests that used get_ocr_model() may call .ocr() on the returned object.
    # Expose the internal ONNX model via a compatibility wrapper.
    return backend._get_model()
