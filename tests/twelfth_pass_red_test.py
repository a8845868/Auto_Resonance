from __future__ import annotations

import inspect
import json
from itertools import count
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

import auto.reward_collection as rewards
import core.control.control as control_module
import core.services.read_only_policy as policy
from core.services.dispatch_outcome import DispatchStatus
from core.services import fatigue_triggers
from core.services.task_schedule_state import (
    TaskScheduleStateCorrupt,
    request_immediate_run,
)
import tools.audit_export as audit_export


NOW = datetime(2026, 7, 19, 12, 0, tzinfo=timezone(timedelta(hours=8)))
_CAPTURE_SEQUENCE = count(1)


def _observation(
    observation_id: str,
    page_type: str = "daily_activity",
    *,
    screenshot_hash: str | None = None,
):
    anchors = (
        policy.ObservedAnchor("top_left_back", "返回", (20, 10, 130, 85)),
        policy.ObservedAnchor("daily_content", "任务列表", (150, 180, 1180, 650)),
    ) if page_type == "daily_activity" else ()
    sequence = next(_CAPTURE_SEQUENCE)
    return policy.PageObservation(
        observation_id=observation_id,
        screenshot_hash=screenshot_hash or (observation_id * 64)[:64],
        page_type=page_type,
        markers=(page_type,),
        anchors=anchors,
        captured_at=NOW + timedelta(microseconds=sequence),
        capture_sequence=sequence,
        source_capture_id=f"test-source-{sequence}",
        source_monotonic_sequence=sequence,
        backend_generation=1,
        instance_id="test-instance-0",
        adb_serial="test-adb-0",
    )


class _Executor:
    def __init__(self):
        self.taps: list[tuple[int, int]] = []
        self.swipes = []

    def tap(self, point):
        self.taps.append(tuple(point))

    def input_tap(self, x, y):
        self.taps.append((int(x), int(y)))

    def swipe(self, trajectory, duration_ms):
        self.swipes.append((tuple(trajectory), int(duration_ms)))

    def input_swipe(self, x1, y1, x2, y2, duration_ms):
        self.swipes.append((((x1, y1), (x2, y2)), int(duration_ms)))


def _legacy_guard(captures, *, executor=None, policies=None):
    values = iter(captures)
    issuer = policy.ReadOnlyPermitIssuer(
        policy.PageObserver(lambda: next(values)),
        policy.AnchorResolver(),
        policies=policies,
        now=lambda: NOW,
    )
    backend = executor or _Executor()
    guard = policy.ReadOnlySafetySession(
        backend,
        permit_issuer=issuer,
        now=lambda: NOW,
    )
    return guard, issuer, backend


def _ocr(text: str, x: int, y: int) -> dict:
    return {
        "text": text,
        "position": [[x - 5, y - 5], [x + 5, y - 5], [x + 5, y + 5], [x - 5, y + 5]],
    }


def _card_frame(status: str | None, *, current: int = 1, target: int = 2):
    items = [
        _ocr(f"{current}/{target}", 300, 350),
        _ocr("安全运输", 300, 400),
        _ocr("+10", 300, 550),
    ]
    if status is not None:
        items.append(_ocr(status, 300, 620))
    return items


def _simple_tree(root, name, content):
    path = root / name
    path.mkdir()
    (path / "sample.py").write_text(content, encoding="utf-8")
    return path


def test_final_shareable_manifest_target_hash_matches_actual_target(tmp_path):
    assert hasattr(audit_export, "build_frozen_reversible_audit_package")
    baseline = _simple_tree(tmp_path, "baseline", "VALUE = 1\n")
    target = _simple_tree(tmp_path, "target", "VALUE = 2\n")
    package = audit_export.build_frozen_reversible_audit_package(
        baseline, target, tmp_path / "package",
        audit_report_text="# final report\n", create_zip=True,
    )
    manifest = json.loads((package / "SHAREABLE-MANIFEST.json").read_text(encoding="utf-8"))
    assert audit_export._tree_hash(package / "sanitized-target") == manifest["sanitized_target_tree"]["tree_hash"]


def test_final_shareable_manifest_forward_hash_matches_applied_tree(tmp_path):
    assert hasattr(audit_export, "build_frozen_reversible_audit_package")
    baseline = _simple_tree(tmp_path, "baseline", "VALUE = 1\n")
    target = _simple_tree(tmp_path, "target", "VALUE = 2\n")
    package = audit_export.build_frozen_reversible_audit_package(
        baseline, target, tmp_path / "package",
        audit_report_text="# final report\n", create_zip=True,
    )
    forward = tmp_path / "forward"
    audit_export._apply_text_patch(
        package / "sanitized-baseline",
        package / "patches" / "full-safe-tree.diff",
        forward,
        reverse=False,
    )
    manifest = json.loads((package / "SHAREABLE-MANIFEST.json").read_text(encoding="utf-8"))
    assert audit_export._tree_hash(forward) == manifest["forward_tree_hash"]


def test_guard_does_not_expose_trusted_executor():
    guard, _issuer, _executor = _legacy_guard([_observation("a")])
    assert not hasattr(guard, "trusted_executor")


def test_caller_cannot_directly_invoke_device_executor():
    assert not hasattr(control_module, "TrustedControlInputExecutor")


def test_guard_permit_issuer_is_private_and_immutable():
    guard, issuer, _executor = _legacy_guard([_observation("a")])
    assert not hasattr(guard, "permit_issuer")
    with pytest.raises(AttributeError):
        guard.permit_issuer = issuer


def test_fake_issuer_cannot_authorize_hardware_input():
    original, _issuer, executor = _legacy_guard([
        _observation("a"), _observation("b"), _observation("c", "home")
    ])
    fake_spec = policy.ReadOnlyPolicySpec(
        "fake", frozenset({"daily_activity"}), "daily_content",
        allowed_region=(0, 0, 1279, 719),
    )
    fake_issuer = policy.ReadOnlyPermitIssuer(
        policy.PageObserver(lambda: _observation("fake")),
        policy.AnchorResolver(), policies={"fake": fake_spec}, now=lambda: NOW,
    )
    try:
        original.permit_issuer = fake_issuer
    except AttributeError:
        pass
    allowed = original.authorize_coordinate(
        (1000, 650), intent=policy.ActionIntent("fake", "daily_content")
    )
    assert allowed is False
    assert executor.taps == []


def test_same_source_capture_cannot_satisfy_issue_consume_and_postcondition():
    same = _observation("cached")
    guard, _issuer, executor = _legacy_guard([same, same, same])
    allowed = guard.authorize_coordinate(
        (50, 40), intent=policy.ActionIntent("reward_back", "top_left_back")
    )
    assert allowed is False
    assert executor.taps == []


def test_page_observer_cannot_synthesize_capture_freshness():
    same = _observation("cached")
    observer = policy.PageObserver(lambda: same)
    observer.observe()
    with pytest.raises(PermissionError, match="capture|source|reuse|sequence"):
        observer.observe()


def test_reward_back_rejects_unapproved_account_settings_transition():
    guard, _issuer, executor = _legacy_guard([
        _observation("issue"),
        _observation("consume"),
        _observation("post", "account_settings"),
    ])
    allowed = guard.authorize_coordinate(
        (50, 40), intent=policy.ActionIntent("reward_back", "top_left_back")
    )
    assert allowed
    assert allowed.status is DispatchStatus.DISPATCHED_UNVERIFIED
    assert allowed.receipt is None
    assert executor.taps == [(50, 40)]
    assert guard.journal[-1].stage == "POSTCONDITION_FAILED"


def test_action_journal_records_post_observation_evidence():
    guard, _issuer, _executor = _legacy_guard([
        _observation("issue"), _observation("consume"), _observation("post", "home")
    ])
    guard.authorize_coordinate(
        (50, 40), intent=policy.ActionIntent("reward_back", "top_left_back")
    )
    entry = guard.journal[-1]
    assert entry.stage == "POSTCONDITION_VERIFIED"
    assert entry.observation_role == "POST"
    assert entry.source_capture_digest
    assert entry.source_capture_sequence > 0


def test_permit_rejects_backend_generation_swap():
    identity = policy.BoundDeviceIdentity(
        "ADB", "0", "127.0.0.1:16384", 1, "backend-a",
        policy.DisplayGeometry().geometry_revision, NOW
    )
    identity_state = {"value": identity}
    captures = {"sequence": 0}
    def observe():
        captures["sequence"] += 1
        sequence = captures["sequence"]
        return replace(
            _observation(f"generation-{sequence}"),
            backend_generation=1, instance_id="0", adb_serial="127.0.0.1:16384",
        )
    issuer = policy.ReadOnlyPermitIssuer(
        policy.PageObserver(observe), policy.AnchorResolver(), now=lambda: NOW,
        bound_device_identity=identity,
        identity_provider=lambda: identity_state["value"],
    )
    executor = _Executor()
    guard = policy.ReadOnlyActionGuard(executor, permit_issuer=issuer, now=lambda: NOW)
    permit = issuer.issue(policy.ActionIntent("reward_back", "top_left_back"), ((50, 40),))
    identity_state["value"] = replace(identity, backend_generation=2, backend_object_identity="backend-b")
    assert guard.authorize_coordinate((50, 40), permit=permit) is False
    assert executor.taps == []
    assert issuer.registry_contains(permit) is False


def test_permit_rejects_instance_or_adb_serial_swap():
    identity = policy.BoundDeviceIdentity(
        "ADB", "0", "127.0.0.1:16384", 1, "backend-a",
        policy.DisplayGeometry().geometry_revision, NOW
    )
    identity_state = {"value": identity}
    captures = {"sequence": 0}
    def observe():
        captures["sequence"] += 1
        sequence = captures["sequence"]
        return replace(
            _observation(f"instance-{sequence}"),
            backend_generation=1, instance_id="0", adb_serial="127.0.0.1:16384",
        )
    issuer = policy.ReadOnlyPermitIssuer(
        policy.PageObserver(observe), policy.AnchorResolver(), now=lambda: NOW,
        bound_device_identity=identity,
        identity_provider=lambda: identity_state["value"],
    )
    executor = _Executor()
    guard = policy.ReadOnlyActionGuard(executor, permit_issuer=issuer, now=lambda: NOW)
    permit = issuer.issue(policy.ActionIntent("reward_back", "top_left_back"), ((50, 40),))
    identity_state["value"] = replace(
        identity, instance_id="1", adb_serial="127.0.0.1:16416"
    )
    assert guard.authorize_coordinate((50, 40), permit=permit) is False
    assert executor.taps == []
    assert issuer.registry_contains(permit) is False


def test_executor_is_bound_to_exact_backend_object(monkeypatch):
    assert hasattr(control_module, "_create_bound_input_executor")
    backend_a, backend_b = _Executor(), _Executor()
    monkeypatch.setattr(control_module, "control", backend_a)
    executor = control_module._create_bound_input_executor(backend_a)
    monkeypatch.setattr(control_module, "control", backend_b)
    executor.tap((50, 40))
    assert backend_a.taps == [(50, 40)]
    assert backend_b.taps == []


def test_out_of_order_policy_context_exit_cannot_disable_active_guard(monkeypatch):
    assert hasattr(control_module, "activate_action_policy")
    monkeypatch.setattr(
        control_module, "_validate_production_session", lambda _policy: None
    )
    a, _ia, _ea = _legacy_guard([_observation("a")])
    b, _ib, _eb = _legacy_guard([_observation("b")])
    token_a = control_module.activate_action_policy(a)
    token_b = control_module.activate_action_policy(b)
    control_module.remove_action_policy(token_a)
    try:
        assert control_module.current_action_policy() is b
    finally:
        control_module.remove_action_policy(token_b)


def test_read_only_mode_never_falls_back_to_legacy_input(monkeypatch):
    assert hasattr(control_module, "activate_action_policy")
    monkeypatch.setattr(
        control_module, "_validate_production_session", lambda _policy: None
    )
    backend = _Executor()
    monkeypatch.setattr(control_module, "control", backend)
    guard, _issuer, _executor = _legacy_guard([_observation("a")])
    token = control_module.activate_action_policy(guard)
    control_module.remove_action_policy(token, close_session=False)
    try:
        with pytest.raises(PermissionError, match="read.only|session|policy"):
            control_module.input_tap((50, 40), random_offset=False)
    finally:
        control_module.close_orphaned_action_policy(guard)
    assert backend.taps == []


def test_nested_task_entry_corruption_is_backed_up_and_rejected(tmp_path):
    path = tmp_path / "schedule.json"
    original = b'{"tasks":{"run_business":"BROKEN"},"completed":[]}'
    path.write_bytes(original)
    with pytest.raises(TaskScheduleStateCorrupt):
        request_immediate_run("run_business", path)
    assert path.read_bytes() == original
    assert len(list(tmp_path.glob("schedule.json.corrupt.*"))) == 1


def test_malformed_checkpoint_action_is_backed_up_and_blocks(tmp_path):
    path = tmp_path / "fatigue.json"
    original = b'{"actions":["BROKEN"]}'
    path.write_bytes(original)
    with pytest.raises(fatigue_triggers.CheckpointStateCorrupt):
        fatigue_triggers.list_fatigue_actions(path=path)
    assert path.read_bytes() == original
    assert len(list(tmp_path.glob("fatigue.json.corrupt.*"))) == 1


def test_completed_but_not_yet_claimable_card_is_not_settled():
    card = rewards._card_items(
        _card_frame("不可领取", current=2, target=2), manual=False
    )[0]
    scanner = rewards.DailyCardScanner()
    scanner.add_cards([card]); scanner.add_cards([card]); scanner.add_cards([card])
    assert card.claim_state is rewards.CardClaimState.NOT_YET_CLAIMABLE
    assert scanner.claim_states_complete is False


def test_disabled_claim_button_remains_unknown_or_blocked():
    card = rewards._card_items(
        _card_frame("按钮禁用", current=2, target=2), manual=False
    )[0]
    assert card.claim_state is rewards.CardClaimState.DISABLED_UNKNOWN
    assert card.claim_state not in {
        rewards.CardClaimState.CLAIMED_SETTLED,
        rewards.CardClaimState.NO_REWARD_APPLICABLE_CONFIRMED,
    }


def test_only_authoritative_settled_states_satisfy_daily_completion():
    for text, settled in (
        ("已领取", True),
        ("不可领取", False),
        ("无可领取", False),
        ("奖励已结清", True),
        ("按钮禁用", False),
    ):
        card = rewards._card_items(
            _card_frame(text, current=2, target=2), manual=False
        )[0]
        scanner = rewards.DailyCardScanner()
        scanner.add_cards([card]); scanner.add_cards([card]); scanner.add_cards([card])
        assert scanner.claim_states_complete is settled, text


def test_progress_ocr_conflict_remains_unknown():
    scanner = rewards.DailyCardScanner()
    first = rewards._card_items(_card_frame("已领取", current=10, target=10), manual=False)[0]
    second = rewards._card_items(_card_frame(None, current=1, target=10), manual=False)[0]
    scanner.add_cards([first]); scanner.add_cards([second])
    merged = scanner.cards[0]
    assert merged.progress_conflict is True
    assert merged.completed is False


def test_single_high_ocr_frame_does_not_permanently_complete_card():
    scanner = rewards.DailyCardScanner()
    scanner.add_page(_card_frame("已领取", current=10, target=10))
    scanner.add_page(_card_frame(None, current=1, target=10))
    assert scanner.cards[0].completed is False


def test_acknowledge_requires_matching_owner_and_lease_token():
    signature = inspect.signature(fatigue_triggers.acknowledge_fatigue_checkpoint)
    assert signature.parameters["owner_id"].default is inspect.Parameter.empty
    assert signature.parameters["lease_token"].default is inspect.Parameter.empty


def test_fail_requires_matching_owner_and_lease_token():
    signature = inspect.signature(fatigue_triggers.fail_fatigue_checkpoint)
    assert signature.parameters["owner_id"].default is inspect.Parameter.empty
    assert signature.parameters["lease_token"].default is inspect.Parameter.empty


def test_no_public_checkpoint_transition_accepts_only_action_id():
    for name in (
        "acknowledge_fatigue_checkpoint",
        "fail_fatigue_checkpoint",
        "complete_checkpoint",
        "fail_checkpoint",
        "transfer_checkpoint",
    ):
        function = getattr(fatigue_triggers, name, None)
        if function is None:
            continue
        signature = inspect.signature(function)
        assert "owner_id" in signature.parameters or "owner" in signature.parameters
        assert "lease_token" in signature.parameters
