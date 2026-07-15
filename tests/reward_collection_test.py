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
    def __init__(self, image=None, items=None):
        self.image = image if image is not None else np.zeros((720, 1280, 3), dtype=np.uint8)
        self.items = items or []

    def ocr(self):
        return self.items


def ocr_box(x, y, text):
    return {
        "text": text,
        "position": ((x - 20, y - 10), (x + 20, y - 10), (x + 20, y + 10), (x - 20, y + 10)),
    }


def yellow_stage_frame(*xs, items=None):
    image = np.zeros((720, 1280, 3), dtype=np.uint8)
    for x in xs:
        image[135:195, x - 30:x + 30] = (0, 255, 255)
    return FakeFrame(image, items)


def daily_page_items(activity="600", claimable=False):
    items = [
        ocr_box(450, 60, "每日活跃"),
        ocr_box(225, 200, activity),
    ]
    if claimable:
        items.append(ocr_box(500, 610, "可领取"))
    return items


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
    # At 600 activity the game normally clears every yellow stage box after
    # one click; the third frame is the later completion check.
    frames = iter([first, second, FakeFrame()])
    driver.frame = lambda: next(frames)
    collector = RewardCollector(driver)
    collector.state = {}

    assert collector.collect_daily_activity() == 1
    stage_taps = [tap for tap in driver.taps if tap[1] == 164]
    assert stage_taps == [(439, 164)]


def test_daily_stage_rewards_rescan_and_claim_each_remaining_box():
    driver = FakeDriver()
    frames = iter([
        yellow_stage_frame(439, 562, 684),
        yellow_stage_frame(562, 684),
        yellow_stage_frame(684),
        yellow_stage_frame(),
        yellow_stage_frame(),  # completion check stops because OCR is absent
    ])
    driver.frame = lambda: next(frames)
    collector = RewardCollector(driver)
    collector.state = {}

    assert collector.collect_daily_activity() == 3
    assert [tap for tap in driver.taps if tap[1] == 164] == [
        (439, 164),
        (562, 164),
        (684, 164),
    ]


def test_daily_completion_requires_two_clean_frames_at_600_or_more():
    driver = FakeDriver()
    driver.frame = Mock(side_effect=[
        yellow_stage_frame(items=daily_page_items("600")),
        yellow_stage_frame(items=daily_page_items("600")),
    ])
    collector = RewardCollector(driver)

    assert collector._daily_completion_confirmed()
    assert driver.frame.call_count == 2


def test_daily_completion_rejects_600_when_a_later_frame_still_has_a_stage_box():
    driver = FakeDriver()
    driver.frame = Mock(side_effect=[
        yellow_stage_frame(items=daily_page_items("600")),
        yellow_stage_frame(562, items=daily_page_items("600")),
    ])
    collector = RewardCollector(driver)

    assert not collector._daily_completion_confirmed()


def test_daily_completion_rejects_claimable_text_even_at_600():
    driver = FakeDriver()
    driver.frame = Mock(return_value=yellow_stage_frame(items=daily_page_items("600", True)))
    collector = RewardCollector(driver)

    assert not collector._daily_completion_confirmed()


def test_daily_completion_rejects_clean_frames_below_600():
    driver = FakeDriver()
    driver.frame = Mock(return_value=yellow_stage_frame(items=daily_page_items("599")))
    collector = RewardCollector(driver)

    assert not collector._daily_completion_confirmed()


def test_daily_stage_reward_retries_when_first_click_does_not_clear_boxes():
    driver = FakeDriver()
    frames = iter([
        yellow_stage_frame(439, 562),
        yellow_stage_frame(439, 562),  # first click did not take effect
        yellow_stage_frame(),  # retry triggers the game-side claim-all behavior
        yellow_stage_frame(),  # completion check stops because OCR is absent
    ])
    driver.frame = lambda: next(frames)
    collector = RewardCollector(driver)
    collector.state = {}

    assert collector.collect_daily_activity() == 1
    assert [tap for tap in driver.taps if tap[1] == 164] == [(439, 164), (439, 164)]


def test_daily_stage_reward_does_not_count_a_box_that_is_still_yellow():
    reduced = np.zeros((720, 1280, 3), dtype=np.uint8)
    reduced[145:180, 419:459] = (0, 255, 255)  # fewer pixels, still clearly yellow
    driver = FakeDriver()
    frames = iter([
        yellow_stage_frame(439),
        FakeFrame(reduced),
        yellow_stage_frame(),
        yellow_stage_frame(),  # completion check stops because OCR is absent
    ])
    driver.frame = lambda: next(frames)
    collector = RewardCollector(driver)
    collector.state = {}

    assert collector.collect_daily_activity() == 1
    assert [tap for tap in driver.taps if tap[1] == 164] == [(439, 164), (439, 164)]


def test_cached_daily_completion_is_revalidated_and_revoked_when_page_disagrees(monkeypatch):
    driver = FakeDriver()
    collector = RewardCollector(driver)
    cycle = _daily_cycle()
    collector.state = {"daily_activity_completed_cycle": cycle}
    monkeypatch.setattr(collector, "_open_from_home", Mock(return_value=True))
    monkeypatch.setattr(
        collector,
        "_daily_completion_confirmed",
        Mock(side_effect=[False, False]),
    )

    assert collector.collect_daily_activity() == 0
    assert "daily_activity_completed_cycle" not in collector.state
    collector._open_from_home.assert_called_once_with("每日活跃")


def test_cached_daily_completion_still_opens_page_for_lightweight_confirmation(monkeypatch):
    collector = RewardCollector(FakeDriver())
    cycle = _daily_cycle()
    collector.state = {"daily_activity_completed_cycle": cycle}
    monkeypatch.setattr(collector, "_open_from_home", Mock(return_value=True))
    monkeypatch.setattr(collector, "_daily_completion_confirmed", Mock(return_value=True))

    assert collector.collect_daily_activity() == 0
    collector._open_from_home.assert_called_once_with("每日活跃")


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


def test_reward_go_home_cancels_clarity_replenish_prompt_before_retrying():
    driver = RewardDriver(sleep=lambda _seconds: None)
    driver.texts = Mock(side_effect=[
        [
            ocr_box(684, 362, "您当前的澄明度不足，是否补充澄明度？"),
            ocr_box(333, 512, "取消"),
            ocr_box(987, 507, "确认"),
        ],
        [{"text": "访问城市"}],
    ])
    driver.tap = Mock()

    assert driver.go_home()
    assert [call.args[0] for call in driver.tap.call_args_list] == [(333, 512)]


def test_reward_go_home_uses_guarded_cancel_coordinate_when_label_is_missed():
    driver = RewardDriver(sleep=lambda _seconds: None)
    driver.texts = Mock(side_effect=[
        [
            ocr_box(684, 362, "您当前的澄明度不足，是否补充澄明度？"),
            ocr_box(987, 507, "确认"),
        ],
        [{"text": "访问城市"}],
    ])
    driver.tap = Mock()

    assert driver.go_home()
    assert [call.args[0] for call in driver.tap.call_args_list] == [(350, 509)]


def test_reward_navigation_failure_is_not_reported_as_zero_reward_success():
    driver = FakeDriver()
    driver.go_home = lambda: False
    collector = RewardCollector(driver)
    collector.state = {}

    with pytest.raises(RuntimeError, match="无法返回主界面"):
        collector.collect_daily_activity()
