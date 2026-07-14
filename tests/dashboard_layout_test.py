import os
import subprocess
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from app.view.logger_interface import LoguruHandler, StructuredLogWidget


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


def test_structured_log_keeps_columns_colours_and_latest_rows():
    _app()
    widget = StructuredLogWidget(maximum_rows=2)
    widget.appendLog("INFO\x1f10:00:00.001\x1ffirst")
    widget.appendLog("WARNING\x1f10:00:01.002\x1fsecond")
    widget.appendLog("ERROR\x1f10:00:02.003\x1fthird")

    assert widget.rowCount() == 2
    assert [widget.item(0, column).text() for column in range(3)] == [
        "WARNING",
        "10:00:01.002",
        "second",
    ]
    assert widget.item(1, 0).foreground().color().name() == "#ef6461"
    assert widget.item(1, 1).foreground().color().name() == "#22b8cf"


def test_loguru_handler_delivers_live_records_to_structured_view():
    app = _app()
    widget = StructuredLogWidget()
    handler = LoguruHandler(widget)

    handler.write("INFO\x1f11:22:33.444\x1flive message\n")
    app.processEvents()

    assert widget.rowCount() == 1
    assert widget.item(0, 2).text() == "live message"


def test_structured_log_coalesces_scroll_requests_on_one_owned_timer():
    _app()
    widget = StructuredLogWidget()
    for index in range(200):
        widget.appendLog(f"INFO\x1f11:22:33.444\x1fmessage {index}")

    assert widget._scrollTimer.parent() is widget
    assert widget._scrollTimer.isActive()


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
