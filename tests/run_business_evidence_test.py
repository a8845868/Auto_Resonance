import json
from pathlib import Path

import numpy as np

from auto.run_business import main as business


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

    monkeypatch.setattr(business, "screenshot", fake_screenshot)
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

    monkeypatch.setattr(business, "screenshot", forbidden_screenshot)
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
    assert not (tmp_path / "logs").exists()
