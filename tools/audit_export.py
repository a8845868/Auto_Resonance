"""Create a fail-closed, sanitized audit handoff directory and ZIP."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import zipfile
from pathlib import Path

import cv2 as cv


class SensitiveDataError(RuntimeError):
    pass


TEXT_SUFFIXES = {".txt", ".md", ".json", ".jsonl", ".log", ".csv", ".xml", ".yaml", ".yml", ".patch", ".diff"}
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".bmp", ".webp"}
SENSITIVE_PATTERNS = {
    "authorization": re.compile(r"(?i)authorization\s*[:=]\s*(?!\[REDACTED)[^\r\n]{8,}"),
    "bearer_token": re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/-]{12,}"),
    "cookie": re.compile(r"(?i)(?:cookie|set-cookie)\s*[:=]\s*[^\r\n]{8,}"),
    "email": re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b"),
    "phone": re.compile(r"(?<!\d)(?:\+?86[- ]?)?1[3-9]\d{9}(?!\d)"),
    "uid": re.compile(r"(?i)\bUID\s*[:：#]?\s*(?!\[REDACTED)[A-Za-z0-9_-]{6,}\b"),
    "user_profile": re.compile(r"(?i)C:\\Users\\(?!Public(?:\\|$)|%USERPROFILE%)[^\\\r\n]+"),
}


def _redact_text(text: str, uid_values: tuple[str, ...]) -> str:
    result = text
    for uid in sorted({str(value) for value in uid_values if str(value)}, key=len, reverse=True):
        result = result.replace(uid, "[REDACTED_ACCOUNT_ID]")
    result = re.sub(
        r"(?i)(\bUID\s*[:：#]?\s*)[A-Za-z0-9_-]{6,}",
        r"\1[REDACTED_ACCOUNT_ID]",
        result,
    )
    result = re.sub(
        r"(?i)C:\\Users\\[^\\/\r\n]+",
        "%USERPROFILE%",
        result,
    )
    # Account-contact data is safe to replace deterministically.  Secrets and
    # authentication headers are intentionally *not* auto-redacted: the gate
    # below must fail closed if they are ever present in a proposed export.
    result = SENSITIVE_PATTERNS["email"].sub("[REDACTED_EMAIL]", result)
    result = SENSITIVE_PATTERNS["phone"].sub("[REDACTED_PHONE]", result)
    return result


def scan_sensitive_text(text: str) -> list[str]:
    return [name for name, pattern in SENSITIVE_PATTERNS.items() if pattern.search(text)]


def _write_masked_image(source: Path, target: Path, masks: tuple[tuple[int, int, int, int], ...]) -> None:
    image = cv.imread(str(source), cv.IMREAD_UNCHANGED)
    if image is None:
        raise SensitiveDataError(f"cannot decode image for masking: {source}")
    height, width = image.shape[:2]
    for x1, y1, x2, y2 in masks:
        left, right = sorted((max(0, int(x1)), min(width, int(x2))))
        top, bottom = sorted((max(0, int(y1)), min(height, int(y2))))
        if left < right and top < bottom:
            image[top:bottom, left:right] = 0
    target.parent.mkdir(parents=True, exist_ok=True)
    if not cv.imwrite(str(target), image):
        raise SensitiveDataError(f"cannot write masked image: {target}")


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _write_hash_manifest(root: Path) -> None:
    lines = []
    for path in sorted(item for item in root.rglob("*") if item.is_file() and item.name != "SHA256SUMS.txt"):
        relative = path.relative_to(root).as_posix()
        lines.append(f"{_hash_file(path)}  {relative}")
    (root / "SHA256SUMS.txt").write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")


def verify_hash_manifest(root: Path) -> bool:
    manifest = root / "SHA256SUMS.txt"
    try:
        lines = manifest.read_text(encoding="utf-8").splitlines()
    except OSError:
        return False
    for line in lines:
        if "  " not in line:
            return False
        expected, relative = line.split("  ", 1)
        if "\\" in relative:
            return False
        target = root / Path(relative)
        if not target.is_file() or _hash_file(target) != expected:
            return False
    return True


def build_shareable_audit(
    source: Path,
    destination: Path,
    *,
    uid_values: tuple[str, ...] = (),
    image_masks: tuple[tuple[int, int, int, int], ...] = (),
    create_zip: bool = False,
) -> Path:
    """Sanitize into a new directory and publish only after the safety gate passes."""

    source = Path(source).resolve()
    destination = Path(destination).resolve()
    if source == destination or source in destination.parents:
        raise ValueError("audit destination must be outside the raw source")
    staging = destination.with_name(destination.name + ".staging")
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    try:
        for path in sorted(item for item in source.rglob("*") if item.is_file()):
            relative = path.relative_to(source)
            target = staging / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            suffix = path.suffix.lower()
            if suffix in TEXT_SUFFIXES:
                text = path.read_text(encoding="utf-8", errors="replace")
                target.write_text(_redact_text(text, uid_values), encoding="utf-8", newline="\n")
            elif suffix in IMAGE_SUFFIXES and image_masks:
                _write_masked_image(path, target, image_masks)
            else:
                shutil.copy2(path, target)
        hits = []
        for path in sorted(item for item in staging.rglob("*") if item.is_file() and item.suffix.lower() in TEXT_SUFFIXES):
            for name in scan_sensitive_text(path.read_text(encoding="utf-8", errors="replace")):
                hits.append(f"{path.relative_to(staging).as_posix()}:{name}")
        if hits:
            raise SensitiveDataError("sensitive data gate failed: " + ", ".join(hits))
        manifest = {
            "schema_version": 1,
            "shareable": True,
            "sanitization": {
                "account_identifiers": "[REDACTED_ACCOUNT_ID]",
                "user_profiles": "%USERPROFILE%",
                "image_masks": [list(mask) for mask in image_masks],
                "sensitive_gate_hits": 0,
            },
        }
        (staging / "SHAREABLE-MANIFEST.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8", newline="\n",
        )
        _write_hash_manifest(staging)
        if destination.exists():
            shutil.rmtree(destination)
        staging.replace(destination)
        if create_zip:
            zip_path = destination.with_suffix(".zip")
            if zip_path.exists():
                zip_path.unlink()
            with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                for path in sorted(item for item in destination.rglob("*") if item.is_file()):
                    archive.write(path, (Path(destination.name) / path.relative_to(destination)).as_posix())
        return destination
    except Exception:
        if staging.exists():
            shutil.rmtree(staging)
        raise


__all__ = [
    "SensitiveDataError",
    "build_shareable_audit",
    "scan_sensitive_text",
    "verify_hash_manifest",
]
