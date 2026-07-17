from unittest.mock import Mock

from auto.resident_activity import ScreenDriver


def _box(x, y, text):
    return {
        "text": text,
        "position": ((x - 10, y - 10), (x + 10, y - 10), (x + 10, y + 10), (x - 10, y + 10)),
    }


def test_go_home_handles_clarity_replenish_dialog_without_name_error():
    driver = ScreenDriver(sleep=lambda _seconds: None)
    driver.texts = Mock(
        side_effect=[
            [
                _box(684, 362, "您当前的澄明度不足，是否补充澄明度？"),
                _box(333, 512, "取消"),
            ],
            [_box(500, 80, "访问城市")],
        ]
    )
    driver.tap = Mock()

    assert driver.go_home() is True
    driver.tap.assert_called_once_with((333, 512))
