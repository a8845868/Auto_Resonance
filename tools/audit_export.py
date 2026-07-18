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


TEXT_SUFFIXES = {
    ".txt", ".md", ".json", ".jsonl", ".log", ".csv", ".xml",
    ".yaml", ".yml", ".patch", ".diff", ".py", ".toml", ".ini",
}
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".bmp", ".webp"}
_USER_PROFILE = re.compile(
    r"(?i)C:[\\/]+Users[\\/]+(?!Public(?:[\\/]|$)|%USERPROFILE%)[^\\/\s\"']+"
)
SENSITIVE_PATTERNS = {
    "authorization": re.compile(r"(?i)authorization\s*[:=]\s*(?!\[REDACTED)[^\r\n]{8,}"),
    "bearer_token": re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/-]{12,}"),
    "cookie": re.compile(r"(?i)(?:cookie|set-cookie)\s*[:=]\s*[^\r\n]{8,}"),
    "email": re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b"),
    "phone": re.compile(r"(?<!\d)(?:\+?86[- ]?)?1[3-9]\d{9}(?!\d)"),
    # The separator is mandatory so Python names such as uid_values are not
    # interpreted as account labels.
    "uid": re.compile(
        r"(?i)\bUID\s*[:：#]\s*(?!\[REDACTED)[A-Za-z0-9_-]{6,}\b"
    ),
    "user_profile": _USER_PROFILE,
}


def _redact_text(text: str, uid_values: tuple[str, ...]) -> str:
    result = text
    for uid in sorted({str(value) for value in uid_values if str(value)}, key=len, reverse=True):
        result = re.sub(
            rf"(?<![A-Za-z0-9_]){re.escape(uid)}(?![A-Za-z0-9_])",
            "[REDACTED_ACCOUNT_ID]",
            result,
        )
    result = re.sub(
        r"(?i)(\bUID\s*[:：#]\s*)[A-Za-z0-9_-]{6,}",
        r"\1[REDACTED_ACCOUNT_ID]",
        result,
    )
    # [\\/]+ also matches the doubled backslashes in JSON source text.
    result = _USER_PROFILE.sub("%USERPROFILE%", result)
    # Contact data can be replaced deterministically. Secrets and auth headers
    # are not auto-redacted: the gate below fails closed if they are present.
    result = SENSITIVE_PATTERNS["email"].sub("[REDACTED_EMAIL]", result)
    result = SENSITIVE_PATTERNS["phone"].sub("[REDACTED_PHONE]", result)
    return result


def scan_sensitive_text(text: str) -> list[str]:
    return [name for name, pattern in SENSITIVE_PATTERNS.items() if pattern.search(text)]


def _json_string_values(value: object):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, child in value.items():
            yield str(key)
            yield from _json_string_values(child)
    elif isinstance(value, list):
        for child in value:
            yield from _json_string_values(child)


def _scan_text_payload(text: str, *, suffix: str) -> tuple[set[str], int]:
    hits = set(scan_sensitive_text(text))
    decoded_values = 0
    decoded_objects: list[object] = []
    if suffix == ".json":
        try:
            decoded_objects.append(json.loads(text))
        except (TypeError, ValueError):
            pass
    elif suffix == ".jsonl":
        for line in text.splitlines():
            try:
                decoded_objects.append(json.loads(line))
            except (TypeError, ValueError):
                continue
    for decoded in decoded_objects:
        for value in _json_string_values(decoded):
            decoded_values += 1
            hits.update(scan_sensitive_text(value))
    return hits, decoded_values


def scan_sensitive_tree(root: Path) -> tuple[list[str], dict[str, int]]:
    """Scan raw text and decoded JSON values using export-relative paths."""

    root = Path(root)
    hits: list[str] = []
    files_scanned = 0
    decoded_values = 0
    for path in sorted(
        item
        for item in root.rglob("*")
        if item.is_file() and item.suffix.lower() in TEXT_SUFFIXES
    ):
        files_scanned += 1
        text = path.read_text(encoding="utf-8", errors="replace")
        names, count = _scan_text_payload(text, suffix=path.suffix.lower())
        decoded_values += count
        relative = path.relative_to(root).as_posix()
        hits.extend(f"{relative}:{name}" for name in sorted(names))
    return hits, {
        "text_files_scanned": files_scanned,
        "decoded_json_values_scanned": decoded_values,
    }


def _validate_semantics(root: Path) -> dict[str, int]:
    compiled = 0
    failures: list[str] = []
    for path in sorted(root.rglob("*.py")):
        relative = path.relative_to(root).as_posix()
        try:
            compile(path.read_text(encoding="utf-8"), relative, "exec")
        except (OSError, SyntaxError, UnicodeError) as error:
            failures.append(f"{relative}:{type(error).__name__}")
        else:
            compiled += 1
    if failures:
        raise SensitiveDataError("semantic validation failed: " + ", ".join(failures))
    return {"python_files_compiled": compiled, "semantic_failures": 0}


def _scan_zip(zip_path: Path) -> tuple[list[str], dict[str, int]]:
    hits: list[str] = []
    files_scanned = 0
    decoded_values = 0
    with zipfile.ZipFile(zip_path) as archive:
        for name in sorted(archive.namelist()):
            suffix = Path(name).suffix.lower()
            if suffix not in TEXT_SUFFIXES:
                continue
            files_scanned += 1
            text = archive.read(name).decode("utf-8", errors="replace")
            names, count = _scan_text_payload(text, suffix=suffix)
            decoded_values += count
            hits.extend(f"{name}:{kind}" for kind in sorted(names))
    return hits, {
        "zip_text_files_scanned": files_scanned,
        "zip_decoded_json_values_scanned": decoded_values,
    }


def _write_masked_image(source: Path, target: Path, masks: tuple[tuple[int, int, int, int], ...]) -> None:
    image = cv.imread(str(source), cv.IMREAD_UNCHANGED)
    if image is None:
        raise SensitiveDataError(f"cannot decode image for masking: {source.name}")
    height, width = image.shape[:2]
    for x1, y1, x2, y2 in masks:
        left, right = sorted((max(0, int(x1)), min(width, int(x2))))
        top, bottom = sorted((max(0, int(y1)), min(height, int(y2))))
        if left < right and top < bottom:
            image[top:bottom, left:right] = 0
    target.parent.mkdir(parents=True, exist_ok=True)
    if not cv.imwrite(str(target), image):
        raise SensitiveDataError(f"cannot write masked image: {target.name}")


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
    """Sanitize into staging and publish only after all gates pass."""

    source = Path(source).resolve()
    destination = Path(destination).resolve()
    if source == destination or source in destination.parents:
        raise ValueError("audit destination must be outside the raw source")
    staging = destination.with_name(destination.name + ".staging")
    zip_path = destination.with_suffix(".zip")
    staging_zip = zip_path.with_name(zip_path.name + ".staging")
    if staging.exists():
        shutil.rmtree(staging)
    staging_zip.unlink(missing_ok=True)
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

        hits, scan_stats = scan_sensitive_tree(staging)
        if hits:
            raise SensitiveDataError("sensitive data gate failed: " + ", ".join(hits))
        semantic_stats = _validate_semantics(staging)
        manifest = {
            "schema_version": 2,
            "shareable": True,
            "sanitization": {
                "account_identifiers": "[REDACTED_ACCOUNT_ID]",
                "user_profiles": "%USERPROFILE%",
                "image_masks": [list(mask) for mask in image_masks],
                "sensitive_gate_hits": 0,
                "zip_second_pass_sensitive_gate_hits": 0,
                **scan_stats,
                **semantic_stats,
            },
        }
        (staging / "SHAREABLE-MANIFEST.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8", newline="\n",
        )
        _write_hash_manifest(staging)

        if create_zip:
            with zipfile.ZipFile(staging_zip, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                for path in sorted(item for item in staging.rglob("*") if item.is_file()):
                    archive.write(path, (Path(destination.name) / path.relative_to(staging)).as_posix())
            zip_hits, _zip_stats = _scan_zip(staging_zip)
            if zip_hits:
                raise SensitiveDataError("ZIP sensitive data gate failed: " + ", ".join(zip_hits))

        if destination.exists():
            shutil.rmtree(destination)
        staging.replace(destination)
        if create_zip:
            zip_path.unlink(missing_ok=True)
            staging_zip.replace(zip_path)
        return destination
    except Exception:
        if staging.exists():
            shutil.rmtree(staging)
        staging_zip.unlink(missing_ok=True)
        raise


__all__ = [
    "SensitiveDataError",
    "build_shareable_audit",
    "scan_sensitive_text",
    "scan_sensitive_tree",
    "verify_hash_manifest",
]
