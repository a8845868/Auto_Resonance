from __future__ import annotations

import inspect
import json
import subprocess
import sys
import zipfile
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from auto import reward_collection as rewards
from core.control import control as control_module
from core.preset import presets
from core.services import read_only_policy as policy
from core.services.dispatch_outcome import DispatchStatus
from core.services.daily_rewards import (
    DailyProgressSnapshot,
    RewardRunStatus,
    decide_reward_run,
)
from tools import audit_export


NOW = datetime(2026, 7, 19, 12, 0, tzinfo=timezone(timedelta(hours=8)))


def _simple_tree(root: Path, name: str, value: int) -> Path:
    tree = root / name
    tree.mkdir()
    (tree / "sample.py").write_text(f"VALUE = {value}\n", encoding="utf-8")
    return tree


def test_final_frozen_package_manifest_matches_fresh_extraction(tmp_path):
    baseline = _simple_tree(tmp_path, "baseline", 1)
    target = _simple_tree(tmp_path, "target", 2)
    package = audit_export.build_frozen_reversible_audit_package(
        baseline,
        target,
        tmp_path / "package",
        audit_report_text="# frozen\n",
        create_zip=True,
    )
    manifest = json.loads(
        (package / "SHAREABLE-MANIFEST.json").read_text(encoding="utf-8")
    )
    with zipfile.ZipFile(package.with_suffix(".zip")) as archive:
        archive.extractall(tmp_path / "fresh")
    fresh = tmp_path / "fresh" / package.name
    assert audit_export._tree_hash(fresh / "sanitized-target") == manifest[
        "sanitized_target_tree"
    ]["tree_hash"]
    assert audit_export._tree_hash(fresh / "sanitized-target") == manifest[
        "forward_tree_hash"
    ]
    assert manifest["final_verifier"]["status"] == "PASS"
    assert manifest["final_verifier"]["verified_at"]


def test_post_manifest_target_mutation_aborts_publication(tmp_path, monkeypatch):
    baseline = _simple_tree(tmp_path, "baseline", 1)
    target = _simple_tree(tmp_path, "target", 2)
    destination = tmp_path / "package"
    sha_path = destination.with_suffix(".SHA256.txt")
    sha_path.write_text("stale\n", encoding="utf-8")
    real_verify = audit_export._verify_frozen_reversible_package

    def mutate_then_verify(package_root, *, zip_path=None):
        (Path(package_root) / "sanitized-target" / "sample.py").write_text(
            "VALUE = 999\n", encoding="utf-8"
        )
        return real_verify(package_root, zip_path=zip_path)

    monkeypatch.setattr(
        audit_export, "_verify_frozen_reversible_package", mutate_then_verify
    )
    with pytest.raises(
        audit_export.SensitiveDataError, match="hash mismatch|exact-set"
    ):
        audit_export.build_frozen_reversible_audit_package(
            baseline,
            target,
            destination,
            audit_report_text="# frozen\n",
            create_zip=True,
        )
    assert not destination.exists()
    assert not destination.with_suffix(".zip").exists()
    assert not sha_path.exists()


def test_package_verifies_with_its_own_final_verifier(tmp_path):
    baseline = _simple_tree(tmp_path, "baseline", 1)
    target = _simple_tree(tmp_path, "target", 2)
    package = audit_export.build_frozen_reversible_audit_package(
        baseline,
        target,
        tmp_path / "package",
        audit_report_text="# frozen\n",
        create_zip=True,
    )
    verifier = package / "VERIFY-PACKAGE.py"
    assert verifier.is_file()
    completed = subprocess.run(
        [sys.executable, str(verifier), "--root", str(package)],
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert "PASS" in completed.stdout


def _empty_observer():
    raise AssertionError("observer should not be reached during activation")


def test_public_test_session_cannot_activate_production_input_policy():
    session, _issuer = policy.create_test_read_only_session(
        _empty_observer, lambda _point: None
    )
    token = None
    try:
        with pytest.raises((TypeError, PermissionError), match="production|provenance"):
            token = control_module.activate_action_policy(session)
    finally:
        if token is not None:
            control_module.remove_action_policy(token)


def test_direct_safety_session_constructor_has_no_production_provenance():
    session = policy.ReadOnlySafetySession(lambda _point: None)
    token = None
    try:
        with pytest.raises((TypeError, PermissionError), match="production|provenance"):
            token = control_module.activate_action_policy(session)
    finally:
        if token is not None:
            control_module.remove_action_policy(token)


def test_arbitrary_policy_map_cannot_enter_production_session():
    assert not hasattr(control_module, "create_read_only_safety_session")
    assert "policies" not in inspect.signature(
        control_module._create_production_read_only_safety_session
    ).parameters


class _FakeBackend:
    ratio = 1.0
    dsize = (1280, 720)

    def __init__(self):
        self.taps = []

    def input_tap(self, x, y):
        self.taps.append((x, y))

    def input_swipe(self, *_args):
        raise AssertionError("swipe not expected")


def _production_observation(sequence: int, page_type: str):
    return policy.PageObservation(
        observation_id=f"production-{sequence}",
        screenshot_hash=f"{sequence:064x}",
        page_type=page_type,
        markers=("top_level_hud",) if page_type == "home" else (page_type,),
        anchors=(
            policy.ObservedAnchor(
                "city_entry", "visit-city", (1080, 450, 1260, 535)
            ),
        )
        if page_type == "home"
        else (),
        captured_at=NOW - timedelta(seconds=1) + timedelta(microseconds=sequence),
        source_capture_id=f"production-capture-{sequence}",
        source_monotonic_sequence=sequence,
        backend_generation=1,
        instance_id="test-instance-0",
        adb_serial="test-adb-0",
    )


def _production_identity(generation=1):
    return policy.BoundDeviceIdentity(
        emulator_backend="FakeBackend",
        instance_id="test-instance-0",
        adb_serial="test-adb-0",
        backend_generation=generation,
        backend_object_identity=f"FakeBackend:{generation}",
        display_geometry_revision=policy.DisplayGeometry().geometry_revision,
        connected_at=NOW,
    )


def test_only_control_factory_session_can_reach_bound_executor(monkeypatch):
    backend = _FakeBackend()
    identity = _production_identity()
    monkeypatch.setattr(control_module, "control", backend)
    monkeypatch.setattr(
        control_module, "current_bound_device_identity", lambda: identity
    )
    observations = iter(
        (
            _production_observation(1, "home"),
            _production_observation(2, "home"),
            _production_observation(3, "city_map"),
        )
    )
    session = control_module._create_production_read_only_safety_session(
        policy.PageObserver(lambda: next(observations)), now=lambda: NOW
    )
    with policy.installed_read_only_guard(session):
        outcome = control_module.input_tap(
            (1170, 492),
            random_offset=False,
            intent=policy.ActionIntent(
                "city_entry_navigation", "city_entry", "production-action"
            ),
        )
        assert outcome
        assert outcome.status is DispatchStatus.DISPATCHED_VERIFIED
        assert outcome.receipt is None
    assert backend.taps == [(1170, 492)]
    action_entries = [
        entry
        for entry in session.journal
        if entry.action_key == "city_entry_navigation"
    ]
    correlation_ids = {entry.correlation_id for entry in action_entries}
    assert len(correlation_ids) == 1
    assert correlation_ids != {""}
    live_result = audit_export.build_live_validation_result(
        [asdict(entry) for entry in action_entries],
        instance_id="0",
        session_id="production-session",
    )
    assert live_result["incomplete_success_count"] == 0
    assert live_result["action_kind"] == ["city_entry_navigation"]


def test_provenance_is_revoked_on_close_or_backend_change(monkeypatch):
    identity = [_production_identity()]
    monkeypatch.setattr(control_module, "control", _FakeBackend())
    monkeypatch.setattr(
        control_module, "current_bound_device_identity", lambda: identity[0]
    )
    session = control_module._create_production_read_only_safety_session(
        policy.PageObserver(lambda: _production_observation(1, "home")),
        now=lambda: NOW,
    )
    control_module._validate_production_session(session)
    session.close()
    with pytest.raises(PermissionError, match="provenance"):
        control_module._validate_production_session(session)

    changed = control_module._create_production_read_only_safety_session(
        policy.PageObserver(lambda: _production_observation(1, "home")),
        now=lambda: NOW,
    )
    identity[0] = _production_identity(2)
    with pytest.raises(PermissionError, match="provenance"):
        control_module._validate_production_session(changed)


def _complete_candidate_card() -> rewards.DailyTaskCard:
    return rewards.DailyTaskCard(
        task_key="task-a",
        title="Task A",
        current=2,
        target=2,
        completed=True,
        claimable=None,
        claimed=None,
        contribution=600,
        page_fingerprint="page-a",
        claim_state_evidence=rewards.CardClaimState.UNKNOWN,
    )


def _daily_chain(*, repeat_card: bool, claim_known: bool = False):
    scanner = rewards.DailyCardScanner()
    first = _complete_candidate_card()
    if claim_known:
        first = rewards.replace(
            first,
            claimable=False,
            claimed=True,
            claim_state_evidence=rewards.CardClaimState.CLAIMED_SETTLED,
            settlement_evidence_frames=2,
        )
    scanner.add_cards([first])
    if repeat_card:
        scanner.add_cards([first])
    scanner.add_cards([])
    scanner.add_cards([])
    thresholds = [
        {"text": str(value), "position": [[100, 100], [110, 100], [110, 110], [100, 110]]}
        for value in (100, 200, 300, 400, 500, 600)
    ]
    visual = {
        "text": "600/600",
        "position": [[200, 180], [260, 180], [260, 220], [200, 220]],
    }
    page_marker = {
        "text": "姣忔棩娲昏穬",
        "position": [[440, 45], [460, 45], [460, 65], [440, 65]],
    }
    frames = [thresholds + [visual, page_marker], thresholds + [visual, page_marker]]
    observed = rewards.observe_daily_activity_layout(
        frames,
        scanner=scanner,
        page_complete=True,
        stage_rewards_claimable=0,
    )
    snapshot = DailyProgressSnapshot(
        server_day_id="2026-07-19",
        daily_activity_current=observed.current,
        daily_activity_max=observed.maximum,
        daily_activity_source=observed.current_source,
        daily_activity_confidence=observed.confidence,
        daily_activity_claimable_tiers=0,
        daily_activity_unclaimed_tiers=0,
        handbook_daily_tasks_total=0,
        handbook_daily_tasks_completed=0,
        handbook_rewards_claimable=0,
        handbook_rewards_unclaimed=0,
        observed_at=NOW,
        daily_task_inventory_complete=observed.page_complete,
        daily_stage_track_complete=True,
        daily_claim_state_confidence=(
            "HIGH" if scanner.claim_states_complete else "UNKNOWN"
        ),
    )
    return scanner, observed, snapshot, decide_reward_run(
        snapshot, now=NOW, travel_manual_enabled=False
    )


def test_single_frame_completed_card_with_unknown_claim_blocks_daily_complete():
    scanner, _observed, _snapshot, decision = _daily_chain(repeat_card=False)
    assert scanner.cards[0].progress_state is rewards.CardProgressState.UNKNOWN
    assert decision.status is RewardRunStatus.UNKNOWN
    assert decision.next_run_at.date() == NOW.date()


def test_one_frame_complete_candidate_remains_unknown():
    scanner, _observed, _snapshot, _decision = _daily_chain(repeat_card=False)
    assert scanner.cards[0].progress_state is rewards.CardProgressState.UNKNOWN


def test_candidate_disappearing_after_scroll_still_blocks_complete():
    scanner, _observed, _snapshot, decision = _daily_chain(repeat_card=False)
    assert scanner.complete is True
    assert scanner.progress_states_complete is False
    assert decision.status is RewardRunStatus.UNKNOWN


def test_stable_complete_card_with_unknown_claim_blocks_complete():
    scanner, _observed, _snapshot, decision = _daily_chain(repeat_card=True)
    assert scanner.cards[0].progress_state is rewards.CardProgressState.STABLE_COMPLETE
    assert scanner.claim_states_complete is False
    assert decision.status is RewardRunStatus.UNKNOWN


def test_visual_600_does_not_override_unknown_card_evidence():
    _scanner, observed, _snapshot, decision = _daily_chain(repeat_card=False)
    assert observed.maximum == 600
    assert decision.status is RewardRunStatus.UNKNOWN


def test_all_card_progress_and_claim_evidence_allows_complete():
    scanner, _observed, _snapshot, decision = _daily_chain(
        repeat_card=True, claim_known=True
    )
    assert scanner.progress_states_complete is True
    assert scanner.claim_states_complete is True
    assert decision.status is RewardRunStatus.COMPLETE


def test_outlet_vertical_scroll_is_valid_and_reaches_later_item(monkeypatch):
    spec = policy.DEFAULT_POLICY_SPECS["outlet_list_scroll"]
    calls = []

    def guarded_swipe(start, end, **_kwargs):
        policy.ReadOnlyPermitIssuer._validate_modality(spec, (start, end), 500)
        calls.append((start, end))
        return True

    answers = iter((False, True))
    result = presets.go_outlets(
        "交易所",
        city_navigator=lambda **_kwargs: presets.CityNavigationResult(
            True,
            presets.CityNavigationState.CITY_MAP,
            1,
            0.1,
            0.8,
            "city-map",
            (),
            (),
            "",
            (),
        ),
        ocr_click=lambda *_args, **_kwargs: next(answers),
        swipe=guarded_swipe,
    )
    assert result.success is True
    assert calls == [((457, 340), (457, 440))]


def test_go_outlets_does_not_abort_on_a_policy_valid_vertical_scroll():
    spec = policy.DEFAULT_POLICY_SPECS["outlet_list_scroll"]
    policy.ReadOnlyPermitIssuer._validate_modality(
        spec, ((457, 340), (457, 390), (457, 440)), 500
    )


def test_diagonal_outlet_scroll_fails_closed():
    spec = policy.DEFAULT_POLICY_SPECS["outlet_list_scroll"]
    with pytest.raises(PermissionError, match="diagonally_ambiguous"):
        policy.ReadOnlyPermitIssuer._validate_modality(
            spec, ((400, 300), (500, 400)), 500
        )


def test_outlet_swipe_journal_records_direction_and_axis():
    region = policy.CalibratedStaticRegion(
        "outlet_list",
        (180, 120, 1080, 650),
        "city_map",
        "outlet_list_scroll",
        "city_map_content_changed",
    )

    def observed(sequence, marker):
        return policy.PageObservation(
            observation_id=f"scroll-{sequence}",
            screenshot_hash=f"{sequence:064x}",
            page_type="city_map",
            markers=("city_map",),
            anchors=(),
            static_regions=(region,),
            content_marker_hash=marker,
            captured_at=NOW - timedelta(seconds=1) + timedelta(microseconds=sequence),
            source_capture_id=f"scroll-capture-{sequence}",
            source_monotonic_sequence=sequence,
            backend_generation=1,
            instance_id="test-instance-0",
            adb_serial="test-adb-0",
        )

    captures = iter((observed(1, "before"), observed(2, "before"), observed(3, "after")))
    executor = type(
        "SwipeExecutor",
        (),
        {
            "tap": lambda *_args: None,
            "swipe": lambda *_args: None,
        },
    )()
    session, _issuer = policy.create_test_read_only_session(
        policy.PageObserver(lambda: next(captures)), executor, now=lambda: NOW
    )
    outcome = session.request_swipe(
        policy.ActionIntent("outlet_list_scroll", "outlet_list", "scroll-action"),
        ((457, 340), (457, 390), (457, 440)),
        500,
        geometry=policy.DisplayGeometry(),
    )
    assert outcome
    assert outcome.status is DispatchStatus.DISPATCHED_VERIFIED
    assert outcome.receipt is None
    final = [
        entry
        for entry in session.journal
        if entry.stage == "POSTCONDITION_VERIFIED"
    ][0]
    assert (final.dominant_axis, final.swipe_direction) == ("VERTICAL", "DOWN")


def _journal_entry(action_id, stage, sequence, *, action_kind="page_back"):
    return {
        "correlation_id": action_id,
        "action_key": action_kind,
        "permit_digest": "a" * 16,
        "stage": stage,
        "allowed": True,
        "source_capture_sequence": sequence,
        "source_capture_digest": f"{sequence:016x}",
        "page_type": "home",
        "reason": "verified",
    }


def test_live_validation_result_is_sanitized_machine_readable():
    assert hasattr(audit_export, "build_live_validation_result")
    journal = [
        _journal_entry("action-1", "PRECONDITION_OBSERVED", 1),
        _journal_entry("action-1", "CONSUME_OBSERVED", 2),
        _journal_entry("action-1", "POSTCONDITION_VERIFIED", 3),
    ]
    result = audit_export.build_live_validation_result(
        journal,
        instance_id="instance-0",
        session_id="private-session-value",
    )
    audit_export.validate_live_validation_result(result)
    allowed = {
        "schema_version", "instance_id", "session_id_digest", "action_id",
        "action_kind", "permit_digest", "pre_capture_sequence",
        "pre_capture_digest", "pre_page_type", "consume_capture_sequence",
        "consume_capture_digest", "consume_page_type", "post_capture_sequence",
        "post_capture_digest", "post_page_type", "stage", "result",
        "reason_code", "irreversible_action_count", "sequence_failure_count",
        "incomplete_success_count",
    }
    assert set(result) == allowed
    serialized = json.dumps(result, ensure_ascii=False)
    for forbidden in ("private-session-value", "screenshot", "raw_ocr", "UID", "C:\\"):
        assert forbidden not in serialized
    addendum = audit_export.render_live_validation_addendum(result)
    assert "irreversible actions: 0" in addendum
    assert "PRODUCT_END_TO_END=PARTIAL" in addendum


def test_live_result_is_frozen_in_current_audit_manifest(tmp_path):
    baseline = _simple_tree(tmp_path, "baseline", 1)
    target = _simple_tree(tmp_path, "target", 2)
    journal = [
        _journal_entry("action-1", "PRECONDITION_OBSERVED", 1),
        _journal_entry("action-1", "CONSUME_OBSERVED", 2),
        _journal_entry("action-1", "POSTCONDITION_VERIFIED", 3),
    ]
    live = audit_export.build_live_validation_result(
        journal, instance_id="instance-0", session_id="private-session-value"
    )
    (target / "LIVE-VALIDATION-RESULT.json").write_text(
        json.dumps(live, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    addendum = audit_export.render_live_validation_addendum(live)
    (target / "LIVE-VALIDATION-ADDENDUM.md").write_text(
        addendum, encoding="utf-8"
    )
    package = audit_export.build_frozen_reversible_audit_package(
        baseline,
        target,
        tmp_path / "package",
        audit_report_text="# frozen\n",
        manifest_metadata={
            "current_live_status": "PASS",
            "live_addendum_path": "LIVE-VALIDATION-ADDENDUM.md",
            "live_result_path": "LIVE-VALIDATION-RESULT.json",
        },
        create_zip=True,
    )
    manifest = json.loads(
        (package / "SHAREABLE-MANIFEST.json").read_text(encoding="utf-8")
    )
    assert manifest["live_result_hash"] == audit_export._hash_file(
        package / "sanitized-target" / "LIVE-VALIDATION-RESULT.json"
    )
    frozen_live = json.loads(
        (package / "sanitized-target" / "LIVE-VALIDATION-RESULT.json").read_text(
            encoding="utf-8"
        )
    )
    assert audit_export.render_live_validation_addendum(frozen_live) == (
        package / "sanitized-target" / "LIVE-VALIDATION-ADDENDUM.md"
    ).read_text(encoding="utf-8")
