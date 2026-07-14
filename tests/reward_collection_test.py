import numpy as np
import pytest
from datetime import datetime
from unittest.mock import Mock

import auto.reward_collection as reward_collection
from auto.reward_collection import RewardCollector, RewardDriver, _daily_cycle


@pytest.fixture(autouse=True)
def isolate_reward_state(monkeypatch, tmp_path):
    """Reward tests must never overwrite config/reward_state.json."""
    monkeypatch.setattr(reward_collection, "STATE_PATH", tmp_path / "reward_state.json")


class FakeFrame:
    def __init__(self):
        self.image = np.zeros((720, 1280, 3), dtype=np.uint8)


class FakeDriver:
    def __init__(self):
        self.taps = []
        self.clicked = []

    def go_home(self): return True
    def red_badge_shortcuts(self): return [(100, 100)]
    def tap(self, pos): self.taps.append(pos)
    def sleep(self, _): pass
    def has_text(self, text): return text in ("每日活跃", "环游手册")
    def frame(self): return FakeFrame()
    def texts(self): return []
    def click_text(self, text, attempts=3):
        self.clicked.append(text)
        return text in ("每日活跃", "任务列表", "一键领取", "环游手册")
    def click_exact_text(self, text, attempts=3):
        self.clicked.append(text)
        return text == "环游手册"


def test_daily_collection_opens_daily_page():
    driver = FakeDriver()
    collector = RewardCollector(driver)
    collector.state = {}
    collector.collect_daily_activity()
    assert driver.taps[0] == (100, 100)


def test_daily_page_can_be_confirmed_by_progress_labels_when_title_is_artwork():
    driver = FakeDriver()
    driver.has_text = lambda _text: False
    driver.texts = lambda: [
        {"text": "完成进度"},
        {"text": "活跃度"},
    ]
    collector = RewardCollector(driver)
    collector.state = {}

    assert collector._open_from_home("每日活跃")
    assert collector.state["shortcut_positions"]["每日活跃"] == [100, 100]


def test_daily_stage_rewards_click_only_one_yellow_box():
    before = np.zeros((720, 1280, 3), dtype=np.uint8)
    for x in (439, 562, 684):
        before[135:195, x - 30:x + 30] = (0, 255, 255)
    after = np.zeros_like(before)

    driver = FakeDriver()
    # The first frame is used for detection, the second verifies the whole
    # batch changed after the single game-side auto-claim.
    first = FakeFrame()
    first.image = before
    second = FakeFrame()
    second.image = after
    frames = iter([first, second])
    driver.frame = lambda: next(frames)
    collector = RewardCollector(driver)
    collector.state = {}

    assert collector.collect_daily_activity() == 1
    stage_taps = [tap for tap in driver.taps if tap[1] == 164]
    assert stage_taps == [(439, 164)]


def test_manual_claims_tasks_before_level_rewards():
    driver = FakeDriver()
    collector = RewardCollector(driver)
    collector.state = {}
    assert collector.collect_travel_manual() == 2
    assert driver.clicked.index("任务列表") < driver.clicked.index("一键领取")
    assert driver.clicked.index("一键领取") < driver.clicked.index("环游手册")
    assert driver.clicked.count("一键领取") == 2


def test_stale_learned_shortcut_falls_back_to_current_badges():
    driver = FakeDriver()
    driver.red_badge_shortcuts = lambda: [(700, 80)]
    collector = RewardCollector(driver)
    collector.state = {"shortcut_positions": {"环游手册": [100, 100]}}
    driver.has_text = Mock(side_effect=[False, True])

    assert collector._open_from_home("环游手册")
    assert driver.taps[:3] == [(100, 100), (82, 36), (700, 80)]
    assert collector.state["shortcut_positions"]["环游手册"] == [700, 80]


def test_one_click_is_not_counted_if_button_remains_visible():
    driver = FakeDriver()
    collector = RewardCollector(driver)
    driver.has_text = lambda text: text in ("环游手册", "一键领取")

    assert collector.collect_travel_manual() == 0


def test_daily_cycle_changes_at_five_am():
    assert _daily_cycle(datetime(2026, 7, 12, 4, 59)) == "2026-07-11"
    assert _daily_cycle(datetime(2026, 7, 12, 5, 0)) == "2026-07-12"


def test_reward_go_home_enters_login_safely_without_clicking_top_left():
    driver = RewardDriver(sleep=lambda _seconds: None)
    driver.texts = Mock(side_effect=[
        [{"text": "修复资源完整性会自动退出游戏，是否继续？"}],
        [{"text": "需要下载资源包（共20.9MB）"}, {"text": "确认"}, {"text": "0%"}],
        [{"text": "点击屏幕进入游戏"}],
        [{"text": "81%"}],
        [{"text": "触碰空白区域退出"}],
        [{"text": "访问城市"}],
    ])
    driver.tap = Mock()

    assert driver.go_home()
    assert [call.args[0] for call in driver.tap.call_args_list] == [
        (320, 500),
        (640, 506),
        (640, 560),
        (100, 650),
    ]


def test_reward_navigation_failure_is_not_reported_as_zero_reward_success():
    driver = FakeDriver()
    driver.go_home = lambda: False
    collector = RewardCollector(driver)
    collector.state = {}

    with pytest.raises(RuntimeError, match="无法返回主界面"):
        collector.collect_daily_activity()
