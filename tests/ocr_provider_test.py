from __future__ import annotations

import importlib
import hashlib
import sys
from types import SimpleNamespace

import numpy as np
import pytest


def test_import_does_not_initialize_ocr_backend(monkeypatch):
    sys.modules.pop("core.image.ocr", None)
    sys.modules.pop("core.image.ocr_backend", None)
    module = importlib.import_module("core.image.ocr")
    assert module._backend is None


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
    from core.image.ocr_backend import OnnxPpocrV4Backend

    created = []
    model = object()
    backend = OnnxPpocrV4Backend(provider=requested)
    monkeypatch.setattr(
        backend, "_available_providers", lambda: available,
    )
    monkeypatch.setattr(
        backend,
        "_create_ocr_model",
        lambda **kwargs: created.append(kwargs) or model,
    )

    assert backend._get_model() is model
    assert backend._get_model() is model
    assert backend.provider == expected
    assert created == [{"use_gpu": use_gpu}]


def test_predict_uses_lazy_backend_and_preserves_result_shape(monkeypatch):
    import core.image.ocr as ocr

    ocr._reset_ocr_backend_for_tests()

    class FakeBackend:
        name = "fake"
        provider = "cpu"

        def predict(self, image, cropped_pos1, cropped_pos2, no_crop):
            return [
                {
                    "text": "嵐心城",
                    "score": 0.99,
                    "position": [[1, 2], [3, 2], [3, 4], [1, 4]],
                }
            ]

    monkeypatch.setattr(ocr, "_get_backend", lambda: FakeBackend())
    result = ocr.predict(
        np.zeros((10, 10, 3), dtype=np.uint8), no_crop=True,
    )
    assert result == [
        {
            "text": "嵐心城",
            "score": 0.99,
            "position": [[1, 2], [3, 2], [3, 4], [1, 4]],
        }
    ]


def test_initialization_error_is_explicit(monkeypatch):
    from core.image.ocr_backend import OnnxPpocrV4Backend

    backend = OnnxPpocrV4Backend(provider="auto")
    monkeypatch.setattr(
        backend, "_available_providers", lambda: ("CPUExecutionProvider",),
    )
    monkeypatch.setattr(
        backend,
        "_create_ocr_model",
        lambda **_kwargs: (_ for _ in ()).throw(OSError("model missing")),
    )
    with pytest.raises(RuntimeError, match="ocr_model_initialization_failed"):
        backend._get_model()


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

    ocr._reset_ocr_backend_for_tests()
    uid = "8821612558"

    class FakeBackend:
        name = "fake"
        provider = "cpu"

        def predict(self, image, cropped_pos1, cropped_pos2, no_crop):
            return [
                {
                    "text": f"UID:{uid}",
                    "score": 0.99,
                    "position": [[128, 704], [199, 704], [199, 717], [128, 717]],
                }
            ]

    logged = []
    monkeypatch.setattr(ocr, "_get_backend", lambda: FakeBackend())
    monkeypatch.setattr(ocr, "logger", SimpleNamespace(debug=logged.append))

    result = ocr.predict(
        np.zeros((720, 1280, 3), dtype=np.uint8), no_crop=True,
    )

    assert result[0]["text"] == f"UID:{uid}"
    assert uid not in repr(logged)
    assert logged[0][0]["text"] == "<redacted_home_profile_id>"


def test_invalid_provider_raises_runtime_error_and_does_not_cache_backend(
    monkeypatch,
):
    import core.image.ocr as ocr

    ocr._reset_ocr_backend_for_tests()
    monkeypatch.setenv("AUTO_RESONANCE_OCR_PROVIDER", "invalid")

    with pytest.raises(RuntimeError, match="ocr_model_initialization_failed"):
        ocr._get_backend()

    # The stale backend must not be cached — a subsequent valid
    # configuration must be able to recover.
    assert ocr._backend is None

    # Reset and prove a valid provider still works afterwards.
    monkeypatch.setenv("AUTO_RESONANCE_OCR_PROVIDER", "cpu")
    backend = ocr._get_backend()
    assert backend.name == "ppocr-v4"
    assert backend.provider == "cpu"
