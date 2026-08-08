"""F-01 containment: read-only fatigue observation must never open the
irreversible use-all bento confirmation dialog.

The bento total is trusted only when every remaining bento's card value is
visible on the cabinet page itself; otherwise it stays None and the planner
defers with FatiguePlanStatus.UNKNOWN instead of probing the dialog.
"""

from unittest.mock import call, patch

import auto.module.strength as strength
from auto.fatigue_recovery import _snapshot_from_observation
from core.services.fatigue_planner import FatiguePlanStatus, plan_fatigue_recovery


USE_ALL_DIALOG_OPEN = (1070, 427)
USE_ALL_DIALOG_CANCEL = (320, 503)


class CabinetImage:
    def __init__(self, items):
        self.items = items

    def ocr(self):
        return self.items


def _cabinet(badge, card_values):
    items = [
        {
            "text": str(badge),
            "position": [[1140, 440], [1180, 440], [1180, 500], [1140, 500]],
        }
    ]
    items.extend({"text": f"消除 {value} 疲劳值"} for value in card_values)
    return CabinetImage(items)


def _observe(cabinet):
    with patch.object(
        strength, "rest_area_availability", return_value=False
    ), patch.object(strength, "_open_fatigue_panel", return_value=True), patch.object(
        strength, "_screen_has", return_value=True
    ), patch.object(
        strength, "remember_rest_area_availability"
    ), patch.object(
        strength, "_wait_text", return_value=True
    ), patch.object(
        strength, "screenshot", return_value=cabinet
    ), patch.object(
        strength, "input_tap"
    ) as tap, patch.object(
        strength, "go_home"
    ), patch.object(
        strength.time, "sleep"
    ):
        observation = strength.observe_recovery_resources("测试站")
    return observation, tap


def test_observation_never_opens_the_use_all_dialog():
    observation, tap = _observe(_cabinet(2, (24, 31)))

    assert call(USE_ALL_DIALOG_OPEN) not in tap.call_args_list
    assert call(USE_ALL_DIALOG_CANCEL) not in tap.call_args_list
    assert observation["lunches_remaining"] == 2
    assert observation["lunch_recovery_values"] == (24, 31)
    assert observation["lunch_total_recovery"] == 55
    assert observation["source_confidence"] == "HIGH"


def test_incomplete_card_values_leave_total_unknown_and_defer_the_plan():
    observation, tap = _observe(_cabinet(3, (24, 31)))

    assert call(USE_ALL_DIALOG_OPEN) not in tap.call_args_list
    assert call(USE_ALL_DIALOG_CANCEL) not in tap.call_args_list
    assert observation["lunch_total_recovery"] is None
    assert observation["source_confidence"] == "PARTIAL"

    snapshot = _snapshot_from_observation("测试站", (700, 816), observation, {})
    plan = plan_fatigue_recovery(snapshot, None)
    assert plan.status is FatiguePlanStatus.UNKNOWN
    assert "bento_value_confidence" in plan.reason


def test_zero_inventory_is_still_high_confidence():
    observation, tap = _observe(_cabinet(0, ()))

    assert call(USE_ALL_DIALOG_OPEN) not in tap.call_args_list
    assert call(USE_ALL_DIALOG_CANCEL) not in tap.call_args_list
    assert observation["lunches_remaining"] == 0
    assert observation["lunch_total_recovery"] is None
    assert observation["source_confidence"] == "HIGH"
