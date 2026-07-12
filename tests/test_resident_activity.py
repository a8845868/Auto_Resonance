import unittest

from auto.resident_activity import (
    FULL_REALM_REWARDS,
    ResidentActivityAutomation,
    SIEGE_TASKS,
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

    def go_home(self):
        return True

    def dismiss_result(self):
        pass


class ResidentActivityTests(unittest.TestCase):
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
            [item("扫荡", 870, 490)],
        ])
        automation = ResidentActivityAutomation(driver)
        self.assertTrue(automation.select_siege_task("武器材质分析"))
        self.assertEqual(driver.taps[-1], (700, 606))

    def test_reward_attempts_uses_ocr_counter_and_safe_fallback(self):
        driver = FakeDriver([[item("本日可获取奖励次数 2/3")]])
        self.assertEqual(ResidentActivityAutomation(driver)._reward_attempts(), 2)
        driver.pages = [[item("无法识别")]]
        self.assertEqual(ResidentActivityAutomation(driver)._reward_attempts(), 3)

    def test_sweep_stops_when_initial_button_disappears(self):
        class SweepDriver(FakeDriver):
            def click_text(self, text, **_):
                self.clicked.append(text)
                if text == "开始扫荡":
                    return True
                return self.clicked.count("扫荡") <= 2

        driver = SweepDriver([[]])
        self.assertEqual(ResidentActivityAutomation(driver).sweep_current_activity(3), 2)
        self.assertEqual(
            driver.clicked,
            ["扫荡", "开始扫荡", "扫荡", "开始扫荡", "扫荡"],
        )

    def test_single_sweep_has_hard_limit_of_one(self):
        class SweepDriver(FakeDriver):
            def click_text(self, text, **_):
                self.clicked.append(text)
                return True

        driver = SweepDriver([[]])
        self.assertEqual(ResidentActivityAutomation(driver).sweep_current_activity(1), 1)
        self.assertEqual(driver.clicked, ["扫荡", "开始扫荡"])

    def test_sweep_is_not_counted_when_team_confirmation_is_missing(self):
        class SweepDriver(FakeDriver):
            def click_text(self, text, **_):
                self.clicked.append(text)
                return text == "扫荡"

        driver = SweepDriver([[]])
        self.assertEqual(ResidentActivityAutomation(driver).sweep_current_activity(1), 0)
        self.assertEqual(driver.clicked, ["扫荡", "开始扫荡"])

    def test_action_button_uses_precise_fixed_center_when_ocr_misses(self):
        class FallbackDriver(FakeDriver):
            def click_text(self, text, **_):
                self.clicked.append(text)
                return False

        driver = FallbackDriver([[item("选择队伍")]])
        automation = ResidentActivityAutomation(driver)
        self.assertTrue(
            automation.click_action_button(
                "开始扫荡",
                fallback=(771, 526),
                screen_marker="选择队伍",
            )
        )
        self.assertEqual(driver.taps, [(771, 526)])

    def test_academy_chest_selects_salvation_supply_stage(self):
        self.assertEqual(FULL_REALM_REWARDS["学会装备箱"], "特供·救世")
        driver = FakeDriver([[
            item("本日可获取奖励次数 1/3"),
            item("全境特供"),
            item("特供·救世"),
            item("进入挑战"),
            item("扫荡"),
            item("开始扫荡"),
        ]])
        completed = ResidentActivityAutomation(driver).run_limited_activity(
            "全境特供", stage=FULL_REALM_REWARDS["学会装备箱"]
        )
        self.assertEqual(completed, 1)
        self.assertIn("特供·救世", driver.clicked)


if __name__ == "__main__":
    unittest.main()
