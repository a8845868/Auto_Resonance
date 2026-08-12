"""PNG/JPEG capture payload decoding without audit-system dependencies."""

from __future__ import annotations

import base64
import binascii
import hashlib
import io
from dataclasses import dataclass

from PIL import Image


@dataclass(frozen=True)
class DecodedCaptureMedia:
    image: Image.Image
    source_media_type: str
    source_payload_sha256: str
    source_payload_bytes: bytes
    normalization_applied: bool
    normalized_format: str | None = None
    normalized_png_sha256: str | None = None

    def metadata(self) -> dict[str, object]:
        return {
            "source_media_type": self.source_media_type,
            "source_payload_sha256": self.source_payload_sha256,
            "normalization_applied": self.normalization_applied,
            "normalized_format": self.normalized_format,
            "normalized_png_sha256": self.normalized_png_sha256,
        }


def decode_capture_data_url(data_url: str) -> DecodedCaptureMedia:
    if not isinstance(data_url, str) or "," not in data_url:
        raise ValueError("capture_payload_data_url_invalid")
    header, encoded_payload = data_url.split(",", 1)
    if not header.startswith("data:") or not header.endswith(";base64"):
        raise ValueError("capture_payload_data_url_invalid")
    media_type = header[5:-7].lower()
    if media_type not in {"image/png", "image/jpeg"}:
        raise ValueError("capture_media_type_unsupported")
    try:
        source_bytes = base64.b64decode(encoded_payload, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError("capture_payload_base64_invalid") from exc
    source_hash = hashlib.sha256(source_bytes).hexdigest()
    try:
        with Image.open(io.BytesIO(source_bytes)) as decoded:
            decoded.load()
            expected = "PNG" if media_type == "image/png" else "JPEG"
            if str(decoded.format or "").upper() != expected:
                raise ValueError("capture_media_type_mismatch")
            if decoded.width < 1 or decoded.height < 1:
                raise ValueError("capture_payload_dimensions_invalid")
            image = decoded.copy()
    except ValueError:
        raise
    except (OSError, SyntaxError) as exc:
        raise ValueError("capture_payload_decode_failed") from exc
    if media_type == "image/png":
        return DecodedCaptureMedia(
            image=image,
            source_media_type=media_type,
            source_payload_sha256=source_hash,
            source_payload_bytes=source_bytes,
            normalization_applied=False,
        )
    normalized = image.convert("RGB")
    buffer = io.BytesIO()
    normalized.save(buffer, format="PNG", optimize=False)
    return DecodedCaptureMedia(
        image=normalized,
        source_media_type=media_type,
        source_payload_sha256=source_hash,
        source_payload_bytes=source_bytes,
        normalization_applied=True,
        normalized_format="image/png",
        normalized_png_sha256=hashlib.sha256(buffer.getvalue()).hexdigest(),
    )


__all__ = ["DecodedCaptureMedia", "decode_capture_data_url"]
