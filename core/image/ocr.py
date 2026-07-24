"""
Author: Night-stars-1 nujj1042633805@gmail.com
Date: 2024-04-01 21:40:57
LastEditTime: 2025-02-04 23:40:25
LastEditors: Night-stars-1 nujj1042633805@gmail.com
"""

import os
import threading
from pathlib import Path
from typing import Tuple, Union

import cv2 as cv
from loguru import logger
from core.image.utils import crop_image

_model = None
_model_provider: str | None = None
_model_lock = threading.Lock()


def _available_providers() -> tuple[str, ...]:
    import onnxruntime

    return tuple(onnxruntime.get_available_providers())


def _create_ocr_model(*, use_gpu: bool):
    from onnxocr.onnx_paddleocr import ONNXPaddleOcr

    return ONNXPaddleOcr(
        use_angle_cls=False,
        use_gpu=use_gpu,
        use_dml=False,
        use_openvino=False,
    )


def _select_provider(requested: str, available: tuple[str, ...]) -> str:
    normalized = requested.strip().lower()
    if normalized not in {"auto", "cpu", "cuda"}:
        raise ValueError("ocr_provider_invalid")
    cuda_available = "CUDAExecutionProvider" in available
    if normalized == "cpu":
        return "cpu"
    if normalized == "cuda" and not cuda_available:
        logger.warning(
            "AUTO_RESONANCE_OCR_PROVIDER=cuda requested but CUDAExecutionProvider "
            "is unavailable; falling back to CPU"
        )
        return "cpu"
    return "cuda" if cuda_available else "cpu"


def get_ocr_model():
    """Lazily initialize one OCR model with an explicit runtime provider."""

    global _model, _model_provider
    if _model is not None:
        return _model
    with _model_lock:
        if _model is not None:
            return _model
        try:
            provider = _select_provider(
                os.getenv("AUTO_RESONANCE_OCR_PROVIDER", "auto"),
                _available_providers(),
            )
            _model = _create_ocr_model(use_gpu=provider == "cuda")
            _model_provider = provider
            logger.info(f"OCR model initialized with provider={provider}")
        except Exception as exc:
            raise RuntimeError(
                f"ocr_model_initialization_failed: {type(exc).__name__}: {exc}"
            ) from exc
    return _model


def _reset_ocr_model_for_tests() -> None:
    global _model, _model_provider
    with _model_lock:
        _model = None
        _model_provider = None

def ocrout2result(out, cropped_pos1):
    out = out[0]
    # logger.debug(f"识别结果: {out}")
    return [
        {
            "text": predict_data[1][0],
            "score": predict_data[1][1],
            "position": [
                [
                    predict_data[0][0][0] + cropped_pos1[0],
                    predict_data[0][0][1] + cropped_pos1[1],
                ],
                [
                    predict_data[0][1][0] + cropped_pos1[0],
                    predict_data[0][1][1] + cropped_pos1[1],
                ],
                [
                    predict_data[0][2][0] + cropped_pos1[0],
                    predict_data[0][2][1] + cropped_pos1[1],
                ],
                [
                    predict_data[0][3][0] + cropped_pos1[0],
                    predict_data[0][3][1] + cropped_pos1[1],
                ],
            ],
        }
        for predict_data in out
    ]


def predict(
    image: Union[str, Path, cv.typing.MatLike],
    cropped_pos1: Tuple[int, int] = (0, 0),
    cropped_pos2: Tuple[int, int] = (0, 0),
    no_crop: bool = False
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
    if (cropped_pos1 != (0, 0) or cropped_pos2 != (0, 0)) and not no_crop:
        image = crop_image(image, cropped_pos1, cropped_pos2)
    out = get_ocr_model().ocr(image)
    result = ocrout2result(out, cropped_pos1)
    logger.debug(result)
    return result


def number_predict(
    image: Union[str, Path, cv.typing.MatLike],
    cropped_pos1: Tuple[int, int] = (0, 0),
    cropped_pos2: Tuple[int, int] = (0, 0),
    no_crop: bool = False
):
    """
    说明：
        OCR识别图片上的文字
    参数：
        :param img_fp: 图片
        :param cropped_pos: 切剪区域 (x1, x2, y1, y2)
    """
    if isinstance(image, Path):
        image = str(image)
    if isinstance(image, str):
        image = cv.imread(image)
    if (cropped_pos1 != (0, 0) or cropped_pos2 != (0, 0)) and not no_crop:
        image = crop_image(image, cropped_pos1, cropped_pos2)
    out = get_ocr_model().ocr(image)
    result = ocrout2result(out, cropped_pos1)
    logger.debug(result)
    return result
