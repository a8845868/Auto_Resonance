from __future__ import annotations

from datetime import datetime, timezone

import numpy as np
import pytest


from core.services.startup_coordinator import (
    Actionability,
    CoordinatorState,
    HANDLER_REGISTRY,
    OverlayKind,
    STATE_CONTRACTS,
    StartupBudgets,
    StartupCoordinator,
    StartupDeadlines,
    classify_startup_frame,
)


NOW = datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)


def _item(text: str, x: int = 500, y: int = 350) -> dict:
    return {
        "text": text,
        "position": [
            [x - 30, y - 12], [x + 30, y - 12],
            [x + 30, y + 12], [x - 30, y + 12],
        ],
    }


class _Frame:
    serial = 0

    def __init__(
        self,
        *texts: str,
        fingerprint: str | None = None,
        sequence: str | None = None,
        visual_evidence=(),
    ):
        type(self).serial += 1
        serial = type(self).serial
        self.raw_frame_hash = fingerprint or f"frame-{serial:04d}"
        self.backend_monotonic_sequence = sequence or str(serial)
        self.visual_evidence = visual_evidence
        self.image = np.full((12, 12, 3), serial % 255, dtype=np.uint8)
        self._items = []
        for index, text in enumerate(texts):
            x = 220 + (index * 145)
            y = 90 if text in {"关闭", "退出", "返回", "稍后", "以后再说"} else 350
            self._items.append(_item(text, x=x, y=y))

    def ocr(self):
        return list(self._items)


class _Clock:
    def __init__(self):
        self.value = 100.0

    def __call__(self):
        return self.value

    def sleep(self, seconds: float):
        self.value += float(seconds)


def _loading(count: int):
    return [_Frame("正在加载") for _ in range(count)]


def _resource(count: int):
    return [_Frame("正在校验资源") for _ in range(count)]


def _home(count: int = 3, *, overlay: tuple[str, ...] = ()):
    return [
        _Frame("访问城市", "作战终端", "启程", *overlay)
        for _ in range(count)
    ]


def _city_detail(count: int = 3):
    return [_Frame("城市详情", "城市设施", "交易所") for _ in range(count)]


def _resolver(
    frames,
    *,
    overlay_dispatch=None,
    city_dispatch=None,
    allow_overlay=False,
    allow_city=False,
    interval=1.0,
    deadlines=None,
):
    values = iter(frames)
    clock = _Clock()
    return StartupCoordinator(
        frame_provider=lambda: next(values),
        overlay_dispatch=overlay_dispatch,
        city_entry_dispatch=city_dispatch,
        allow_overlay_actions=allow_overlay,
        allow_city_entry=allow_city,
        sleep=clock.sleep,
        monotonic=clock,
        now=lambda: NOW,
        sampling_interval=interval,
        budgets=StartupBudgets(
            observation_sample_budget=200,
            obstruction_action_budget=1,
            physical_dispatch_budget=1,
            recovery_escalation_budget=8,
        ),
        deadlines=deadlines or StartupDeadlines(
            emulator_startup=180,
            game_process_startup=120,
            game_loading_resource=300,
            home_ready=120,
            city_entry_postcondition=30,
            unknown_observation_only=20,
        ),
    )


def test_all_required_states_have_explicit_contracts():
    assert set(STATE_CONTRACTS) == set(CoordinatorState)
    for contract in STATE_CONTRACTS.values():
        assert contract.entry_evidence
        assert contract.allowed_read_observations
        assert "blind_tap" in contract.forbidden_actions
        assert contract.recovery_strategy


def test_handler_registry_is_fail_closed_and_fingerprint_bounded():
    assert {
        handler.name for handler in HANDLER_REGISTRY.values()
    } >= {
        "SIGNIN_HANDLER", "ANNOUNCEMENT_HANDLER",
        "ACTIVITY_OVERLAY_HANDLER", "UPDATE_HANDLER",
        "NETWORK_ERROR_HANDLER",
    }
    assert HANDLER_REGISTRY[OverlayKind.SIGNIN].involves_reward_or_resource
    assert HANDLER_REGISTRY[OverlayKind.SIGNIN].max_actions_per_fingerprint == 1
    assert HANDLER_REGISTRY[OverlayKind.FORCED_UPDATE].max_actions_per_fingerprint == 0


def test_base_home_survives_announcement_overlay_but_action_is_blocked():
    observed = classify_startup_frame(
        _Frame("访问城市", "作战终端", "启程", "游戏公告", "关闭")
    )
    assert observed.base_page == "HOME_READY"
    assert observed.overlays == (OverlayKind.ANNOUNCEMENT,)
    assert observed.actionability is Actionability.SAFE_ACTION_AVAILABLE
    assert observed.blocking_reason == "known_overlay_blocks_base_action"


def test_signin_claim_is_never_actionable_even_with_close_text():
    observed = classify_startup_frame(
        _Frame("访问城市", "作战终端", "每日签到奖励", "领取", "关闭")
    )
    assert observed.overlays == (OverlayKind.SIGNIN,)
    assert observed.actionability is Actionability.MANUAL_REQUIRED
    assert observed.blocking_reason == "reward_resource_or_account_action_present"


def test_game_loading_ten_frames_then_home_observes_more_than_three():
    result = _resolver(_loading(10) + _home()).resolve()
    assert result.state is CoordinatorState.HOME_READY
    assert result.status == "PASS"
    assert result.observation_count == 12
    assert result.physical_dispatch_count == 0


def test_resource_checking_thirty_seconds_then_home():
    result = _resolver(_resource(30) + _home()).resolve()
    assert result.state is CoordinatorState.HOME_READY
    assert result.observation_count == 32
    assert result.physical_dispatch_count == 0


@pytest.mark.parametrize(
    ("overlay_text", "expected_overlay"),
    [
        ("游戏公告", OverlayKind.ANNOUNCEMENT),
        ("活动说明", OverlayKind.ACTIVITY),
    ],
)
def test_known_overlay_is_dismissed_once_then_home(
    overlay_text: str, expected_overlay: OverlayKind,
):
    taps = []
    frames = _home(3, overlay=(overlay_text, "关闭")) + _home()
    result = _resolver(
        frames,
        overlay_dispatch=lambda point, *, intent: taps.append((point, intent)) or True,
        allow_overlay=True,
    ).resolve()
    assert result.state is CoordinatorState.HOME_READY
    assert result.obstruction_action_count == 1
    assert len(taps) == 1
    assert expected_overlay.value in result.trace[0].overlays
    assert taps[0][1].action_key == "dialog_cancel"


def test_signin_without_claim_can_close_once_but_never_claims():
    actions = []
    frames = _home(3, overlay=("每日签到奖励", "已领取", "关闭")) + _home()
    result = _resolver(
        frames,
        overlay_dispatch=lambda _point, *, intent: actions.append(intent) or True,
        allow_overlay=True,
    ).resolve()
    assert result.state is CoordinatorState.HOME_READY
    assert len(actions) == 1
    assert actions[0].action_key == "dialog_cancel"
    assert "claim" not in actions[0].correlation_id.lower()


def test_forced_update_is_manual_and_never_dispatched():
    actions = []
    frames = [_Frame("版本过低", "前往更新") for _ in range(3)]
    result = _resolver(
        frames,
        overlay_dispatch=lambda *args, **kwargs: actions.append((args, kwargs)),
        allow_overlay=True,
    ).resolve()
    assert result.state is CoordinatorState.UPDATE_REQUIRED
    assert result.status == "BLOCKED"
    assert actions == []


def test_completed_update_entry_screen_is_known_manual_without_input():
    frames = [_Frame("更新已经完成，请点击任意位置进入游戏") for _ in range(3)]
    result = _resolver(frames).resolve()
    assert result.state is CoordinatorState.UNKNOWN_MANUAL_REQUIRED
    assert result.reason == "manual_game_entry_required"
    assert result.physical_dispatch_count == 0
    assert result.obstruction_action_count == 0


def test_maintenance_blocks_automation_but_gui_remains_available():
    frames = [_Frame("服务器维护中") for _ in range(3)]
    result = _resolver(frames).resolve()
    assert result.state is CoordinatorState.MAINTENANCE
    assert result.status == "BLOCKED"
    assert result.gui_available is True
    assert result.physical_dispatch_count == 0


def test_unknown_fifteen_frames_then_home_recovers_without_input():
    frames = [_Frame("未识别动画") for _ in range(15)] + _home()
    result = _resolver(frames).resolve()
    assert result.state is CoordinatorState.HOME_READY
    assert result.observation_count == 17
    assert result.physical_dispatch_count == 0
    assert result.obstruction_action_count == 0


def test_persistent_unknown_reaches_manual_required_at_deadline():
    deadlines = StartupDeadlines(180, 120, 300, 120, 30, 5)
    result = _resolver(
        [_Frame("永久未知") for _ in range(20)], deadlines=deadlines,
    ).resolve()
    assert result.state is CoordinatorState.UNKNOWN_MANUAL_REQUIRED
    assert result.reason == "state_deadline_expired:UNKNOWN_RECOVERABLE"
    assert result.observation_count > 3
    assert result.physical_dispatch_count == 0


def test_home_under_overlay_never_enters_city_until_overlay_clears():
    dispatches = []
    frames = (
        _home(3, overlay=("游戏公告", "关闭"))
        + _home(3)
        + [_Frame() for _ in range(3)]
        + _city_detail()
    )
    result = _resolver(
        frames,
        overlay_dispatch=lambda *_args, **_kwargs: True,
        city_dispatch=lambda point, *, intent: dispatches.append((point, intent)) or True,
        allow_overlay=True,
        allow_city=True,
    ).resolve()
    assert result.state is CoordinatorState.CITY_DETAIL
    assert result.physical_dispatch_count == 1
    assert len(dispatches) == 1
    first_dispatch_event = next(
        event for event in result.trace if event.physical_dispatch_count == 1
    )
    assert first_dispatch_event.overlays == ()


def test_slow_city_transition_keeps_enter_city_at_most_once():
    dispatches = []
    frames = _home(3) + [_Frame() for _ in range(20)] + _city_detail()
    result = _resolver(
        frames,
        city_dispatch=lambda point, *, intent: dispatches.append((point, intent)) or True,
        allow_city=True,
    ).resolve()
    assert result.state is CoordinatorState.CITY_DETAIL
    assert result.observation_count == 25
    assert result.physical_dispatch_count == 1
    assert len(dispatches) == 1


def test_city_postcondition_deadline_does_not_reset_on_persistent_home():
    deadlines = StartupDeadlines(180, 120, 300, 120, 5, 20)
    frames = _home(3) + _home(20)
    result = _resolver(
        frames,
        city_dispatch=lambda *_args, **_kwargs: True,
        allow_city=True,
        deadlines=deadlines,
    ).resolve()
    assert result.state is CoordinatorState.UNKNOWN_MANUAL_REQUIRED
    assert result.reason == "state_deadline_expired:CITY_TRANSITION"
    assert result.physical_dispatch_count == 1


def test_guard_denied_city_request_is_not_counted_as_physical_dispatch():
    result = _resolver(
        _home(3),
        city_dispatch=lambda *_args, **_kwargs: False,
        allow_city=True,
    ).resolve()
    assert result.state is CoordinatorState.UNKNOWN_MANUAL_REQUIRED
    assert result.reason == "city_entry_guard_denied"
    assert result.physical_dispatch_count == 0


def test_guard_exception_stays_fail_closed_without_physical_dispatch():
    def denied(*_args, **_kwargs):
        raise PermissionError("ledger_denied")

    result = _resolver(
        _home(3), city_dispatch=denied, allow_city=True,
    ).resolve()
    assert result.state is CoordinatorState.UNKNOWN_MANUAL_REQUIRED
    assert result.reason == "city_entry_guard_denied"
    assert result.physical_dispatch_count == 0


def test_stale_frames_do_not_count_toward_consensus():
    stale = [_Frame("访问城市", "作战终端", "启程", fingerprint="same", sequence="1") for _ in range(6)]
    result = _resolver(stale + _home()).resolve()
    assert result.state is CoordinatorState.HOME_READY
    assert result.stale_frame_count >= 5
    assert result.observation_count == 8


def test_conflicting_anchors_do_not_become_city_detail():
    observed = classify_startup_frame(
        _Frame("访问城市", "作战终端", "城市详情", "城市设施", "交易所")
    )
    assert observed.base_page == "UNKNOWN_RECOVERABLE"
    assert "conflicting_anchor_categories" in observed.evidence_categories


def test_blank_frame_is_not_city_transition_without_committed_action():
    observed = classify_startup_frame(_Frame())
    assert observed.base_page == "UNKNOWN_RECOVERABLE"
    assert observed.blocking_reason == "blank_frame_without_committed_transition"


def test_adb_sequence_change_does_not_redispatch_city_entry():
    dispatches = []
    home = [
        _Frame("访问城市", "作战终端", "启程", sequence=str(index))
        for index in range(1, 4)
    ]
    reconnect = [
        _Frame(sequence=str(index)) for index in range(1, 5)
    ]
    result = _resolver(
        home + reconnect + _city_detail(),
        city_dispatch=lambda point, *, intent: dispatches.append((point, intent)) or True,
        allow_city=True,
    ).resolve()
    assert result.state is CoordinatorState.CITY_DETAIL
    assert len(dispatches) == 1
    assert result.physical_dispatch_count == 1


def test_city_detail_requires_two_independent_evidence_categories():
    one = classify_startup_frame(_Frame("城市详情"))
    fused = classify_startup_frame(
        _Frame("城市详情", visual_evidence=("city_map_template",))
    )
    assert one.base_page == "UNKNOWN_RECOVERABLE"
    assert fused.base_page == "CITY_DETAIL"

# This module uses only injected frames and dispatch fakes.
