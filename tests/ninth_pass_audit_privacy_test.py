from __future__ import annotations

import json
import zipfile
from pathlib import Path

import cv2 as cv
import numpy as np
import pytest

import tools.audit_export as audit


def _trees(tmp_path: Path) -> tuple[Path, Path]:
    baseline = tmp_path / "baseline"
    target = tmp_path / "target"
    baseline.mkdir()
    target.mkdir()
    (baseline / "sample.py").write_text("value = 1\n", encoding="utf-8")
    (target / "sample.py").write_text("value = 2\n", encoding="utf-8")
    return baseline, target


def _image(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    assert cv.imwrite(str(path), np.full((80, 160, 3), 255, np.uint8))


def _privacy_manifest(path: str) -> dict:
    return {
        "source_kind": "synthetic_test",
        "purpose": "privacy gate regression",
        "sanitized_path": path,
        "mask_regions": [[0, 0, 160, 80]],
        "privacy_review_status": "APPROVED",
        "contains_account_identifier": False,
        "contains_player_name": False,
        "contains_balance": False,
        "contains_payment_or_order": False,
    }


def test_shareable_tree_excludes_runtime_artifacts_by_default(tmp_path: Path):
    baseline, target = _trees(tmp_path)
    for tree in (baseline, target):
        (tree / "artifacts").mkdir()
        (tree / "artifacts" / "private.txt").write_text("runtime", encoding="utf-8")
        (tree / "logs").mkdir()
        (tree / "logs" / "run.log").write_text("runtime", encoding="utf-8")
    output = audit.build_reversible_audit_package(baseline, target, tmp_path / "out")
    assert not (output / "sanitized-target" / "artifacts").exists()
    assert not (output / "sanitized-target" / "logs").exists()


def test_historical_screenshot_with_visible_uid_blocks_publish(tmp_path: Path):
    baseline, target = _trees(tmp_path)
    relative = "evidence/historical.png"
    _image(target / relative)
    with pytest.raises(audit.SensitiveDataError, match="image privacy"):
        audit.build_reversible_audit_package(
            baseline,
            target,
            tmp_path / "out",
            evidence_allowlist=(relative,),
            image_privacy_manifests={relative: _privacy_manifest(relative)},
            image_ocr_provider=lambda _path: ["UID: synthetic123456"],
        )


def test_image_with_player_name_or_balance_blocks_publish(tmp_path: Path):
    baseline, target = _trees(tmp_path)
    relative = "evidence/account.png"
    _image(target / relative)
    with pytest.raises(audit.SensitiveDataError, match="image privacy"):
        audit.build_reversible_audit_package(
            baseline,
            target,
            tmp_path / "out",
            evidence_allowlist=(relative,),
            image_privacy_manifests={relative: _privacy_manifest(relative)},
            image_ocr_provider=lambda _path: ["player_name: synthetic", "balance: 999999"],
        )


def test_non_allowlisted_image_blocks_publish(tmp_path: Path):
    baseline, target = _trees(tmp_path)
    _image(target / "frame.png")
    with pytest.raises(audit.SensitiveDataError, match="allowlist"):
        audit.build_reversible_audit_package(baseline, target, tmp_path / "out")


def test_allowlisted_image_requires_mask_and_privacy_manifest(tmp_path: Path):
    baseline, target = _trees(tmp_path)
    relative = "evidence/frame.png"
    _image(baseline / relative)
    _image(target / relative)
    with pytest.raises(audit.SensitiveDataError, match="manifest|mask"):
        audit.build_reversible_audit_package(
            baseline,
            target,
            tmp_path / "out",
            evidence_allowlist=(relative,),
            image_ocr_provider=lambda _path: [],
        )
    output = audit.build_reversible_audit_package(
        baseline,
        target,
        tmp_path / "safe",
        evidence_allowlist=(relative,),
        image_privacy_manifests={relative: _privacy_manifest(relative)},
        image_ocr_provider=lambda _path: [],
    )
    assert (output / "sanitized-target" / relative).is_file()


def test_audit_journal_minimizes_account_telemetry():
    minimized = audit.minimize_audit_journal({
        "action_key": "reward_back",
        "page_context": "UID: synthetic123456 player_name: synthetic balance: 999999 level: 99",
        "observation_id": "obs-1",
        "page_classifier": "daily_activity",
        "anchor_key": "top_left_back",
        "coordinate": [80, 40],
        "allowed": True,
    })
    encoded = json.dumps(minimized, ensure_ascii=False)
    assert "synthetic123456" not in encoded
    assert "999999" not in encoded
    assert "page_context" not in minimized
    assert minimized["marker_hash"]


def test_generated_digest_entropy_does_not_mask_ordinary_phone_values():
    synthetic_phone = "".join(("13800", "138000"))
    digest_hits, _ = audit._scan_text_payload(
        json.dumps({"sanitized_target_tree_hash": f"abc{synthetic_phone}def"}),
        suffix=".json",
        filename="SHAREABLE-MANIFEST.json",
    )
    ordinary_hits, _ = audit._scan_text_payload(
        json.dumps({"note": synthetic_phone}),
        suffix=".json",
        filename="SHAREABLE-MANIFEST.json",
    )
    assert "phone" not in digest_hits
    assert "phone" in ordinary_hits


def test_hash_manifest_rejects_unlisted_extra_file(tmp_path: Path):
    root = tmp_path / "package"
    root.mkdir()
    (root / "safe.txt").write_text("safe", encoding="utf-8")
    audit._write_hash_manifest(root)
    (root / "extra.txt").write_text("unlisted", encoding="utf-8")
    assert audit.verify_hash_manifest(root) is False


def test_zip_exact_set_matches_sha_manifest(tmp_path: Path):
    baseline, target = _trees(tmp_path)
    output = audit.build_reversible_audit_package(
        baseline, target, tmp_path / "out", create_zip=True
    )
    assert audit.verify_zip_exact_set(output.with_suffix(".zip")) is True
    tampered = tmp_path / "tampered.zip"
    with zipfile.ZipFile(output.with_suffix(".zip")) as source, zipfile.ZipFile(tampered, "w") as dest:
        for item in source.infolist():
            dest.writestr(item, source.read(item.filename))
        dest.writestr(f"{output.name}/extra.txt", "unlisted")
    assert audit.verify_zip_exact_set(tampered) is False


def test_forward_and_reverse_safe_trees_contain_no_runtime_artifacts(tmp_path: Path):
    baseline, target = _trees(tmp_path)
    for tree in (baseline, target):
        (tree / "dist").mkdir()
        (tree / "dist" / "private.json").write_text("{}", encoding="utf-8")
    output = audit.build_reversible_audit_package(baseline, target, tmp_path / "out")
    patch = output / "patches" / "full-safe-tree.diff"
    forward = tmp_path / "forward"
    reverse = tmp_path / "reverse"
    audit._apply_text_patch(output / "sanitized-baseline", patch, forward, reverse=False)
    audit._apply_text_patch(output / "sanitized-target", patch, reverse, reverse=True)
    for tree in (output / "sanitized-baseline", output / "sanitized-target", forward, reverse):
        assert not any(path.parts[-2] in {"artifacts", "logs", "dist", "build"} for path in tree.rglob("*"))
