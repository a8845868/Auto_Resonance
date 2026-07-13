from unittest.mock import patch

import core.preset.presets as presets


class OcrFrame:
    def __init__(self, *texts):
        self.texts = texts

    def ocr(self):
        return [{"text": text} for text in self.texts]


def test_station_platform_transition_waits_for_real_driving_state():
    frames = [
        OcrFrame(),
        OcrFrame("目的地：武林源", "剩余行程：1002km", "自动巡航中"),
    ]
    with patch.object(presets, "screenshot", side_effect=frames), patch.object(
        presets, "click_image", return_value=False
    ) as click_image, patch.object(presets.time, "sleep"):
        assert presets._wait_for_departure(timeout=5)

    click_image.assert_called_once()
