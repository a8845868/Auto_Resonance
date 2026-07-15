import unittest
from unittest.mock import Mock, patch

from auto.resident_activity import (
    DETAIL_MARKER_ROI,
    DETAIL_SWEEP_ROI,
    FULL_REALM_REWARDS,
    REWARD_DISMISS_ROI,
    REWARD_ITEMS_ROI,
    REWARD_TITLE_ROI,
    ResidentActivityAutomation,
    SIEGE_TASKS,
    START_SWEEP_BUTTON_CENTER,
    ScreenDriver,
    SWEEP_BUTTON_CENTER,
    TEAM_START_ROI,
    TEAM_TITLE_ROI,
    _matches,
)


def item(text, x=600, y=420):
    return {
        "text": text,
        "position": [[x - 20, y - 10], [x + 20, y - 10], [x + 20, y + 10], [x - 20, y + 10]],
    }


class FakeDriver:
    def __init__(self, pages):
        self.pages = pages
        self.page = 0
        self.taps = []
        self.clicked = []

    def texts(self):
        return self.pages[self.page]

    def tap(self, pos):
        self.taps.append(pos)

    def sleep(self, _):
        pass

    def swipe_left(self):
        self.page = min(self.page + 1, len(self.pages) - 1)

    def swipe_right(self):
        self.page = max(self.page - 1, 0)

    def click_text(self, text, **_):
        self.clicked.append(text)
        return any(_matches(entry["text"], text) for entry in self.texts())

    def go_home(self):
        return True

    def dismiss_result(self):
        pass


class SweepFlowDriver(FakeDriver):
    def __init__(
        self,
        max_sweeps=1,
        *,
        team_available=True,
        reward_available=True,
    ):
        super().__init__([[]])
        self.state = "detail"
        self.completed = 0
        self.max_sweeps = max_sweeps
        self.team_available = team_available
        self.reward_available = reward_available

    def texts(self):
        if self.state == "detail" and self.completed >= self.max_sweeps:
            return []
        labels = {
            "detail": [item("扫荡", 872, 490), item("难度选择", 110, 677)],
            "team": (
                [item("选择队伍", 640, 166), item("开始扫荡", 772, 526)]
                if self.team_available
                else []
            ),
            "reward": (
                [
                    item("获得物品", 675, 54),
                    item("200", 413, 205),
                    item("触碰空白区域退出", 657, 683),
                ]
                if self.reward_available
                else []
            ),
        }
        return labels[self.state]

    def tap(self, pos):
        super().tap(pos)
        if pos == SWEEP_BUTTON_CENTER and self.state == "detail":
            self.state = "team"
        elif (
            pos == START_SWEEP_BUTTON_CENTER
            and self.state == "team"
            and self.team_available
        ):
            self.state = "reward"

    def dismiss_result(self):
        self.completed += 1
        self.state = "detail"


class ResidentActivityTests(unittest.TestCase):
    def test_go_home_confirms_prelogin_resource_download(self):
        driver = ScreenDriver(sleep=lambda _seconds: None)
        driver.texts = Mock(side_effect=[
            [item("需要下载资源包（共20.9MB）"), item("确认"), item("0%")],
            [item("55%")],
            [item("访问城市")],
        ])
        driver.tap = Mock()

        self.assertTrue(driver.go_home())
        self.assertEqual(
            [call.args[0] for call in driver.tap.call_args_list],
            [(640, 506)],
        )

    def test_go_home_cancels_clarity_replenish_prompt_before_retrying(self):
        driver = ScreenDriver(sleep=lambda _seconds: None)
        driver.texts = Mock(side_effect=[
            [
                item("您当前的澄明度不足，是否补充澄明度？", 684, 362),
                item("取消", 333, 512),
                item("确认", 986, 508),
            ],
            [item("访问城市")],
        ])
        driver.tap = Mock()

        self.assertTrue(driver.go_home())
        self.assertEqual(driver.tap.call_args_list[0].args[0], (333, 512))

    def test_go_home_uses_guarded_clarity_cancel_fallback(self):
        driver = ScreenDriver(sleep=lambda _seconds: None)
        driver.texts = Mock(side_effect=[
            [
                item("您当前的澄明度不足，是否补充澄明度？", 684, 362),
                item("确认", 986, 508),
            ],
            [item("访问城市")],
        ])
        driver.tap = Mock()

        self.assertTrue(driver.go_home())
        self.assertEqual(driver.tap.call_args_list[0].args[0], (350, 509))

    def test_all_siege_tasks_are_in_required_order(self):
        self.assertEqual(SIEGE_TASKS, (
            "特殊订单", "利刃行动", "挑灯看剑", "武器材质分析", "骑士小说",
            "我思我在", "所知所闻", "大的！", "总体围剿",
        ))

    def test_select_siege_task_scrolls_and_taps_challenge_below_title(self):
        driver = FakeDriver([[item("特殊订单")], [item("武器材质分析", 700, 410)]])
        automation = ResidentActivityAutomation(driver)
        self.assertTrue(automation.select_siege_task("武器材质分析"))
        self.assertEqual(driver.taps[-1], (700, 600))

    def test_reward_attempts_uses_ocr_counter_and_safe_fallback(self):
        driver = FakeDriver([[item("本日可获取奖励次数 2/3")]])
        self.assertEqual(ResidentActivityAutomation(driver)._reward_attempts(), 2)
        driver.pages = [[item("无法识别")]]
        self.assertEqual(ResidentActivityAutomation(driver)._reward_attempts(), 3)

    def test_sweep_stops_when_initial_button_disappears(self):
        driver = SweepFlowDriver(max_sweeps=2)
        self.assertEqual(ResidentActivityAutomation(driver).sweep_current_activity(3), 2)
        self.assertEqual(driver.taps.count(SWEEP_BUTTON_CENTER), 2)
        self.assertEqual(driver.taps.count(START_SWEEP_BUTTON_CENTER), 2)

    def test_single_sweep_has_hard_limit_of_one(self):
        driver = SweepFlowDriver(max_sweeps=3)
        self.assertEqual(ResidentActivityAutomation(driver).sweep_current_activity(1), 1)
        self.assertEqual(driver.taps, [SWEEP_BUTTON_CENTER, START_SWEEP_BUTTON_CENTER])

    def test_sweep_is_not_counted_when_team_confirmation_is_missing(self):
        driver = SweepFlowDriver(team_available=False)
        self.assertEqual(ResidentActivityAutomation(driver).sweep_current_activity(1), 0)
        self.assertEqual(driver.taps, [SWEEP_BUTTON_CENTER])

    def test_sweep_is_not_counted_when_reward_screen_is_missing(self):
        driver = SweepFlowDriver(reward_available=False)
        self.assertEqual(ResidentActivityAutomation(driver).sweep_current_activity(1), 0)
        self.assertEqual(driver.taps, [SWEEP_BUTTON_CENTER, START_SWEEP_BUTTON_CENTER])

    def test_matching_text_outside_expected_roi_never_unlocks_a_click(self):
        driver = FakeDriver([[
            item("扫荡", 300, 200),
            item("难度选择", 110, 677),
            item("开始扫荡", 772, 526),
            item("获得物品", 675, 54),
            item("200", 413, 205),
            item("触碰空白区域退出", 657, 683),
        ]])
        self.assertEqual(ResidentActivityAutomation(driver).sweep_current_activity(1), 0)
        self.assertEqual(driver.taps, [])

    def test_start_sweep_text_cannot_match_initial_sweep_marker(self):
        driver = FakeDriver([[
            item("开始扫荡", 872, 490),
            item("难度选择", 110, 677),
        ]])
        self.assertEqual(ResidentActivityAutomation(driver).sweep_current_activity(1), 0)
        self.assertEqual(driver.taps, [])

    def test_sweep_screen_markers_match_verified_regions(self):
        automation = ResidentActivityAutomation(FakeDriver([[]]))
        fixtures = (
            ("扫荡", 872, 490, DETAIL_SWEEP_ROI),
            ("难度选择", 110, 677, DETAIL_MARKER_ROI),
            ("选择队伍", 640, 166, TEAM_TITLE_ROI),
            ("开始扫荡", 772, 526, TEAM_START_ROI),
            ("获得物品", 675, 54, REWARD_TITLE_ROI),
            ("200", 413, 205, REWARD_ITEMS_ROI),
            ("触碰空白区域退出", 657, 683, REWARD_DISMISS_ROI),
        )
        for text, x, y, roi in fixtures:
            self.assertTrue(automation.text_in_roi([item(text, x, y)], text, roi))

    def test_reward_page_waits_for_stable_items_before_dismissal(self):
        class AnimatedRewardDriver(SweepFlowDriver):
            def __init__(self):
                super().__init__()
                self.reward_frame = 0

            def texts(self):
                if self.state != "reward":
                    return super().texts()
                frames = [
                    [
                        item("获得物品", 675, 54),
                        item("200", 413, 205),
                        item("触碰空白区域退出", 657, 683),
                    ],
                    [
                        item("获得物品", 675, 54),
                        item("200", 413, 205),
                        item("10", 821, 338),
                        item("触碰空白区域退出", 657, 683),
                    ],
                ]
                frame = frames[min(self.reward_frame, len(frames) - 1)]
                self.reward_frame += 1
                return frame

        driver = AnimatedRewardDriver()
        automation = ResidentActivityAutomation(driver)

        self.assertEqual(
            automation.sweep_current_activity(1, expected_reward="照夜双刃 / 噪音激酶"),
            1,
        )
        self.assertGreaterEqual(driver.reward_frame, 6)
        self.assertEqual(
            [entry.amount for entry in automation.reward_history[0].entries],
            [200, 10],
        )

    def test_real_reward_title_position_is_inside_reward_roi(self):
        automation = ResidentActivityAutomation(FakeDriver([[]]))
        actual = {
            "text": "获得物品",
            "position": [[593.0, 29.0], [759.0, 32.0], [758.0, 78.0], [592.0, 75.0]],
        }

        self.assertTrue(
            automation.text_in_roi([actual], "获得物品", REWARD_TITLE_ROI)
        )

    def test_run_siege_uses_verified_sweep_flow(self):
        driver = SweepFlowDriver(max_sweeps=1)
        driver.click_text = lambda text, **_: text == "利刃围剿"
        automation = ResidentActivityAutomation(driver)
        automation.select_siege_task = lambda _task: True

        self.assertEqual(automation.run_siege("挑灯看剑", safety_limit=3), 1)
        self.assertEqual(driver.taps, [SWEEP_BUTTON_CENTER, START_SWEEP_BUTTON_CENTER])

    def test_academy_chest_selects_salvation_supply_stage(self):
        self.assertEqual(FULL_REALM_REWARDS["学会装备箱"], "特供·救世")
        driver = FakeDriver([[
            item("本日可获取奖励次数 1/3"),
            item("全境特供"),
            item("特供·救世"),
            item("进入挑战"),
            item("扫荡", 872, 490),
            item("难度选择", 110, 677),
            item("选择队伍", 640, 166),
            item("开始扫荡", 772, 526),
            item("获得物品", 675, 54),
            item("200", 413, 205),
            item("触碰空白区域退出", 657, 683),
        ]])
        completed = ResidentActivityAutomation(driver).run_limited_activity(
            "全境特供", stage=FULL_REALM_REWARDS["学会装备箱"]
        )
        self.assertEqual(completed, 1)
        self.assertIn("特供·救世", driver.clicked)

    def test_home_navigation_failure_is_not_reported_as_zero_completion(self):
        driver = FakeDriver([[]])
        driver.go_home = lambda: False
        automation = ResidentActivityAutomation(driver)

        with patch("auto.resident_activity.connect", return_value=True):
            with self.assertRaisesRegex(RuntimeError, "无法打开活动总览"):
                automation.run("挑灯看剑")


if __name__ == "__main__":
    unittest.main()
