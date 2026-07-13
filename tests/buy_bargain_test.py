from unittest.mock import patch

import auto.run_business.buy as buy
from core.module.bgr import BGR


class FakeHsv:
    def __init__(self, hue):
        self.h = hue


class FakeImage:
    def __init__(self, hue=100):
        self.hue = hue

    def get_bgr(self, pos):
        assert pos == (1176, 461)
        return BGR(1, 128, 248, offset=0)

    def crop_image(self, *_args):
        return self

    def get_hsv(self, _pos):
        return FakeHsv(self.hue)


def test_buy_bargain_completes_without_legacy_fixed_pixel_wait():
    with patch.object(buy, "screenshot", return_value=FakeImage()), patch.object(
        buy, "input_tap"
    ) as tap, patch.object(buy.time, "sleep"):
        assert buy.click_bargain_button(num=2)

    assert tap.call_count == 2


def test_discount_result_polls_until_transient_success_colour():
    frames = [FakeImage(10), FakeImage(50), FakeImage(100)]
    with patch.object(buy, "screenshot", side_effect=frames), patch.object(
        buy.time, "sleep"
    ):
        assert buy._wait_for_discount_result(timeout=5)
