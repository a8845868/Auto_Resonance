from __future__ import annotations

import importlib
import hashlib
import sys
from types import SimpleNamespace

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


def test_home_profile_uid_log_redaction_preserves_non_sensitive_ocr():
    import core.image.ocr as ocr

    uid = "8821612558"
    result = [
        {"text": f"UID:{uid}", "position": [[120, 704], [200, 704], [200, 718], [120, 718]]},
        {"text": f"U1D: {uid}", "position": [[120, 704], [200, 704], [200, 718], [120, 718]]},
        {"text": uid, "position": [[148, 704], [200, 704], [200, 718], [148, 718]]},
        {"text": "访问城市", "position": [[1080, 480], [1180, 480], [1180, 510], [1080, 510]]},
    ]

    redacted = ocr.redact_ocr_result_for_log(result, frame_size=(1280, 720))
    serialized = repr(redacted)

    assert uid not in serialized
    assert hashlib.sha256(uid.encode()).hexdigest() not in serialized
    assert [value["text"] for value in redacted[:3]] == [
        "<redacted_home_profile_id>",
        "<redacted_home_profile_id>",
        "<redacted_home_profile_id>",
    ]
    assert redacted[-1]["text"] == "访问城市"
    assert result[0]["text"] == f"UID:{uid}"


def test_predict_logs_redacted_copy_but_returns_original(monkeypatch):
    import core.image.ocr as ocr

    uid = "8821612558"

    class Model:
        def ocr(self, _image):
            return [[[
                [[128, 704], [199, 704], [199, 717], [128, 717]],
                (f"UID:{uid}", 0.99),
            ]]]

    logged = []
    monkeypatch.setattr(ocr, "get_ocr_model", lambda: Model())
    monkeypatch.setattr(ocr, "logger", SimpleNamespace(debug=logged.append))

    result = ocr.predict(np.zeros((720, 1280, 3), dtype=np.uint8), no_crop=True)

    assert result[0]["text"] == f"UID:{uid}"
    assert uid not in repr(logged)
    assert logged[0][0]["text"] == "<redacted_home_profile_id>"
