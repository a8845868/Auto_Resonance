"""Pluggable OCR backend protocol and built-in implementations.

Callers should always go through :func:`core.image.ocr.predict` rather than
importing backends directly — the module-level singleton keeps the lazy-init
and thread-safety guarantees that every caller already relies on.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Tuple, Union

import cv2 as cv
from loguru import logger

from core.image.utils import crop_image


class OcrBackend:
    """Protocol for a pluggable OCR engine.

    A backend receives a pre-loaded image and returns the standard
    ``list[dict]`` format.  Stateful initialization (model download,
    ONNX session creation, etc.) is the backend's responsibility.
    """

    @property
    def name(self) -> str:
        raise NotImplementedError

    @property
    def provider(self) -> str:
        raise NotImplementedError

    def predict(
        self,
        image: Union[str, Path, cv.typing.MatLike],
        cropped_pos1: Tuple[int, int] = (0, 0),
        cropped_pos2: Tuple[int, int] = (0, 0),
        no_crop: bool = False,
    ) -> list[dict]:
        """OCR *image* and return the canonical ``list[dict]`` format.

        Each dict has keys ``text`` (str), ``score`` (float), and
        ``position`` (list of four ``[x, y]`` pairs in absolute
        full-frame coordinates).
        """
        raise NotImplementedError


# ---------------------------------------------------------------------------
# PP-OCRv4 (current production backend)
# ---------------------------------------------------------------------------


def _onnx_ocr_result_to_dict(
    out: list, cropped_pos1: Tuple[int, int]
) -> list[dict]:
    """Convert raw ONNXPaddleOcr output to the standard dict format."""
    out = out[0]
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


class OnnxPpocrV4Backend(OcrBackend):
    """PP-OCRv4 via ``onnxocr-ppocrv4`` — the current production engine."""

    def __init__(self, *, provider: str = "auto") -> None:
        self._requested_provider = provider
        self._model = None
        self._model_provider: str | None = None
        self._model_lock = threading.Lock()

    # -- OcrBackend interface ------------------------------------------------

    @property
    def name(self) -> str:
        return "ppocr-v4"

    @property
    def provider(self) -> str:
        if self._model_provider is not None:
            return self._model_provider
        return self._select_provider(
            self._requested_provider, self._available_providers(),
        )

    def predict(
        self,
        image: Union[str, Path, cv.typing.MatLike],
        cropped_pos1: Tuple[int, int] = (0, 0),
        cropped_pos2: Tuple[int, int] = (0, 0),
        no_crop: bool = False,
    ) -> list[dict]:
        if isinstance(image, Path):
            image = str(image)
        if isinstance(image, str):
            image = cv.imread(image)
        if (cropped_pos1 != (0, 0) or cropped_pos2 != (0, 0)) and not no_crop:
            image = crop_image(image, cropped_pos1, cropped_pos2)
        raw = self._get_model().ocr(image)
        return _onnx_ocr_result_to_dict(raw, cropped_pos1)

    # -- internal ------------------------------------------------------------

    @staticmethod
    def _available_providers() -> tuple[str, ...]:
        import onnxruntime

        return tuple(onnxruntime.get_available_providers())

    @staticmethod
    def _select_provider(
        requested: str, available: tuple[str, ...]
    ) -> str:
        normalized = requested.strip().lower()
        if normalized not in {"auto", "cpu", "cuda"}:
            raise ValueError("ocr_provider_invalid")
        cuda_available = "CUDAExecutionProvider" in available
        if normalized == "cpu":
            return "cpu"
        if normalized == "cuda" and not cuda_available:
            logger.warning(
                "AUTO_RESONANCE_OCR_PROVIDER=cuda requested but "
                "CUDAExecutionProvider is unavailable; falling back to CPU"
            )
            return "cpu"
        return "cuda" if cuda_available else "cpu"

    @staticmethod
    def _create_ocr_model(*, use_gpu: bool):
        from onnxocr.onnx_paddleocr import ONNXPaddleOcr

        return ONNXPaddleOcr(
            use_angle_cls=False,
            use_gpu=use_gpu,
            use_dml=False,
            use_openvino=False,
        )

    def _get_model(self):
        """Lazily initialize the ONNX model with the resolved provider."""
        if self._model is not None:
            return self._model
        with self._model_lock:
            if self._model is not None:
                return self._model
            try:
                provider = self._select_provider(
                    self._requested_provider,
                    self._available_providers(),
                )
                self._model = self._create_ocr_model(
                    use_gpu=provider == "cuda",
                )
                self._model_provider = provider
                logger.info(
                    f"OCR model initialized: {self.name} provider={provider}"
                )
            except Exception as exc:
                raise RuntimeError(
                    f"ocr_model_initialization_failed: "
                    f"{type(exc).__name__}: {exc}"
                ) from exc
        return self._model
