from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import cv2 as cv

from auto.reward_collection import (
    DailyCardScanner,
    DailyTaskCard,
    ManualRewardTrackScanner,
    ManualTrackSegment,
    PageScanEvidence,
    _horizontal_displacement,
    observe_daily_activity_layout,
    observe_manual_level_layout,
)


NOW = datetime(2026, 7, 18, 12, 0, tzinfo=timezone(timedelta(hours=8)))
EVIDENCE = Path("dist/audit_private/AutoResonance-Sixth-Private-20260718/instance0-rerun")


def _ocr(text: str, x: int, y: int) -> dict:
    return {"text": text, "position": [[x - 5, y - 5], [x + 5, y - 5], [x + 5, y + 5], [x - 5, y + 5]]}


def _daily(current: str | None = "0") -> list[dict]:
    items = [_ocr("每日活跃", 450, 56)]
    items += [_ocr(str(value), x, 142) for value, x in zip(range(100, 601, 100), (442, 562, 685, 806, 928, 1050))]
    if current is not None:
        items.append(_ocr(current, 225, 208))
    return items


def _card(title="任务完整标题", *, contribution=50, claimable=False, claimed=False):
    return DailyTaskCard(
        task_key="task", title=title, current=1, target=1, completed=True,
        claimable=claimable, claimed=claimed, contribution=contribution,
        page_fingerprint="frame",
    )


def test_repeated_same_page_does_not_complete_scan():
    scanner = DailyCardScanner()
    for _ in range(4):
        scanner.add_cards([_card()], evidence=PageScanEvidence(False, False, NOW))
    assert scanner.complete is False


def test_failed_swipe_does_not_complete_scan():
    scanner = DailyCardScanner()
    scanner.add_cards([_card()], evidence=PageScanEvidence(False, True, NOW))
    assert scanner.complete is False


def test_end_marker_completes_scan_after_real_movement():
    scanner = DailyCardScanner()
    scanner.add_cards([_card()], evidence=PageScanEvidence(False, False, NOW))
    scanner.add_cards([_card("任务完整标题二")], evidence=PageScanEvidence(True, True, NOW + timedelta(seconds=1), displacement_px=300))
    assert scanner.complete is True


def test_later_missing_contribution_does_not_erase_known_value():
    scanner = DailyCardScanner()
    scanner.add_cards([_card(contribution=50)])
    scanner.add_cards([_card(contribution=None)])
    assert scanner.cards[0].contribution == 50


def test_later_ocr_miss_does_not_erase_claimable():
    scanner = DailyCardScanner()
    scanner.add_cards([_card(claimable=True)])
    scanner.add_cards([_card(claimable=False)])
    assert scanner.cards[0].claimable is True


def test_conflicting_claim_state_is_unknown():
    scanner = DailyCardScanner()
    scanner.add_cards([_card(claimable=True)])
    scanner.add_cards([_card(claimed=True)])
    assert scanner.cards[0].claimable is None
    assert scanner.cards[0].claimed is None


def test_truncated_title_variants_do_not_double_count():
    scanner = DailyCardScanner()
    scanner.add_cards([_card("在营收报告排行榜中完成3次点赞")])
    scanner.add_cards([replace(_card("在营收报告排行榜中完成3次点"), task_key="truncated")])
    assert len(scanner.cards) == 1


def test_real_sixth_daily_duplicate_title_is_reconciled():
    raw = json.loads((EVIDENCE / "daily-observation.json").read_text(encoding="utf-8"))["ocr"]
    scanner = DailyCardScanner()
    scanner.add_page(raw)
    scanner.add_page(raw)
    assert len({card.task_key for card in scanner.cards}) == len(scanner.cards)


def test_standalone_daily_zero_is_observed():
    observed = observe_daily_activity_layout([_daily()] * 3, page_complete=False)
    assert observed.current == 0
    assert observed.confidence == "HIGH"
    assert observed.page_complete is False


def test_standalone_current_requires_daily_page_anchors():
    assert observe_daily_activity_layout([[_ocr("0", 225, 208)]] * 3, page_complete=True).current is None


def test_task_card_ratio_never_becomes_total_current():
    frames = [_daily(None) + [_ocr("1/1", 225, 332)]] * 3
    assert observe_daily_activity_layout(frames, page_complete=False).current is None


def test_visual_current_requires_multiframe_stability():
    frames = [_daily("0"), _daily("100"), _daily("0")]
    assert observe_daily_activity_layout(frames, page_complete=True).current is None


def test_visual_current_out_of_range_is_unknown():
    assert observe_daily_activity_layout([_daily("700")] * 3, page_complete=True).current is None


def test_real_sixth_daily_frame_produces_known_zero_of_600():
    raw = json.loads((EVIDENCE / "daily-observation.json").read_text(encoding="utf-8"))["ocr"]
    image = cv.imread(str(EVIDENCE / "daily-observation.png"))
    observed = observe_daily_activity_layout([raw] * 3, images=[image] * 3, page_complete=True)
    assert (observed.current, observed.maximum) == (0, 600)


def test_known_zero_snapshot_selects_exactly_one_safe_capability():
    from core.services.daily_capabilities import DailyCapability, select_daily_capabilities
    from core.services.daily_rewards import DailyProgressSnapshot, RewardStrategy
    snapshot = DailyProgressSnapshot(
        server_day_id="2026-07-18", daily_activity_current=0, daily_activity_max=600,
        daily_activity_source="game_observed", daily_activity_confidence="HIGH",
        daily_activity_claimable_tiers=0, daily_activity_unclaimed_tiers=6,
        handbook_daily_tasks_total=5, handbook_daily_tasks_completed=0,
        handbook_rewards_claimable=0, handbook_rewards_unclaimed=5, observed_at=NOW,
    )
    capabilities = [
        DailyCapability(task_key="safe", enabled=True, automation_available=True, activity_contribution_min=100, activity_contribution_max=100, handbook_contribution=1),
        DailyCapability(task_key="unsafe", enabled=True, automation_available=False, activity_contribution_min=100, activity_contribution_max=100, handbook_contribution=1),
    ]
    assert [item.task_key for item in select_daily_capabilities(capabilities, snapshot, RewardStrategy.MAXIMIZE_PROGRESS)] == ["safe"]


def test_visible_manual_segment_is_not_full_track():
    scanner = ManualRewardTrackScanner()
    scanner.add_segments([ManualTrackSegment(24, "slot-1", False, True, False)], PageScanEvidence(False, False, NOW))
    assert scanner.complete is False


def test_manual_track_requires_real_scroll_displacement():
    scanner = ManualRewardTrackScanner()
    scanner.add_segments([], PageScanEvidence(False, True, NOW))
    assert scanner.complete is False


def test_offscreen_claimable_level_reward_is_detected():
    scanner = ManualRewardTrackScanner()
    scanner.add_segments([ManualTrackSegment(24, "a", False, True, False)], PageScanEvidence(False, False, NOW))
    scanner.add_segments([ManualTrackSegment(28, "b", True, False, False)], PageScanEvidence(True, True, NOW + timedelta(seconds=1), 300))
    assert scanner.claimable_level_rewards == 1


def test_wrong_manual_page_cannot_be_high_confidence():
    observation = observe_manual_level_layout([[_ocr("任务列表", 500, 50)]] * 2, track_scan_complete=True)
    assert observation.confidence == "UNKNOWN"


def test_no_scroll_movement_without_end_evidence_is_unknown():
    scanner = ManualRewardTrackScanner()
    scanner.add_segments([], PageScanEvidence(False, False, NOW))
    assert scanner.observation().confidence == "UNKNOWN"


def test_manual_track_segments_deduplicate_by_level_and_slot():
    scanner = ManualRewardTrackScanner()
    segment = ManualTrackSegment(24, "slot-1", False, True, False)
    scanner.add_segments([segment], PageScanEvidence(False, False, NOW))
    scanner.add_segments([segment], PageScanEvidence(True, True, NOW + timedelta(seconds=1), 300))
    assert len(scanner.segments) == 1


def test_real_sixth_manual_frame_is_only_partial_track():
    raw = json.loads((EVIDENCE / "manual-observation.json").read_text(encoding="utf-8"))["ocr"]
    observation = observe_manual_level_layout([raw] * 2, track_scan_complete=False)
    assert observation.track_scan_complete is False
    assert observation.confidence == "UNKNOWN"


def test_track_scan_frames_are_time_separated():
    scanner = ManualRewardTrackScanner(min_frame_separation=timedelta(milliseconds=200))
    scanner.add_segments([], PageScanEvidence(False, False, NOW))
    scanner.add_segments([], PageScanEvidence(True, True, NOW + timedelta(milliseconds=50), 300))
    assert scanner.complete is False


def test_scanner_fails_closed_when_page_anchor_is_lost():
    scanner = DailyCardScanner()
    scanner.add_cards([_card()], evidence=PageScanEvidence(False, False, NOW))
    scanner.add_cards(
        [_card("other")],
        evidence=PageScanEvidence(True, True, NOW + timedelta(seconds=1), 300, False),
    )
    assert scanner.cancelled is True
    assert scanner.complete is False


def test_ocr_jitter_is_not_real_scroll_displacement():
    before = [_ocr("共同标题一", 300, 300), _ocr("共同标题二", 600, 300)]
    after = [_ocr("共同标题一", 304, 300), _ocr("共同标题二", 597, 300)]
    assert _horizontal_displacement(before, after) == 0
