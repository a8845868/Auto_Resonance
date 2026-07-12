from unittest.mock import patch

import auto.run_business.sell as sell
from core.module.bgr import BGR


class FakeHsv:
    def __init__(self, hue):
        self.hue = hue

    def __getitem__(self, index):
        return self.hue if index == 0 else 0


class FakeImage:
    def __init__(self, *, complete=True, hue=0, button=None):
        self.complete = complete
        self.hue = hue
        self.button = button or BGR(0, 175, 243, offset=0)

    def get_bgr(self, pos):
        if pos == (1176, 461):
            return self.button
        if self.complete:
            return BGR(30, 40, 50, offset=0)
        return BGR(0, 0, 0, offset=0)

    def crop_image(self, *_args):
        return self

    def get_hsv(self, _pos):
        return FakeHsv(self.hue)


def test_raise_result_skips_incomplete_frame_and_polls_until_success():
    frames = [
        FakeImage(complete=False),
        FakeImage(hue=10),
        FakeImage(hue=35),
    ]
    with patch.object(sell, "screenshot", side_effect=frames), patch.object(
        sell.time, "sleep"
    ):
        assert sell._wait_for_raise_result(timeout=5)


def test_sell_bargain_retries_after_a_failed_raise():
    with patch.object(sell, "screenshot", return_value=FakeImage()), patch.object(
        sell, "_wait_for_raise_result", side_effect=[True, False, True]
    ), patch.object(sell, "input_tap") as tap, patch.object(sell.time, "sleep"):
        assert sell.click_bargain_button(num=2)

    assert tap.call_count == 3
