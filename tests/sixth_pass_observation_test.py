from __future__ import annotations

import json
from pathlib import Path

from auto.reward_collection import (
    DailyCardScanner,
    ManualCardScanner,
    observe_daily_activity_layout,
    observe_manual_level_layout,
)


def _ocr(text: str, x: int, y: int) -> dict:
    return {"text": text, "position": [[x - 5, y - 5], [x + 5, y - 5], [x + 5, y + 5], [x - 5, y + 5]]}


def _daily_page(current_ratio: str = "1/1") -> list[dict]:
    items = [_ocr("每日活跃", 450, 56)]
    items += [_ocr(str(value), x, 142) for value, x in zip(range(100, 601, 100), (442, 562, 685, 806, 928, 1050))]
    items += [_ocr("今日首次登录", 200, 384), _ocr(current_ratio, 226, 332), _ocr("活跃度", 260, 524), _ocr("+100", 259, 548), _ocr("可领取", 227, 620)]
    return items


def _manual_page() -> list[dict]:
    return [
        _ocr("每日任务", 527, 153), _ocr("今日首次登录", 470, 281), _ocr("1/1", 469, 320),
        _ocr("10邮票积分", 469, 505), _ocr("领取", 470, 543),
        _ocr("获得5场战斗胜利", 677, 281), _ocr("0/5", 676, 320), _ocr("20邮票积分", 676, 505), _ocr("未完成", 680, 543),
    ]


def test_current_daily_layout_reads_stage_maximum():
    assert observe_daily_activity_layout([_daily_page()] * 3, page_complete=True).maximum == 600


def test_current_daily_layout_never_uses_task_ratio_as_total():
    observation = observe_daily_activity_layout([_daily_page("0/120")] * 3, page_complete=False)
    assert observation.maximum == 600
    assert observation.current is None


def test_daily_card_scanner_deduplicates_across_scroll():
    scanner = DailyCardScanner()
    scanner.add_page(_daily_page())
    scanner.add_page(_daily_page())
    assert len(scanner.cards) == 1


def test_daily_card_scanner_requires_end_of_list():
    scanner = DailyCardScanner()
    scanner.add_page(_daily_page())
    assert scanner.complete is False
    scanner.add_page(_daily_page())
    scanner.add_page(_daily_page())
    assert scanner.complete is True


def test_daily_current_can_be_derived_only_from_complete_card_inventory():
    scanner = DailyCardScanner()
    for _ in range(3):
        scanner.add_page(_daily_page())
    observation = observe_daily_activity_layout([_daily_page()] * 3, scanner=scanner)
    assert observation.current == 100
    assert observation.current_source == "complete_card_inventory"


def test_daily_visual_and_card_totals_conflict_to_unknown():
    scanner = DailyCardScanner()
    for _ in range(3):
        scanner.add_page(_daily_page())
    items = _daily_page() + [_ocr("200/600", 220, 200)]
    observation = observe_daily_activity_layout([items] * 3, scanner=scanner)
    assert observation.current is None
    assert observation.current_source == "conflict"


def test_fifth_pass_daily_evidence_produces_structured_observation():
    path = Path("dist/audit_output/AutoResonance-Pro-Fifth-Audit-20260718/evidence/daily-reward-stable/daily-reward-stable-manifest.json")
    frames = [item["ocr"] for item in json.loads(path.read_text(encoding="utf-8"))]
    scanner = DailyCardScanner()
    for frame in frames:
        scanner.add_page(frame)
    observation = observe_daily_activity_layout(frames, scanner=scanner)
    assert observation.maximum == 600
    assert observation.task_rewards_claimable >= 1
    assert any(card.completed for card in observation.task_cards)


def test_manual_cards_are_counted_only_after_full_scan():
    scanner = ManualCardScanner()
    scanner.add_page(_manual_page())
    assert scanner.summary() is None


def test_manual_individual_ratios_never_become_aggregate():
    scanner = ManualCardScanner()
    for _ in range(3):
        scanner.add_page(_manual_page())
    assert scanner.summary() == (1, 2)


def test_manual_task_cards_deduplicate_across_scroll():
    scanner = ManualCardScanner()
    for _ in range(3):
        scanner.add_page(_manual_page())
    assert len(scanner.cards) == 2


def test_manual_level_claimability_does_not_require_guessed_level():
    observation = observe_manual_level_layout([[_ocr("等级奖励", 500, 50), _ocr("可领取", 900, 500)]] * 2, track_scan_complete=True)
    assert observation.current_level is None
    assert observation.claimable_level_rewards == 1


def test_manual_locked_and_claimed_states_are_distinct():
    frames = [[_ocr("等级奖励", 500, 50), _ocr("已领取", 700, 500), _ocr("未解锁", 900, 500)]] * 2
    observation = observe_manual_level_layout(frames, track_scan_complete=True)
    assert observation.visible_claimed_levels == 1
    assert observation.visible_locked_levels == 1


def test_fifth_pass_manual_task_evidence_replays_as_card_inventory():
    path = Path("dist/audit_output/AutoResonance-Pro-Fifth-Audit-20260718/evidence/manual-task/manual-task-manifest.json")
    frames = [item["ocr"] for item in json.loads(path.read_text(encoding="utf-8"))]
    scanner = ManualCardScanner()
    for frame in frames:
        scanner.add_page(frame)
    assert scanner.complete
    assert scanner.summary()[1] >= 4


def test_fifth_pass_manual_level_evidence_replays_fail_closed_without_false_reward():
    path = Path("dist/audit_output/AutoResonance-Pro-Fifth-Audit-20260718/evidence/manual-level-stable/manual-level-stable-manifest.json")
    frames = [item["ocr"] for item in json.loads(path.read_text(encoding="utf-8"))]
    observation = observe_manual_level_layout(frames, track_scan_complete=True)
    assert observation.claimable_level_rewards == 0

