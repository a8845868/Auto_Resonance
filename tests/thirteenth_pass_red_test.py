from __future__ import annotations

import inspect
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

import auto.exchange_navigation as exchange
import auto.reward_collection as rewards
import core.control.control as control
import core.preset.control as preset_control
import core.preset.presets as presets
import core.services.read_only_policy as policy
import core.services.task_schedule_state as schedule
import tools.audit_export as audit_export
import tools.sixth_read_only_probe as probe


NOW = datetime(2026, 7, 19, 18, 0, tzinfo=timezone(timedelta(hours=8)))


class _Clock:
    def __init__(self):
        self.value = 100.0

    def __call__(self):
        return self.value

    def sleep(self, seconds):
        self.value += float(seconds)


def _city_frame(state, fingerprint, *, score=0.80, anchor=None):
    if anchor is None and state == "HOME":
        anchor = (1080, 450, 1260, 535)
    return SimpleNamespace(
        state=state,
        page_fingerprint=fingerprint,
        last_template_score=score,
        city_entry_anchor=anchor,
        diagnostics=(),
    )


def _replay_city_frame(index: int):
    payload = json.loads(
        (Path(__file__).parent / "fixtures" / "thirteenth_city_navigation_replay.json")
        .read_text(encoding="utf-8")
    )
    item = payload["frames"][index]
    anchor = None
    if item["state"] == "HOME":
        matches = [entry for entry in item["ocr"] if entry["text"] == "访问城市"]
        assert len(matches) == 1
        anchor = tuple(matches[0]["bbox"])
    return _city_frame(
        item["state"], item["fingerprint"],
        score=item["fame_score"], anchor=anchor,
    )


def _go_city(frames, *, tap=lambda *_args, **_kwargs: True, cancellation=None,
             max_attempts=3, timeout=2.0, stall_frames=2):
    clock = _Clock()
    values = iter(frames)
    return presets.go_city(
        frame_provider=lambda: next(values),
        frame_analyzer=lambda frame: frame,
        tap=tap,
        sleep=clock.sleep,
        monotonic=clock,
        cancellation=cancellation,
        max_attempts=max_attempts,
        timeout=timeout,
        stall_frames=stall_frames,
    )


def _observation(sequence: int, page_type="daily_activity"):
    anchors = (
        policy.ObservedAnchor("top_left_back", "返回", (20, 10, 130, 85)),
    )
    return policy.PageObservation(
        observation_id=f"obs-{sequence}",
        screenshot_hash=f"{sequence:064x}",
        page_type=page_type,
        markers=(page_type,),
        anchors=anchors,
        captured_at=NOW - timedelta(seconds=1) + timedelta(microseconds=sequence),
        capture_sequence=sequence,
        source_capture_id=f"backend-capture-{sequence}",
        source_monotonic_sequence=sequence,
        backend_generation=1,
        instance_id="test-instance-0",
        adb_serial="test-adb-0",
    )


def _city_policy_observation(sequence: int, page_type: str):
    anchors = (
        (policy.ObservedAnchor("city_entry", "访问城市", (1080, 450, 1260, 535)),)
        if page_type == "home" else ()
    )
    markers = ("top_level_hud",) if page_type == "home" else (page_type,)
    return policy.PageObservation(
        observation_id=f"city-obs-{sequence}",
        screenshot_hash=f"{sequence:064x}",
        page_type=page_type,
        markers=markers,
        anchors=anchors,
        captured_at=NOW - timedelta(seconds=1) + timedelta(microseconds=sequence),
        capture_sequence=sequence,
        source_capture_id=f"backend-city-capture-{sequence}",
        source_monotonic_sequence=sequence,
        backend_generation=1,
        instance_id="test-instance-0",
        adb_serial="test-adb-0",
    )


def _navigation_policy_observation(
    sequence: int, page_type: str, bbox=(848, 260, 954, 292),
):
    anchors = (
        (policy.ObservedAnchor("交易所", "交易所", bbox),)
        if page_type == "city_map" else ()
    )
    return policy.PageObservation(
        observation_id=f"navigation-obs-{sequence}",
        screenshot_hash=f"{sequence:064x}", page_type=page_type,
        markers=(page_type,), anchors=anchors,
        captured_at=NOW - timedelta(seconds=1) + timedelta(microseconds=sequence),
        capture_sequence=sequence,
        source_capture_id=f"backend-navigation-capture-{sequence}",
        source_monotonic_sequence=sequence, backend_generation=1,
        instance_id="test-instance-0", adb_serial="test-adb-0",
    )


class _Executor:
    def __init__(self):
        self.taps = []

    def tap(self, point):
        self.taps.append(tuple(point))

    def swipe(self, trajectory, duration_ms):
        pass


def _guard(captures):
    values = iter(captures)
    issuer = policy.ReadOnlyPermitIssuer(
        policy.PageObserver(lambda: next(values)),
        policy.AnchorResolver(),
        now=lambda: NOW,
        postcondition_sleep=lambda _seconds: None,
    )
    executor = _Executor()
    return policy.ReadOnlySafetySession(
        executor, permit_issuer=issuer, now=lambda: NOW
    ), issuer, executor


def _ocr(text, x, y):
    return {
        "text": text,
        "position": [[x - 5, y - 5], [x + 5, y - 5],
                     [x + 5, y + 5], [x - 5, y + 5]],
    }


def _card_frame(status):
    items = [_ocr("2/2", 300, 350), _ocr("安全运输", 300, 400), _ocr("+10", 300, 550)]
    if status is not None:
        items.append(_ocr(status, 300, 620))
    return rewards._card_items(items, manual=False)


def test_go_city_permanent_subthreshold_template_times_out():
    replay = _replay_city_frame(0)
    frames = [
        SimpleNamespace(**{**vars(replay), "page_fingerprint": f"home-{index}"})
        for index in range(4)
    ]
    result = _go_city(frames, max_attempts=3, timeout=20, stall_frames=99)
    assert result.success is False
    assert result.state.value == "TIMEOUT"
    assert result.attempts == 3
    assert result.last_template_score == pytest.approx(0.80)
    assert result.actions_executed == ("city_entry_navigation",)


def test_go_city_guard_denial_stops_immediately():
    calls = []
    result = _go_city(
        [_city_frame("HOME", "home", anchor=(1080, 450, 1260, 535))],
        tap=lambda *args, **kwargs: calls.append((args, kwargs)) or False,
    )
    assert result.state.value == "READ_ONLY_DENIED"
    assert result.attempts == 1
    assert len(calls) == 1


def test_go_city_dialogue_state_blocks_repeated_entry():
    taps = []
    result = _go_city([
        _replay_city_frame(0),
        _replay_city_frame(1),
    ], tap=lambda *args, **kwargs: taps.append(args) or True)
    assert result.success is False
    assert result.state.value == "NPC_DIALOGUE"
    assert len(taps) == 1


def test_go_city_unchanged_fingerprint_stalls():
    taps = []
    result = _go_city([
        _city_frame("HOME", "same", anchor=(1080, 450, 1260, 535)),
        _city_frame("HOME", "same", anchor=(1080, 450, 1260, 535)),
    ], tap=lambda *args, **kwargs: taps.append(args) or True)
    assert result.state.value == "STALLED"
    assert len(taps) == 1


def test_go_city_stop_request_cancels_promptly():
    checks = {"count": 0}
    def cancelled():
        checks["count"] += 1
        return checks["count"] >= 2
    result = _go_city([_city_frame("HOME", "home")], cancellation=cancelled)
    assert result.state.value == "CANCELLED"
    assert result.elapsed_seconds < 3


def test_go_outlets_does_not_continue_after_city_failure(monkeypatch):
    city_result_type = getattr(presets, "CityNavigationResult")
    state_type = getattr(presets, "CityNavigationState")
    failure = city_result_type(False, state_type.TIMEOUT, 3, 2.0, 0.8, "home", (), (), "timeout", ())
    calls = []
    result = presets.go_outlets(
        "交易所", city_navigator=lambda **kwargs: failure,
        ocr_click=lambda *args, **kwargs: calls.append("ocr"),
        swipe=lambda *args, **kwargs: calls.append("swipe"),
    )
    assert not result
    assert calls == []
    success = city_result_type(True, state_type.CITY_MAP, 2, 1.0, 0.8, "city", (), (), "", ())
    ocr_calls = []
    selected = presets.go_outlets(
        "交易所", city_navigator=lambda **kwargs: success,
        ocr_click=lambda *args, **kwargs: ocr_calls.append((args, kwargs)) or True,
    )
    assert selected
    assert ocr_calls[0][1]["cropped_pos1"] == (160, 40)
    assert ocr_calls[0][1]["cropped_pos2"] == (1000, 500)
    assert ocr_calls[0][1]["excursion_pos"] == (0, 80)


def test_blurry_ocr_click_propagates_guard_denial(monkeypatch):
    class Frame:
        def crop_image(self, *_args):
            return self

        def ocr(self):
            return [{
                "text": "交易所",
                "position": [[850, 260], [950, 260], [950, 290], [850, 290]],
            }]

    monkeypatch.setattr(preset_control, "screenshot", lambda: Frame())
    monkeypatch.setattr(preset_control, "input_tap", lambda *_args, **_kwargs: False)
    assert not preset_control.blurry_ocr_click(
        "交易所", trynum=1, log=False,
        action_key="navigation_anchor", page_id="city_outlets",
    )


def test_reward_driver_unknown_page_uses_specific_page_back_policy():
    driver = rewards.RewardDriver(sleep=lambda _seconds: None)
    frames = iter([[{"text": "未识别页面"}], [{"text": "访问城市"}]])
    driver.texts = lambda: next(frames)
    calls = []
    driver.tap = lambda *args, **kwargs: calls.append((args, kwargs)) or True
    assert driver.go_home(attempt_limit=2)
    assert calls[0][1]["action_key"] == "page_back"


def test_probe_safe_return_requires_independently_classified_home(monkeypatch):
    pages = iter(["city_map", "home"])
    monkeypatch.setattr(
        probe, "_trusted_observation",
        lambda: SimpleNamespace(
            observation=SimpleNamespace(page_type=next(pages)),
        ),
    )
    calls = []
    driver = SimpleNamespace(
        tap=lambda *args, **kwargs: calls.append((args, kwargs)) or True,
        sleep=lambda _seconds: None,
    )
    assert probe._return_home_safely(driver, attempt_limit=2)
    assert calls[0][1]["action_key"] == "page_back"

    monkeypatch.setattr(
        probe, "_trusted_observation",
        lambda: SimpleNamespace(
            observation=SimpleNamespace(page_type="unknown"),
        ),
    )
    calls.clear()
    assert not probe._return_home_safely(driver, attempt_limit=2)
    assert calls == []


def test_open_exchange_action_uses_overall_deadline(monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(exchange, "screenshot", lambda: [])
    import core.preset.control as preset_control
    monkeypatch.setattr(preset_control, "go_home", lambda **kwargs: clock.sleep(2) or True)
    result = exchange.open_exchange_action(
        exchange.ExchangeAction.BUY, read_only=True,
        timeout=1, monotonic=clock, sleep=clock.sleep,
    )
    assert result.success is False
    assert result.reason == "overall_deadline_exceeded"


def test_city_entry_uses_observed_visit_city_anchor():
    taps = []
    result = _go_city([
        _city_frame("HOME", "home", anchor=(1080, 450, 1260, 535)),
        _city_frame("CITY_MAP", "city"),
    ], tap=lambda point, **kwargs: taps.append(point) or True)
    assert result.success is True
    assert taps == [(1170, 492)]
    assert probe._classify_page([
        "城市手册", "交易所", "RANSHINCITY", "城市发展度",
    ])[0] == "city_map"


def test_city_entry_postcondition_requires_city_map():
    guard, _issuer, executor = _guard([
        _city_policy_observation(1, "home"),
        _city_policy_observation(2, "home"),
        _city_policy_observation(3, "home"),
        _city_policy_observation(4, "city_map"),
    ])
    assert guard.authorize_coordinate(
        (1170, 492),
        intent=policy.ActionIntent("city_entry_navigation", "city_entry"),
    )
    assert executor.taps == [(1170, 492)]
    post = [entry for entry in guard.journal if entry.observation_role == "POST"]
    assert [entry.source_capture_sequence for entry in post] == [4]


def test_city_entry_policy_rejects_npc_dialogue():
    spec = policy.DEFAULT_POLICY_SPECS["city_entry_navigation"]
    assert spec.allowed_page_types == frozenset({"home", "hud"})
    assert spec.allowed_post_page_types == frozenset({"city_map"})
    assert "npc_dialogue" not in spec.allowed_post_page_types
    guard, _issuer, executor = _guard([
        _city_policy_observation(10, "home"),
        _city_policy_observation(11, "home"),
        _city_policy_observation(12, "npc_dialogue"),
    ])
    assert not guard.authorize_coordinate(
        (1170, 492),
        intent=policy.ActionIntent("city_entry_navigation", "city_entry"),
    )
    assert executor.taps == [(1170, 492)]
    assert guard.journal[-1].stage == "POSTCONDITION_FAILED"
    assert guard.journal[-1].source_capture_sequence == 12


def test_navigation_anchor_allows_only_bounded_ocr_bbox_jitter():
    guard, _issuer, executor = _guard([
        _navigation_policy_observation(20, "city_map"),
        _navigation_policy_observation(21, "city_map", (852, 263, 950, 289)),
        _navigation_policy_observation(22, "npc_dialogue"),
    ])
    assert guard.authorize_coordinate(
        (901, 359), intent=policy.ActionIntent("navigation_anchor", "交易所"),
    )
    assert executor.taps == [(901, 359)]

    denied, _issuer, denied_executor = _guard([
        _navigation_policy_observation(30, "city_map"),
        _navigation_policy_observation(31, "city_map", (859, 260, 954, 292)),
    ])
    assert not denied.authorize_coordinate(
        (901, 359), intent=policy.ActionIntent("navigation_anchor", "交易所"),
    )
    assert denied_executor.taps == []
    assert denied.journal[-1].stage == "CONSUME_DENIED"


def test_precondition_journal_stage_is_emitted():
    guard, _issuer, _executor = _guard([_observation(1), _observation(2), _observation(3, "home")])
    assert guard.authorize_coordinate((50, 40), intent=policy.ActionIntent("reward_back", "top_left_back"))
    assert "PRECONDITION_OBSERVED" in [entry.stage for entry in guard.journal]


def test_pre_consume_post_capture_sequences_strictly_increase():
    guard, _issuer, _executor = _guard([_observation(1), _observation(2), _observation(3, "home")])
    assert guard.authorize_coordinate((50, 40), intent=policy.ActionIntent("reward_back", "top_left_back"))
    by_role = {entry.observation_role: entry.source_capture_sequence for entry in guard.journal if entry.observation_role}
    assert by_role["PRE"] < by_role["CONSUME"] < by_role["POST"]


def test_issue_denial_and_consume_denial_are_distinct():
    issue_guard, _issuer, _executor = _guard([_observation(1, "unknown")])
    assert not issue_guard.authorize_coordinate((50, 40), intent=policy.ActionIntent("reward_back", "top_left_back"))
    assert issue_guard.journal[-1].stage == "ISSUE_DENIED"
    consume_guard, _issuer, _executor = _guard([_observation(10), _observation(11, "unknown")])
    assert not consume_guard.authorize_coordinate((50, 40), intent=policy.ActionIntent("reward_back", "top_left_back"))
    assert consume_guard.journal[-1].stage == "CONSUME_DENIED"


def test_probe_cannot_generate_its_own_capture_authority():
    source = inspect.getsource(probe._trusted_observation)
    assert "capture_envelope" in source
    assert "time_ns" not in source
    assert "_SOURCE_CAPTURE_SEQUENCE" not in inspect.getsource(probe)


def test_three_static_frames_require_three_backend_capture_calls(monkeypatch):
    class Backend:
        ratio = 1.0
        dsize = (1280, 720)
        def __init__(self): self.calls = 0
        def screenshot(self):
            self.calls += 1
            return np.zeros((720, 1280, 3), dtype=np.uint8)
    backend = Backend()
    monkeypatch.setattr(control, "control", backend)
    envelopes = [control.capture_envelope() for _ in range(3)]
    assert backend.calls == 3
    assert [item.backend_monotonic_sequence for item in envelopes] == sorted({item.backend_monotonic_sequence for item in envelopes})
    assert len({item.backend_capture_id for item in envelopes}) == 3


def test_invalid_next_run_is_corrupt_not_due(tmp_path):
    path = tmp_path / "task_schedule.json"
    path.write_text(json.dumps({"schema_version": 1, "tasks": {"x": {"next_run": "tomorrow"}}, "completed": []}), encoding="utf-8")
    with pytest.raises(schedule.TaskScheduleStateCorrupt):
        schedule.is_task_due("x", path=path)


def test_invalid_task_status_is_corrupt(tmp_path):
    path = tmp_path / "task_schedule.json"
    path.write_text(json.dumps({"schema_version": 1, "tasks": {"x": {"status": "maybe"}}, "completed": []}), encoding="utf-8")
    with pytest.raises(schedule.TaskScheduleStateCorrupt):
        schedule.load_task_schedule(path)


def test_corrupt_schedule_is_not_overwritten(tmp_path):
    path = tmp_path / "task_schedule.json"
    original = '{"schema_version":1,"tasks":{"x":{"next_run":"bad"}},"completed":[]}'
    path.write_text(original, encoding="utf-8")
    with pytest.raises(schedule.TaskScheduleStateCorrupt):
        schedule.set_next_run("x", NOW, path)
    assert path.read_text(encoding="utf-8") == original
    assert list(tmp_path.glob("task_schedule.json.corrupt.*"))


def test_single_claimed_ocr_frame_is_not_settled():
    scanner = rewards.DailyCardScanner()
    scanner.add_cards(_card_frame("已领取"))
    card = scanner.cards[0]
    assert card.claim_state is not rewards.CardClaimState.CLAIMED_SETTLED
    assert card.settlement_evidence_frames == 1


def test_two_independent_claimed_frames_settle():
    scanner = rewards.DailyCardScanner()
    scanner.add_cards(_card_frame("已领取"))
    scanner.add_cards(_card_frame("已领取"))
    assert scanner.cards[0].claim_state is rewards.CardClaimState.CLAIMED_SETTLED
    assert scanner.cards[0].settlement_evidence_frames >= 2


def test_ocr_miss_does_not_promote_single_settlement_candidate():
    scanner = rewards.DailyCardScanner()
    scanner.add_cards(_card_frame("已领取"))
    scanner.add_cards(_card_frame(None))
    scanner.add_cards(_card_frame(None))
    assert scanner.cards[0].claim_state is not rewards.CardClaimState.CLAIMED_SETTLED
    assert scanner.cards[0].settlement_evidence_frames == 1


def test_live_probe_result_is_structured_on_navigation_timeout(monkeypatch, tmp_path):
    monkeypatch.setattr(probe, "_return_home_safely", lambda *args, **kwargs: True)
    monkeypatch.setattr(probe, "_capture", lambda *args, **kwargs: {"name": args[-1]})
    timeout = exchange.ExchangeNavigationResult(False, exchange.ExchangeAction.BUY, reason="overall_deadline_exceeded")
    monkeypatch.setattr(probe.exchange_navigation, "open_exchange_action", lambda *args, **kwargs: timeout)
    guard = SimpleNamespace(journal=())
    result = probe._run_probe(SimpleNamespace(adb_port=16384, navigation_only=True), tmp_path, guard, policy_canaries=[])
    assert result["acceptance_status"] == "BLOCKED"
    assert result["blocked_stage"] == "buy_navigation"
    assert result["blocked_reason"] == "overall_deadline_exceeded"


def test_live_addendum_is_included_in_current_audit_manifest(tmp_path):
    baseline = tmp_path / "baseline"; target = tmp_path / "target"
    baseline.mkdir(); target.mkdir()
    (baseline / "sample.py").write_text("VALUE=1\n", encoding="utf-8")
    (target / "sample.py").write_text("VALUE=2\n", encoding="utf-8")
    addendum = target / "LIVE-VALIDATION-ADDENDUM.md"
    addendum.write_text("status: BLOCKED\n", encoding="utf-8")
    package = audit_export.build_frozen_reversible_audit_package(
        baseline, target, tmp_path / "package", audit_report_text="# report\n",
        manifest_metadata={"current_live_status": "BLOCKED", "live_addendum_path": addendum.name},
    )
    manifest = json.loads((package / "SHAREABLE-MANIFEST.json").read_text(encoding="utf-8"))
    assert manifest["current_live_status"] == "BLOCKED"
    assert manifest["live_addendum_hash"]
