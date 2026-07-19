from __future__ import annotations

import inspect
import json
from datetime import datetime, timedelta, timezone

import pytest

import auto.reward_collection as rewards
from core.services import fatigue_triggers as triggers
from core.services.read_only_policy import (
    ActionIntent,
    AnchorResolver,
    ObservedAnchor,
    PageObservation,
    PageObserver,
    ReadOnlyActionGuard,
    ReadOnlyPermitIssuer,
)
from core.services.server_calendar import SERVER_CLOCK
from core.services.task_schedule_state import request_immediate_run


NOW = datetime(2026, 7, 19, 12, 0, tzinfo=timezone(timedelta(hours=8)))


def _observation() -> PageObservation:
    return PageObservation(
        observation_id="eleventh-red-observation",
        screenshot_hash="e" * 64,
        page_type="daily_activity",
        markers=("daily_activity",),
        anchors=(
            ObservedAnchor("top_left_back", "返回", (20, 10, 130, 85)),
            ObservedAnchor("daily_content", "任务列表", (150, 180, 1180, 650)),
        ),
        captured_at=NOW,
    )


def _guard(*, execute=None):
    observation = _observation()
    issuer = ReadOnlyPermitIssuer(
        PageObserver(lambda: observation), AnchorResolver(), now=lambda: NOW
    )
    return ReadOnlyActionGuard(
        execute or (lambda _point: None), permit_issuer=issuer, now=lambda: NOW
    ), issuer


def _ocr(text: str, x: int, y: int) -> dict:
    return {
        "text": text,
        "position": [[x - 5, y - 5], [x + 5, y - 5], [x + 5, y + 5], [x - 5, y + 5]],
    }


def _card_frame(status: str | None, *, current: int = 1, target: int = 2) -> list[dict]:
    items = [
        _ocr(f"{current}/{target}", 300, 350),
        _ocr("安全运输", 300, 400),
        _ocr("+10", 300, 550),
    ]
    if status is not None:
        items.append(_ocr(status, 300, 620))
    return items


def test_scroll_policy_cannot_be_consumed_by_coordinate_api():
    guard, _issuer = _guard()

    assert guard.authorize_coordinate(
        (1000, 620),
        intent=ActionIntent(
            "daily_horizontal_scroll", "daily_content", "modality-confusion"
        ),
    ) is False
    assert guard.journal[-1].reason == "action_kind_mismatch"


def test_public_guard_api_has_no_per_call_executor_callback():
    assert "_execute" not in inspect.signature(
        ReadOnlyActionGuard.authorize_coordinate
    ).parameters
    assert "_execute" not in inspect.signature(
        ReadOnlyActionGuard.authorize_swipe
    ).parameters


def test_registry_record_does_not_alias_returned_handle_or_observation():
    _guard_value, issuer = _guard()
    observation = issuer.observer.observe()
    permit = issuer.issue(
        ActionIntent("reward_back", "top_left_back", "no-alias"), ((50, 40),)
    )
    entry = issuer._registry[permit.opaque_token]

    assert not hasattr(entry.record, "permit")
    assert all(
        getattr(entry.record, name) is not observation
        for name in entry.record.__slots__
    )


def test_executor_failure_is_journaled_as_execution_failed():
    def fail(_point):
        raise RuntimeError("device write failed")

    guard, _issuer = _guard(execute=fail)
    with pytest.raises(RuntimeError, match="device write failed"):
        guard.authorize_coordinate(
            (50, 40),
            intent=ActionIntent("reward_back", "top_left_back", "write-failed"),
        )

    assert guard.journal[-1].reason == "execution_failed"
    assert guard.journal[-1].allowed is False


def test_active_cycle_transfer_crosses_0500_without_day_mismatch(tmp_path):
    path = tmp_path / "fatigue.json"
    journal_day = SERVER_CLOCK.server_day_id()
    cycle_day = (datetime.fromisoformat(journal_day) - timedelta(days=1)).date().isoformat()
    now = SERVER_CLOCK.server_now()
    state = {
        "server_day_id": journal_day,
        "actions": [{
            "id": "checkpoint-b",
            "trigger_type": "WAYPOINT",
            "waypoint_id": "B",
            "plan_revision": "rev-b",
            "cycle_id": "B|C",
            "cycle_server_day": cycle_day,
            "state": "CLAIMED",
            "schedule_status": "CLAIMED",
            "owner_id": "worker",
            "lease_token": "lease",
            "lease_expires_at": (now + timedelta(minutes=10)).isoformat(),
        }],
    }
    path.write_text(json.dumps(state), encoding="utf-8")
    result = {
        "success": True,
        "deferred": True,
        "status": "DEFER_UNTIL_WAYPOINT",
        "transfer_intent": {
            "target_waypoint": "C",
            "trigger_type": "WAYPOINT",
            "action_payload": {"kind": "REOBSERVE", "waypoint_id": "C"},
            "source_plan_revision": "rev-c",
            "cycle_id": "B|C",
            "cycle_server_day": cycle_day,
            "reason": "cross-reset",
        },
    }

    completed = triggers.complete_fatigue_checkpoint_processing(
        "checkpoint-b", result, owner_id="worker", lease_token="lease", path=path
    )

    assert completed["outcome"] == "TRANSFER_TO_NEW_CHECKPOINT"
    assert completed["checkpoint"]["state"] == "SUPERSEDED"
    assert completed["replacement"]["cycle_server_day"] == cycle_day


def test_complete_inventory_with_no_completed_cards_has_known_claim_state():
    scanner = rewards.DailyCardScanner()
    scanner.add_page(_card_frame(None))
    scanner.add_page(_card_frame(None))
    scanner.add_page(_card_frame(None))

    assert scanner.complete is True
    assert scanner.cards
    assert not any(card.completed for card in scanner.cards)
    assert scanner.claim_states_complete is True


def test_not_claimable_text_is_not_claimable():
    card = rewards._card_items(_card_frame("不可领取"), manual=False)[0]

    assert card.claimable is False
    assert card.claimed is False


def test_no_claimable_text_is_not_claimable():
    card = rewards._card_items(_card_frame("无可领取"), manual=False)[0]

    assert card.claimable is False
    assert card.claimed is False


def test_corrupt_task_schedule_is_preserved_and_not_overwritten(tmp_path):
    path = tmp_path / "task_schedule.json"
    original = b'{"tasks":{"other":{"next_run":"2030-01-01T00:00:00"}},BROKEN'
    path.write_bytes(original)

    with pytest.raises(RuntimeError, match="corrupt|unreadable|invalid"):
        request_immediate_run("run_business", path)

    assert path.read_bytes() == original
    backups = list(tmp_path.glob("task_schedule.json.corrupt.*"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == original
