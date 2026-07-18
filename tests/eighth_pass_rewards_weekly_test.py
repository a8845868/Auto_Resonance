from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

import auto.reward_collection as rewards
import core.services.weekly_plan_state as weekly
from auto.reward_collection import (
    DailyCardScanner,
    DailyTaskCard,
    ManualRewardTrackScanner,
    ManualTrackSegment,
    MovementState,
    PageScanEvidence,
    _horizontal_displacement,
)
from core.services.daily_capabilities import CurrentResourceEvidence
from core.services.daily_rewards import DailyProgressSnapshot, RewardRunStatus, decide_reward_run
from core.services.trade_planning import StalePriceSnapshot, validate_price_execution_evidence


NOW = datetime(2026, 7, 18, 12, 0, tzinfo=timezone(timedelta(hours=8)))


def _snapshot(**updates) -> DailyProgressSnapshot:
    values = dict(
        server_day_id="2026-07-18", daily_activity_current=600,
        daily_activity_max=600, daily_activity_source="game_observed",
        daily_activity_confidence="HIGH", daily_activity_claimable_tiers=0,
        daily_activity_unclaimed_tiers=0, handbook_daily_tasks_total=1,
        handbook_daily_tasks_completed=1, handbook_rewards_claimable=0,
        handbook_rewards_unclaimed=0, observed_at=NOW,
        daily_task_inventory_complete=True, daily_stage_track_complete=True,
        daily_claim_state_confidence="HIGH", handbook_task_inventory_complete=True,
        handbook_level_track_complete=True, handbook_claim_state_confidence="HIGH",
        scan_revision="scan-r1", page_fingerprint="page-r1",
    )
    values.update(updates)
    return DailyProgressSnapshot(**values)


def test_incomplete_daily_card_inventory_prevents_complete_status():
    result = decide_reward_run(_snapshot(daily_task_inventory_complete=False), now=NOW)
    assert result.status is RewardRunStatus.UNKNOWN


def test_incomplete_daily_stage_track_prevents_complete_status():
    result = decide_reward_run(_snapshot(daily_stage_track_complete=False), now=NOW)
    assert result.status is RewardRunStatus.UNKNOWN


def test_collect_scheduled_rewards_preserves_scan_completeness_fields(monkeypatch):
    monkeypatch.setattr(rewards.RewardCollector, "run", lambda self, *_a: {"每日活跃": 0, "环游手册": 0})
    monkeypatch.setattr(rewards.RewardCollector, "observe_daily_activity", lambda self: {
        "current": 600, "maximum": 600, "claimable_tiers": 0, "unclaimed_tiers": 0,
        "confidence": "HIGH", "page_complete": False, "task_inventory_complete": False,
        "stage_track_complete": True, "claim_state_confidence": "HIGH",
        "scan_revision": "daily-r1", "page_fingerprint": "daily-page",
    })
    monkeypatch.setattr(rewards.RewardCollector, "observe_travel_manual", lambda self: {
        "completed": 1, "total": 1, "claimable_rewards": 0, "unclaimed_rewards": 0,
        "confidence": "HIGH", "task_inventory_complete": True,
        "level_track_complete": True, "claim_state_confidence": "HIGH",
        "scan_revision": "manual-r1", "page_fingerprint": "manual-page",
    })
    monkeypatch.setattr(rewards, "_save_state", lambda _state: None)
    result = rewards.collect_scheduled_rewards()
    assert result["status"] == "UNKNOWN"
    assert result["snapshot_after"]["daily_task_inventory_complete"] is False


def _ocr(text: str, x: int, y: int) -> dict:
    return {"text": text, "position": [[x - 5, y - 5], [x + 5, y - 5], [x + 5, y + 5], [x - 5, y + 5]]}


def test_fixed_page_anchors_do_not_mask_real_scroll_displacement():
    before = [
        _ocr("每日活跃", 300, 50), _ocr("返回", 80, 50),
        _ocr("任务标题一", 900, 350), _ocr("任务标题二", 1000, 430),
    ]
    after = [
        _ocr("每日活跃", 300, 50), _ocr("返回", 80, 50),
        _ocr("任务标题一", 400, 350), _ocr("任务标题二", 500, 430),
    ]
    observed = _horizontal_displacement(before, after, content_roi=(150, 200, 1150, 650))
    assert observed.state is MovementState.MOVED
    assert observed.displacement_px == -500
    assert observed.fixed_anchor_displacement == 0


def _card(name: str = "task") -> DailyTaskCard:
    return DailyTaskCard(name, name, 1, 1, True, False, False, 10, "page")


def _evidence(*, moved=False, displacement=0, swipe=True, end=False, sequence=0, at=NOW):
    return PageScanEvidence(
        movement_confirmed=moved, end_marker=end, captured_at=at,
        displacement_px=abs(displacement), swipe_attempted=swipe,
        content_displacement_px=displacement, matched_content_items=1,
        fixed_anchor_displacement_px=0, end_candidate_sequence=sequence,
        end_confirmed_after_last_move=end,
    )


def test_end_marker_must_follow_last_successful_movement():
    scanner = DailyCardScanner()
    scanner.add_cards([_card()], evidence=_evidence(end=True, sequence=2))
    scanner.add_cards([_card("later")], evidence=_evidence(moved=True, displacement=-300, at=NOW + timedelta(seconds=1)))
    assert scanner.complete is False


def test_movement_after_end_candidate_clears_end_candidate():
    scanner = DailyCardScanner()
    scanner.add_cards([_card()], evidence=_evidence(moved=True, displacement=-300))
    scanner.add_cards([_card()], evidence=_evidence(sequence=1, at=NOW + timedelta(seconds=1)))
    scanner.add_cards([_card("later")], evidence=_evidence(moved=True, displacement=-300, at=NOW + timedelta(seconds=2)))
    assert scanner.complete is False


def test_repeated_same_frame_never_completes_scan():
    scanner = DailyCardScanner()
    scanner.add_cards([_card()], evidence=_evidence(swipe=False, at=NOW))
    scanner.add_cards([_card()], evidence=_evidence(swipe=False, end=True, sequence=2, at=NOW))
    assert scanner.complete is False


def _segment(x: int, *, claimable=None, claimed=None, locked=None) -> ManualTrackSegment:
    return ManualTrackSegment(
        level=24, slot=f"x{x}", claimable=claimable, claimed=claimed, locked=locked,
        reward_lane="main", reward_type="level_reward", screen_x=x,
    )


def test_manual_same_level_different_screen_x_deduplicates():
    scanner = ManualRewardTrackScanner()
    scanner.add_segments([_segment(900)], _evidence(swipe=False))
    scanner.add_segments([_segment(400)], _evidence(moved=True, displacement=-500, at=NOW + timedelta(seconds=1)))
    assert len(scanner.segments) == 1


def test_manual_claimable_evidence_is_not_erased_by_ocr_miss():
    scanner = ManualRewardTrackScanner()
    scanner.add_segments([_segment(900, claimable=True)], _evidence(swipe=False))
    scanner.add_segments([_segment(400, claimable=None)], _evidence(moved=True, displacement=-500, at=NOW + timedelta(seconds=1)))
    assert scanner.segments[0].claimable is True


def test_manual_claim_state_conflict_remains_unknown():
    scanner = ManualRewardTrackScanner()
    scanner.add_segments([_segment(900, claimable=True)], _evidence(swipe=False))
    scanner.add_segments([_segment(400, claimable=False)], _evidence(moved=True, displacement=-500, at=NOW + timedelta(seconds=1)))
    assert scanner.segments[0].claimable is None


def test_manual_max_visible_reward_level_is_not_player_level():
    scanner = ManualRewardTrackScanner()
    scanner.add_segments([_segment(900, locked=True)], _evidence(swipe=False))
    assert scanner.observation().current_level is None


def _weekly_state() -> dict:
    return {
        "week_start": weekly.current_week_start(), "cycle": ["A", "B"],
        "runs": [{"A": 0, "B": 0}], "total_runs": 1, "completed_runs": 0,
        "completed_books": 0, "books_total": 0,
    }


def _resource(revision: str = "resources-r2") -> CurrentResourceEvidence:
    return CurrentResourceEvidence(
        fatigue_used=0, fatigue_cap=200, available_fatigue=200,
        recoverable_fatigue_today=0, purchase_books_available=0,
        source="game_observed", observed_at=NOW, valid_until=NOW + timedelta(minutes=10),
        server_day_id="2026-07-18", revision=revision,
    )


def _patch_completed_facts(monkeypatch):
    cycle = SimpleNamespace(ready_to_finalize=False, phase="CYCLE_COMPLETED")
    facts = SimpleNamespace(baseline_known=True, full_week_total=1, confirmed_delta_since_baseline=1)
    monkeypatch.setattr(weekly, "load_trade_cycle_state", lambda *_a, **_k: cycle)
    monkeypatch.setattr(weekly, "load_trade_week_state", lambda *_a, **_k: facts)


def test_record_completed_run_and_resource_update_do_not_clobber(tmp_path: Path, monkeypatch):
    path = tmp_path / "weekly.json"; path.write_text(json.dumps(_weekly_state()), encoding="utf-8")
    _patch_completed_facts(monkeypatch)
    barrier = threading.Barrier(2)
    original = weekly.update_weekly_state
    def delayed(mutator, *, path=None):
        barrier.wait()
        return original(mutator, path=path)
    monkeypatch.setattr(weekly, "update_weekly_state", delayed)
    errors = []
    def complete():
        try: weekly.record_completed_run({}, ledger_path=tmp_path / "ledger", path=path)
        except Exception as exc: errors.append(exc)
    def resource():
        try: weekly.save_current_resource_evidence(_resource(), path=path)
        except Exception as exc: errors.append(exc)
    threads = [threading.Thread(target=complete), threading.Thread(target=resource)]
    [t.start() for t in threads]; [t.join() for t in threads]
    assert not errors
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["completed_runs"] == 1 and saved["current_resources"]["revision"] == "resources-r2"


def test_record_completed_run_and_city_update_do_not_clobber(tmp_path: Path, monkeypatch):
    path = tmp_path / "weekly.json"; path.write_text(json.dumps(_weekly_state()), encoding="utf-8")
    _patch_completed_facts(monkeypatch)
    barrier = threading.Barrier(2); original = weekly.update_weekly_state
    monkeypatch.setattr(weekly, "update_weekly_state", lambda mutator, *, path=None: (barrier.wait(), original(mutator, path=path))[1])
    errors = []
    threads = [
        threading.Thread(target=lambda: weekly.record_completed_run({}, ledger_path=tmp_path / "ledger", path=path)),
        threading.Thread(target=lambda: weekly.save_current_city_evidence("A", source="game_observed", observed_at=NOW, valid_until=NOW + timedelta(minutes=10), revision="city-r2", path=path)),
    ]
    [t.start() for t in threads]; [t.join() for t in threads]
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["completed_runs"] == 1 and saved["current_city_evidence"]["revision"] == "city-r2"


def test_week_rollover_and_resource_update_do_not_clobber(tmp_path: Path, monkeypatch):
    path = tmp_path / "weekly.json"; state = _weekly_state(); state["week_start"] = "2000-01-03"; path.write_text(json.dumps(state), encoding="utf-8")
    barrier = threading.Barrier(2); original = weekly.update_weekly_state
    monkeypatch.setattr(weekly, "update_weekly_state", lambda mutator, *, path=None: (barrier.wait(), original(mutator, path=path))[1])
    threads = [
        threading.Thread(target=weekly.roll_weekly_plan_forward, kwargs={"path": path}),
        threading.Thread(target=weekly.save_current_resource_evidence, args=(_resource(),), kwargs={"path": path}),
    ]
    [t.start() for t in threads]; [t.join() for t in threads]
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["week_start"] == weekly.current_week_start()
    assert saved["current_resources"]["revision"] == "resources-r2"


def test_three_city_plan_requires_price_evidence_for_every_edge():
    state = {
        "schema_version": 3, "cycle": ["A", "B", "C"], "price_time": NOW.isoformat(),
        "price_source": "game_observed", "price_revision": "r1",
        "current_price_evidence": {"source": "game_observed", "observed_at": NOW.isoformat(),
            "valid_until": (NOW + timedelta(minutes=10)).isoformat(), "revision": "r1",
            "station_pair": ["A", "B"], "server_day_id": "2026-07-18"},
    }
    with pytest.raises(StalePriceSnapshot, match="two-station|every route edge"):
        validate_price_execution_evidence(state, now=NOW)
