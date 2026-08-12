import unittest
from unittest.mock import Mock, patch

from auto.resident_activity import (
    ENTER_CHALLENGE_Y,
    FULL_REALM_REWARDS,
    REWARD_TITLE_ROI,
    ResidentActivityAutomation,
    SIEGE_TASKS,
    START_SWEEP_BUTTON_CENTER,
    SWEEP_BUTTON_CENTER,
    _find_resonance_port,
    _matches,
)
from core.services.action_summary_execution_interlock import (
    ActionSummaryExecutionMode,
)
from core.control.adb_port import EmulatorInfo, EmulatorType


def item(text, x=600, y=420):
    return {
        "text": text,
        "position": [[x - 20, y - 10], [x + 20, y - 10], [x + 20, y + 10], [x - 20, y + 10]],
    }


def legacy_automation(driver):
    return ResidentActivityAutomation(
        driver,
        execution_mode=ActionSummaryExecutionMode.LEGACY_COMPATIBILITY,
    )


class FakeDriver:
    def __init__(self, pages):
        self.pages = pages
        self.page = 0
        self.taps = []
        self.clicked = []

    def texts(self):
        return self.pages[self.page]

    def tap(self, pos, **_):
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

    def has_text(self, text):
        return any(_matches(entry["text"], text) for entry in self.texts())

    def reward_icon_score(self, reward, region):
        return 0.9

    def go_home(self):
        return True

    def dismiss_result(self):
        pass


class SweepFlowDriver(FakeDriver):
    def __init__(self, max_sweeps=1, team_available=True):
        super().__init__([[]])
        self.state = "detail"
        self.completed = 0
        self.max_sweeps = max_sweeps
        self.team_available = team_available

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
            "reward": [item("获得物品", 675, 222)],
        }
        return labels[self.state]

    def tap(self, pos, **_):
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
    def test_resonance_port_is_selected_from_multiple_mumu_instances(self):
        devices = [
            EmulatorInfo("明日方舟", 16384, "", EmulatorType.MUMUV5, 0),
            EmulatorInfo("雷索纳斯", 16544, "", EmulatorType.MUMUV5, 5),
        ]
        self.assertEqual(_find_resonance_port(devices), 16544)

    def test_all_siege_tasks_are_in_required_order(self):
        self.assertEqual(SIEGE_TASKS, (
            "特殊订单", "利刃行动", "挑灯看剑", "武器材质分析", "骑士小说",
            "我思我在", "所知所闻", "大的！", "总体围剿",
        ))

    def test_select_siege_task_scrolls_and_taps_challenge_below_title(self):
        class TaskDriver(FakeDriver):
            def tap(self, pos, **_):
                super().tap(pos)
                if pos[1] > 500:
                    self.page = 2

        driver = TaskDriver([
            [item("特殊订单")],
            [
                item("武器材质分析", 700, 410),
                item("进入挑战", 700, 606),
                item("进入挑战", 1000, 606),
            ],
            [item("扫荡", 870, 490), item("难度选择", 110, 677)],
        ])
        automation = legacy_automation(driver)
        self.assertTrue(automation.select_siege_task("武器材质分析"))
        self.assertEqual(driver.taps[-1], (700, ENTER_CHALLENGE_Y))

    def test_first_limited_activity_uses_full_button_center(self):
        class ChallengeDriver(FakeDriver):
            def tap(self, pos, **kwargs):
                super().tap(pos, **kwargs)
                if pos == (593, ENTER_CHALLENGE_Y):
                    self.page = 1

        driver = ChallengeDriver([
            [
                item("进入挑战", 593, 606),
                item("进入挑战", 875, 606),
            ],
            [item("扫荡", 872, 490), item("难度选择", 110, 677)],
        ])
        self.assertTrue(
            legacy_automation(driver).enter_first_visible_challenge()
        )
        self.assertEqual(driver.taps, [(593, ENTER_CHALLENGE_Y)])

    def test_reward_attempts_uses_ocr_counter_and_safe_fallback(self):
        driver = FakeDriver([[item("本日可获取奖励次数 2/3")]])
        self.assertEqual(legacy_automation(driver)._reward_attempts(), 2)
        driver.pages = [[item("无法识别")]]
        self.assertEqual(legacy_automation(driver)._reward_attempts(), 3)

    def test_sweep_stops_when_initial_button_disappears(self):
        driver = SweepFlowDriver(max_sweeps=2)
        self.assertEqual(legacy_automation(driver).sweep_current_activity(3), 2)
        self.assertEqual(driver.taps.count(SWEEP_BUTTON_CENTER), 2)
        self.assertEqual(driver.taps.count(START_SWEEP_BUTTON_CENTER), 2)

    def test_single_sweep_has_hard_limit_of_one(self):
        driver = SweepFlowDriver(max_sweeps=3)
        self.assertEqual(legacy_automation(driver).sweep_current_activity(1), 1)
        self.assertEqual(driver.taps, [SWEEP_BUTTON_CENTER, START_SWEEP_BUTTON_CENTER])

    def test_sweep_is_not_counted_when_team_confirmation_is_missing(self):
        driver = SweepFlowDriver(team_available=False)
        self.assertEqual(legacy_automation(driver).sweep_current_activity(1), 0)
        self.assertEqual(driver.taps, [SWEEP_BUTTON_CENTER])

    def test_matching_text_outside_expected_roi_never_unlocks_a_click(self):
        driver = FakeDriver([[
            item("扫荡", 300, 200),
            item("难度选择", 110, 677),
            item("开始扫荡", 772, 526),
        ]])
        self.assertEqual(legacy_automation(driver).sweep_current_activity(1), 0)
        self.assertEqual(driver.taps, [])

    def test_reward_title_animation_positions_are_both_accepted(self):
        automation = ResidentActivityAutomation(FakeDriver([[]]))
        for y in (119, 222):
            self.assertTrue(
                automation.text_in_roi(
                    [item("获得物品", 675, y)],
                    "获得物品",
                    (500, 70, 820, 270),
                )
            )

    def test_academy_chest_selects_salvation_supply_stage(self):
        self.assertEqual(FULL_REALM_REWARDS["学会装备箱"], "特供·救世")

    def test_supply_stage_uses_nearest_challenge_button(self):
        class DetailDriver(FakeDriver):
            def tap(self, pos, **kwargs):
                super().tap(pos, **kwargs)
                if pos == (700, ENTER_CHALLENGE_Y):
                    self.page = 1

        driver = DetailDriver([
            [
                item("特供·救世", 700, 420),
                item("进入挑战", 420, 606),
                item("进入挑战", 700, 606),
            ],
            [
                item("特供·救世", 1080, 105),
                item("奖励预览", 825, 385),
                item("扫荡", 870, 490),
            ],
        ])
        self.assertTrue(
            legacy_automation(driver).select_activity_stage(
                "特供·救世", "学会装备箱"
            )
        )
        self.assertEqual(driver.taps[0], (700, ENTER_CHALLENGE_Y))

    def test_wrong_detail_reward_returns_without_sweeping(self):
        class WrongRewardDriver(FakeDriver):
            def reward_icon_score(self, reward, region):
                return 0.9 if region[1] == 455 else 0.1

            def tap(self, pos, **kwargs):
                super().tap(pos, **kwargs)
                if pos == (700, ENTER_CHALLENGE_Y):
                    self.page = 1

        driver = WrongRewardDriver([
            [
                item("特供·救世", 700, 420),
                item("进入挑战", 700, 606),
            ],
            [
                item("特供·救世", 1080, 105),
                item("奖励预览", 825, 385),
                item("扫荡", 870, 500),
            ],
        ])
        self.assertFalse(
            legacy_automation(driver).select_activity_stage(
                "特供·救世", "学会装备箱"
            )
        )
        self.assertEqual(driver.taps[-1], (82, 36))

    def test_home_navigation_failure_is_not_reported_as_zero_completion(self):
        driver = FakeDriver([[]])
        driver.go_home = lambda: False
        automation = legacy_automation(driver)

        with patch("auto.resident_activity.connect", return_value=True):
            with self.assertRaisesRegex(RuntimeError, "无法打开活动总览"):
                automation.run("挑灯看剑")

    def test_open_action_summary_uses_bounded_navigator(self):
        driver = FakeDriver([[]])
        driver.capture_frame = Mock()
        driver.dispatch_navigation = Mock()
        result = Mock(success=True)
        navigator = Mock()
        navigator.navigate.return_value = result

        with patch(
            "auto.resident_activity.ActionSummaryNavigator",
            return_value=navigator,
        ) as factory:
            self.assertTrue(
                ResidentActivityAutomation(
                    driver, use_proven_edge_planner=False
                ).open_action_summary()
            )

        factory.assert_called_once()
        navigator.navigate.assert_called_once_with()
        self.assertEqual(driver.taps, [])

    def test_read_only_product_model_never_clicks_action_summary_page(self):
        class ModelFrame:
            source_capture_id = "read-only-model"

            class Image:
                shape = (720, 1280, 3)

            image = Image()

            @staticmethod
            def ocr():
                return [
                    item("利刃围剿", 680, 84),
                    item("特殊订单", 580, 420),
                    item("利刃行动", 860, 420),
                    item("进入挑战", 580, 606),
                    item("进入挑战", 860, 606),
                ]

        driver = FakeDriver([[]])
        driver.capture_frame = Mock(return_value=ModelFrame())
        automation = ResidentActivityAutomation(driver)
        automation.open_action_summary = Mock(return_value=True)
        automation.select_siege_task = Mock(
            side_effect=AssertionError("legacy business selection must not run")
        )
        automation.sweep_current_activity = Mock(
            side_effect=AssertionError("legacy sweep must not run")
        )

        model, decision = automation.read_action_summary_product_model()

        self.assertEqual(model.page_state, "ACTION_SUMMARY_VISIBLE")
        self.assertEqual(decision.decision.value, "TASK_AVAILABLE_NEEDS_POLICY")
        automation.open_action_summary.assert_called_once_with()
        automation.select_siege_task.assert_not_called()
        automation.sweep_current_activity.assert_not_called()
        driver.capture_frame.assert_called_once_with()
        self.assertEqual(driver.taps, [])


if __name__ == "__main__":
    unittest.main()
