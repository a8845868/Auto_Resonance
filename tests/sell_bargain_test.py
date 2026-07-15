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


class FakeOcrImage:
    def __init__(self, texts):
        self.texts = texts

    def ocr(self):
        return [{"text": text} for text in self.texts]


class FakeSelectedSellPage:
    def ocr(self):
        return []

    def get_bgr(self, pos):
        colors = {
            (315, 675): BGR(245, 245, 245, offset=0),
            (1056, 647): BGR(245, 245, 245, offset=0),
            (1176, 461): BGR(0, 175, 243, offset=0),
            (1156, 100): BGR(0, 0, 100, offset=0),
        }
        return colors[pos]


class FakeSellAllButton:
    def __init__(self, selected=False):
        self.selected = selected

    def get_bgr(self, pos):
        assert pos == (1156, 100)
        if self.selected:
            return BGR(0, 0, 100, offset=0)
        return BGR(40, 120, 70, offset=0)


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


def test_exit_warning_is_treated_as_resumable_sell_state():
    image = FakeOcrImage(["退出后议价幅度将重置，是否继续？"])
    with patch.object(sell, "screenshot", return_value=image), patch.object(
        sell, "input_tap"
    ) as tap, patch.object(sell.time, "sleep"):
        assert sell.is_sell_page()

    tap.assert_called_once_with((319, 512))


def test_selected_cargo_sell_page_is_detected_without_ocr_labels():
    with patch.object(sell, "screenshot", return_value=FakeSelectedSellPage()):
        assert sell.is_sell_page()


def test_market_volatility_prompt_is_confirmed_until_settlement():
    frames = [
        FakeOcrImage(["当前行情发生波动，是否继续出售？"]),
        FakeOcrImage(["SETTLEMENTREPORT"]),
    ]
    with patch.object(sell, "screenshot", side_effect=frames), patch.object(
        sell, "input_tap"
    ) as tap, patch.object(sell.time, "sleep"):
        assert sell.click_sell_button()

    assert tap.call_args_list[0].args == ((1056, 647),)
    assert tap.call_args_list[1].args == ((960, 512),)


def test_sell_all_retries_until_source_cargo_becomes_selected():
    image = FakeSellAllButton()

    def select(_pos):
        image.selected = True

    with patch.object(sell, "screenshot", return_value=image), patch.object(
        sell, "input_tap", side_effect=select
    ) as tap, patch.object(
        sell, "read_selected_sell_quote", return_value=(1200, 9000)
    ), patch.object(sell.time, "sleep"):
        assert sell.select_all_sellable_cargo()

    tap.assert_called_once_with((1187, 103))


def test_selected_button_with_delayed_quote_is_never_toggled_off():
    image = FakeSellAllButton(selected=True)
    with patch.object(sell, "screenshot", return_value=image), patch.object(
        sell, "input_tap"
    ) as tap, patch.object(
        sell, "read_selected_sell_quote", side_effect=[None, (1200, 9000)]
    ), patch.object(sell.time, "sleep"):
        assert sell.select_all_sellable_cargo()

    tap.assert_not_called()


def test_known_cargo_selection_failure_is_not_treated_as_empty_warehouse():
    with patch.object(sell, "is_all_cargo_selected", return_value=False), patch.object(
        sell, "cargo_contains_expected_goods", return_value=True
    ), patch.object(sell, "read_raise_percent", return_value=20.0), patch.object(
        sell, "select_all_sellable_cargo", return_value=False
    ), patch.object(sell, "go_home") as go_home:
        assert not sell.sell_business(
            num=2,
            empty_ok=True,
            expected_goods=["武林丝绸"],
        )

    go_home.assert_not_called()
