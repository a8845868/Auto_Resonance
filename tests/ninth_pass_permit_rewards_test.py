from __future__ import annotations

import inspect
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import auto.reward_collection as rewards
import core.control.control as control_module
from auto.reward_collection import (
    CardClaimState,
    DailyCardScanner,
    DailyTaskCard,
    ManualCardScanner,
    ManualRewardTrackScanner,
    MovementObservation,
    MovementState,
    _card_items,
    _horizontal_displacement,
    _page_scan_evidence,
)
from core.services.read_only_policy import (
    ActionIntent,
    AnchorResolver,
    ObservedAnchor,
    PageObservation,
    PageObserver,
    ReadOnlyActionGuard,
    ReadOnlyPermitIssuer,
)


NOW = datetime(2026, 7, 19, 12, 0, tzinfo=timezone(timedelta(hours=8)))


def _observation(
    *,
    page_type: str = "daily_activity",
    anchors: tuple[ObservedAnchor, ...] | None = None,
    markers: tuple[str, ...] = ("每日活跃",),
    captured_at: datetime = NOW,
) -> PageObservation:
    return PageObservation(
        observation_id="obs-1",
        screenshot_hash="a" * 64,
        page_type=page_type,
        markers=markers,
        anchors=anchors or (
            ObservedAnchor("top_left_back", "返回", (20, 20, 100, 70)),
            ObservedAnchor("daily_content", "任务列表", (150, 180, 1180, 650)),
        ),
        captured_at=captured_at,
    )


def _guard(observation: PageObservation | None = None):
    state = {"value": observation or _observation()}
    observer = PageObserver(lambda: state["value"])
    issuer = ReadOnlyPermitIssuer(observer, AnchorResolver(), now=lambda: NOW)
    return ReadOnlyActionGuard(permit_issuer=issuer, now=lambda: NOW), issuer, state


def test_caller_cannot_self_issue_read_only_permit():
    guard, _issuer, _state = _guard()
    with pytest.raises(PermissionError, match="trusted issuer"):
        guard.issue_permit(
            action_key="reward_back", page_id="fake", page_fingerprint="fake",
            anchor_key="top_left_back", coordinate=(50, 40),
        )


def test_fake_page_id_and_anchor_are_rejected():
    _guard_value, issuer, _state = _guard(_observation(page_type="account_settings", markers=("注销",)))
    with pytest.raises(PermissionError):
        issuer.issue(ActionIntent("reward_back", "invented_anchor", "fake"), ((50, 40),))


def test_navigation_intent_on_reward_claim_coordinate_is_rejected():
    observation = _observation(anchors=(
        ObservedAnchor("top_left_back", "返回", (20, 20, 100, 70)),
        ObservedAnchor("reward_claim", "可领取", (850, 520, 1050, 650)),
    ))
    _guard_value, issuer, _state = _guard(observation)
    with pytest.raises(PermissionError, match="anchor|coordinate"):
        issuer.issue(ActionIntent("reward_back", "top_left_back", "claim"), ((930, 590),))


def test_navigation_intent_on_account_logout_coordinate_is_rejected():
    observation = _observation(
        page_type="account_settings",
        markers=("账号设置", "注销"),
        anchors=(ObservedAnchor("logout", "注销", (850, 520, 1050, 650)),),
    )
    _guard_value, issuer, _state = _guard(observation)
    with pytest.raises(PermissionError):
        issuer.issue(ActionIntent("reward_back", "logout", "logout"), ((930, 590),))


def test_stale_page_observation_invalidates_permit():
    guard, issuer, state = _guard()
    permit = issuer.issue(ActionIntent("reward_back", "top_left_back", "stale"), ((50, 40),))
    state["value"] = _observation(captured_at=NOW - timedelta(seconds=30))
    assert guard.authorize_coordinate((50, 40), permit=permit) is False


def test_anchor_bbox_must_cover_authorized_coordinate():
    _guard_value, issuer, _state = _guard()
    with pytest.raises(PermissionError, match="anchor|coordinate"):
        issuer.issue(ActionIntent("reward_back", "top_left_back", "bbox"), ((120, 90),))


def test_random_offset_cannot_escape_permit_bounds(monkeypatch):
    guard, _issuer, _state = _guard()
    device = SimpleNamespace(ratio=1, input_tap=lambda x, y: calls.append((x, y)))
    calls = []
    monkeypatch.setattr(control_module, "control", device)
    monkeypatch.setattr(control_module.random, "randint", lambda *_args: 999)
    previous = control_module.install_action_policy(guard)
    try:
        assert control_module.input_tap(
            (50, 40), random_offset=True,
            intent=ActionIntent("reward_back", "top_left_back", "offset"),
        ) is True
    finally:
        control_module.install_action_policy(previous)
    assert calls == [(50, 40)]


def test_read_only_mode_disables_post_authorization_randomization(monkeypatch):
    test_random_offset_cannot_escape_permit_bounds(monkeypatch)


def test_swipe_end_outside_region_is_rejected():
    _guard_value, issuer, _state = _guard()
    with pytest.raises(PermissionError, match="trajectory|region"):
        issuer.issue(
            ActionIntent("daily_horizontal_scroll", "daily_content", "swipe-end"),
            ((900, 350), (1200, 700)),
        )


def test_swipe_segment_outside_region_is_rejected():
    _guard_value, issuer, _state = _guard()
    with pytest.raises(PermissionError, match="trajectory|region"):
        issuer.issue(
            ActionIntent("daily_horizontal_scroll", "daily_content", "swipe-segment"),
            ((900, 350), (50, 50), (400, 350)),
        )


def test_permit_is_single_use_and_observation_bound():
    guard, issuer, _state = _guard()
    permit = issuer.issue(ActionIntent("reward_back", "top_left_back", "once"), ((50, 40),))
    assert guard.authorize_coordinate((50, 40), permit=permit) is True
    assert guard.authorize_coordinate((50, 40), permit=permit) is False


def _ocr(text: str, x: int, y: int) -> dict:
    return {"text": text, "position": [[x - 5, y - 5], [x + 5, y - 5], [x + 5, y + 5], [x - 5, y + 5]]}


def _card_items_frame(status: str | None = None, *, manual: bool = False) -> list[dict]:
    ratio_y = 330 if not manual else 340
    title_y = 395 if not manual else 275
    status_y = 620 if not manual else 545
    result = [_ocr("1/1", 500, ratio_y), _ocr("完成任务", 500, title_y)]
    if status:
        result.append(_ocr(status, 500, status_y))
    return result


def test_completed_card_without_status_text_is_unknown():
    card = _card_items(_card_items_frame(), manual=False)[0]
    assert card.claim_state is CardClaimState.UNKNOWN


def test_status_ocr_miss_does_not_become_no_reward():
    card = _card_items(_card_items_frame(manual=True), manual=True)[0]
    assert card.claimable is None and card.claimed is None


def test_known_claimable_survives_later_ocr_miss():
    scanner = DailyCardScanner()
    scanner.add_page(_card_items_frame("可领取"))
    scanner.add_page(_card_items_frame())
    assert scanner.cards[0].claim_state is CardClaimState.CLAIMABLE


def test_conflicting_claim_state_remains_unknown():
    scanner = DailyCardScanner()
    scanner.add_page(_card_items_frame("可领取"))
    scanner.add_page(_card_items_frame("已领取"))
    assert scanner.cards[0].claim_state is CardClaimState.CONFLICT


def test_complete_inventory_with_unknown_card_cannot_be_complete():
    scanner = DailyCardScanner()
    scanner.add_page(_card_items_frame())
    assert scanner.claim_states_complete is False


def test_hidden_claimable_card_cannot_schedule_next_server_day():
    scanner = ManualCardScanner()
    scanner.add_page(_card_items_frame(manual=True))
    assert scanner.claim_states_complete is False


def _movement(state: MovementState, displacement: int | None = None, matches: int = 2) -> MovementObservation:
    return MovementObservation(
        state=state,
        displacement_px=displacement,
        matched_content_items=matches,
        dispersion=0.0 if state is not MovementState.UNKNOWN else None,
        direction_consistent=state is not MovementState.UNKNOWN,
        fixed_anchor_displacement=0,
    )


def _card(name: str = "task") -> DailyTaskCard:
    return DailyTaskCard(name, name, 1, 1, True, False, True, 10, "page")


def test_one_move_then_two_unknown_frames_is_not_complete():
    scanner = DailyCardScanner()
    for index, movement in enumerate((
        _movement(MovementState.MOVED, -300),
        _movement(MovementState.UNKNOWN),
        _movement(MovementState.UNKNOWN),
    )):
        scanner.add_cards([_card(str(index))], evidence=_page_scan_evidence(scanner, movement, NOW + timedelta(seconds=index)))
    assert scanner.complete is False


def test_unknown_displacement_does_not_increment_end_candidate():
    scanner = DailyCardScanner()
    scanner.add_cards([_card()], evidence=_page_scan_evidence(scanner, _movement(MovementState.MOVED, -300), NOW))
    evidence = _page_scan_evidence(scanner, _movement(MovementState.UNKNOWN), NOW + timedelta(seconds=1))
    assert evidence.end_candidate_sequence == 0


def test_stationary_requires_minimum_matched_content_items():
    before = [_ocr("任务A", 900, 350)]
    after = [_ocr("任务A", 901, 350)]
    assert _horizontal_displacement(before, after, content_roi=(150, 200, 1150, 650)).state is MovementState.UNKNOWN


def test_fixed_header_only_matches_do_not_confirm_end():
    before = [_ocr("每日活跃", 300, 50), _ocr("返回", 80, 50)]
    after = [_ocr("每日活跃", 300, 50), _ocr("返回", 80, 50)]
    observed = _horizontal_displacement(before, after, content_roi=(150, 200, 1150, 650))
    assert observed.state is MovementState.UNKNOWN


def test_later_movement_clears_previous_end_candidate():
    scanner = DailyCardScanner()
    scanner.add_cards([_card()], evidence=_page_scan_evidence(scanner, _movement(MovementState.MOVED, -300), NOW))
    scanner.add_cards([_card()], evidence=_page_scan_evidence(scanner, _movement(MovementState.STATIONARY_CONFIRMED, 0), NOW + timedelta(seconds=1)))
    scanner.add_cards([_card()], evidence=_page_scan_evidence(scanner, _movement(MovementState.MOVED, -200), NOW + timedelta(seconds=2)))
    assert scanner.complete is False


def test_production_daily_loop_preserves_unknown_displacement():
    assert "displacement or 0" not in inspect.getsource(rewards.RewardCollector.observe_daily_activity)
    evidence = _page_scan_evidence(DailyCardScanner(), _movement(MovementState.UNKNOWN), NOW)
    assert evidence.content_displacement_px is None


def test_production_manual_loop_preserves_unknown_displacement():
    assert "displacement or 0" not in inspect.getsource(rewards.RewardCollector.observe_travel_manual)
    evidence = _page_scan_evidence(ManualCardScanner(), _movement(MovementState.UNKNOWN), NOW)
    assert evidence.content_displacement_px is None


def test_manual_track_unknown_frames_cannot_complete_track():
    scanner = ManualRewardTrackScanner()
    scanner.add_segments([], _page_scan_evidence(scanner, _movement(MovementState.MOVED, -300), NOW))
    scanner.add_segments([], _page_scan_evidence(scanner, _movement(MovementState.UNKNOWN), NOW + timedelta(seconds=1)))
    scanner.add_segments([], _page_scan_evidence(scanner, _movement(MovementState.UNKNOWN), NOW + timedelta(seconds=2)))
    assert scanner.complete is False
