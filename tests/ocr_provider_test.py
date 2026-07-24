from __future__ import annotations

import importlib
import sys

import numpy as np
import pytest


def test_import_does_not_initialize_ocr_model(monkeypatch):
    sys.modules.pop("core.image.ocr", None)
    module = importlib.import_module("core.image.ocr")
    assert module._model is None
    assert module._model_provider is None


@pytest.mark.parametrize(
    ("requested", "available", "expected", "use_gpu"),
    [
        ("auto", ("CPUExecutionProvider",), "cpu", False),
        ("auto", ("CUDAExecutionProvider", "CPUExecutionProvider"), "cuda", True),
        ("cpu", ("CUDAExecutionProvider", "CPUExecutionProvider"), "cpu", False),
        ("cuda", ("CPUExecutionProvider",), "cpu", False),
    ],
)
def test_provider_selection_and_single_initialization(
    monkeypatch, requested, available, expected, use_gpu
):
    import core.image.ocr as ocr

    created = []
    model = object()
    ocr._reset_ocr_model_for_tests()
    monkeypatch.setenv("AUTO_RESONANCE_OCR_PROVIDER", requested)
    monkeypatch.setattr(ocr, "_available_providers", lambda: available)
    monkeypatch.setattr(
        ocr,
        "_create_ocr_model",
        lambda **kwargs: created.append(kwargs) or model,
    )

    assert ocr.get_ocr_model() is model
    assert ocr.get_ocr_model() is model
    assert ocr._model_provider == expected
    assert created == [{"use_gpu": use_gpu}]


def test_predict_uses_lazy_model_and_preserves_result_shape(monkeypatch):
    import core.image.ocr as ocr

    class Model:
        def ocr(self, _image):
            return [[[[[1, 2], [3, 2], [3, 4], [1, 4]], ("岚心城", 0.99)]]]

    monkeypatch.setattr(ocr, "get_ocr_model", lambda: Model())
    result = ocr.predict(np.zeros((10, 10, 3), dtype=np.uint8), no_crop=True)
    assert result == [
        {
            "text": "岚心城",
            "score": 0.99,
            "position": [[1, 2], [3, 2], [3, 4], [1, 4]],
        }
    ]


def test_initialization_error_is_explicit(monkeypatch):
    import core.image.ocr as ocr

    ocr._reset_ocr_model_for_tests()
    monkeypatch.setattr(ocr, "_available_providers", lambda: ("CPUExecutionProvider",))
    monkeypatch.setattr(
        ocr,
        "_create_ocr_model",
        lambda **_kwargs: (_ for _ in ()).throw(OSError("model missing")),
    )
    with pytest.raises(RuntimeError, match="ocr_model_initialization_failed"):
        ocr.get_ocr_model()
