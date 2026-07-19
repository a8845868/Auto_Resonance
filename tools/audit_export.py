"""Create a fail-closed, sanitized audit handoff directory and ZIP."""

from __future__ import annotations

import hashlib
import difflib
import json
import re
import shutil
import subprocess
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Callable, Iterable

import cv2 as cv


class SensitiveDataError(RuntimeError):
    pass


TEXT_SUFFIXES = {
    ".txt", ".md", ".json", ".jsonl", ".log", ".csv", ".xml",
    ".yaml", ".yml", ".patch", ".diff", ".py", ".toml", ".ini",
}
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".bmp", ".webp", ".gif"}
RUNTIME_PATH_NAMES = {
    "artifacts", "logs", "config", "runtime", "runtime_state", "caches",
    "cache", ".pytest_cache", ".pytest-tmp", "dist", "build", "__pycache__",
}
IMAGE_PRIVACY_PATTERNS = {
    "account_identifier": re.compile(r"(?i)(?:uid|account[_ ]?id|\u8d26\u53f7)\s*[:=\uFF1A]?\s*[A-Za-z0-9_-]{4,}"),
    "player_name": re.compile(r"(?i)(?:player[_ ]?name|character[_ ]?name|\u89d2\u8272\u540d|\u73a9\u5bb6\u540d)\s*[:=\uFF1A]?\s*\S+"),
    "balance": re.compile(r"(?i)(?:balance|currency|asset|\u4f59\u989d|\u8d44\u4ea7|\u8d27\u5e01)\s*[:=\uFF1A]?\s*[0-9,]{2,}"),
    "level": re.compile(r"(?i)(?:level|lv\.?|\u7b49\u7ea7)\s*[:=\uFF1A]?\s*\d+"),
    "payment": re.compile(r"(?i)(?:payment|order|\u652f\u4ed8|\u8ba2\u5355)\s*[:=\uFF1A]?\s*\S+"),
}
_USER_PROFILE = re.compile(
    r"(?i)C:[\\/]+Users[\\/]+(?!Public(?:[\\/]|$)|%USERPROFILE%)[^\\/\s\"']+"
)
_POSIX_USER_HOME = re.compile(r"(?i)(?:/Users|/home)/(?!Shared(?:/|$))[^/\s\"']+")
_BINARY_PATCH = re.compile(
    r"(?im)^GIT binary patch\s*$|^(?:literal|delta)\s+\d+\s*$|^Binary files .+ differ\s*$"
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
    "posix_user_home": _POSIX_USER_HOME,
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
    result = _POSIX_USER_HOME.sub("%USERPROFILE%", result)
    # Contact data can be replaced deterministically. Secrets and auth headers
    # are not auto-redacted: the gate below fails closed if they are present.
    result = SENSITIVE_PATTERNS["email"].sub("[REDACTED_EMAIL]", result)
    result = SENSITIVE_PATTERNS["phone"].sub("[REDACTED_PHONE]", result)
    return result


def scan_sensitive_text(text: str) -> list[str]:
    return [name for name, pattern in SENSITIVE_PATTERNS.items() if pattern.search(text)]


def _normalized_relative(path: Path, root: Path) -> str:
    relative = path.relative_to(root).as_posix()
    if relative.startswith("/") or ".." in Path(relative).parts:
        raise SensitiveDataError("audit tree contains path traversal")
    return relative


def _is_runtime_path(relative: str) -> bool:
    return any(part.casefold() in RUNTIME_PATH_NAMES for part in Path(relative).parts)


def _validate_tree_entries(root: Path) -> None:
    folded: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        relative = _normalized_relative(path, root)
        if path.is_symlink():
            raise SensitiveDataError(f"symbolic link forbidden: {relative}")
        key = relative.casefold()
        previous = folded.get(key)
        if previous is not None and previous != relative:
            raise SensitiveDataError(f"case-insensitive path collision: {previous} / {relative}")
        folded[key] = relative


def _image_privacy_hits(texts: Iterable[str]) -> list[str]:
    joined = "\n".join(str(text) for text in texts)
    hits = set(scan_sensitive_text(joined))
    hits.update(name for name, pattern in IMAGE_PRIVACY_PATTERNS.items() if pattern.search(joined))
    return sorted(hits)


def minimize_audit_journal(entry: dict) -> dict:
    """Return the minimum non-account telemetry needed to audit one action."""

    context = str(entry.get("page_context", ""))
    marker_hash = hashlib.sha256(context.encode("utf-8")).hexdigest()[:16]
    allowed = {
        "timestamp", "action_key", "observation_id", "screenshot_hash",
        "page_classifier", "anchor_key", "anchor_bbox", "anchor_source",
        "permit_id", "permit_digest", "correlation_id", "coordinate",
        "final_trajectory", "logical_trajectory", "physical_trajectory",
        "coordinate_space", "geometry_revision", "registry_registered",
        "permit_uses", "permit_max_uses", "allowed", "reason",
    }
    minimized = {key: value for key, value in entry.items() if key in allowed}
    minimized["marker_hash"] = str(entry.get("marker_hash") or marker_hash)
    return minimized


def _mask_digest_fields(value: object) -> object:
    """Remove generated digest entropy without hiding ordinary JSON values."""

    if isinstance(value, dict):
        masked = {}
        for key, child in value.items():
            normalized = str(key).casefold()
            if (
                "hash" in normalized
                or "sha256" in normalized
                or normalized in {"digest", "page_fingerprint"}
            ) and isinstance(child, str):
                masked[key] = "[DIGEST]"
            else:
                masked[key] = _mask_digest_fields(child)
        return masked
    if isinstance(value, list):
        return [_mask_digest_fields(child) for child in value]
    return value


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


def _scan_text_payload(
    text: str,
    *,
    suffix: str,
    filename: str = "",
) -> tuple[set[str], int]:
    if filename.upper() == "SHA256SUMS.TXT":
        # A random hexadecimal digest can contain an 11-digit run that looks
        # like a phone number.  Hashes cannot contain the source plaintext, so
        # scan only the export-relative path column of the manifest.
        text = "\n".join(
            line.split("  ", 1)[1] if "  " in line else line
            for line in text.splitlines()
        )
    decoded_values = 0
    decoded_objects: list[object] = []
    fully_decoded = False
    if suffix == ".json":
        try:
            decoded_objects.append(json.loads(text))
            fully_decoded = True
        except (TypeError, ValueError):
            pass
    elif suffix == ".jsonl":
        fully_decoded = True
        for line in text.splitlines():
            try:
                decoded_objects.append(json.loads(line))
            except (TypeError, ValueError):
                fully_decoded = False
    masked_objects = [_mask_digest_fields(decoded) for decoded in decoded_objects]
    scan_text = text
    if fully_decoded:
        scan_text = "\n".join(
            json.dumps(decoded, ensure_ascii=False, sort_keys=True)
            for decoded in masked_objects
        )
    hits = set(scan_sensitive_text(scan_text))
    for decoded in masked_objects:
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
        names, count = _scan_text_payload(
            text,
            suffix=path.suffix.lower(),
            filename=path.name,
        )
        decoded_values += count
        relative = path.relative_to(root).as_posix()
        hits.extend(f"{relative}:{name}" for name in sorted(names))
    return hits, {
        "text_files_scanned": files_scanned,
        "decoded_json_values_scanned": decoded_values,
    }


def scan_media_tree(
    root: Path,
    *,
    evidence_allowlist: tuple[str, ...] = (),
    image_ocr_provider: Callable[[Path], Iterable[str]] | None = None,
) -> tuple[list[str], dict[str, int]]:
    """Inventory every image and fail closed unless OCR confirms an allowlisted file."""

    hits: list[str] = []
    allowlist = {Path(item).as_posix() for item in evidence_allowlist}
    images = [
        path for path in root.rglob("*")
        if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES
    ]
    for path in sorted(images):
        relative = path.relative_to(root).as_posix()
        if relative not in allowlist:
            hits.append(f"{relative}:image_not_allowlisted")
            continue
        if image_ocr_provider is None:
            hits.append(f"{relative}:image_ocr_unavailable")
            continue
        try:
            names = _image_privacy_hits(image_ocr_provider(path))
        except Exception:
            names = ["image_ocr_failed"]
        hits.extend(f"{relative}:{name}" for name in names)
    return hits, {"images_inventoried": len(images), "allowlisted_images": len(images) - len([h for h in hits if h.endswith('image_not_allowlisted')])}


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
            names, count = _scan_text_payload(
                text,
                suffix=suffix,
                filename=Path(name).name,
            )
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
    _validate_tree_entries(root)
    lines = []
    for path in sorted(item for item in root.rglob("*") if item.is_file() and item.name != "SHA256SUMS.txt"):
        relative = path.relative_to(root).as_posix()
        lines.append(f"{_hash_file(path)}  {relative}")
    (root / "SHA256SUMS.txt").write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")


def verify_hash_manifest(root: Path) -> bool:
    try:
        _validate_tree_entries(root)
    except SensitiveDataError:
        return False
    manifest = root / "SHA256SUMS.txt"
    try:
        lines = manifest.read_text(encoding="utf-8").splitlines()
    except OSError:
        return False
    listed: set[str] = set()
    for line in lines:
        if "  " not in line:
            return False
        expected, relative = line.split("  ", 1)
        if "\\" in relative or not relative or relative.startswith("/") or ".." in Path(relative).parts:
            return False
        if relative.casefold() in {item.casefold() for item in listed}:
            return False
        listed.add(relative)
        target = root / Path(relative)
        if not target.is_file() or _hash_file(target) != expected:
            return False
    actual = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() and path.name != "SHA256SUMS.txt"
    }
    return listed == actual


def verify_zip_exact_set(zip_path: Path) -> bool:
    """Verify the ZIP has one safe root and exactly the SHA-listed members."""

    try:
        with zipfile.ZipFile(zip_path) as archive:
            if archive.testzip() is not None:
                return False
            names = [item.filename for item in archive.infolist() if not item.is_dir()]
            if not names or any("\\" in name or name.startswith("/") or ".." in Path(name).parts for name in names):
                return False
            if len({name.casefold() for name in names}) != len(names):
                return False
            roots = {Path(name).parts[0] for name in names}
            if len(roots) != 1:
                return False
            root = next(iter(roots))
            hash_name = f"{root}/SHA256SUMS.txt"
            if hash_name not in names:
                return False
            lines = archive.read(hash_name).decode("utf-8").splitlines()
            listed: dict[str, str] = {}
            for line in lines:
                if "  " not in line:
                    return False
                expected, relative = line.split("  ", 1)
                if not relative or "\\" in relative or relative.startswith("/") or ".." in Path(relative).parts:
                    return False
                if relative.casefold() in {item.casefold() for item in listed}:
                    return False
                listed[relative] = expected
            actual = {
                name[len(root) + 1:]
                for name in names
                if name != hash_name
            }
            if set(listed) != actual:
                return False
            for relative, expected in listed.items():
                if hashlib.sha256(archive.read(f"{root}/{relative}")).hexdigest() != expected:
                    return False
            return True
    except (OSError, ValueError, zipfile.BadZipFile, UnicodeError):
        return False


def _replace_with_retry(source: Path, destination: Path, *, attempts: int = 5) -> None:
    """Publish an artifact despite short-lived Windows scanner file locks."""

    for attempt in range(attempts):
        try:
            source.replace(destination)
            return
        except PermissionError:
            if attempt + 1 >= attempts:
                raise
            time.sleep(0.05 * (attempt + 1))


def _binary_patch_files(root: Path) -> list[str]:
    found: list[str] = []
    for path in sorted(item for item in Path(root).rglob("*") if item.is_file()):
        if path.suffix.lower() not in {".patch", ".diff", ".txt", ".md"}:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if _BINARY_PATCH.search(text):
            found.append(path.relative_to(root).as_posix())
    return found


def _sanitize_tree(
    source: Path,
    destination: Path,
    *,
    uid_values: tuple[str, ...],
    image_masks: tuple[tuple[int, int, int, int], ...],
    evidence_allowlist: tuple[str, ...] = (),
    image_privacy_manifests: dict[str, dict] | None = None,
    image_ocr_provider: Callable[[Path], Iterable[str]] | None = None,
) -> dict:
    _validate_tree_entries(source)
    allowlist = {Path(item).as_posix() for item in evidence_allowlist}
    manifests = image_privacy_manifests or {}
    image_inventory: list[dict] = []
    excluded: list[str] = []
    destination.mkdir(parents=True, exist_ok=True)
    for path in sorted(item for item in source.rglob("*") if item.is_file()):
        relative_text = _normalized_relative(path, source)
        if _is_runtime_path(relative_text):
            excluded.append(relative_text)
            continue
        relative = Path(relative_text)
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        suffix = path.suffix.lower()
        if suffix in TEXT_SUFFIXES:
            text = path.read_text(encoding="utf-8", errors="replace")
            target.write_text(_redact_text(text, uid_values), encoding="utf-8", newline="\n")
        elif suffix in IMAGE_SUFFIXES:
            if relative_text not in allowlist:
                raise SensitiveDataError(f"image evidence is not allowlisted: {relative_text}")
            manifest = dict(manifests.get(relative_text) or {})
            masks = tuple(
                tuple(map(int, mask))
                for mask in manifest.get("mask_regions", image_masks)
                if isinstance(mask, (list, tuple)) and len(mask) == 4
            )
            required_false = (
                "contains_account_identifier", "contains_player_name",
                "contains_balance", "contains_payment_or_order",
            )
            if (
                not manifest
                or manifest.get("privacy_review_status") != "APPROVED"
                or not masks
                or any(manifest.get(key) is not False for key in required_false)
            ):
                raise SensitiveDataError(f"image privacy manifest or mask invalid: {relative_text}")
            if image_ocr_provider is None:
                raise SensitiveDataError(f"image privacy OCR confirmation unavailable: {relative_text}")
            _write_masked_image(path, target, masks)
            try:
                image_hits = _image_privacy_hits(image_ocr_provider(target))
            except Exception as error:
                raise SensitiveDataError(
                    f"image privacy OCR confirmation failed: {relative_text}:{type(error).__name__}"
                ) from error
            if image_hits:
                raise SensitiveDataError(
                    f"image privacy gate failed: {relative_text}:{','.join(image_hits)}"
                )
            image_inventory.append({
                "source_kind": str(manifest.get("source_kind", "")),
                "purpose": str(manifest.get("purpose", "")),
                "sanitized_path": relative_text,
                "mask_regions": [list(mask) for mask in masks],
                "sanitized_sha256": _hash_file(target),
                "privacy_review_status": "APPROVED",
                **{key: False for key in required_false},
            })
        else:
            shutil.copy2(path, target)
    if image_inventory:
        (destination / "IMAGE-PRIVACY-MANIFEST.json").write_text(
            json.dumps({"images": image_inventory}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8", newline="\n",
        )
    return {"images": image_inventory, "excluded_paths": excluded}


def _tree_hash(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        relative = path.relative_to(root).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        digest.update(bytes.fromhex(_hash_file(path)))
    return digest.hexdigest()


def _text_tree_diff(baseline: Path, target: Path) -> str:
    lines: list[str] = []
    baseline_files = {path.relative_to(baseline).as_posix(): path for path in baseline.rglob("*") if path.is_file()}
    target_files = {path.relative_to(target).as_posix(): path for path in target.rglob("*") if path.is_file()}
    for relative in sorted(baseline_files.keys() | target_files.keys()):
        old_path, new_path = baseline_files.get(relative), target_files.get(relative)
        old_bytes = old_path.read_bytes() if old_path else b""
        new_bytes = new_path.read_bytes() if new_path else b""
        if old_path and new_path and old_bytes == new_bytes:
            continue
        if b"\0" in old_bytes or b"\0" in new_bytes:
            raise SensitiveDataError(f"binary patch forbidden for shareable tree: {relative}")
        try:
            old_text = old_bytes.decode("utf-8")
            new_text = new_bytes.decode("utf-8")
        except UnicodeDecodeError as error:
            raise SensitiveDataError(f"binary patch forbidden for shareable tree: {relative}") from error
        lines.append(f"diff --git a/{relative} b/{relative}\n")
        if old_path is None:
            lines.append("new file mode 100644\n")
        elif new_path is None:
            lines.append("deleted file mode 100644\n")
        lines.extend(difflib.unified_diff(
            old_text.splitlines(keepends=True), new_text.splitlines(keepends=True),
            fromfile=f"a/{relative}" if old_path else "/dev/null",
            tofile=f"b/{relative}" if new_path else "/dev/null",
            lineterm="\n",
        ))
    return "".join(lines).replace("\r\n", "\n")


def _apply_text_patch(source_tree: Path, patch: Path, output: Path, *, reverse: bool) -> None:
    shutil.copytree(source_tree, output, dirs_exist_ok=True)
    command = ["git", "apply", "--no-index", "--whitespace=nowarn"]
    if reverse:
        command.append("--reverse")
    command.append(str(patch))
    completed = subprocess.run(
        command, cwd=output, text=True, encoding="utf-8", errors="replace",
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
    )
    if completed.returncode:
        raise SensitiveDataError(
            "safe-tree patch application failed: "
            + (completed.stderr.strip().splitlines()[-1] if completed.stderr.strip() else "git apply")
        )
    # Git on Windows may honor core.autocrlf while materializing patched files;
    # safe-tree identity is defined over canonical LF bytes.
    for path in sorted(item for item in output.rglob("*") if item.is_file()):
        if path.suffix.lower() in TEXT_SUFFIXES:
            text = path.read_text(encoding="utf-8", errors="strict")
            path.write_text(text, encoding="utf-8", newline="\n")


def build_reversible_audit_package(
    baseline_source: Path,
    target_source: Path,
    destination: Path,
    *,
    uid_values: tuple[str, ...] = (),
    image_masks: tuple[tuple[int, int, int, int], ...] = (),
    evidence_allowlist: tuple[str, ...] = (),
    image_privacy_manifests: dict[str, dict] | None = None,
    image_ocr_provider: Callable[[Path], Iterable[str]] | None = None,
    create_zip: bool = False,
) -> Path:
    """Publish one squashed text patch between two independently safe trees."""

    baseline_source = Path(baseline_source).resolve()
    target_source = Path(target_source).resolve()
    destination = Path(destination).resolve()
    staging = destination.with_name(destination.name + ".staging")
    zip_path = destination.with_suffix(".zip")
    staging_zip = zip_path.with_name(zip_path.name + ".staging")
    if _binary_patch_files(baseline_source) or _binary_patch_files(target_source):
        raise SensitiveDataError("binary patch forbidden because reverse preimage cannot be proven safe")
    if staging.exists():
        shutil.rmtree(staging)
    staging_zip.unlink(missing_ok=True)
    staging.mkdir(parents=True)
    try:
        safe_baseline = staging / "sanitized-baseline"
        safe_target = staging / "sanitized-target"
        baseline_inventory = _sanitize_tree(
            baseline_source, safe_baseline, uid_values=uid_values,
            image_masks=image_masks, evidence_allowlist=evidence_allowlist,
            image_privacy_manifests=image_privacy_manifests,
            image_ocr_provider=image_ocr_provider,
        )
        target_inventory = _sanitize_tree(
            target_source, safe_target, uid_values=uid_values,
            image_masks=image_masks, evidence_allowlist=evidence_allowlist,
            image_privacy_manifests=image_privacy_manifests,
            image_ocr_provider=image_ocr_provider,
        )
        baseline_hits, baseline_scan = scan_sensitive_tree(safe_baseline)
        target_hits, target_scan = scan_sensitive_tree(safe_target)
        baseline_media_hits, baseline_media_scan = scan_media_tree(
            safe_baseline, evidence_allowlist=evidence_allowlist,
            image_ocr_provider=image_ocr_provider,
        )
        target_media_hits, target_media_scan = scan_media_tree(
            safe_target, evidence_allowlist=evidence_allowlist,
            image_ocr_provider=image_ocr_provider,
        )
        if baseline_hits or target_hits or baseline_media_hits or target_media_hits:
            raise SensitiveDataError("sanitized baseline/target sensitive data gate failed")
        _validate_semantics(safe_baseline)
        _validate_semantics(safe_target)

        patches = staging / "patches"
        patches.mkdir()
        diff_text = _text_tree_diff(safe_baseline, safe_target)
        full_diff = patches / "full-safe-tree.diff"
        full_diff.write_text(diff_text, encoding="utf-8", newline="\n")
        mail_patch = patches / "0001-safe-tree-audit.patch"
        mail_patch.write_text(
            "From audit-safe-tree Mon Sep 17 00:00:00 2001\n"
            "From: [REDACTED_EMAIL]\n"
            "Date: Thu, 1 Jan 1970 00:00:00 +0000\n"
            "Subject: [PATCH] audit safe-tree delta\n\n---\n"
            + diff_text,
            encoding="utf-8", newline="\n",
        )
        if _binary_patch_files(patches):
            raise SensitiveDataError("binary patch forbidden in generated shareable artifacts")

        with tempfile.TemporaryDirectory(prefix="audit-forward-") as forward_temp, tempfile.TemporaryDirectory(prefix="audit-reverse-") as reverse_temp:
            forward_tree, reverse_tree = Path(forward_temp), Path(reverse_temp)
            _apply_text_patch(safe_baseline, full_diff, forward_tree, reverse=False)
            _apply_text_patch(safe_target, full_diff, reverse_tree, reverse=True)
            forward_hits, forward_scan = scan_sensitive_tree(forward_tree)
            reverse_hits, reverse_scan = scan_sensitive_tree(reverse_tree)
            forward_media_hits, forward_media_scan = scan_media_tree(
                forward_tree, evidence_allowlist=evidence_allowlist,
                image_ocr_provider=image_ocr_provider,
            )
            reverse_media_hits, reverse_media_scan = scan_media_tree(
                reverse_tree, evidence_allowlist=evidence_allowlist,
                image_ocr_provider=image_ocr_provider,
            )
            if forward_hits or reverse_hits or forward_media_hits or reverse_media_hits:
                raise SensitiveDataError("forward/reverse applied tree sensitive data gate failed")
            forward_hash, reverse_hash = _tree_hash(forward_tree), _tree_hash(reverse_tree)
        baseline_hash, target_hash = _tree_hash(safe_baseline), _tree_hash(safe_target)
        if forward_hash != target_hash or reverse_hash != baseline_hash:
            raise SensitiveDataError("safe-tree patch hash mismatch")
        patch_hits, patch_scan = scan_sensitive_tree(patches)
        if patch_hits:
            raise SensitiveDataError("safe-tree patch sensitive data gate failed")

        manifest = {
            "schema_version": 4, "shareable": True,
            "patch_model": "sanitized baseline -> 1 text audit patch -> sanitized target",
            "sanitized_baseline_tree": {"path": "sanitized-baseline", "tree_hash": baseline_hash, "sensitive_hits": 0, **baseline_scan, **baseline_media_scan},
            "sanitized_target_tree": {"path": "sanitized-target", "tree_hash": target_hash, "sensitive_hits": 0, **target_scan, **target_media_scan},
            "forward_tree_hash": forward_hash, "reverse_tree_hash": reverse_hash,
            "forward_sensitive_hits": 0, "reverse_sensitive_hits": 0,
            "binary_patch_count": 0, "atomic_history_reproducible": False,
            "audit_patch_count": 1,
            "runtime_artifacts_included": 0,
            "image_inventory": {
                "baseline": baseline_inventory["images"],
                "target": target_inventory["images"],
            },
            "scan_stats": {
                "forward": {**forward_scan, **forward_media_scan},
                "reverse": {**reverse_scan, **reverse_media_scan},
                "patches": patch_scan,
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
            with zipfile.ZipFile(staging_zip) as archive:
                if archive.testzip() is not None:
                    raise SensitiveDataError("ZIP CRC validation failed")
            zip_hits, _ = _scan_zip(staging_zip)
            if zip_hits:
                raise SensitiveDataError("ZIP sensitive data gate failed")
            if not verify_zip_exact_set(staging_zip):
                raise SensitiveDataError("ZIP exact-set hash manifest gate failed")
        if destination.exists():
            shutil.rmtree(destination)
        _replace_with_retry(staging, destination)
        if create_zip:
            zip_path.unlink(missing_ok=True)
            _replace_with_retry(staging_zip, zip_path)
        return destination
    except Exception:
        if staging.exists():
            shutil.rmtree(staging)
        staging_zip.unlink(missing_ok=True)
        raise


def build_shareable_audit(
    source: Path,
    destination: Path,
    *,
    uid_values: tuple[str, ...] = (),
    image_masks: tuple[tuple[int, int, int, int], ...] = (),
    evidence_allowlist: tuple[str, ...] = (),
    image_privacy_manifests: dict[str, dict] | None = None,
    image_ocr_provider: Callable[[Path], Iterable[str]] | None = None,
    create_zip: bool = False,
) -> Path:
    """Sanitize into staging and publish only after all gates pass."""

    source = Path(source).resolve()
    destination = Path(destination).resolve()
    if source == destination or source in destination.parents:
        raise ValueError("audit destination must be outside the raw source")
    if _binary_patch_files(source):
        raise SensitiveDataError("binary patch forbidden because a sensitive preimage may be reversible")
    staging = destination.with_name(destination.name + ".staging")
    zip_path = destination.with_suffix(".zip")
    staging_zip = zip_path.with_name(zip_path.name + ".staging")
    if staging.exists():
        shutil.rmtree(staging)
    staging_zip.unlink(missing_ok=True)
    staging.mkdir(parents=True)
    try:
        inventory = _sanitize_tree(
            source, staging, uid_values=uid_values, image_masks=image_masks,
            evidence_allowlist=evidence_allowlist,
            image_privacy_manifests=image_privacy_manifests,
            image_ocr_provider=image_ocr_provider,
        )

        hits, scan_stats = scan_sensitive_tree(staging)
        media_hits, media_stats = scan_media_tree(
            staging, evidence_allowlist=evidence_allowlist,
            image_ocr_provider=image_ocr_provider,
        )
        if hits or media_hits:
            raise SensitiveDataError("sensitive data gate failed: " + ", ".join(hits))
        semantic_stats = _validate_semantics(staging)
        manifest = {
            "schema_version": 4,
            "shareable": True,
            "sanitization": {
                "account_identifiers": "[REDACTED_ACCOUNT_ID]",
                "user_profiles": "%USERPROFILE%",
                "image_masks": [list(mask) for mask in image_masks],
                "sensitive_gate_hits": 0,
                "zip_second_pass_sensitive_gate_hits": 0,
                **scan_stats,
                **media_stats,
                **semantic_stats,
            },
            "runtime_artifacts_included": 0,
            "image_inventory": inventory["images"],
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
            if not verify_zip_exact_set(staging_zip):
                raise SensitiveDataError("ZIP exact-set hash manifest gate failed")

        if destination.exists():
            shutil.rmtree(destination)
        _replace_with_retry(staging, destination)
        if create_zip:
            zip_path.unlink(missing_ok=True)
            _replace_with_retry(staging_zip, zip_path)
        return destination
    except Exception:
        if staging.exists():
            shutil.rmtree(staging)
        staging_zip.unlink(missing_ok=True)
        raise


__all__ = [
    "SensitiveDataError",
    "build_reversible_audit_package",
    "build_shareable_audit",
    "scan_sensitive_text",
    "scan_sensitive_tree",
    "scan_media_tree",
    "minimize_audit_journal",
    "verify_hash_manifest",
    "verify_zip_exact_set",
]
