import unittest

from auto.resident_activity import ResidentActivityAutomation, SIEGE_TASKS, _matches


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


class ResidentActivityTests(unittest.TestCase):
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

    def test_sweep_stops_when_button_disappears(self):
        class SweepDriver(FakeDriver):
            def click_text(self, text, **_):
                if text != "扫荡":
                    return False
                self.clicked.append(text)
                return len(self.clicked) <= 2

        driver = SweepDriver([[]])
        self.assertEqual(ResidentActivityAutomation(driver).sweep_current_activity(3), 2)


if __name__ == "__main__":
    unittest.main()
