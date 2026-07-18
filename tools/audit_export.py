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
) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    for path in sorted(item for item in source.rglob("*") if item.is_file()):
        relative = path.relative_to(source)
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        suffix = path.suffix.lower()
        if suffix in TEXT_SUFFIXES:
            text = path.read_text(encoding="utf-8", errors="replace")
            target.write_text(_redact_text(text, uid_values), encoding="utf-8", newline="\n")
        elif suffix in IMAGE_SUFFIXES and image_masks:
            _write_masked_image(path, target, image_masks)
        else:
            shutil.copy2(path, target)


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
        _sanitize_tree(baseline_source, safe_baseline, uid_values=uid_values, image_masks=image_masks)
        _sanitize_tree(target_source, safe_target, uid_values=uid_values, image_masks=image_masks)
        baseline_hits, baseline_scan = scan_sensitive_tree(safe_baseline)
        target_hits, target_scan = scan_sensitive_tree(safe_target)
        if baseline_hits or target_hits:
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
            if forward_hits or reverse_hits:
                raise SensitiveDataError("forward/reverse applied tree sensitive data gate failed")
            forward_hash, reverse_hash = _tree_hash(forward_tree), _tree_hash(reverse_tree)
        baseline_hash, target_hash = _tree_hash(safe_baseline), _tree_hash(safe_target)
        if forward_hash != target_hash or reverse_hash != baseline_hash:
            raise SensitiveDataError("safe-tree patch hash mismatch")
        patch_hits, patch_scan = scan_sensitive_tree(patches)
        if patch_hits:
            raise SensitiveDataError("safe-tree patch sensitive data gate failed")

        manifest = {
            "schema_version": 3, "shareable": True,
            "patch_model": "sanitized baseline -> 1 text audit patch -> sanitized target",
            "sanitized_baseline_tree": {"path": "sanitized-baseline", "tree_hash": baseline_hash, "sensitive_hits": 0, **baseline_scan},
            "sanitized_target_tree": {"path": "sanitized-target", "tree_hash": target_hash, "sensitive_hits": 0, **target_scan},
            "forward_tree_hash": forward_hash, "reverse_tree_hash": reverse_hash,
            "forward_sensitive_hits": 0, "reverse_sensitive_hits": 0,
            "binary_patch_count": 0, "atomic_history_reproducible": False,
            "audit_patch_count": 1,
            "scan_stats": {"forward": forward_scan, "reverse": reverse_scan, "patches": patch_scan},
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
    "verify_hash_manifest",
]
