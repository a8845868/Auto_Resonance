"""Pluggable OCR backend protocol and built-in implementations.

Callers should always go through :func:`core.image.ocr.predict` rather than
importing backends directly — the module-level singleton keeps the lazy-init
and thread-safety guarantees that every caller already relies on.
"""

from __future__ import annotations

import ctypes
import os
import sys
import threading
from collections.abc import Iterable
from pathlib import Path
from typing import Any, Tuple, Union

import cv2 as cv
from loguru import logger

from core.image.utils import crop_image


_PADDLE_CUDA_RUNTIME_LOCK = threading.Lock()
_PADDLE_CUDA_RUNTIME_READY = False
_PADDLE_CUDA_DLL_DIRECTORIES: list[Any] = []
_PADDLE_CUDA_DLL_HANDLES: list[Any] = []


def _prepare_windows_paddle_cuda_runtime() -> None:
    """Expose CUDA DLLs installed by Paddle's Windows dependency wheels.

    The CUDA 13 Paddle wheel installs NVIDIA runtime DLLs below
    ``site-packages/nvidia``.  Python 3.8+ no longer searches ``PATH`` for
    extension-module dependencies by default, and Paddle's own dynamic loader
    does not register those wheel directories.  Keep both directory and DLL
    handles alive for the process so PP-OCRv6 can use the packaged runtime
    without requiring a machine-wide PATH change.
    """

    global _PADDLE_CUDA_RUNTIME_READY
    if os.name != "nt" or _PADDLE_CUDA_RUNTIME_READY:
        return
    with _PADDLE_CUDA_RUNTIME_LOCK:
        if _PADDLE_CUDA_RUNTIME_READY:
            return
        site_packages = Path(sys.prefix) / "Lib" / "site-packages"
        cuda_dir = site_packages / "nvidia" / "cu13" / "bin" / "x86_64"
        cudnn_dir = site_packages / "nvidia" / "cudnn" / "bin"
        if not cuda_dir.is_dir():
            return

        directories = [cuda_dir]
        if cudnn_dir.is_dir():
            directories.append(cudnn_dir)
        add_directory = getattr(os, "add_dll_directory", None)
        for directory in directories:
            directory_text = str(directory)
            path_parts = os.environ.get("PATH", "").split(os.pathsep)
            if directory_text not in path_parts:
                os.environ["PATH"] = (
                    directory_text
                    + os.pathsep
                    + os.environ.get("PATH", "")
                )
            if callable(add_directory):
                _PADDLE_CUDA_DLL_DIRECTORIES.append(
                    add_directory(directory_text)
                )

        ordered_patterns = (
            "cudart64_*.dll",
            "nvJitLink_*.dll",
            "cublasLt64_*.dll",
            "cublas64_*.dll",
            "cufft64_*.dll",
            "curand64_*.dll",
            "cusparse64_*.dll",
            "cusolver64_*.dll",
        )
        try:
            for pattern in ordered_patterns:
                for library in sorted(cuda_dir.glob(pattern)):
                    _PADDLE_CUDA_DLL_HANDLES.append(
                        ctypes.WinDLL(str(library))
                    )
            if cudnn_dir.is_dir():
                cudnn_libraries = sorted(cudnn_dir.glob("cudnn*.dll"))
                cudnn_libraries.sort(
                    key=lambda item: (item.name != "cudnn64_9.dll", item.name)
                )
                for library in cudnn_libraries:
                    _PADDLE_CUDA_DLL_HANDLES.append(
                        ctypes.WinDLL(str(library))
                    )
        except OSError as exc:
            raise RuntimeError(
                "paddle_cuda_runtime_dll_load_failed: "
                f"{type(exc).__name__}: {exc}"
            ) from exc
        _PADDLE_CUDA_RUNTIME_READY = True


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


# ---------------------------------------------------------------------------
# PP-OCRv6 Medium (optional PaddleOCR backend)
# ---------------------------------------------------------------------------


def _paddle_result_payload(result: object) -> dict[str, Any]:
    """Return the documented PaddleOCR result payload as a plain mapping."""

    value = getattr(result, "json", result)
    if callable(value):
        value = value()
    if not isinstance(value, dict):
        return {}
    nested = value.get("res")
    return nested if isinstance(nested, dict) else value


def _paddle_ocr_result_to_dict(
    results: object,
    cropped_pos1: Tuple[int, int],
) -> list[dict]:
    """Convert PaddleOCR 3.x Result objects to the repository OCR contract."""

    if results is None:
        return []
    if isinstance(results, Iterable) and not isinstance(
        results, (dict, str, bytes)
    ) and not hasattr(results, "json"):
        results = list(results)
    if not isinstance(results, (list, tuple)):
        results = [results]
    converted: list[dict] = []
    offset_x, offset_y = cropped_pos1
    for result in results:
        payload = _paddle_result_payload(result)
        texts = payload.get("rec_texts")
        scores = payload.get("rec_scores")
        polygons = payload.get("rec_polys")
        if polygons is None:
            polygons = payload.get("dt_polys")
        texts = [] if texts is None else texts
        scores = [] if scores is None else scores
        polygons = [] if polygons is None else polygons
        for text, score, polygon in zip(texts, scores, polygons):
            try:
                points = [
                    [float(point[0]) + offset_x, float(point[1]) + offset_y]
                    for point in polygon
                ]
            except (TypeError, ValueError, IndexError):
                continue
            if len(points) < 4:
                continue
            converted.append({
                "text": str(text),
                "score": float(score),
                "position": points[:4],
            })
    return converted


class PaddlePpocrV6Backend(OcrBackend):
    """PP-OCRv6 Medium through the optional PaddleOCR 3.x runtime."""

    def __init__(self, *, provider: str = "auto") -> None:
        self._requested_provider = provider
        self._model = None
        self._model_provider: str | None = None
        self._model_lock = threading.Lock()

    @property
    def name(self) -> str:
        return "ppocr-v6-medium"

    @property
    def provider(self) -> str:
        if self._model_provider is not None:
            return self._model_provider
        return self._select_provider(
            self._requested_provider,
            self._cuda_available(),
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
        if image is None:
            raise ValueError("ocr_image_unreadable")
        if (cropped_pos1 != (0, 0) or cropped_pos2 != (0, 0)) and not no_crop:
            image = crop_image(image, cropped_pos1, cropped_pos2)
        raw = self._get_model().predict(image)
        return _paddle_ocr_result_to_dict(raw, cropped_pos1)

    @staticmethod
    def _select_provider(requested: str, cuda_available: bool) -> str:
        normalized = requested.strip().lower()
        if normalized not in {"auto", "cpu", "cuda"}:
            raise ValueError("ocr_provider_invalid")
        if normalized == "cpu":
            return "cpu"
        if normalized == "cuda" and not cuda_available:
            logger.warning(
                "AUTO_RESONANCE_OCR_PROVIDER=cuda requested but the "
                "Paddle runtime has no CUDA support; falling back to CPU"
            )
            return "cpu"
        return "cuda" if cuda_available else "cpu"

    @staticmethod
    def _cuda_available() -> bool:
        try:
            _prepare_windows_paddle_cuda_runtime()
            import paddle

            detector = getattr(paddle, "is_compiled_with_cuda", None)
            if callable(detector):
                return bool(detector())
            device = getattr(paddle, "device", None)
            detector = getattr(device, "is_compiled_with_cuda", None)
            return bool(detector()) if callable(detector) else False
        except ImportError as exc:
            raise RuntimeError(
                "ppocr_v6_runtime_missing: install the project optional "
                "dependency with `pip install -e .[ocr-v6-cpu]`, or install "
                "the official Paddle GPU inference engine before selecting "
                "PP-OCRv6"
            ) from exc
        except RuntimeError as exc:
            logger.warning(
                "Paddle CUDA runtime unavailable; falling back to CPU: "
                f"{type(exc).__name__}: {exc}"
            )
            return False

    @staticmethod
    def _create_ocr_model(*, provider: str):
        try:
            from paddleocr import PaddleOCR
        except ImportError as exc:
            raise RuntimeError(
                "ppocr_v6_runtime_missing: install the project optional "
                "dependency with `pip install -e .[ocr-v6-cpu]` (or "
                "`pip install -e .[ocr-v6-gpu]` for CUDA)"
            ) from exc
        kwargs = {
            "ocr_version": "PP-OCRv6",
            "text_detection_model_name": "PP-OCRv6_medium_det",
            "text_recognition_model_name": "PP-OCRv6_medium_rec",
            "use_doc_orientation_classify": False,
            "use_doc_unwarping": False,
            "use_textline_orientation": False,
            "device": "gpu:0" if provider == "cuda" else "cpu",
        }
        if provider != "cuda":
            # The oneDNN backend in paddlepaddle 3.x CPU cannot convert the
            # PIR ArrayAttribute<Double> used by PP-OCRv6 detection models.
            # paddlepaddle-gpu already skips oneDNN in GPU mode.
            kwargs["enable_mkldnn"] = False
        return PaddleOCR(**kwargs)

    def _get_model(self):
        if self._model is not None:
            return self._model
        with self._model_lock:
            if self._model is not None:
                return self._model
            try:
                provider = self._select_provider(
                    self._requested_provider,
                    self._cuda_available(),
                )
                model = self._create_ocr_model(provider=provider)
            except Exception as exc:
                raise RuntimeError(
                    "ocr_model_initialization_failed: "
                    f"{type(exc).__name__}: {exc}"
                ) from exc
            self._model = model
            self._model_provider = provider
            logger.info(
                f"OCR model initialized: {self.name} provider={provider}"
            )
        return self._model
