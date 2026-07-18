from __future__ import annotations

import json
from pathlib import Path
import zipfile

import pytest

import tools.audit_export as audit


def _trees(tmp_path: Path) -> tuple[Path, Path]:
    baseline = tmp_path / "raw-baseline"; target = tmp_path / "raw-target"
    baseline.mkdir(); target.mkdir()
    (baseline / "sample.py").write_text(
        "author = 'reviewer@example.test'\npath = r'C:\\Users\\Auditor\\repo'\nvalue = 1\n",
        encoding="utf-8",
    )
    (target / "sample.py").write_text(
        "author = 'reviewer@example.test'\npath = r'C:\\Users\\Auditor\\repo'\nvalue = 2\n",
        encoding="utf-8",
    )
    return baseline, target


def test_binary_patch_reverse_cannot_recover_sensitive_preimage(tmp_path: Path):
    baseline, target = _trees(tmp_path)
    (target / "unsafe.patch").write_text(
        "diff --git a/secret.py b/secret.py\nGIT binary patch\nliteral 12\nABCD\n",
        encoding="utf-8",
    )
    with pytest.raises(audit.SensitiveDataError, match="binary patch"):
        audit.build_reversible_audit_package(baseline, target, tmp_path / "out")
    assert not (tmp_path / "out").exists()


def test_shareable_package_scans_sanitized_baseline_and_target(tmp_path: Path):
    baseline, target = _trees(tmp_path)
    output = audit.build_reversible_audit_package(baseline, target, tmp_path / "out")
    manifest = json.loads((output / "SHAREABLE-MANIFEST.json").read_text(encoding="utf-8"))
    assert manifest["sanitized_baseline_tree"]["sensitive_hits"] == 0
    assert manifest["sanitized_target_tree"]["sensitive_hits"] == 0


def test_shareable_package_scans_forward_and_reverse_applied_trees(tmp_path: Path):
    baseline, target = _trees(tmp_path)
    output = audit.build_reversible_audit_package(baseline, target, tmp_path / "out")
    manifest = json.loads((output / "SHAREABLE-MANIFEST.json").read_text(encoding="utf-8"))
    assert manifest["forward_tree_hash"] == manifest["sanitized_target_tree"]["tree_hash"]
    assert manifest["reverse_tree_hash"] == manifest["sanitized_baseline_tree"]["tree_hash"]
    assert manifest["reverse_sensitive_hits"] == 0


def test_shareable_package_rejects_binary_patch_with_sensitive_preimage(tmp_path: Path):
    baseline, target = _trees(tmp_path)
    (baseline / "history.patch").write_text(
        "From: Real Author <real.author@example.test>\nGIT binary patch\nliteral 3\nXYZ\n",
        encoding="utf-8",
    )
    with pytest.raises(audit.SensitiveDataError, match="binary patch"):
        audit.build_reversible_audit_package(baseline, target, tmp_path / "out", create_zip=True)
    assert not (tmp_path / "out.zip").exists()


def test_hash_manifest_digest_is_not_misclassified_as_phone(tmp_path: Path):
    package = tmp_path / "package"
    package.mkdir()
    (package / "safe.py").write_text("value = 1\n", encoding="utf-8")
    (package / "SHA256SUMS.txt").write_text(
        "abc13800138000def  safe.py\n",
        encoding="utf-8",
    )
    hits, _ = audit.scan_sensitive_tree(package)
    assert hits == []

    archive_path = tmp_path / "package.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.write(package / "safe.py", "package/safe.py")
        archive.write(package / "SHA256SUMS.txt", "package/SHA256SUMS.txt")
    zip_hits, _ = audit._scan_zip(archive_path)
    assert zip_hits == []
