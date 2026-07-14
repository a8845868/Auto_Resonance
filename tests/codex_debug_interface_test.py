import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from app.common.config import cfg
import app.view.codex_debug_interface as debug_module
from app.view.codex_debug_interface import CodexDebugInterface


def test_current_status_prefers_new_pending_then_same_incident_run(tmp_path):
    old_status_path = tmp_path / "runs" / "old-attempt" / "status.json"
    old_status_path.parent.mkdir(parents=True)
    old_status_path.write_text(
        json.dumps(
            {
                "incident_id": "incident-old",
                "status": "candidate_unvalidated",
                "mode": "repair",
            }
        ),
        encoding="utf-8",
    )

    pending_path = tmp_path / "dispatch" / "pending" / "incident-new.json"
    pending_path.parent.mkdir(parents=True)
    pending_path.write_text(
        json.dumps(
            {
                "incident_id": "incident-new",
                "incident_path": "incidents/incident-new.json",
                "mode": "diagnose",
            }
        ),
        encoding="utf-8",
    )

    queued = debug_module._current_self_healing_status(tmp_path)
    assert queued == {
        "status": "queued",
        "mode": "diagnose",
        "incident_id": "incident-new",
        "incident_path": "incidents/incident-new.json",
    }

    new_status_path = tmp_path / "runs" / "new-attempt" / "status.json"
    new_status_path.parent.mkdir(parents=True)
    new_status_path.write_text(
        json.dumps(
            {
                "incident_id": "incident-new",
                "attempt_id": "attempt-new",
                "status": "codex_running",
                "mode": "diagnose",
            }
        ),
        encoding="utf-8",
    )

    active = debug_module._current_self_healing_status(tmp_path)
    assert active["status"] == "codex_running"
    assert active["attempt_id"] == "attempt-new"
    assert pending_path.is_file()


def test_status_text_exposes_strategy_branch_worktree_and_paths():
    text = "\n".join(
        debug_module._self_healing_status_lines(
            {
                "status": "codex_running",
                "mode": "repair",
                "branch_name": "codex/self-heal/incident-attempt",
                "worktree_path": r"C:\project\_worktrees\self_healing\attempt",
                "incident_id": "incident",
                "attempt_id": "attempt",
                "started_at": "2026-07-14T08:53:10Z",
                "finished_at": "2026-07-14T08:53:11Z",
                "reason": "diagnosing_failure",
                "codex_output_path": r"C:\project\logs\codex-output.jsonl",
                "run_path": r"C:\project\logs\status.json",
            },
            enabled=True,
            allow_repair=True,
        )
    )

    assert "Codex 运行中" in text
    assert "minimal reasoning · Fast" in text
    assert "codex/self-heal/incident-attempt" in text
    assert "_worktrees" in text
    assert "incident_id: incident" in text
    assert "attempt_id: attempt" in text
    assert "started_at: 2026-07-14T08:53:10Z" in text
    assert "finished_at: 2026-07-14T08:53:11Z" in text
    assert "codex-output.jsonl" in text
    assert "status.json" in text

    terminal = "\n".join(
        debug_module._self_healing_status_lines(
            {"status": "blocked", "mode": "repair"},
            enabled=True,
            allow_repair=True,
        )
    )
    assert "最近状态: 已阻止（blocked）" in terminal


def test_output_reader_handles_native_json_wrappers_and_old_plain_text(tmp_path):
    output_path = tmp_path / "codex-output.jsonl"
    native_event = {"type": "thread.started", "thread_id": "thread-1"}
    output_path.write_text(
        "\n".join(
            (
                json.dumps(native_event),
                json.dumps(
                    {"type": "stderr", "stream": "stderr", "text": "failed"}
                ),
                json.dumps({"stream": "stdout", "line": "legacy wrapper"}),
                "old plain-text output",
            )
        ),
        encoding="utf-8",
    )

    text = debug_module._read_output_display(output_path)

    assert "[thread.started]" in text
    assert '"thread_id":"thread-1"' in text
    assert "[stderr] failed" in text
    assert "[stdout] legacy wrapper" in text
    assert "old plain-text output" in text


def test_output_path_must_remain_inside_self_healing_runs(tmp_path):
    runs = tmp_path / "runs" / "incident-attempt"
    safe = runs / "codex-output.jsonl"
    outside = tmp_path / "private.txt"

    assert debug_module._safe_codex_output_path(
        {"codex_output_path": str(safe)}, tmp_path
    ) == safe.resolve()
    assert (
        debug_module._safe_codex_output_path(
            {"codex_output_path": str(outside)}, tmp_path
        )
        is None
    )
    assert (
        debug_module._safe_codex_output_path(
            {"codex_output_path": str(runs / "other.log")}, tmp_path
        )
        is None
    )


def test_same_debug_page_refreshes_when_active_output_file_grows(
    tmp_path, monkeypatch
):
    app = QApplication.instance() or QApplication([])
    output_path = tmp_path / "codex-output.jsonl"
    output_path.write_text(
        json.dumps({"stream": "stdout", "text": "first"}) + "\n",
        encoding="utf-8",
    )
    status = {
        "status": "codex_running",
        "mode": "repair",
        "codex_output_path": str(output_path),
    }
    monkeypatch.setattr(
        debug_module, "_current_self_healing_status", lambda: dict(status)
    )
    monkeypatch.setattr(
        debug_module, "_safe_codex_output_path", lambda _status: output_path
    )
    monkeypatch.setattr(cfg.enableCodexSelfHealing, "value", True)
    monkeypatch.setattr(cfg.allowCodexIsolatedRepair, "value", True)

    page = CodexDebugInterface()
    try:
        assert page.refreshTimer.isActive()
        assert page.refreshTimer.interval() == debug_module.CODEX_DEBUG_REFRESH_INTERVAL_MS
        assert "first" in page.outputWidget.toPlainText()
        first_page_identity = id(page)

        with output_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"stream": "stderr", "text": "second"}) + "\n")
            stream.flush()
        page.refresh()
        app.processEvents()

        assert id(page) == first_page_identity
        assert "first" in page.outputWidget.toPlainText()
        assert "[stderr] second" in page.outputWidget.toPlainText()
        assert "每 1 秒自动读取" in page.lastRefreshLabel.text()
    finally:
        page.shutdown()
        page.deleteLater()
        app.processEvents()


def test_shutdown_stops_live_refresh_timer(monkeypatch):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(debug_module, "_current_self_healing_status", lambda: {})

    page = CodexDebugInterface()
    page.shutdown()

    assert page.refreshTimer.isActive() is False
    page.deleteLater()
    app.processEvents()
