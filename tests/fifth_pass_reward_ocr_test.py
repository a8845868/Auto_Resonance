from __future__ import annotations

from unittest.mock import Mock

import numpy as np

from auto.reward_collection import (
    RewardCollector,
    _manual_daily_progress,
    _manual_level_frame_observation,
    _observe_manual_level_rewards,
)


def _box(x, y, text):
    return {
        "text": text,
        "position": (
            (x - 20, y - 10),
            (x + 20, y - 10),
            (x + 20, y + 10),
            (x - 20, y + 10),
        ),
    }


class Frame:
    def __init__(self, items):
        self._items = items
        self.image = np.zeros((720, 1280, 3), dtype=np.uint8)

    def ocr(self):
        return self._items


def _level_frame(x=300, level_text="LV.12"):
    return Frame([
        _box(350, 80, "环游手册 等级奖励"),
        _box(x, 180, level_text),
        _box(1000, 560, "一键领取"),
    ])


def _aggregate_items(ratio_x=220, ratio_y=205):
    return [
        _box(200, 100, "每日任务"),
        _box(220, 160, "总进度"),
        _box(ratio_x, ratio_y, "4/5"),
    ]


def test_manual_level_without_numeric_level_is_unknown():
    assert _manual_level_frame_observation(_level_frame(level_text="等级奖励")) is None


def test_manual_level_position_must_be_stable():
    driver = Mock()
    driver.frame.side_effect = [_level_frame(280), _level_frame(360)]
    driver.sleep = Mock()
    assert _observe_manual_level_rewards(driver) is None


def test_aggregate_parser_ignores_nearer_individual_ratio():
    items = _aggregate_items() + [_box(225, 175, "0/10")]
    assert _manual_daily_progress(items) == (4, 5)


def test_ambiguous_aggregate_ratios_return_unknown():
    items = [
        _box(200, 100, "每日任务"),
        _box(250, 160, "总进度"),
        _box(210, 205, "4/5"),
        _box(290, 205, "3/5"),
    ]
    assert _manual_daily_progress(items) is None


def test_aggregate_ratio_position_is_stable_across_frames():
    driver = Mock()
    driver.click_text.return_value = True
    driver.texts.side_effect = [
        _aggregate_items(ratio_y=200),
        _aggregate_items(ratio_y=225),
    ]
    driver.sleep = Mock()
    collector = RewardCollector(driver)
    collector._open_from_home = Mock(return_value=True)
    assert collector.observe_travel_manual() is None
