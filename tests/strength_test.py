from unittest.mock import call, patch

import auto.module.strength as strength


class FakeOcrImage:
    def __init__(self, texts):
        self.texts = texts

    def ocr(self):
        return [{"text": text} for text in self.texts]


class PositionedOcrImage:
    def __init__(self, items=None):
        self.items = items

    def ocr(self):
        return self.items or [{
            "text": "SKIP",
            "position": [[1170, 20], [1240, 20], [1240, 50], [1170, 50]],
        }]


def test_free_drink_card_is_not_treated_as_silver_branch_prompt():
    image = FakeOcrImage(["银枝气泡水", "本次免费!"])

    with patch.object(strength, "screenshot", return_value=image), patch.object(
        strength, "input_tap"
    ) as tap:
        assert not strength._resolve_silver_prompt()

    tap.assert_not_called()


def test_free_drink_path_stops_before_a_partial_drink_would_be_wasted():
    image = FakeOcrImage(["银枝气泡水", "本次免费!"])

    with patch.object(strength, "screenshot", return_value=image), patch.object(
        strength, "_wait_text", return_value=True
    ), patch.object(strength, "input_tap") as tap, patch.object(
        strength.time, "sleep"
    ), patch.object(
        strength, "read_strength", return_value=None
    ), patch.object(
        strength, "_skip_drink_animation", return_value=True
    ):
        result = strength._use_free_rest_area(120, 0)

    assert result.fatigue == 20
    assert result.status == "used"
    assert tap.call_args_list.count(call((960, 422))) == 2
    assert call((320, 531)) not in tap.call_args_list


def test_drink_is_skipped_when_less_than_fifty_fatigue_remains():
    with patch.object(strength, "_screen_has", return_value=False), patch.object(
        strength, "input_tap"
    ) as tap:
        result = strength._use_free_rest_area(49, 0)

    assert result.fatigue == 49
    assert result.status == "not_needed"
    tap.assert_not_called()


def test_500_iron_drink_is_treated_as_free_and_used_until_full():
    image = FakeOcrImage(["银枝气泡水", "500"])

    with patch.object(strength, "screenshot", return_value=image), patch.object(
        strength, "_wait_text", return_value=True
    ), patch.object(strength, "input_tap") as tap, patch.object(
        strength.time, "sleep"
    ), patch.object(
        strength, "read_strength", return_value=None
    ), patch.object(
        strength, "_skip_drink_animation", return_value=True
    ):
        result = strength._use_free_rest_area(100, 0)

    assert result.fatigue == 0
    assert result.status == "used"
    assert tap.call_args_list.count(call((960, 422))) == 2
    assert call((320, 531)) not in tap.call_args_list


def test_repeat_drink_warning_enables_daily_suppression_before_confirming():
    image = FakeOcrImage(["还没有到失效时间", "再喝一杯", "当天不再提醒"])

    with patch.object(strength, "screenshot", return_value=image), patch.object(
        strength, "input_tap"
    ) as tap, patch.object(strength.time, "sleep"):
        assert strength._confirm_repeat_drink()

    assert tap.call_args_list == [call((576, 671)), call((960, 503))]


def test_drink_animation_is_skipped_as_soon_as_skip_is_visible():
    with patch.object(
        strength, "screenshot", return_value=PositionedOcrImage()
    ), patch.object(strength, "input_tap") as tap, patch.object(
        strength.time, "sleep"
    ):
        assert strength._skip_drink_animation(timeout=1)

    tap.assert_called_once_with((1205.0, 35.0))


def test_rest_area_root_reopens_the_drink_selection():
    image = FakeOcrImage(["休息区", "喝一杯"])

    with patch.object(strength, "screenshot", return_value=image), patch.object(
        strength, "_wait_text", return_value=True
    ), patch.object(strength, "input_tap") as tap:
        assert strength._ensure_drink_selection()

    tap.assert_called_once_with((960, 325))


def test_separate_silver_branch_confirmation_is_detected():
    image = FakeOcrImage(["是否使用银枝购买银枝气泡水？"])

    with patch.object(strength, "screenshot", return_value=image):
        assert strength._silver_prompt_visible()


def test_recover_strength_records_drinks_and_inspects_lunchbox_after_full_recovery():
    usage = {}
    with patch.object(
        strength,
        "read_strength",
        side_effect=[(802, 816), (0, 816), (0, 816)],
    ), patch.object(strength, "_open_fatigue_panel", return_value=True), patch.object(
        strength,
        "_use_free_rest_area",
        return_value=strength.RestAreaRecovery(0, "used", 16),
    ) as use_free, patch.object(
        strength, "_return_to_trade", return_value=True
    ), patch.object(
        strength, "_use_all_safe_lunchboxes", return_value=0
    ) as use_lunchboxes:
        assert strength.recover_strength("sell", min_available=80, usage=usage)

    use_free.assert_called_once_with(802, 0, None)
    use_lunchboxes.assert_called_once_with(0, usage)
    assert usage == {"bubble_water_uses": 16}


def test_lunchbox_inventory_reads_only_the_remaining_count_badge():
    image = PositionedOcrImage([
        {
            "text": "7/14",
            "position": [[650, 10], [750, 10], [750, 70], [650, 70]],
        },
        {
            "text": "2",
            "position": [[1140, 440], [1180, 440], [1180, 500], [1140, 500]],
        },
    ])

    assert strength._lunchbox_inventory(image) == 2


def test_safe_lunchbox_batch_records_inventory_consumption():
    cabinet = PositionedOcrImage([{
        "text": "2",
        "position": [[1140, 440], [1180, 440], [1180, 500], [1140, 500]],
    }])
    confirmation = FakeOcrImage(["消除 100 疲劳值"])
    usage = {}

    with patch.object(strength, "_wait_text", return_value=True), patch.object(
        strength, "screenshot", side_effect=[cabinet, confirmation]
    ), patch.object(strength, "input_tap"), patch.object(strength.time, "sleep"):
        remaining_fatigue = strength._use_all_safe_lunchboxes(200, usage)

    assert remaining_fatigue == 100
    assert usage["lunches_remaining"] == 0
    assert usage["lunch_schedule"]


def test_known_station_without_rest_area_is_skipped_without_clicking():
    with patch.object(strength, "input_tap") as tap, patch.object(
        strength, "_screen_has"
    ) as screen_has:
        result = strength._use_free_rest_area(758, 0, "武林源")

    assert result == strength.RestAreaRecovery(758, "unavailable")
    tap.assert_not_called()
    screen_has.assert_not_called()


def test_recovery_defers_lunches_when_non_wasteful_drink_requires_another_city():
    with patch.object(strength, "read_strength", return_value=(758, 816)), patch.object(
        strength, "_open_fatigue_panel"
    ) as open_panel, patch.object(strength, "_use_all_safe_lunchboxes") as lunches:
        assert not strength.recover_strength(
            "buy", min_available=0, station_name="武林源"
        )

    open_panel.assert_not_called()
    lunches.assert_not_called()


def test_recovery_uses_safe_lunches_at_no_rest_station_for_urgent_shortfall():
    usage = {}
    with patch.object(
        strength, "read_strength", side_effect=[(791, 816), (600, 816)]
    ), patch.object(
        strength, "_open_fatigue_panel", return_value=True
    ) as open_panel, patch.object(
        strength,
        "_use_free_rest_area",
        return_value=strength.RestAreaRecovery(791, "unavailable"),
    ), patch.object(
        strength, "_use_all_safe_lunchboxes", return_value=600
    ) as lunches, patch.object(
        strength, "_return_to_trade", return_value=True
    ):
        assert strength.recover_strength(
            "buy", min_available=80, station_name="武林源", usage=usage
        )

    open_panel.assert_called_once()
    lunches.assert_called_once_with(791, usage)
    assert usage == {"lunch_batches": 1, "lunch_fatigue_restored": 191}


def test_negotiation_no_longer_starts_recovery_inside_trading():
    with patch.object(strength, "read_strength", return_value=(800, 816)), patch.object(
        strength, "recover_strength"
    ) as recover:
        assert strength.prepare_negotiation("sell", desired_successes=2) == 0

    recover.assert_not_called()


def test_return_to_trade_stops_when_home_recovery_fails():
    with patch.object(strength, "go_home", return_value=False), patch.object(
        strength, "go_outlets"
    ) as go_outlets:
        assert not strength._return_to_trade("buy")

    go_outlets.assert_not_called()
