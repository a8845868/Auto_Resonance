from types import SimpleNamespace

import numpy as np

import auto.inventory as inventory
from auto.run_business import main as business


class _Frame:
    def __init__(self, texts=()):
        self.image = np.zeros((720, 1280, 3), dtype=np.uint8)
        self._items = [
            {
                "text": text,
                "position": (
                    (100, 100 + index * 60),
                    (180, 100 + index * 60),
                    (180, 125 + index * 60),
                    (100, 125 + index * 60),
                ),
            }
            for index, text in enumerate(texts)
        ]

    def ocr(self):
        return list(self._items)


class _Recorder:
    def __init__(self):
        self.cycle_id = "inventory-cycle"
        self.latest_ledger_context = None
        self.rows = []

    def capture(self, label, **metadata):
        self.rows.append({"label": label, **metadata})
        return []


def test_evidence_off_keeps_restock_book_read_behavior_unchanged(monkeypatch):
    frames = iter(
        [
            _Frame(),
            _Frame(("进货采买书", "×12")),
            _Frame(("进货采买书", "×12")),
            _Frame(("进货采买书", "×12")),
        ]
    )
    monkeypatch.setattr(inventory, "connect", lambda: True)
    monkeypatch.setattr(inventory, "go_home", lambda: True)
    monkeypatch.setattr(inventory, "_open_assets_entry", lambda: True)
    monkeypatch.setattr(inventory, "screenshot", lambda: next(frames))
    monkeypatch.setattr(inventory.time, "sleep", lambda _seconds: None)

    token = business._RUN_BUSINESS_EVIDENCE_RECORDER.set(None)
    try:
        assert inventory.read_restock_book_count(max_pages=1) == 12
    finally:
        business._RUN_BUSINESS_EVIDENCE_RECORDER.reset(token)


def test_capture_with_no_recorder_has_zero_side_effect(monkeypatch):
    def forbidden_screenshot():
        raise AssertionError("recorder=None must not capture")

    monkeypatch.setattr(business, "screenshot", forbidden_screenshot)
    token = business._RUN_BUSINESS_EVIDENCE_RECORDER.set(None)
    try:
        business._capture_run_business_evidence(
            "INVENTORY_SCAN_START",
            ledger_context=None,
            leg_id="",
            current_page_classification="INVENTORY_PRE_NAVIGATION",
        )
    finally:
        business._RUN_BUSINESS_EVIDENCE_RECORDER.reset(token)


def test_evidence_on_records_inventory_navigation_metadata(monkeypatch):
    recorder = _Recorder()
    monkeypatch.setattr(inventory, "connect", lambda: True)
    monkeypatch.setattr(inventory, "screenshot", lambda: _Frame())
    monkeypatch.setattr(inventory, "go_home", lambda: True)
    monkeypatch.setattr(inventory, "_open_assets_entry", lambda: False)

    token = business._RUN_BUSINESS_EVIDENCE_RECORDER.set(recorder)
    try:
        assert inventory.read_restock_book_count() is None
    finally:
        business._RUN_BUSINESS_EVIDENCE_RECORDER.reset(token)

    assert [row["state_transition_name"] for row in recorder.rows] == [
        "INVENTORY_SCAN_START",
        "INVENTORY_GO_HOME_BEFORE",
        "INVENTORY_GO_HOME_AFTER",
        "INVENTORY_OPEN_ASSETS_BEFORE",
        "INVENTORY_OPEN_ASSETS_AFTER",
        "INVENTORY_SCAN_INCOMPLETE",
    ]
    assert [row["current_page_classification"] for row in recorder.rows] == [
        "INVENTORY_PRE_NAVIGATION",
        "INVENTORY_PRE_NAVIGATION",
        "HOME_VERIFIED",
        "HOME_VERIFIED",
        "ASSETS_ENTRY_NOT_VERIFIED",
        "ASSETS_ENTRY_NOT_VERIFIED",
    ]
    assert {row["cycle_id"] for row in recorder.rows} == {"inventory-cycle"}
    assert all(row["leg_id"] == "" for row in recorder.rows)
    assert all(row["ledger_event_count"] == 0 for row in recorder.rows)


def test_inventory_scan_evidence_records_page_and_confirmed_count(monkeypatch):
    recorder = _Recorder()
    frames = iter(
        [
            _Frame(),
            _Frame(("进货采买书", "×12")),
            _Frame(("进货采买书", "×12")),
            _Frame(("进货采买书", "×12")),
        ]
    )
    monkeypatch.setattr(inventory, "connect", lambda: True)
    monkeypatch.setattr(inventory, "go_home", lambda: True)
    monkeypatch.setattr(inventory, "_open_assets_entry", lambda: True)
    monkeypatch.setattr(inventory, "screenshot", lambda: next(frames))
    monkeypatch.setattr(inventory.time, "sleep", lambda _seconds: None)

    token = business._RUN_BUSINESS_EVIDENCE_RECORDER.set(recorder)
    try:
        assert inventory.read_restock_book_count(max_pages=1) == 12
    finally:
        business._RUN_BUSINESS_EVIDENCE_RECORDER.reset(token)

    transitions = [row["state_transition_name"] for row in recorder.rows]
    assert "INVENTORY_PAGE_SCANNED" in transitions
    assert transitions[-1] == "INVENTORY_COMPLETE_CONFIRMED"
    assert recorder.rows[-1]["current_page_classification"] == (
        "INVENTORY_COMPLETE_CONFIRMED"
    )


def test_inventory_scan_incomplete_distinguishes_uncertain_count(monkeypatch):
    recorder = _Recorder()
    frames = iter(
        [
            _Frame(),
            _Frame(("进货采买书", "×12")),
            _Frame(("进货采买书", "×12")),
        ]
    )
    monkeypatch.setattr(inventory, "connect", lambda: True)
    monkeypatch.setattr(inventory, "go_home", lambda: True)
    monkeypatch.setattr(inventory, "_open_assets_entry", lambda: True)
    monkeypatch.setattr(inventory, "screenshot", lambda: next(frames))
    monkeypatch.setattr(
        inventory, "_confirm_restock_book_count", lambda _items: None
    )
    monkeypatch.setattr(inventory, "input_tap", lambda _point: None)
    monkeypatch.setattr(inventory.time, "sleep", lambda _seconds: None)

    token = business._RUN_BUSINESS_EVIDENCE_RECORDER.set(recorder)
    try:
        assert inventory.read_restock_book_count(max_pages=1) is None
    finally:
        business._RUN_BUSINESS_EVIDENCE_RECORDER.reset(token)

    assert [row["state_transition_name"] for row in recorder.rows[-2:]] == [
        "INVENTORY_MULTI_FRAME_FAILED",
        "INVENTORY_SCAN_INCOMPLETE",
    ]
    assert recorder.rows[-1]["current_page_classification"] == (
        "INVENTORY_ITEM_COUNT_UNCERTAIN"
    )


def test_evidence_capture_failure_does_not_change_return_value(monkeypatch):
    monkeypatch.setattr(inventory, "connect", lambda: False)
    monkeypatch.setattr(
        inventory,
        "_capture_run_business_evidence",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("disk full")),
    )

    assert inventory.read_restock_book_count() is None


def test_train_in_transit_skips_all_internal_inventory_evidence(monkeypatch):
    recorder = _Recorder()
    go_home_calls = []
    monkeypatch.setattr(inventory, "connect", lambda: True)
    monkeypatch.setattr(
        inventory,
        "screenshot",
        lambda: _Frame(("自动巡航中", "剩余行程：830km")),
    )
    monkeypatch.setattr(inventory, "go_home", lambda: go_home_calls.append(True))

    token = business._RUN_BUSINESS_EVIDENCE_RECORDER.set(recorder)
    try:
        assert inventory.read_restock_book_count() is None
    finally:
        business._RUN_BUSINESS_EVIDENCE_RECORDER.reset(token)

    assert recorder.rows == []
    assert go_home_calls == []
