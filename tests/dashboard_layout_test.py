import os
import subprocess
import sys
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import QApplication, QTextEdit

from app.view.logger_interface import LoguruHandler, StructuredLogWidget
from app.view.dashboard_interface import (
    action_summary_execution_status,
    build_resident_activity_task,
)


def _app():
    return QApplication.instance() or QApplication([])


def test_structured_log_parses_live_and_historical_formats():
    assert StructuredLogWidget.parseLine("WARNING\x1f12:34:56.789\x1fretry") == (
        "WARNING",
        "12:34:56.789",
        "retry",
    )
    assert StructuredLogWidget.parseLine(
        "23:45:15 - ERROR | task_queue.run:139 - failed"
    ) == ("ERROR", "23:45:15", "failed")


def test_action_summary_block_is_not_rendered_as_completed_sweeps():
    status = action_summary_execution_status({
        "execution_status": "BLOCKED",
        "reason": "business_policy_required",
        "decision": {"decision": "TASK_AVAILABLE_NEEDS_POLICY"},
        "business_dispatches": 0,
    })

    assert status == (
        "■  行动汇总已安全阻断：business_policy_required（TASK_AVAILABLE_NEEDS_POLICY）",
        "#f0a44b",
    )


def test_dashboard_queue_entry_uses_default_safe_public_wrapper():
    blocked = {
        "execution_status": "BLOCKED",
        "reason": "business_policy_required",
        "business_dispatches": 0,
    }
    with patch(
        "app.view.dashboard_interface.run_resident_activity",
        return_value=blocked,
    ) as public_entry:
        task = build_resident_activity_task("特殊订单", "学会装备箱")
        result = task.run()

    public_entry.assert_called_once_with("特殊订单", "学会装备箱")
    assert task.key == "resident_activity"
    assert result == blocked


def test_structured_log_keeps_columns_colours_and_latest_rows():
    _app()
    widget = StructuredLogWidget(maximum_rows=2)
    widget.appendLog("INFO\x1f10:00:00.001\x1ffirst")
    widget.appendLog("WARNING\x1f10:00:01.002\x1fsecond")
    widget.appendLog("ERROR\x1f10:00:02.003\x1fthird")

    lines = widget.toPlainText().splitlines()
    assert len(lines) == 2
    assert lines[0].startswith("WARNING  10:00:01.002")
    assert lines[0].endswith("│ second")
    html = widget.toHtml().lower()
    assert "#e5a445" in html
    assert "#ef6461" in html
    assert "#22b8cf" in html


def test_loguru_handler_delivers_live_records_to_structured_view():
    app = _app()
    widget = StructuredLogWidget()
    handler = LoguruHandler(widget)

    handler.write("INFO\x1f11:22:33.444\x1flive message\n")
    app.processEvents()

    assert widget.document().blockCount() == 1
    assert widget.toPlainText().endswith("│ live message")


def test_structured_log_coalesces_scroll_requests_on_one_owned_timer():
    _app()
    widget = StructuredLogWidget()
    for index in range(200):
        widget.appendLog(f"INFO\x1f11:22:33.444\x1fmessage {index}")

    assert widget._scrollTimer.parent() is widget
    assert widget._scrollTimer.isActive()


def test_structured_log_wraps_and_copies_an_arbitrary_character_range():
    app = _app()
    widget = StructuredLogWidget()
    widget.appendLog("WARNING\x1f12:00:00.001\x1fa long warning message")
    text = widget.toPlainText()
    start = text.index("long")
    cursor = widget.textCursor()
    cursor.setPosition(start)
    cursor.setPosition(start + len("long warning"), QTextCursor.MoveMode.KeepAnchor)
    widget.setTextCursor(cursor)

    widget.copy()

    assert widget.lineWrapMode() == QTextEdit.LineWrapMode.WidgetWidth
    assert app.clipboard().text() == "long warning"


def test_structured_log_repeated_native_lifecycle_exits_cleanly():
    code = """
from PySide6.QtWidgets import QApplication
from app.view.logger_interface import StructuredLogWidget
app = QApplication([])
for cycle in range(20):
    widget = StructuredLogWidget()
    for index in range(200):
        widget.appendLog(f'INFO\\x1f11:22:33.444\\x1f{cycle}-{index}')
    widget.deleteLater()
    app.processEvents()
app.processEvents()
"""
    environment = os.environ.copy()
    environment["QT_QPA_PLATFORM"] = "offscreen"

    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=os.getcwd(),
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stderr
