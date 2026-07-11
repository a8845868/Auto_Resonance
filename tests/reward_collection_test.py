import numpy as np
from datetime import datetime

from auto.reward_collection import RewardCollector, _daily_cycle


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


def test_daily_collection_opens_daily_page():
    driver = FakeDriver()
    collector = RewardCollector(driver)
    collector.state = {}
    collector.collect_daily_activity()
    assert driver.taps[0] == (100, 100)
    assert "每日活跃" in driver.clicked


def test_manual_claims_tasks_before_level_rewards():
    driver = FakeDriver()
    collector = RewardCollector(driver)
    collector.state = {}
    assert collector.collect_travel_manual() == 2
    assert driver.clicked.index("任务列表") < driver.clicked.index("一键领取")
    assert driver.clicked.index("一键领取") < driver.clicked.index("环游手册")
    assert driver.clicked.count("一键领取") == 2


def test_daily_cycle_changes_at_five_am():
    assert _daily_cycle(datetime(2026, 7, 12, 4, 59)) == "2026-07-11"
    assert _daily_cycle(datetime(2026, 7, 12, 5, 0)) == "2026-07-12"
