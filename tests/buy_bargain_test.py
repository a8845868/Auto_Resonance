from unittest.mock import call, patch

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


def ocr_item(text, x1, y1, x2, y2):
    return {
        "text": text,
        "position": [[x1, y1], [x2, y1], [x2, y2], [x1, y2]],
    }


class FakeOcrImage:
    def __init__(self, items):
        self.items = items

    def ocr(self):
        return self.items


def test_buy_confirmation_requires_stable_cargo_increase_and_dispatches_once():
    before = [ocr_item("100/1121", 1157, 386, 1247, 405)]
    after = [ocr_item("160/1121", 1157, 386, 1247, 405)]
    with patch.object(
        buy,
        "screenshot",
        side_effect=[FakeOcrImage(before), FakeOcrImage(after), FakeOcrImage(after)],
    ), patch.object(buy, "_buy_tap", return_value=object()) as tap, patch.object(
        buy.time, "sleep"
    ):
        assert buy.click_buy_button() is True

    tap.assert_called_once_with((1056, 647))


def test_buy_confirmation_blank_or_unrelated_frames_never_confirm_or_redispatch():
    before = [ocr_item("100/1121", 1157, 386, 1247, 405)]
    unrelated = [ocr_item("访问城市", 1080, 386, 1247, 405)]
    clock = iter([0.0, 0.1, 0.2, 11.0])
    with patch.object(
        buy,
        "screenshot",
        side_effect=[FakeOcrImage(before), FakeOcrImage([]), FakeOcrImage(unrelated)],
    ), patch.object(buy, "_buy_tap", return_value=object()) as tap, patch.object(
        buy.time, "perf_counter", side_effect=lambda: next(clock)
    ), patch.object(buy.time, "sleep"):
        assert buy.click_buy_button() is False

    tap.assert_called_once_with((1056, 647))


def test_affirmative_overlay_confirms_without_waiting_for_cargo_observation():
    before = [ocr_item("100/1121", 1157, 386, 1247, 405)]
    overlay = [
        {"text": "获得物品"},
        {"text": "触碰空白区域退出"},
    ]
    after = [ocr_item("160/1121", 1157, 386, 1247, 405)]
    with patch.object(
        buy,
        "screenshot",
        side_effect=[
            FakeOcrImage(before),
            FakeOcrImage(overlay),
            FakeOcrImage(after),
            FakeOcrImage(after),
        ],
    ), patch.object(buy, "_buy_tap", return_value=object()) as tap, patch.object(
        buy.time, "sleep"
    ):
        assert buy.click_buy_button() is True

    assert tap.call_args_list == [
        call((1056, 647)),
        call((896, 676)),
    ]


def test_affirmative_overlay_confirms_purchase_when_dismissal_is_denied():
    before = [ocr_item("100/1121", 1157, 386, 1247, 405)]
    overlay = [
        {"text": "获得物品"},
        {"text": "触碰空白区域退出"},
    ]
    with patch.object(
        buy,
        "screenshot",
        side_effect=[FakeOcrImage(before), FakeOcrImage(overlay)],
    ), patch.object(
        buy, "_buy_tap", side_effect=[object(), False]
    ) as tap, patch.object(buy.time, "sleep"):
        assert buy.click_buy_button() is True

    assert tap.call_args_list == [
        call((1056, 647)),
        call((896, 676)),
    ]


def test_single_reward_marker_never_authorizes_overlay_dismissal():
    before = [ocr_item("100/1121", 1157, 386, 1247, 405)]
    clock = iter([0.0, 0.1, 11.0])
    with patch.object(
        buy,
        "screenshot",
        side_effect=[FakeOcrImage(before), FakeOcrImage([{"text": "获得物品"}])],
    ), patch.object(buy, "_buy_tap", return_value=object()) as tap, patch.object(
        buy.time, "perf_counter", side_effect=lambda: next(clock)
    ), patch.object(buy.time, "sleep"):
        assert buy.click_buy_button() is False

    tap.assert_called_once_with((1056, 647))


def test_buy_confirmation_without_pre_dispatch_cargo_proof_sends_zero_input():
    with patch.object(buy, "screenshot", return_value=FakeOcrImage([])), patch.object(
        buy, "_buy_tap"
    ) as tap:
        assert buy.click_buy_button() is False

    tap.assert_not_called()


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


def test_buy_business_treats_already_full_cargo_as_success_without_buying():
    with patch.object(buy, "get_boatload", return_value=0), patch.object(
        buy, "_confirm_cargo_full", return_value=True
    ), patch.object(
        buy, "is_empty_goods", return_value=True
    ), patch.object(buy, "buy_good") as buy_good, patch.object(
        buy, "click_bargain_button"
    ) as bargain, patch.object(
        buy, "click_buy_button"
    ) as click_buy, patch.object(
        buy, "go_home"
    ) as go_home:
        assert buy.buy_business(["good-a"], ["good-b"]) is True

    buy_good.assert_not_called()
    bargain.assert_not_called()
    click_buy.assert_not_called()
    go_home.assert_not_called()


def test_cargo_full_confirmation_uses_buy_page_capacity_counter():
    items = [
        ocr_item("386/816+", 1141, 19, 1224, 42),
        ocr_item("1121/1121", 1157, 386, 1247, 405),
    ]

    assert buy._cargo_capacity_full(items)
    assert not buy._cargo_capacity_full(
        [ocr_item("1120/1121", 1157, 386, 1247, 405)]
    )


def test_cargo_full_confirmation_requires_consecutive_positive_frames():
    full = [ocr_item("1121/1121", 1157, 386, 1247, 405)]
    not_full = [ocr_item("1120/1121", 1157, 386, 1247, 405)]
    with patch.object(
        buy,
        "screenshot",
        side_effect=[FakeOcrImage(full), FakeOcrImage(not_full), FakeOcrImage(full)],
    ), patch.object(buy.time, "sleep"):
        assert not buy._confirm_cargo_full(attempts=3, stable_frames=2)

    with patch.object(
        buy,
        "screenshot",
        side_effect=[FakeOcrImage(not_full), FakeOcrImage(full), FakeOcrImage(full)],
    ), patch.object(buy.time, "sleep"):
        assert buy._confirm_cargo_full(attempts=3, stable_frames=2)


def test_zero_boatload_without_capacity_confirmation_fails_closed():
    with patch.object(buy, "get_boatload", return_value=0), patch.object(
        buy, "_confirm_cargo_full", return_value=False
    ), patch.object(
        buy, "is_empty_goods", return_value=True
    ), patch.object(
        buy, "buy_good"
    ) as buy_good, patch.object(
        buy, "go_home"
    ) as go_home:
        assert buy.buy_business(["good-a"], []) is False

    buy_good.assert_not_called()
    go_home.assert_called_once()


def test_buy_business_propagates_buy_button_confirmation_failure():
    with patch.object(buy, "get_boatload", return_value=50), patch.object(
        buy, "buy_good", return_value=(True, 0)
    ), patch.object(
        buy, "is_empty_goods", return_value=False
    ), patch.object(
        buy, "click_bargain_button", return_value=True
    ), patch.object(
        buy, "click_buy_button", return_value=False
    ), patch.object(
        buy, "input_tap"
    ) as tap:
        assert buy.buy_business(["good-a"], []) is False

    tap.assert_not_called()
