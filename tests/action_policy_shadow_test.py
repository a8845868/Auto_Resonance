"""F-02 stage 0 contract tests for the metadata-only action-policy shadow journal.

Contract (user-frozen):
  DEFAULT_ENABLED=NO  ENFORCEMENT=NO  PERMIT_ISSUANCE=NO  EXTRA_CAPTURE=NO
  POSTCONDITION_EVALUATION=NO  DISPATCH_BEHAVIOR_CHANGED=NO
  WRITER_FAILURE_BLOCKS_INPUT=NO
"""

import json
import threading

import pytest

import core.control.control as control
import core.services.action_policy_shadow as shadow
from core.services.read_only_policy import ActionIntent


DISPATCH_SENTINEL = object()


class _LoggerStub:
    def __init__(self):
        self.warnings = []
        self.infos = []

    def warning(self, message, *args, **kwargs):
        self.warnings.append(str(message))

    def info(self, message, *args, **kwargs):
        self.infos.append(str(message))


@pytest.fixture()
def shadow_env(monkeypatch, tmp_path):
    journal_dir = tmp_path / "shadow"
    monkeypatch.delenv(shadow.SHADOW_ENV_VAR, raising=False)
    monkeypatch.setenv(shadow.SHADOW_DIR_ENV_VAR, str(journal_dir))
    monkeypatch.setattr(shadow, "_ENABLED_ANNOUNCED", False)
    monkeypatch.setattr(shadow, "_WRITE_FAILURE_WARNED", False)
    monkeypatch.setattr(shadow, "_SIZE_CAP_WARNED", False)
    monkeypatch.setattr(shadow, "_WRITE_DISABLED", False)
    stub = _LoggerStub()
    monkeypatch.setattr(shadow, "logger", stub)
    return journal_dir, stub


@pytest.fixture()
def legacy_control(monkeypatch):
    calls = {"tap": [], "swipe": [], "keyevent": []}
    monkeypatch.setattr(control, "current_action_policy", lambda: None)
    monkeypatch.setattr(control, "_READ_ONLY_FAIL_CLOSED", False)

    def fake_tap(pos, random_offset=True, **kwargs):
        calls["tap"].append((pos, random_offset))
        return DISPATCH_SENTINEL

    def fake_swipe(pos1, pos2, swipe_time=100, **kwargs):
        calls["swipe"].append((pos1, pos2, swipe_time))
        return DISPATCH_SENTINEL

    monkeypatch.setattr(control, "_legacy_input_tap", fake_tap)
    monkeypatch.setattr(control, "_legacy_input_swipe", fake_swipe)
    return calls


def _rows(journal_dir):
    files = sorted(journal_dir.glob("*.jsonl"))
    rows = []
    for path in files:
        for line in path.read_text(encoding="utf-8").splitlines():
            rows.append(json.loads(line))
    return rows


def test_flag_unset_creates_no_journal_no_warning_and_leaves_dispatch_unchanged(
    shadow_env, legacy_control
):
    journal_dir, log = shadow_env
    result = control.input_tap((10, 20), intent=None)

    assert result is DISPATCH_SENTINEL
    assert legacy_control["tap"] == [((10, 20), True)]
    assert not journal_dir.exists()
    assert log.warnings == []
    assert log.infos == []


def test_flag_set_classifies_all_tiers_without_changing_dispatch(
    shadow_env, legacy_control, monkeypatch
):
    journal_dir, _log = shadow_env
    monkeypatch.setenv(shadow.SHADOW_ENV_VAR, "1")

    intents = [
        None,
        ActionIntent("fatigue_confirm", "drink"),
        ActionIntent("city_entry_navigation", "city_entry"),
        ActionIntent("completely_unknown_key", "anchor"),
    ]
    for intent in intents:
        assert control.input_tap((10, 20), intent=intent) is DISPATCH_SENTINEL

    assert len(legacy_control["tap"]) == 4
    rows = _rows(journal_dir)
    assert [row["blocked_tier"] for row in rows] == [
        shadow.BLOCKED_TIER_NO_INTENT,
        shadow.BLOCKED_TIER_BUSINESS,
        shadow.BLOCKED_TIER_READ_ONLY,
        shadow.BLOCKED_TIER_NO_SPEC,
    ]
    assert [row["would_verdict"] for row in rows] == [
        "WOULD_DENY_NO_INTENT",
        "WOULD_DENY_BLOCKED",
        "WOULD_AUTHORIZE_PENDING_OBSERVATION",
        "WOULD_DENY_NO_SPEC",
    ]
    for row in rows:
        assert row["api"] == "tap"
        assert row["logical_points"] == [[10, 20]]
        # Repo-relative dotted caller only; never a filesystem path.
        assert "\\" not in row["caller_hint"] and "/" not in row["caller_hint"]
    assert rows[2]["spec_exists"] is True
    assert rows[3]["spec_exists"] is False


def test_swipe_and_system_back_rows_are_recorded(
    shadow_env, legacy_control, monkeypatch
):
    journal_dir, _log = shadow_env
    monkeypatch.setenv(shadow.SHADOW_ENV_VAR, "1")

    class _Backend:
        def input_keyevent(self, keycode):
            legacy_control["keyevent"].append(keycode)

    monkeypatch.setattr(control, "control", _Backend())
    monkeypatch.setattr(control, "STOP", False)

    assert control.input_swipe(
        (100, 200), (300, 400), swipe_time=250,
        intent=ActionIntent("daily_horizontal_scroll", "daily_content"),
    ) is DISPATCH_SENTINEL
    assert control.input_system_back(
        intent=ActionIntent("close_home_sidebar", "sidebar_close")
    ) is True

    assert legacy_control["swipe"] == [((100, 200), (300, 400), 250)]
    assert legacy_control["keyevent"] == [4]
    rows = _rows(journal_dir)
    assert [row["api"] for row in rows] == ["swipe", "keyevent"]
    assert rows[0]["duration_ms"] == 250
    assert rows[0]["blocked_tier"] == shadow.BLOCKED_TIER_READ_ONLY
    assert rows[0]["logical_points"] == [[100, 200], [300, 400]]
    assert rows[1]["action_key"] == "close_home_sidebar"
    assert rows[1]["blocked_tier"] == shadow.BLOCKED_TIER_NO_SPEC


def test_writer_failure_never_blocks_dispatch_and_warns_once_sanitized(
    shadow_env, legacy_control, monkeypatch, tmp_path
):
    _journal_dir, log = shadow_env
    monkeypatch.setenv(shadow.SHADOW_ENV_VAR, "1")
    blocked = tmp_path / "blocked-not-a-directory"
    blocked.write_text("occupied", encoding="utf-8")
    monkeypatch.setenv(shadow.SHADOW_DIR_ENV_VAR, str(blocked))

    first = control.input_tap((5, 6), intent=None)
    second = control.input_tap((7, 8), intent=None)

    assert first is DISPATCH_SENTINEL and second is DISPATCH_SENTINEL
    assert len(legacy_control["tap"]) == 2
    assert len(log.warnings) == 1
    assert str(tmp_path) not in log.warnings[0]
    assert "\\" not in log.warnings[0]
    # First failure durably disables further attempts for this process.
    assert shadow._WRITE_DISABLED is True


def test_concurrent_write_failures_warn_exactly_once(
    shadow_env, monkeypatch, tmp_path
):
    _journal_dir, log = shadow_env
    monkeypatch.setenv(shadow.SHADOW_ENV_VAR, "1")
    blocked = tmp_path / "blocked-not-a-directory"
    blocked.write_text("occupied", encoding="utf-8")
    monkeypatch.setenv(shadow.SHADOW_DIR_ENV_VAR, str(blocked))

    barrier = threading.Barrier(8)

    def worker():
        barrier.wait()
        shadow.record_shadow_dispatch(
            api="tap", intent=None, logical_points=((1, 2),)
        )

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(log.warnings) == 1


def test_concurrent_rows_stay_single_line_atomic(shadow_env, monkeypatch):
    journal_dir, _log = shadow_env
    monkeypatch.setenv(shadow.SHADOW_ENV_VAR, "1")

    def worker():
        for _ in range(25):
            shadow.record_shadow_dispatch(
                api="tap",
                intent=ActionIntent("page_back", "top_left_back"),
                logical_points=((1, 2),),
                random_offset=False,
            )

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    rows = _rows(journal_dir)
    assert len(rows) == 100
    assert all(row["action_key"] == "page_back" for row in rows)


def test_size_cap_is_a_hard_byte_limit_with_one_warning(shadow_env, monkeypatch):
    journal_dir, log = shadow_env
    monkeypatch.setenv(shadow.SHADOW_ENV_VAR, "1")

    def write_row():
        shadow.record_shadow_dispatch(
            api="tap",
            intent=ActionIntent("page_back", "top_left_back"),
            logical_points=((1, 2),),
        )

    write_row()
    journal = next(iter(journal_dir.glob("*.jsonl")))
    first_row_size = journal.stat().st_size
    # A cap that fits the first row but not a second one.
    monkeypatch.setattr(shadow, "MAX_JOURNAL_BYTES", first_row_size + 5)

    write_row()
    write_row()

    assert len(_rows(journal_dir)) == 1
    assert journal.stat().st_size <= shadow.MAX_JOURNAL_BYTES
    cap_warnings = [item for item in log.warnings if "size cap" in item]
    assert len(cap_warnings) == 1
