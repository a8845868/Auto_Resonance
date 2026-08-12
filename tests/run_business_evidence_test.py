import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np

from auto.run_business import main as business
from core.services import session_evidence


class _Frame:
    def __init__(self, ocr_items=None):
        self.image = np.zeros((720, 1280, 3), dtype=np.uint8)
        self._ocr_items = list(ocr_items or [])

    def ocr(self):
        return list(self._ocr_items)


def test_recorder_atomically_writes_final_cycle_json(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    recorder = business.RunBusinessEvidenceRecorder(True, "cycle-atomic")

    result_path = Path(
        recorder.write_result(
            {
                "cycle_id": "cycle-atomic",
                "status": "RETURNED",
                "result": {"success": True},
            }
        )
    )

    assert result_path.name == "FINAL_CYCLE.json"
    assert result_path.parent.name.endswith("-cycle")
    assert json.loads(result_path.read_text(encoding="utf-8")) == {
        "cycle_id": "cycle-atomic",
        "status": "RETURNED",
        "result": {"success": True},
    }
    assert list(result_path.parent.glob("FINAL_CYCLE.json.*.tmp")) == []


def test_capture_writes_transition_and_cycle_metadata(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    frame = _Frame([{"text": "我要买", "score": 0.99, "position": []}])
    screenshot_calls = []

    def fake_screenshot():
        screenshot_calls.append(True)
        return frame

    monkeypatch.setattr("core.control.control.screenshot", fake_screenshot)
    recorder = business.RunBusinessEvidenceRecorder(True, "cycle-42")

    observed = recorder.capture(
        "enter_buy_page_after",
        state_transition_name="ENTER_BUY_PAGE_AFTER",
        cycle_id="cycle-42",
        leg_id="岚心城|武林源",
        ledger_event_count=4,
        current_page_classification="BUY_PAGE_VERIFIED_BY_NAVIGATION",
    )

    assert observed == frame.ocr()
    assert screenshot_calls == [True]
    metadata_paths = list(recorder.root.glob("*.metadata.json"))
    assert len(metadata_paths) == 1
    metadata = json.loads(metadata_paths[0].read_text(encoding="utf-8"))
    assert metadata["state_transition_name"] == "ENTER_BUY_PAGE_AFTER"
    assert metadata["cycle_id"] == "cycle-42"
    assert metadata["leg_id"] == "岚心城|武林源"
    assert metadata["ledger_event_count"] == 4
    assert (
        metadata["current_page_classification"]
        == "BUY_PAGE_VERIFIED_BY_NAVIGATION"
    )
    assert len(list(recorder.root.glob("*.png"))) == 1
    assert len(list(recorder.root.glob("*.ocr.json"))) == 1


def test_evidence_disabled_has_no_capture_or_file_overhead(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("AUTO_RESONANCE_RUN_BUSINESS_EVIDENCE", raising=False)

    def forbidden_screenshot():
        raise AssertionError("disabled evidence must not capture a frame")

    monkeypatch.setattr("core.control.control.screenshot", forbidden_screenshot)
    recorder = business.RunBusinessEvidenceRecorder(False, "disabled-cycle")
    assert recorder.capture(
        "disabled",
        state_transition_name="DISABLED",
        cycle_id="disabled-cycle",
        leg_id="",
        ledger_event_count=0,
        current_page_classification="UNKNOWN",
    ) == []
    assert recorder.write_result({"success": True}) == ""

    calls = []

    @business._with_run_business_evidence
    def sample_run(routes, recovery_attempts=2, ledger_context=None):
        calls.append((routes, recovery_attempts, ledger_context))
        return True

    assert sample_run("routes", recovery_attempts=1, ledger_context=None) is True
    assert calls == [("routes", 1, None)]

    @business._with_run_business_preflight_evidence
    def sample_preflight():
        calls.append("preflight")
        return False

    assert sample_preflight() is False
    assert calls[-1] == "preflight"
    assert not (tmp_path / "logs").exists()


def test_adaptive_preflight_failure_writes_navigation_evidence(
    tmp_path, monkeypatch
):
    import app.common.config as config_module
    import core.services as services

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AUTO_RESONANCE_RUN_BUSINESS_EVIDENCE", "1")
    monkeypatch.setattr("core.control.control.screenshot", lambda: _Frame())
    monkeypatch.setattr(
        config_module,
        "cfg",
        SimpleNamespace(
            InventoryBooks=SimpleNamespace(value=3),
            AutoReadInventoryBooks=SimpleNamespace(value=False),
        ),
    )
    monkeypatch.setattr(
        services,
        "load_weekly_plan",
        lambda: {"cycle": ["岚心城", "武林源"]},
    )
    monkeypatch.setattr(
        services,
        "progress_summary",
        lambda _state: {"finished": False, "remaining_books": 1},
    )
    monkeypatch.setattr(business, "unavailable_stations", lambda _cycle: [])
    monkeypatch.setattr(business, "is_sell_page", lambda: False)
    navigation_calls = []

    def failed_buy_navigation(mode):
        navigation_calls.append(mode)
        return False

    monkeypatch.setattr(business, "go_business", failed_buy_navigation)

    assert business.adaptive_weekly_run() is False
    assert navigation_calls == ["buy"]

    evidence_roots = list((tmp_path / "logs" / "run_business").iterdir())
    assert len(evidence_roots) == 1
    metadata = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(evidence_roots[0].glob("*.metadata.json"))
    ]
    transitions = [row["state_transition_name"] for row in metadata]
    assert transitions == [
        "ADAPTIVE_PREFLIGHT_START",
        "ADAPTIVE_PREFLIGHT_PLAN_READY",
        "ADAPTIVE_PREFLIGHT_INVENTORY_BEFORE",
        "ADAPTIVE_PREFLIGHT_INVENTORY_AFTER",
        "ADAPTIVE_PREFLIGHT_BUY_PAGE_BEFORE",
        "ADAPTIVE_PREFLIGHT_BUY_PAGE_AFTER",
        "FINAL_CYCLE_RESULT",
    ]
    assert metadata[-2]["current_page_classification"] == (
        "BUY_PAGE_NOT_VERIFIED"
        "|stage=unknown"
        "|reason=structured_result_unavailable"
        "|clicked=None"
    )
    assert {row["cycle_id"] for row in metadata} == {metadata[0]["cycle_id"]}
    assert all(
        row["leg_id"] == "岚心城|武林源"
        for row in metadata[1:-1]
    )

    final = json.loads(
        (evidence_roots[0] / "FINAL_CYCLE.json").read_text(encoding="utf-8")
    )
    assert final["cycle_id"] == metadata[0]["cycle_id"]
    assert final["status"] == "RETURNED"
    assert final["result"] is False


def test_preflight_and_inner_run_share_one_evidence_session(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AUTO_RESONANCE_RUN_BUSINESS_EVIDENCE", "1")
    monkeypatch.setattr("core.control.control.screenshot", lambda: _Frame())
    ledger_context = {"cycle_id": "ledger-cycle"}
    monkeypatch.setattr(
        session_evidence,
        "_run_business_ledger_event_count",
        lambda context: 9 if context is ledger_context else 0,
    )

    @business._with_run_business_evidence
    def inner_run(routes, recovery_attempts=2, ledger_context=None):
        business._capture_run_business_evidence(
            "INNER_RUN_TRANSITION",
            ledger_context=ledger_context,
            leg_id="A|B",
            current_page_classification="INNER_PAGE",
        )
        return True

    @business._with_run_business_preflight_evidence
    def outer_preflight():
        business._capture_run_business_evidence(
            "OUTER_PREFLIGHT_TRANSITION",
            ledger_context=None,
            leg_id="A|B",
            current_page_classification="OUTER_PAGE",
        )
        return inner_run("routes", ledger_context=ledger_context)

    assert outer_preflight() is True
    evidence_roots = list((tmp_path / "logs" / "run_business").iterdir())
    assert len(evidence_roots) == 1
    metadata = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(evidence_roots[0].glob("*.metadata.json"))
    ]
    assert [row["state_transition_name"] for row in metadata] == [
        "ADAPTIVE_PREFLIGHT_START",
        "OUTER_PREFLIGHT_TRANSITION",
        "INNER_RUN_TRANSITION",
        "FINAL_CYCLE_RESULT",
    ]
    assert len({row["cycle_id"] for row in metadata}) == 1
    final = json.loads(
        (evidence_roots[0] / "FINAL_CYCLE.json").read_text(encoding="utf-8")
    )
    assert final["ledger_event_count"] == 9
    assert final["result"] is True
