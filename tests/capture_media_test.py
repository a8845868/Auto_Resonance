import base64
import hashlib
import io

import pytest
from PIL import Image

from core.services.capture_media import decode_capture_data_url


def payload(media_type):
    image = Image.new("RGB", (9, 7), (21, 83, 144))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG" if media_type == "image/png" else "JPEG")
    raw = buffer.getvalue()
    return f"data:{media_type};base64,{base64.b64encode(raw).decode()}", raw


def test_png_is_preserved_without_normalization():
    url, raw = payload("image/png")
    media = decode_capture_data_url(url)
    assert media.source_payload_sha256 == hashlib.sha256(raw).hexdigest()
    assert media.source_payload_bytes == raw
    assert media.normalization_applied is False


def test_jpeg_preserves_source_and_records_normalized_png_hash():
    url, raw = payload("image/jpeg")
    media = decode_capture_data_url(url)
    normalized = io.BytesIO()
    media.image.save(normalized, format="PNG", optimize=False)
    assert media.source_payload_sha256 == hashlib.sha256(raw).hexdigest()
    assert media.normalization_applied is True
    assert media.normalized_format == "image/png"
    assert media.normalized_png_sha256 == hashlib.sha256(normalized.getvalue()).hexdigest()


def test_invalid_mime_and_corrupt_jpeg_are_rejected():
    with pytest.raises(ValueError, match="unsupported"):
        decode_capture_data_url("data:image/webp;base64,AAAA")
    corrupt = base64.b64encode(b"not-jpeg").decode()
    with pytest.raises(ValueError, match="decode_failed"):
        decode_capture_data_url(f"data:image/jpeg;base64,{corrupt}")
