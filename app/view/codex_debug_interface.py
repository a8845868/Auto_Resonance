"""Read-only, live view of Codex self-healing state and output."""

from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path

from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import QFrame, QLabel, QVBoxLayout, QWidget
from qfluentwidgets import PlainTextEdit, ScrollArea

from app.common.config import cfg
from app.common.style_sheet import StyleSheet
from core.services.codex_repair import (
    DEFAULT_STORAGE_ROOT as SELF_HEALING_STORAGE_ROOT,
    list_legacy_worktrees,
    list_run_statuses,
)
from core.services.self_healing import global_runner_active


CODEX_DEBUG_REFRESH_INTERVAL_MS = 1_000
MAX_DEBUG_OUTPUT_BYTES = 2 * 1024 * 1024
MAX_DEBUG_OUTPUT_CHARS = 300_000
_ACTIVE_REPAIR_STATUSES = {"starting", "running", "codex_running"}


def _read_json_document(path):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _latest_pending_dispatch(storage_root):
    pending_dir = Path(storage_root) / "dispatch" / "pending"
    try:
        paths = sorted(
            pending_dir.glob("*.json"),
            key=lambda path: path.stat().st_mtime_ns,
            reverse=True,
        )
    except OSError:
        return None
    for path in paths:
        document = _read_json_document(path)
        if document:
            return document
    return None


def _current_self_healing_status(storage_root=None):
    """Read the newest durable run/queue state without mutating either store."""

    root = Path(storage_root or SELF_HEALING_STORAGE_ROOT).resolve()
    try:
        statuses = list_run_statuses(root, limit=1)
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
        statuses = []
    latest = statuses[0] if statuses and isinstance(statuses[0], dict) else None
    pending = _latest_pending_dispatch(root)
    if (
        storage_root is None
        and latest
        and str(latest.get("status", "")).casefold() in _ACTIVE_REPAIR_STATUSES
        and not global_runner_active(root)
    ):
        latest = dict(latest)
        latest["status"] = "interrupted_stale"
        latest["reason"] = "runner_process_not_active"

    def with_legacy(document):
        result = dict(document or {})
        if storage_root is None:
            legacy = list_legacy_worktrees()
            if legacy:
                result["legacy_worktree_paths"] = legacy
        return result

    if latest and str(latest.get("status", "")).casefold() in _ACTIVE_REPAIR_STATUSES:
        return with_legacy(latest)
    if pending:
        # The pending marker remains while its incident is being processed. A
        # status for the same incident is the newer source, including terminal
        # diagnosis/candidate states.
        if latest and str(latest.get("incident_id", "")) == str(
            pending.get("incident_id", "")
        ):
            return with_legacy(latest)
        return with_legacy(
            {
                "status": "queued",
                "mode": pending.get("mode", ""),
                "incident_id": pending.get("incident_id", ""),
                "incident_path": pending.get("incident_path", ""),
            }
        )
    return with_legacy(latest)


def _one_line(value, limit=800):
    text = " ".join(str(value or "").splitlines()).strip()
    if len(text) <= limit:
        return text
    return f"{text[: limit - 14]}...<truncated>"


def _self_healing_status_lines(status, *, enabled, allow_repair):
    status = status if isinstance(status, dict) else {}
    raw_status = _one_line(status.get("status"))
    mode = _one_line(status.get("mode")) or (
        "repair" if allow_repair else "diagnose"
    )
    status_labels = {
        "starting": "运行中",
        "running": "运行中",
        "codex_running": "Codex 运行中",
        "queued": "等待 Codex",
        "candidate_unvalidated": "候选补丁未验证，需人工审查",
        "diagnosed": "诊断完成",
        "no_change": "完成，未产生修改",
        "failed": "执行失败",
        "blocked": "已阻止",
        "disabled": "未启用",
        "runner_failed": "运行器失败",
        "interrupted": "已由操作员中断",
        "interrupted_stale": "运行器已中断，现场已保留",
    }
    if raw_status:
        state_label = status_labels.get(raw_status.casefold(), raw_status)
        state_text = f"{state_label}（{raw_status}）"
    else:
        state_text = "等待异常" if enabled else "未启用"
    state_caption = (
        "当前状态"
        if raw_status.casefold() in _ACTIVE_REPAIR_STATUSES | {"queued"}
        else "最近状态"
    )
    mode_label = {"repair": "修复", "diagnose": "诊断"}.get(
        mode.casefold(), mode or "未知"
    )

    branch_name = _one_line(status.get("branch_name"))
    worktree_path = _one_line(status.get("worktree_path"))
    if not branch_name and worktree_path:
        branch_name = "detached"
    reason = _one_line(
        status.get("failure_reason") or status.get("reason") or status.get("error")
    )
    output_path = _one_line(
        status.get("codex_output_path") or status.get("output_path")
    )
    status_path = _one_line(status.get("run_path") or status.get("status_path"))

    lines = [
        f"{state_caption}: {state_text}",
        f"自愈模式: {mode_label}（{mode}）",
        "Codex 策略: minimal reasoning · Fast",
        f"branch_name: {branch_name or '—'}",
        f"worktree_path: {worktree_path or '—'}",
    ]
    for key in ("incident_id", "attempt_id", "started_at", "finished_at"):
        value = _one_line(status.get(key))
        if value:
            lines.append(f"{key}: {value}")
    if reason:
        lines.append(f"失败原因/说明: {reason}")
    if output_path:
        lines.append(f"输出路径: {output_path}")
    if status_path and status_path != output_path:
        lines.append(f"状态路径: {status_path}")
    for legacy_path in status.get("legacy_worktree_paths") or []:
        path_text = _one_line(legacy_path)
        if path_text:
            lines.append(f"历史外置工作树（只读保留）: {path_text}")
    return lines


def _format_output_line(line):
    line = str(line).rstrip("\r\n")
    if not line:
        return ""
    try:
        event = json.loads(line)
    except (json.JSONDecodeError, TypeError, ValueError):
        return line
    if not isinstance(event, dict):
        return line

    text = event.get("text")
    if text is None:
        text = event.get("line")
    if text is not None:
        label = event.get("stream") or event.get("type") or "event"
        return f"[{_one_line(label, 80)}] {text}"

    event_type = _one_line(event.get("type") or "event", 80)
    return f"[{event_type}] {json.dumps(event, ensure_ascii=False, separators=(',', ':'))}"


def _read_output_display(path):
    if not path:
        return "尚无 Codex 输出。"
    try:
        output_path = Path(path)
        with output_path.open("rb") as stream:
            stream.seek(0, 2)
            size = stream.tell()
            start = max(0, size - MAX_DEBUG_OUTPUT_BYTES)
            stream.seek(start)
            content = stream.read(MAX_DEBUG_OUTPUT_BYTES)
    except (OSError, TypeError, ValueError):
        return f"等待 Codex 输出……\n{path}"

    text = content.decode("utf-8", errors="replace")
    if start:
        # Discard a potentially partial first record after seeking into a file.
        _, separator, text = text.partition("\n")
        if not separator:
            text = ""
    rendered = "\n".join(_format_output_line(line) for line in text.splitlines())
    rendered = rendered.rstrip()
    if start:
        rendered = "…（仅显示最近的有界输出）\n" + rendered
    if len(rendered) > MAX_DEBUG_OUTPUT_CHARS:
        rendered = "…（较早输出已从面板省略）\n" + rendered[-MAX_DEBUG_OUTPUT_CHARS:]
    return rendered or "Codex 已启动，等待首条输出……"


def _safe_codex_output_path(status, storage_root=None):
    """Accept only the fixed output filename beneath the durable runs store."""

    value = status.get("codex_output_path") or status.get("output_path")
    if not value:
        return None
    try:
        path = Path(value).resolve()
        runs_root = Path(storage_root or SELF_HEALING_STORAGE_ROOT).resolve() / "runs"
        path.relative_to(runs_root)
    except (OSError, TypeError, ValueError):
        return None
    if path.name.casefold() != "codex-output.jsonl":
        return None
    return path


def _output_signature(path):
    if path is None:
        return (None,)
    try:
        stat = path.stat()
    except OSError:
        return (str(path), "missing")
    return (str(path), stat.st_ino, stat.st_size, stat.st_mtime_ns)


class CodexDebugInterface(ScrollArea):
    """Live observer only; this page never dispatches or mutates a repair."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._last_output_path = None
        self._last_output_text = None
        self._last_output_signature = object()

        self.scrollWidget = QWidget(self)
        self.mainLayout = QVBoxLayout(self.scrollWidget)
        self.setObjectName("CodexDebugInterface")
        self.scrollWidget.setObjectName("view")
        self.setWidget(self.scrollWidget)
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.mainLayout.setContentsMargins(28, 24, 28, 24)
        self.mainLayout.setSpacing(14)
        StyleSheet.HOME_INTERFACE.apply(self)

        title = QLabel("调试", self.scrollWidget)
        title.setStyleSheet("font-size: 24px; font-weight: 700;")
        self.mainLayout.addWidget(title)

        status_panel = QFrame(self.scrollWidget)
        status_panel.setObjectName("codexStatusPanel")
        status_panel.setStyleSheet(
            "QFrame#codexStatusPanel { border: 1px solid rgba(128,128,128,0.28); "
            "border-radius: 8px; background: rgba(128,128,128,0.06); }"
        )
        status_layout = QVBoxLayout(status_panel)
        status_layout.setContentsMargins(16, 12, 16, 12)
        status_title = QLabel("Codex 自愈状态", status_panel)
        status_title.setStyleSheet("font-size: 19px; font-weight: 600;")
        self.statusLabel = QLabel(status_panel)
        self.statusLabel.setWordWrap(True)
        self.statusLabel.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.lastRefreshLabel = QLabel(status_panel)
        self.lastRefreshLabel.setStyleSheet("color: #888;")
        status_layout.addWidget(status_title)
        status_layout.addWidget(self.statusLabel)
        status_layout.addWidget(self.lastRefreshLabel)
        self.mainLayout.addWidget(status_panel)

        output_title = QLabel("Codex 自愈实时日志", self.scrollWidget)
        output_title.setStyleSheet("font-size: 20px; font-weight: 600;")
        self.outputWidget = PlainTextEdit(self.scrollWidget)
        self.outputWidget.setReadOnly(True)
        self.outputWidget.setMinimumHeight(380)
        self.mainLayout.addWidget(output_title)
        self.mainLayout.addWidget(self.outputWidget, 1)

        self.refreshTimer = QTimer(self)
        self.refreshTimer.setInterval(CODEX_DEBUG_REFRESH_INTERVAL_MS)
        self.refreshTimer.timeout.connect(self.refresh)
        self.refreshTimer.start()
        self.refresh()

    def refresh(self):
        """Re-read durable state and the bounded output tail every second."""

        try:
            status = _current_self_healing_status()
            lines = _self_healing_status_lines(
                status,
                enabled=bool(cfg.enableCodexSelfHealing.value),
                allow_repair=bool(cfg.allowCodexIsolatedRepair.value),
            )
        except Exception as error:  # observation must never affect scheduling
            status = {}
            lines = [
                "当前状态: 状态读取失败",
                f"失败原因/说明: {_one_line(error)}",
                "请检查 logs/self_healing 下的状态文件。",
            ]
        self.statusLabel.setText("\n".join(lines))
        self.lastRefreshLabel.setText(
            f"最后刷新: {datetime.now():%Y-%m-%d %H:%M:%S}（每 1 秒自动读取）"
        )

        declared_output = status.get("codex_output_path") or status.get("output_path")
        output_path = _safe_codex_output_path(status)
        signature = _output_signature(output_path)
        if declared_output and output_path is None:
            signature = ("unsafe", str(declared_output))
            output_text = "已拒绝读取状态文件指向的非自愈日志路径。"
        elif signature != self._last_output_signature:
            output_text = _read_output_display(output_path)
        else:
            output_text = self._last_output_text
        if signature != self._last_output_signature or output_text != self._last_output_text:
            self.outputWidget.setPlainText(output_text)
            scroll_bar = self.outputWidget.verticalScrollBar()
            scroll_bar.setValue(scroll_bar.maximum())
            self._last_output_path = output_path
            self._last_output_text = output_text
            self._last_output_signature = signature

    def shutdown(self):
        self.refreshTimer.stop()
