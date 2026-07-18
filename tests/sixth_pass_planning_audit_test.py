from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import cv2 as cv
import numpy as np
import pytest

from core.services.daily_capabilities import CurrentResourceEvidence, resolve_daily_capability_prerequisites
from core.services.trade_planning import validate_executable_trade_budget
from core.services.weekly_plan_state import progress_summary, save_current_resource_evidence
from tools.audit_export import SensitiveDataError, build_shareable_audit, verify_hash_manifest


NOW = datetime(2026, 7, 18, 12, 0, tzinfo=timezone(timedelta(hours=8)))


def _trade_state(**updates):
    state = {
        "cycle": ["A", "B"], "total_runs": 1, "completed_runs": 0,
        "runs": [{"A": 1, "B": 0}], "books_total": 1, "cycle_fatigue": 100,
        "expected_profit": 1000, "price_time": NOW.isoformat(),
        "price_source": "game_observed", "price_revision": "r1",
        "station_availability_evidence": {"available": ["A", "B"], "closed": [], "unknown": [], "source": "game_observed", "observed_at": NOW.isoformat(), "valid_until": (NOW + timedelta(minutes=5)).isoformat()},
    }
    state.update(updates)
    return state


def _resources(**updates):
    values = dict(fatigue_used=20, fatigue_cap=200, available_fatigue=180, recoverable_fatigue_today=0, purchase_books_available=1, source="game_observed", observed_at=NOW, valid_until=NOW + timedelta(minutes=10), server_day_id="2026-07-18", revision="obs-1")
    values.update(updates)
    return CurrentResourceEvidence(**values)


def test_required_fatigue_is_never_used_as_available_budget():
    result = resolve_daily_capability_prerequisites(trade_state=_trade_state(), passenger_state={}, now=NOW)
    assert result.evidence["fresh_trade_plan"].status == "UNKNOWN"


def test_required_books_are_never_used_as_inventory():
    result = resolve_daily_capability_prerequisites(trade_state=_trade_state(), passenger_state={}, now=NOW)
    assert "resource" in result.evidence["fresh_trade_plan"].block_reason


def test_unknown_actual_resources_block_trade_prerequisite():
    result = resolve_daily_capability_prerequisites(trade_state=_trade_state(), passenger_state={}, now=NOW)
    assert "fresh_trade_plan" not in result.satisfied


def test_insufficient_actual_fatigue_blocks_even_when_plan_requires_same_amount():
    result = resolve_daily_capability_prerequisites(trade_state=_trade_state(), passenger_state={}, now=NOW, resource_evidence=_resources(available_fatigue=99))
    assert result.evidence["fresh_trade_plan"].status == "BLOCKED"


def test_sufficient_fresh_actual_resources_satisfy_prerequisite():
    result = resolve_daily_capability_prerequisites(trade_state=_trade_state(), passenger_state={}, now=NOW, resource_evidence=_resources())
    assert "fresh_trade_plan" in result.satisfied


def test_stale_resource_evidence_is_unknown():
    result = resolve_daily_capability_prerequisites(trade_state=_trade_state(), passenger_state={}, now=NOW, resource_evidence=_resources(valid_until=NOW - timedelta(seconds=1)))
    assert result.evidence["fresh_trade_plan"].status == "UNKNOWN"


def test_no_observed_city_does_not_default_to_cycle_origin(monkeypatch):
    import core.services.weekly_plan_state as weekly
    monkeypatch.setattr(weekly, "load_trade_week_state", lambda *a, **k: type("Facts", (), {"current_partial_cycle": None, "server_week_id": "w", "source": type("S", (), {"value": "ledger"})(), "confidence": "HIGH", "baseline_known": True, "baseline_round_trips": 0, "tracking_started_at": None, "confirmed_delta_since_baseline": 0, "full_week_total": 0, "confirmed_round_trips": 0, "confirmed_legs": 0, "purchase_books_used": 0, "confirmed_profit": 0, "last_reconciled_at": None, "last_confirmed_transaction_at": None})())
    summary = progress_summary(_trade_state(current_resources=_resources().__dict__, recovery_resources={"recoverable_fatigue_today": 0}))
    assert summary["current_city"] is None
    assert summary["today_suggested_runs"] is None


def test_fresh_observed_city_drives_partial_schedule():
    state = _trade_state(current_city_evidence={"city": "A", "source": "game_observed", "observed_at": NOW.isoformat(), "valid_until": (NOW + timedelta(minutes=10)).isoformat(), "server_day_id": "2026-07-18", "revision": "loc-1"})
    assert state["current_city_evidence"]["city"] == "A"


def test_stale_current_resources_make_recommendation_unknown():
    stale = _resources(valid_until=NOW - timedelta(seconds=1)).to_dict()
    result = resolve_daily_capability_prerequisites(trade_state=_trade_state(current_resources=stale), passenger_state={}, now=NOW)
    assert result.evidence["fresh_trade_plan"].status == "UNKNOWN"


def test_recommendation_reports_each_missing_evidence_field():
    result = resolve_daily_capability_prerequisites(trade_state=_trade_state(), passenger_state={}, now=NOW)
    assert "current_resources" in result.evidence["fresh_trade_plan"].block_reason


def test_resource_observation_persists_source_ttl_and_revision(tmp_path):
    path = tmp_path / "weekly.json"
    saved = save_current_resource_evidence(_resources(), path=path)
    assert saved["current_resources"]["source"] == "game_observed"
    assert saved["current_resources"]["valid_until"]
    assert saved["current_resources"]["revision"] == "obs-1"


def test_audit_export_redacts_uid_from_ocr_json(tmp_path):
    source = tmp_path / "raw"; source.mkdir()
    (source / "manifest.json").write_text(json.dumps({"ocr": [{"text": "UID 123456789"}]}), encoding="utf-8")
    package = build_shareable_audit(source, tmp_path / "out", uid_values=("123456789",))
    text = (package / "manifest.json").read_text(encoding="utf-8")
    assert "123456789" not in text and "[REDACTED_ACCOUNT_ID]" in text


def test_audit_export_masks_uid_pixels(tmp_path):
    source = tmp_path / "raw"; source.mkdir()
    image = np.full((100, 200, 3), 255, np.uint8); cv.imwrite(str(source / "frame.png"), image)
    package = build_shareable_audit(
        source,
        tmp_path / "out",
        evidence_allowlist=("frame.png",),
        image_privacy_manifests={"frame.png": {
            "source_kind": "synthetic_test",
            "purpose": "mask_regression",
            "mask_regions": ((0, 70, 200, 100),),
            "privacy_review_status": "APPROVED",
            "contains_account_identifier": False,
            "contains_player_name": False,
            "contains_balance": False,
            "contains_payment_or_order": False,
        }},
        image_ocr_provider=lambda _path: (),
    )
    masked = cv.imread(str(package / "frame.png"))
    assert int(masked[80:95].max()) == 0


def test_audit_export_redacts_contact_data_before_gate(tmp_path):
    source = tmp_path / "raw"; source.mkdir()
    (source / "report.txt").write_text(
        "owner=user@example.com phone=13800138000", encoding="utf-8"
    )
    package = build_shareable_audit(source, tmp_path / "out")
    text = (package / "report.txt").read_text(encoding="utf-8")
    assert "user@example.com" not in text
    assert "13800138000" not in text
    assert "[REDACTED_EMAIL]" in text
    assert "[REDACTED_PHONE]" in text


def test_sensitive_pattern_blocks_shareable_export(tmp_path):
    source = tmp_path / "raw"; source.mkdir()
    (source / "secret.txt").write_text("Authorization: Bearer abcdefghijklmnopqrstuvwxyz", encoding="utf-8")
    with pytest.raises(SensitiveDataError):
        build_shareable_audit(source, tmp_path / "out")


def test_sanitized_export_hash_manifest_verifies_cross_platform(tmp_path):
    source = tmp_path / "raw"; source.mkdir(); (source / "safe.txt").write_text("safe", encoding="utf-8")
    package = build_shareable_audit(source, tmp_path / "out")
    manifest = (package / "SHA256SUMS.txt").read_text(encoding="utf-8")
    assert "\\" not in manifest
    assert verify_hash_manifest(package)
