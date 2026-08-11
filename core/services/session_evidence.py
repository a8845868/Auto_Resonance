"""Shared opt-in session evidence infrastructure.

This module intentionally keeps control and image backends as lazy runtime
dependencies so importing the evidence context does not initialize them.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from contextvars import ContextVar
from datetime import datetime
from pathlib import Path

from loguru import logger


class SessionEvidenceRecorder:
    """Persist opt-in frame/OCR evidence without changing session decisions."""

    def __init__(self, enabled: bool, cycle_id: str):
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        self.enabled = bool(enabled)
        self.cycle_id = str(cycle_id)
        self.root = Path("logs") / "run_business" / f"{timestamp}-cycle"
        self.index = 0
        self.latest_ledger_context: dict | None = None

    @staticmethod
    def _write_json_atomic(destination: Path, payload: object) -> None:
        temporary = destination.with_name(
            f"{destination.name}.{uuid.uuid4().hex}.tmp"
        )
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        temporary.replace(destination)

    def capture(
        self,
        label: str,
        *,
        state_transition_name: str,
        cycle_id: str,
        leg_id: str,
        ledger_event_count: int | None,
        current_page_classification: str,
        image=None,
        ocr_items: list[dict] | None = None,
    ) -> list[dict]:
        if not self.enabled:
            return []
        if image is None:
            from core.control.control import screenshot

            image = screenshot()
        if ocr_items is None:
            ocr_items = image.ocr()

        safe_label = re.sub(r"[^0-9A-Za-z_-]+", "-", label).strip("-") or "step"
        self.root.mkdir(parents=True, exist_ok=True)
        stem = f"{self.index:03d}-{safe_label}"
        self.index += 1
        import cv2 as cv

        if not cv.imwrite(str(self.root / f"{stem}.png"), image.image):
            raise OSError(f"run-business evidence image write failed: {stem}")
        self._write_json_atomic(self.root / f"{stem}.ocr.json", ocr_items)
        self._write_json_atomic(
            self.root / f"{stem}.metadata.json",
            {
                "state_transition_name": str(state_transition_name),
                "cycle_id": str(cycle_id),
                "leg_id": str(leg_id),
                "ledger_event_count": ledger_event_count,
                "current_page_classification": str(current_page_classification),
                "captured_at": datetime.now().isoformat(timespec="seconds"),
            },
        )
        return ocr_items

    def write_result(self, result: dict) -> str:
        if not self.enabled:
            return ""
        self.root.mkdir(parents=True, exist_ok=True)
        destination = self.root / "FINAL_CYCLE.json"
        self._write_json_atomic(destination, result)
        return str(destination)


_SESSION_EVIDENCE: ContextVar[SessionEvidenceRecorder | None] = ContextVar(
    "session_evidence", default=None
)


def _session_evidence_enabled() -> bool:
    return os.environ.get("AUTO_RESONANCE_RUN_BUSINESS_EVIDENCE") == "1"


def _run_business_ledger_event_count(context: dict | None) -> int | None:
    if context is None:
        return 0
    try:
        from core.services.trade_ledger import LEDGER_PATH, load_trade_cycle_state

        state = load_trade_cycle_state(
            context.get("ledger_path", LEDGER_PATH), context["cycle_id"]
        )
        return len(state.events)
    except Exception as error:
        logger.warning(
            "Unable to count trade-ledger events for evidence: "
            f"{type(error).__name__}"
        )
        return None


def capture_session_evidence(
    state_transition_name: str,
    *,
    ledger_context: dict | None,
    leg_id: str,
    current_page_classification: str,
) -> None:
    recorder = _SESSION_EVIDENCE.get()
    if recorder is None:
        return
    try:
        if ledger_context is not None:
            recorder.latest_ledger_context = ledger_context
        recorder.capture(
            state_transition_name.lower(),
            state_transition_name=state_transition_name,
            cycle_id=recorder.cycle_id,
            leg_id=leg_id,
            ledger_event_count=_run_business_ledger_event_count(ledger_context),
            current_page_classification=current_page_classification,
        )
    except Exception as error:
        logger.warning(
            "Unable to capture run-business evidence for "
            f"{state_transition_name}: {type(error).__name__}"
        )


def _write_session_final_result(
    recorder: SessionEvidenceRecorder,
    *,
    ledger_context: dict | None,
    result: object = None,
    error: Exception | None = None,
) -> None:
    effective_ledger_context = (
        ledger_context
        if ledger_context is not None
        else recorder.latest_ledger_context
    )
    payload = {
        "cycle_id": recorder.cycle_id,
        "ledger_event_count": _run_business_ledger_event_count(
            effective_ledger_context
        ),
        "completed_at": datetime.now().isoformat(timespec="seconds"),
        "status": "EXCEPTION" if error is not None else "RETURNED",
        "result": result,
        "exception": (
            {"type": type(error).__name__, "message": str(error)}
            if error is not None
            else None
        ),
    }
    try:
        recorder.write_result(payload)
    except Exception as write_error:
        logger.warning(
            "Unable to write final run-business evidence: "
            f"{type(write_error).__name__}"
        )
