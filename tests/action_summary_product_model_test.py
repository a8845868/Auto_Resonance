from __future__ import annotations

import json
import hashlib
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from core.services.action_summary_product_model import (
    ActionSummaryDecisionType,
    PageConfidence,
    RewardState,
    TaskCardState,
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
        capture_id: str | None = "model-fixture",
        *,
        raw_frame_hash: str = "",
        captured_at: object | None = None,
    ) -> None:
        self.image = np.zeros((720, 1280, 3), dtype=np.uint8)
        self.source_capture_id = capture_id
        self.raw_frame_hash = raw_frame_hash
        self.captured_at = captured_at
        self.labels = labels

    def ocr(self) -> list[dict]:
        return list(self.labels)


def trusted_items() -> list[dict]:
    return [
        item("利刃围剿", 680, 84, 126, 36),
        item("特殊订单", 580, 422, 107, 32),
        item("利刃行动", 856, 422, 105, 28),
        item("挑战看剑", 1136, 422, 103, 28),
        item("REWARD", 580, 450, 45, 16),
        item("REWARD", 856, 450, 45, 16),
        item("REWARD", 1136, 450, 45, 16),
        item("进入挑战", 594, 606, 90, 26),
        item("进入挑战", 875, 606, 90, 26),
        item("X进入挑战", 1138, 606, 118, 26),
        item("-40", 580, 636, 55, 20),
        item("-40", 856, 636, 55, 20),
        item("-40", 1138, 636, 55, 20),
    ]


def test_trusted_page_builds_three_read_only_task_cards():
    model = observe_action_summary_page(Frame(trusted_items()))

    assert model.page_state == "ACTION_SUMMARY_VISIBLE"
    assert model.model_scope == "ACTION_SUMMARY_READ_ONLY_V1"
    assert model.activity_family == "SIEGE"
    assert model.activity_title_hash == hashlib.sha256(
        "利刃围剿".encode("utf-8")
    ).hexdigest()
    assert model.page_confidence is PageConfidence.HIGH
    assert model.visible_task_cards == 3
    assert [card.state for card in model.task_cards] == [
        TaskCardState.AVAILABLE,
        TaskCardState.AVAILABLE,
        TaskCardState.AVAILABLE,
    ]
    assert [card.cost for card in model.task_cards] == [40, 40, 40]
    assert all(card.cost_resource_id == "UNKNOWN" for card in model.task_cards)
    assert all(card.remaining_attempts is None for card in model.task_cards)
    assert all(card.reward_state is RewardState.UNKNOWN for card in model.task_cards)


def test_single_historical_title_is_not_a_trusted_page():
    model = observe_action_summary_page(Frame([item("利刃围剿", 680, 84)]))

    assert model.page_state == "UNKNOWN"
    assert model.visible_task_cards == 0
    assert model.page_capabilities == frozenset()


def test_multiple_card_states_are_structurally_resolved():
    labels = [
        item("利刃围剿", 680, 84),
        item("任务甲", 300, 420), item("进入挑战", 300, 600),
        item("任务乙", 580, 420), item("已完成", 580, 600),
        item("任务丙", 860, 420), item("未解锁", 860, 600),
        item("任务丁", 1130, 420), item("查看详情", 1130, 600),
    ]
    model = observe_action_summary_page(Frame(labels))

    assert [card.state for card in model.task_cards] == [
        TaskCardState.AVAILABLE,
        TaskCardState.COMPLETED,
        TaskCardState.LOCKED,
        TaskCardState.UNKNOWN,
    ]


def test_missing_attempt_cost_and_reward_facts_remain_unknown():
    labels = [
        item("利刃围剿", 680, 84),
        item("任务甲", 500, 420), item("进入挑战", 500, 600),
        item("任务乙", 800, 420), item("进入挑战", 800, 600),
    ]
    model = observe_action_summary_page(Frame(labels))

    assert all(card.remaining_attempts is None for card in model.task_cards)
    assert all(card.cost is None for card in model.task_cards)
    assert all(card.reward_state is RewardState.UNKNOWN for card in model.task_cards)


def test_overlay_is_expressed_independently_and_blocks_decision():
    model = observe_action_summary_page(
        Frame(trusted_items() + [item("触碰空白区域退出", 640, 680)])
    )
    decision = decide_action_summary(model)

    assert model.page_state == "ACTION_SUMMARY_VISIBLE"
    assert model.overlay_states == ("MODAL_OVERLAY",)
    assert decision.decision is ActionSummaryDecisionType.AMBIGUOUS_PAGE


def test_visible_capabilities_are_descriptive_only():
    model = observe_action_summary_page(Frame(trusted_items()))

    assert model.page_capabilities == frozenset({
        "VIEW_TASK_LIST",
        "CARD_SELECTABLE",
        "SELECT_TASK_AVAILABLE",
        "CHALLENGE_AVAILABLE",
        "TASK_EXECUTION_AVAILABLE",
    })
    assert model.page_actions.can_open_task is True
    assert model.page_actions.can_challenge is True
    assert model.page_actions.can_sweep is False
    assert model.page_actions.can_claim is False
    assert model.page_actions.can_scroll is False


def test_available_task_decision_requires_future_policy_and_authorization():
    decision = decide_action_summary(
        observe_action_summary_page(Frame(trusted_items()))
    )

    assert decision.decision is ActionSummaryDecisionType.TASK_AVAILABLE_NEEDS_POLICY
    assert decision.required_future_authorization == (
        "BUSINESS_POLICY",
        "EXPLICIT_ACTION_AUTHORIZATION",
    )


def test_claimable_reward_is_advisory_and_never_an_action():
    labels = trusted_items() + [item("可领取", 594, 560)]
    decision = decide_action_summary(observe_action_summary_page(Frame(labels)))

    assert decision.decision is ActionSummaryDecisionType.COMPLETED_REWARD_AVAILABLE
    assert decision.required_future_authorization


def test_claimable_reward_precedes_resource_insufficient():
    labels = trusted_items() + [
        item("可领取", 594, 560),
        item("资源不足", 680, 200),
    ]

    decision = decide_action_summary(observe_action_summary_page(Frame(labels)))

    assert decision.decision is ActionSummaryDecisionType.COMPLETED_REWARD_AVAILABLE


def test_explicit_resource_and_attempt_failures_are_distinct_decisions():
    resource = decide_action_summary(observe_action_summary_page(Frame(
        trusted_items() + [item("资源不足", 680, 200)]
    )))
    attempts = decide_action_summary(observe_action_summary_page(Frame(
        trusted_items() + [item("挑战次数已用完", 680, 200)]
    )))

    assert resource.decision is ActionSummaryDecisionType.RESOURCE_INSUFFICIENT
    assert attempts.decision is ActionSummaryDecisionType.ATTEMPTS_EXHAUSTED


def test_all_completed_and_all_locked_have_distinct_decisions():
    completed_items = [
        item("利刃围剿", 680, 84),
        item("任务甲", 500, 420), item("已完成", 500, 600),
        item("任务乙", 800, 420), item("已完成", 800, 600),
    ]
    locked_items = [
        item("利刃围剿", 680, 84),
        item("任务甲", 500, 420), item("未解锁", 500, 600),
        item("任务乙", 800, 420), item("锁定", 800, 600),
    ]

    assert decide_action_summary(
        observe_action_summary_page(Frame(completed_items))
    ).decision is ActionSummaryDecisionType.NO_ACTION_REQUIRED
    assert decide_action_summary(
        observe_action_summary_page(Frame(locked_items))
    ).decision is ActionSummaryDecisionType.LOCKED


def test_unsupported_and_unknown_task_states_remain_non_actionable():
    unsupported_items = [
        item("利刃围剿", 680, 84),
        item("任务甲", 500, 420), item("查看详情", 500, 600),
        item("任务乙", 800, 420), item("查看详情", 800, 600),
    ]
    unknown_items = [
        item("利刃围剿", 680, 84),
        item("任务甲", 500, 420), item("进行中", 500, 600),
        item("任务乙", 800, 420), item("进行中", 800, 600),
    ]

    unsupported = decide_action_summary(
        observe_action_summary_page(Frame(unsupported_items))
    )
    unknown = decide_action_summary(observe_action_summary_page(Frame(unknown_items)))
    assert unsupported.decision is ActionSummaryDecisionType.UNSUPPORTED_TASK
    assert unsupported.required_future_authorization == ()
    assert unknown.decision is ActionSummaryDecisionType.UNKNOWN
    assert unknown.required_future_authorization == ()


def test_unknown_page_has_zero_capabilities_and_ambiguous_decision():
    model = observe_action_summary_page(Frame([item("普通材料", 600, 400)]))
    decision = decide_action_summary(model)

    assert model.page_state == "UNKNOWN"
    assert model.page_actions.can_open_task is False
    assert model.page_capabilities == frozenset()
    assert decision.decision is ActionSummaryDecisionType.AMBIGUOUS_PAGE
    assert decision.required_future_authorization == ()


def test_page_model_has_no_input_authority_or_control_dependency():
    module_path = Path("core/services/action_summary_product_model.py")
    source = module_path.read_text(encoding="utf-8")

    assert "core.control" not in source
    assert "input_tap" not in source
    assert "input_swipe" not in source
    model = observe_action_summary_page(Frame(trusted_items()))
    assert not any(hasattr(model, name) for name in ("tap", "click", "dispatch"))


def test_model_import_does_not_initialize_control_backend():
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; "
                "import core.services.action_summary_product_model; "
                "assert 'core.control.control' not in sys.modules; "
                "assert 'auto' not in sys.modules"
            ),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr


def test_sanitized_live_fixture_contains_no_raw_ocr_or_image():
    path = Path("tests/fixtures/action_summary_read_only/live_structure_v1.json")
    fixture = json.loads(path.read_text(encoding="utf-8"))

    assert fixture["raw_image_included"] is False
    assert fixture["raw_ocr_text_included"] is False
    assert fixture["expected"]["model_scope"] == "ACTION_SUMMARY_READ_ONLY_V1"
    assert fixture["expected"]["activity_family"] == "SIEGE"
    assert fixture["expected"]["visible_task_cards"] == 3
    assert fixture["cost_observations"] == [40, 40, 40]
    assert all(len(value) == 64 for value in fixture["task_title_hashes"])


def test_serialized_model_contains_enums_not_execution_objects():
    document = observe_action_summary_page(Frame(trusted_items())).to_dict()

    assert document["page_state"] == "ACTION_SUMMARY_VISIBLE"
    assert document["task_cards"][0]["state"] == "available"
    assert document["task_cards"][0]["reward_state"] == "unknown"
    assert document["task_cards"][0]["available_actions"] == [
        "CARD_SELECTABLE",
        "CHALLENGE_AVAILABLE",
        "SELECT_TASK_AVAILABLE",
        "TASK_EXECUTION_AVAILABLE",
    ]


def test_trusted_generic_page_does_not_parse_unknown_activity_as_siege():
    labels = [
        item("未来活动", 680, 84),
        item("未知任务", 500, 420), item("进入挑战", 500, 600),
        item("-30", 500, 636),
        item("未知任务", 800, 420), item("进入挑战", 800, 600),
        item("-30", 800, 636),
    ]

    model = observe_action_summary_page(Frame(labels))
    decision = decide_action_summary(model)

    assert model.page_state == "ACTION_SUMMARY_VISIBLE"
    assert model.activity_family == "UNKNOWN"
    assert model.activity_title_hash is None
    assert model.task_cards == ()
    assert model.visible_task_cards == 0
    assert model.page_capabilities == frozenset({"VIEW_TASK_LIST"})
    assert decision.decision is ActionSummaryDecisionType.UNSUPPORTED_TASK
    assert decision.required_future_authorization == ()


def test_duplicate_title_cards_at_different_positions_are_preserved():
    labels = [
        item("利刃围剿", 680, 84),
        item("同名任务", 500, 420), item("进入挑战", 500, 600),
        item("同名任务", 800, 420), item("进入挑战", 800, 600),
    ]

    model = observe_action_summary_page(Frame(labels))

    assert model.visible_task_cards == 2
    assert model.task_cards[0].title_hash == model.task_cards[1].title_hash
    assert model.task_cards[0].card_instance_id != model.task_cards[1].card_instance_id


def test_duplicate_ocr_bbox_is_deduplicated_without_losing_other_card():
    first_title = item("同名任务", 500, 420)
    first_anchor = item("进入挑战", 500, 600)
    labels = [
        item("利刃围剿", 680, 84),
        first_title,
        dict(first_title),
        first_anchor,
        dict(first_anchor),
        item("同名任务", 800, 420),
        item("进入挑战", 800, 600),
    ]

    model = observe_action_summary_page(Frame(labels))

    assert model.visible_task_cards == 2


def test_card_instance_id_is_stable_for_same_structure():
    first = observe_action_summary_page(Frame(trusted_items()))
    second = observe_action_summary_page(Frame(trusted_items()))

    assert [card.card_instance_id for card in first.task_cards] == [
        card.card_instance_id for card in second.task_cards
    ]


def test_model_binds_real_capture_identity_and_frame_hash():
    timestamp = datetime(2026, 7, 26, 12, 34, 56, tzinfo=timezone.utc)
    model = observe_action_summary_page(Frame(
        trusted_items(),
        "capture-42",
        raw_frame_hash="a" * 64,
        captured_at=timestamp,
    ))

    expected_token = hashlib.sha256(
        f"capture-42|{'a' * 64}".encode("utf-8")
    ).hexdigest()
    assert model.source_capture_id == "capture-42"
    assert model.source_frame_sha256 == "a" * 64
    assert model.captured_at == timestamp.isoformat()
    assert model.model_freshness_token == expected_token


def test_missing_capture_id_remains_none_and_grants_no_freshness_token():
    model = observe_action_summary_page(Frame(trusted_items(), None))

    assert model.source_capture_id is None
    assert model.source_frame_sha256 is not None
    assert model.model_freshness_token is None
    assert not any(hasattr(model, name) for name in ("tap", "dispatch", "authorize"))


def test_selectability_and_execution_capabilities_are_separate():
    labels = [
        item("利刃围剿", 680, 84),
        item("可执行", 350, 420), item("进入挑战", 350, 600),
        item("未知", 650, 420), item("查看详情", 650, 600),
        item("已结束", 950, 420), item("已完成", 950, 570),
        item("查看详情", 950, 600),
    ]

    model = observe_action_summary_page(Frame(labels))
    available, unknown, completed = model.task_cards

    assert "CARD_SELECTABLE" in available.available_actions
    assert "TASK_EXECUTION_AVAILABLE" in available.available_actions
    assert "CARD_SELECTABLE" in unknown.available_actions
    assert "TASK_EXECUTION_AVAILABLE" not in unknown.available_actions
    assert "CARD_SELECTABLE" in completed.available_actions
    assert "TASK_EXECUTION_AVAILABLE" not in completed.available_actions
