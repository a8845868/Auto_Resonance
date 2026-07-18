from __future__ import annotations

import json
import py_compile
import zipfile
from pathlib import Path

import pytest

from tools.audit_export import (
    SensitiveDataError,
    _redact_text,
    build_shareable_audit,
    scan_sensitive_text,
    scan_sensitive_tree,
)


def test_uid_regex_does_not_mangle_uid_values_identifier():
    source = "def scrub(uid_values):\n    return uid_values\n"
    assert _redact_text(source, ()) == source


def test_uid_regex_does_not_mangle_test_function_names():
    source = "def test_uid_regex_does_not_mangle_uid_values_identifier():\n    pass\n"
    assert _redact_text(source, ()) == source


def test_uid_label_with_separator_is_redacted():
    assert _redact_text("UID: abcdef123456", ()) == "UID: [REDACTED_ACCOUNT_ID]"
    assert _redact_text("UID：abcdef123456", ()) == "UID：[REDACTED_ACCOUNT_ID]"


def test_exported_python_snapshots_compile(tmp_path: Path):
    source = tmp_path / "raw"
    source.mkdir()
    (source / "snapshot.py").write_text(
        "uid_values = ('abcdef123456',)\nassert uid_values\n", encoding="utf-8"
    )
    output = build_shareable_audit(source, tmp_path / "share", uid_values=("abcdef123456",))
    py_compile.compile(str(output / "snapshot.py"), doraise=True)


def test_exported_format_patches_apply_and_compile(tmp_path: Path):
    source = tmp_path / "raw"
    source.mkdir()
    patch = """diff --git a/sample.py b/sample.py
new file mode 100644
--- /dev/null
+++ b/sample.py
@@ -0,0 +1,2 @@
+uid_values = (\"abcdef123456\",)
+assert uid_values
"""
    (source / "0001-test.patch").write_text(patch, encoding="utf-8")
    output = build_shareable_audit(source, tmp_path / "share", uid_values=("abcdef123456",))
    exported = (output / "0001-test.patch").read_text(encoding="utf-8")
    assert "uid_values" in exported
    compile("\n".join(line[1:] for line in exported.splitlines() if line.startswith("+") and not line.startswith("+++")), "sample.py", "exec")


def test_full_diff_and_patch_tree_match_safe_snapshot(tmp_path: Path):
    source = tmp_path / "raw"
    source.mkdir()
    expected = "value = 1\n"
    (source / "changed-files" ).mkdir()
    (source / "changed-files" / "sample.py").write_text(expected, encoding="utf-8")
    output = build_shareable_audit(source, tmp_path / "share")
    assert (output / "changed-files" / "sample.py").read_text(encoding="utf-8") == expected


def test_exported_tests_preserve_assertion_semantics(tmp_path: Path):
    source = tmp_path / "raw"
    source.mkdir()
    test_source = "uid_values = ('abcdef123456',)\nassert uid_values == ('abcdef123456',)\n"
    (source / "test_snapshot.py").write_text(test_source, encoding="utf-8")
    output = build_shareable_audit(source, tmp_path / "share", uid_values=("abcdef123456",))
    exported = (output / "test_snapshot.py").read_text(encoding="utf-8")
    namespace = {}
    exec(compile(exported, "test_snapshot.py", "exec"), namespace)
    assert namespace["uid_values"] == ("[REDACTED_ACCOUNT_ID]",)


def test_shareable_manifest_refuses_semantically_invalid_artifacts(tmp_path: Path):
    source = tmp_path / "raw"
    source.mkdir()
    (source / "broken.py").write_text("def broken(:\n", encoding="utf-8")
    with pytest.raises(SensitiveDataError, match="semantic"):
        build_shareable_audit(source, tmp_path / "share")
    assert not (tmp_path / "share").exists()


@pytest.mark.parametrize(
    "value",
    [
        r"C:\Users\Administrator\Documents\audit.json",
        "C:/Users/Administrator/Documents/audit.json",
    ],
)
def test_literal_windows_user_path_is_redacted(value: str):
    result = _redact_text(value, ())
    assert "Administrator" not in result
    assert "%USERPROFILE%" in result


def test_json_escaped_windows_user_path_is_redacted():
    raw = json.dumps({"path": r"C:\Users\Administrator\Documents\audit.json"})
    result = _redact_text(raw, ())
    assert "Administrator" not in result
    assert json.loads(result)["path"].startswith("%USERPROFILE%")


def test_forward_slash_user_path_is_redacted():
    assert _redact_text("C:/Users/Administrator/a.txt", ()) == "%USERPROFILE%/a.txt"


def test_diagnostic_path_is_export_relative(tmp_path: Path):
    source = tmp_path / "raw"
    source.mkdir()
    (source / "bad.txt").write_text("Authorization: secret-secret-secret", encoding="utf-8")
    with pytest.raises(SensitiveDataError) as error:
        build_shareable_audit(source, tmp_path / "share")
    assert str(tmp_path) not in str(error.value)
    assert "bad.txt:authorization" in str(error.value)


def test_decoded_json_string_gate_detects_user_profile(tmp_path: Path):
    root = tmp_path / "tree"
    root.mkdir()
    (root / "evidence.json").write_text(
        json.dumps({"path": r"C:\Users\Administrator\secret.txt"}), encoding="utf-8"
    )
    hits, stats = scan_sensitive_tree(root)
    assert "evidence.json:user_profile" in hits
    assert stats["decoded_json_values_scanned"] >= 1


def test_final_zip_second_pass_sensitive_scan(tmp_path: Path):
    source = tmp_path / "raw"
    source.mkdir()
    (source / "evidence.json").write_text(
        json.dumps({"path": r"C:\Users\Administrator\secret.txt"}), encoding="utf-8"
    )
    output = build_shareable_audit(source, tmp_path / "share", create_zip=True)
    with zipfile.ZipFile(output.with_suffix(".zip")) as archive:
        payload = archive.read("share/evidence.json").decode("utf-8")
    assert "Administrator" not in payload
    manifest = json.loads((output / "SHAREABLE-MANIFEST.json").read_text(encoding="utf-8"))
    assert manifest["sanitization"]["zip_second_pass_sensitive_gate_hits"] == 0
