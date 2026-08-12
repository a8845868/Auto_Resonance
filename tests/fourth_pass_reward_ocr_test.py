from __future__ import annotations

from unittest.mock import Mock

import numpy as np

from auto.reward_collection import (
    RewardCollector,
    _manual_daily_progress,
    _observe_manual_level_rewards,
)


def _box(x, y, text):
    return {
        "text": text,
        "position": ((x - 20, y - 10), (x + 20, y - 10), (x + 20, y + 10), (x - 20, y + 10)),
    }


class Frame:
    def __init__(self, items, image=None):
        self._items = items
        self.image = image if image is not None else np.zeros((720, 1280, 3), dtype=np.uint8)

    def ocr(self):
        return self._items


def _level_frame(*, claimable=True, anchor=True, level=12):
    items = []
    if anchor:
        items.append(_box(350, 80, "环游手册 等级奖励"))
    items.append(_box(300, 180, f"LV.{level}"))
    if claimable:
        items.append(_box(1000, 560, "一键领取"))
    return Frame(items)


def _task_items():
    return [
        _box(200, 100, "每日任务"),
        _box(220, 160, "总进度"),
        _box(220, 200, "4/5"),
    ]


def test_manual_level_rewards_require_multiframe_stability():
    driver = Mock()
    driver.frame.side_effect = [_level_frame(level=12), _level_frame(level=12)]
    driver.sleep = Mock()
    assert _observe_manual_level_rewards(driver) == 1
    assert driver.frame.call_count == 2


def test_manual_level_single_frame_miss_is_unknown_not_complete():
    driver = Mock()
    driver.frame.side_effect = [_level_frame(), _level_frame(anchor=False)]
    driver.sleep = Mock()
    assert _observe_manual_level_rewards(driver) is None


def test_manual_ratio_requires_aggregate_anchor():
    assert _manual_daily_progress([
        _box(200, 100, "每日任务"),
        _box(220, 200, "4/5"),
    ]) is None


def test_manual_parser_ignores_individual_task_ratio_in_same_roi():
    items = _task_items() + [_box(350, 230, "1/1")]
    assert _manual_daily_progress(items) == (4, 5)


def test_observe_travel_manual_requires_stable_level_page():
    driver = Mock()
    driver.click_text.return_value = True
    driver.click_exact_text.return_value = True
    driver.texts.side_effect = [_task_items(), _task_items()]
    driver.frame.side_effect = [_level_frame(level=12), _level_frame(level=13)]
    driver.sleep = Mock()
    collector = RewardCollector(driver)
    collector._open_from_home = Mock(return_value=True)
    assert collector.observe_travel_manual() is None


def test_daily_completion_uses_observed_maximum():
    driver = Mock()
    driver.frame.side_effect = [
        Frame([_box(450, 60, "每日活跃"), _box(225, 200, "700/700")]),
        Frame([_box(450, 60, "每日活跃"), _box(225, 200, "700/700")]),
    ]
    driver.sleep = Mock()
    collector = RewardCollector(driver)
    assert collector._daily_completion_confirmed()
