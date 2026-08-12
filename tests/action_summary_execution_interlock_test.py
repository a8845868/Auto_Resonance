from __future__ import annotations

from dataclasses import fields, replace
import subprocess
import sys
from unittest.mock import Mock, patch

import numpy as np
import pytest

from auto.resident_activity import (
    ResidentActivityAutomation,
    run_resident_activity,
)
from core.services.action_summary_execution_interlock import (
    ActionSummaryExecutionAuthorization,
    ActionSummaryExecutionMode,
    evaluate_execution_interlock,
    validate_execution_authorization,
)
from core.services.proven_capability_navigation import CapabilityNavigationResult
from core.services.runtime_errors import BlockedBySafetyError
from core.control.nemu_capture import NemuCaptureError
from core.services.action_summary_product_model import (
    decide_action_summary,
    observe_action_summary_page,
)


def item(text: str, x: int, y: int, width: int = 100, height: int = 24) -> dict:
    return {
        "text": text,
        "position": [
            [x - width // 2, y - height // 2],
            [x + width // 2, y - height // 2],
            [x + width // 2, y + height // 2],
            [x - width // 2, y + height // 2],
        ],
    }


class Frame:
    def __init__(
        self,
        labels: list[dict],
        capture_id: str,
        frame_hash: str,
    ) -> None:
        self.image = np.zeros((720, 1280, 3), dtype=np.uint8)
        self.source_capture_id = capture_id
        self.raw_frame_hash = frame_hash
        self.labels = labels

    def ocr(self) -> list[dict]:
        return list(self.labels)


def siege_labels(*, jitter: int = 0) -> list[dict]:
    return [
        item("利刃围剿", 680, 84),
        item("同名任务", 500 + jitter, 420),
        item("进入挑战", 500 + jitter, 600),
        item("-40", 500 + jitter, 636),
        item("同名任务", 800 + jitter, 420),
        item("进入挑战", 800 + jitter, 600),
        item("-40", 800 + jitter, 636),
    ]


def model(capture_id: str = "capture-a", frame_hash: str = "a" * 64, *, jitter: int = 0):
    return observe_action_summary_page(Frame(
        siege_labels(jitter=jitter), capture_id, frame_hash
    ))


def authorization(source_model, *, action: str = "CHALLENGE"):
    card = source_model.task_cards[0]
    return ActionSummaryExecutionAuthorization(
        schema_version="1.0",
        authorization_id="test-only-authorization",
        model_scope=source_model.model_scope,
        activity_family=source_model.activity_family,
        model_freshness_token=source_model.model_freshness_token,
        source_capture_id=source_model.source_capture_id,
        source_frame_sha256=source_model.source_frame_sha256,
        selected_card_match_key=card.card_match_key,
        allowed_action=action,
        max_dispatches=1,
        issued_reason="test_contract_only",
        policy_id="not-implemented",
        policy_version="0",
    )


def test_default_execution_mode_is_read_only():
    automation = ResidentActivityAutomation(Mock())

    assert automation.execution_mode is ActionSummaryExecutionMode.READ_ONLY


def test_decision_is_bound_to_model_and_never_authorizes_execution():
    page = model()
    decision = decide_action_summary(page)

    assert decision.model_freshness_token == page.model_freshness_token
    assert decision.source_frame_sha256 == page.source_frame_sha256
    assert decision.activity_family == "SIEGE"
    assert decision.selected_card_match_key is None
    assert decision.policy_status == "NOT_STARTED"
    assert decision.execution_authorized is False


def test_card_match_key_tolerates_small_jitter_and_separates_same_title_columns():
    initial = model()
    jittered = model("capture-b", "b" * 64, jitter=2)

    assert [card.card_match_key for card in initial.task_cards] == [
        card.card_match_key for card in jittered.task_cards
    ]
    assert initial.task_cards[0].card_match_key != initial.task_cards[1].card_match_key


def test_authorization_contract_contains_no_bbox_or_input_primitive():
    names = {field.name for field in fields(ActionSummaryExecutionAuthorization)}

    assert "bbox" not in names
    assert "point" not in names
    assert "tap" not in names
    assert "swipe" not in names


def test_missing_and_stale_authorizations_are_blocked():
    source = model()
    fresh = model("capture-b", "b" * 64, jitter=1)
    auth = authorization(source)

    assert validate_execution_authorization(
        None, source_model=source, fresh_model=fresh, requested_action="CHALLENGE"
    ).reason == "execution_authorization_required"
    assert validate_execution_authorization(
        replace(auth, model_freshness_token="stale"),
        source_model=source,
        fresh_model=fresh,
        requested_action="CHALLENGE",
    ).reason == "authorization_stale"
    assert validate_execution_authorization(
        replace(auth, source_frame_sha256="c" * 64),
        source_model=source,
        fresh_model=fresh,
        requested_action="CHALLENGE",
    ).reason == "authorization_model_mismatch"


def test_activity_and_action_mismatches_are_blocked():
    source = model()
    fresh = model("capture-b", "b" * 64)
    auth = authorization(source)

    assert validate_execution_authorization(
        replace(auth, activity_family="UNKNOWN"),
        source_model=source,
        fresh_model=fresh,
        requested_action="CHALLENGE",
    ).reason == "authorization_activity_mismatch"
    assert validate_execution_authorization(
        auth,
        source_model=source,
        fresh_model=fresh,
        requested_action="SWEEP",
    ).reason == "authorization_action_mismatch"


def test_authorized_card_key_must_belong_to_bound_source_model():
    source = model()
    fresh = model("capture-b", "b" * 64)

    assert validate_execution_authorization(
        replace(authorization(source), selected_card_match_key="MATCH_FORGED"),
        source_model=source,
        fresh_model=fresh,
        requested_action="CHALLENGE",
    ).reason == "authorization_model_mismatch"


def test_unknown_activity_cannot_validate_authorization():
    labels = [
        item("未来活动", 680, 84),
        item("任务甲", 500, 420), item("进入挑战", 500, 600), item("-30", 500, 636),
        item("任务乙", 800, 420), item("进入挑战", 800, 600), item("-30", 800, 636),
    ]
    unknown = observe_action_summary_page(Frame(labels, "unknown-a", "d" * 64))
    auth = authorization(model())

    assert unknown.activity_family == "UNKNOWN"
    assert validate_execution_authorization(
        auth,
        source_model=unknown,
        fresh_model=unknown,
        requested_action="CHALLENGE",
    ).reason == "authorization_activity_mismatch"


def test_fresh_frame_reparse_is_required_and_never_grants_v1_authority():
    source = model()
    auth = authorization(source)

    assert validate_execution_authorization(
        auth,
        source_model=source,
        fresh_model=None,
        requested_action="CHALLENGE",
    ).reason == "fresh_model_required"
    assert validate_execution_authorization(
        auth,
        source_model=source,
        fresh_model=source,
        requested_action="CHALLENGE",
    ).reason == "authorization_stale"
    validation = validate_execution_authorization(
        auth,
        source_model=source,
        fresh_model=model("capture-b", "b" * 64, jitter=2),
        requested_action="CHALLENGE",
    )
    assert validation.valid is False
    assert validation.reason == "execution_authority_not_implemented"
    assert validation.matched_fresh_card_key == source.task_cards[0].card_match_key


def test_fresh_card_missing_or_ambiguous_is_blocked_without_old_bbox_reuse():
    source = model()
    fresh = model("capture-b", "b" * 64)
    auth = authorization(source)

    missing_fresh = replace(
        fresh,
        task_cards=(fresh.task_cards[1],),
    )
    missing = validate_execution_authorization(
        auth,
        source_model=source,
        fresh_model=missing_fresh,
        requested_action="CHALLENGE",
    )
    ambiguous_fresh = replace(
        fresh,
        task_cards=(fresh.task_cards[0], fresh.task_cards[0]),
    )
    ambiguous = validate_execution_authorization(
        auth,
        source_model=source,
        fresh_model=ambiguous_fresh,
        requested_action="CHALLENGE",
    )

    assert missing.reason == "fresh_card_not_found"
    assert ambiguous.reason == "fresh_card_ambiguous"


def test_available_task_returns_structured_business_policy_block():
    page = model()
    decision = decide_action_summary(page)
    result = evaluate_execution_interlock(
        page, decision, mode=ActionSummaryExecutionMode.READ_ONLY
    )

    assert result.success is False
    assert result.terminal is True
    assert result.execution_status == "BLOCKED"
    assert result.reason == "business_policy_required"
    assert result.execution_authorized is False
    assert result.authorization_valid is False
    assert result.business_dispatches == 0
    assert result.irreversible_actions == 0
    assert result.task_outcome.value == "DEFERRED_EXPECTED"
    assert result.task_terminal is True
    assert result.task_deferred is True
    assert result.progress_made is False
    assert result.business_progress_made is False
    assert result.next_run_reason == "business_policy_required"
    assert result.incident_eligible is False
    assert result.halt_eligible is False


@pytest.mark.parametrize("method,args", [
    ("_reward_attempts", ()),
    ("enter_first_visible_challenge", ()),
    ("select_siege_task", ("特殊订单",)),
    ("sweep_current_activity", (1,)),
])
def test_legacy_helpers_are_blocked_in_default_mode(method, args):
    automation = ResidentActivityAutomation(Mock())

    with pytest.raises(PermissionError, match="legacy_compatibility_disabled"):
        getattr(automation, method)(*args)


def test_public_run_and_run_once_never_reach_legacy_by_default():
    page = model()
    decision = decide_action_summary(page)
    automation = ResidentActivityAutomation(Mock())
    automation.read_action_summary_product_model = Mock(return_value=(page, decision))
    automation.select_siege_task = Mock(side_effect=AssertionError("legacy selection"))
    automation.sweep_current_activity = Mock(side_effect=AssertionError("legacy sweep"))

    with patch("auto.resident_activity.connect_resonance", return_value=True):
        run_result = automation.run("特殊订单")
        once_result = automation.run_once("特殊订单")

    assert run_result["reason"] == "business_policy_required"
    assert once_result["reason"] == "business_policy_required"
    assert run_result["business_dispatches"] == 0
    assert once_result["business_dispatches"] == 0
    automation.select_siege_task.assert_not_called()
    automation.sweep_current_activity.assert_not_called()


def test_read_only_failure_never_falls_back_to_legacy():
    automation = ResidentActivityAutomation(Mock())
    automation.read_action_summary_product_model = Mock(
        side_effect=RuntimeError("read-only observation failed")
    )
    automation.select_siege_task = Mock(side_effect=AssertionError("legacy selection"))
    automation.sweep_current_activity = Mock(side_effect=AssertionError("legacy sweep"))

    with patch("auto.resident_activity.connect_resonance", return_value=True):
        with pytest.raises(RuntimeError, match="read-only observation failed"):
            automation.run("特殊订单")

    automation.select_siege_task.assert_not_called()
    automation.sweep_current_activity.assert_not_called()


def test_policy_gated_mode_without_authorization_remains_blocked():
    page = model()
    decision = decide_action_summary(page)
    result = evaluate_execution_interlock(
        page,
        decision,
        mode=ActionSummaryExecutionMode.POLICY_GATED,
        authorization=None,
        requested_action="CHALLENGE",
    )

    assert result.execution_status == "BLOCKED"
    assert result.execution_authorized is False
    assert result.business_dispatches == 0


def test_gui_wrapper_constructs_default_read_only_automation():
    instance = Mock()
    instance.run.return_value = {"terminal": True, "success": False}
    with patch(
        "auto.resident_activity.ResidentActivityAutomation",
        return_value=instance,
    ) as factory:
        run_resident_activity("特殊订单")

    factory.assert_called_once_with(
        execution_mode=ActionSummaryExecutionMode.READ_ONLY
    )
    instance.run.assert_called_once_with("特殊订单", "学会装备箱")


def test_legacy_compatibility_requires_explicit_construction():
    automation = ResidentActivityAutomation(
        Mock(), execution_mode=ActionSummaryExecutionMode.LEGACY_COMPATIBILITY
    )

    assert automation.execution_mode is ActionSummaryExecutionMode.LEGACY_COMPATIBILITY


def test_explicit_legacy_public_results_record_compatibility_mode():
    automation = ResidentActivityAutomation(
        Mock(), execution_mode=ActionSummaryExecutionMode.LEGACY_COMPATIBILITY
    )
    automation._run_legacy = Mock(return_value={"历史任务": 1})
    automation._run_once_legacy = Mock(return_value={"历史任务": 1})

    assert automation.run("历史任务") == {
        "历史任务": 1,
        "execution_mode": "LEGACY_COMPATIBILITY",
    }
    assert automation.run_once("历史任务") == {
        "历史任务": 1,
        "execution_mode": "LEGACY_COMPATIBILITY",
    }


def test_interlock_import_does_not_initialize_control_backend():
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; "
                "import core.services.action_summary_execution_interlock; "
                "assert 'core.control.control' not in sys.modules; "
                "assert 'auto' not in sys.modules"
            ),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr


def test_navigation_failure_raises_blocked_safety_without_second_capture():
    """When open_action_summary() returns False, read_action_summary_product_model
    raises BlockedBySafetyError immediately — no second screenshot, no input,
    no legacy fallback."""
    capture_calls = []

    class _Driver:
        def capture_frame(self):
            capture_calls.append(1)
            raise RuntimeError("NEMU code 2 — should never be called")

    automation = ResidentActivityAutomation(
        _Driver(),
        execution_mode=ActionSummaryExecutionMode.READ_ONLY,
    )
    automation.open_action_summary = lambda: False
    automation.last_capability_navigation_result = CapabilityNavigationResult(
        success=False, terminal=False, target_capability="ACTION_SUMMARY_VISIBLE",
        initial_state="HOME_READY", final_state="UNKNOWN",
        planned_edge_ids=(), completed_edge_ids=(), failed_edge_id=None,
        physical_dispatches=0, unknown_state_actions=0, irreversible_actions=0,
        reason="unknown_start_state",
    )

    with pytest.raises(BlockedBySafetyError, match="unknown_start_state"):
        automation.read_action_summary_product_model()

    assert capture_calls == []


def test_navigation_failure_without_stored_reason_uses_fallback():
    """When last_capability_navigation_result has no reason, a fallback is used."""
    automation = ResidentActivityAutomation(
        Mock(), execution_mode=ActionSummaryExecutionMode.READ_ONLY,
    )
    automation.open_action_summary = lambda: False
    automation.last_capability_navigation_result = None

    with pytest.raises(BlockedBySafetyError, match="action_summary_navigation_failed"):
        automation.read_action_summary_product_model()


def test_read_only_model_succeeds_after_proven_navigation(monkeypatch):
    """Normal case: navigation succeeds, one screenshot runs, BlockedBySafetyError
    is NOT raised."""
    capture_calls = []

    class _Frame:
        def ocr(self):
            return []

    def _capture():
        capture_calls.append(1)
        return _Frame()

    automation = ResidentActivityAutomation(
        Mock(), execution_mode=ActionSummaryExecutionMode.READ_ONLY,
    )
    automation.open_action_summary = lambda: True
    automation.driver.capture_frame = _capture

    monkeypatch.setattr(
        "auto.resident_activity.observe_action_summary_page",
        lambda _frame: Mock(),
    )
    monkeypatch.setattr(
        "auto.resident_activity.decide_action_summary",
        lambda _model: Mock(),
    )

    result = automation.read_action_summary_product_model()

    assert result is not None
    assert len(capture_calls) == 1


def test_final_model_nemu_capture_failure_is_blocked_safety_not_fatal():
    capture_calls = []

    class _Driver:
        def capture_frame(self):
            capture_calls.append(1)
            raise NemuCaptureError(native_return_code=2)

    automation = ResidentActivityAutomation(
        _Driver(), execution_mode=ActionSummaryExecutionMode.READ_ONLY,
    )
    automation.open_action_summary = lambda: True

    with pytest.raises(BlockedBySafetyError, match="nemu_capture_failed:2"):
        automation.read_action_summary_product_model()

    assert capture_calls == [1]
