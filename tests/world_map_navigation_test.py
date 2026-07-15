from unittest.mock import MagicMock, call, patch

import core.preset.presets as presets


def box(text, x1, y1, x2, y2):
    return {
        "text": text,
        "score": 0.99,
        "position": [[x1, y1], [x2, y1], [x2, y2], [x1, y2]],
    }


def test_world_map_pan_reanchors_from_visible_intermediate_station():
    station_data = {
        "start": (2950.0, 675.0),
        "middle": (-750.0, 1625.0),
        "target": (-7075.0, 400.0),
    }
    differences = presets.calculate_station_differences(station_data)

    with patch.object(presets, "STATION_DIFFERENCES", differences):
        first_move = presets._world_map_pan_vector("start", "target")
        anchor = presets._station_label_center(
            [box("middle", 1123.0, 402.0, 1187.0, 416.0)], station_data
        )
        reanchored_move = presets._world_map_pan_vector(anchor[0], "target")

    assert first_move == (4010.0, -110.0)
    assert anchor == ("middle", 1155.0, 409.0)
    assert reanchored_move == (2530.0, -490.0)
    assert abs(reanchored_move[0]) < abs(first_move[0])


def test_world_map_step_keeps_long_swipes_inside_screen():
    step_x, step_y = presets._world_map_step(4010.0, -110.0)

    assert step_x == presets.WORLD_MAP_MAX_PAN_STEP_X
    assert round(step_y, 1) == -19.2


def test_click_station_reanchors_during_long_pan_before_selecting_target():
    station_data = {
        "start": (2950.0, 675.0),
        "middle": (-750.0, 1625.0),
        "target": (-7075.0, 400.0),
    }
    initial = MagicMock()
    initial.match_template.return_value = True
    initial.ocr.return_value = []
    start_map = MagicMock()
    start_map.ocr.return_value = [box("start", 619.0, 388.0, 665.0, 406.0)]
    before_first_pan = MagicMock()
    before_first_pan.match_template.return_value = None
    before_first_pan.ocr.return_value = start_map.ocr.return_value
    middle_map = MagicMock()
    middle_map.match_template.return_value = None
    middle_map.ocr.return_value = [box("middle", 1123.0, 402.0, 1187.0, 416.0)]
    target_map = MagicMock()
    target_map.match_template.return_value = None
    target_map.ocr.return_value = [box("target", 610.0, 390.0, 670.0, 410.0)]

    differences = presets.calculate_station_differences(station_data)
    with patch.object(presets, "STATION_POS_DATA", station_data), patch.object(
        presets, "STATION_DIFFERENCES", differences
    ), patch.object(
        presets, "STATION_NAME2PNG", {"target": "target.png"}
    ), patch.object(
        presets,
        "screenshot",
        side_effect=[initial, start_map, before_first_pan, middle_map, target_map],
    ), patch.object(
        presets, "_open_world_map_at_default_zoom"
    ), patch.object(
        presets, "go_home"
    ), patch.object(
        presets, "input_swipe"
    ) as swipe, patch.object(
        presets, "wait_stopped"
    ), patch.object(
        presets, "input_tap"
    ) as tap, patch.object(
        presets, "click_image", return_value=True
    ), patch.object(
        presets, "_wait_for_departure", return_value=True
    ), patch.object(
        presets.time, "sleep"
    ):
        result = presets.click_station("target", cur_station="start")

    assert bool(result) is True
    assert swipe.call_args_list[:2] == [
        call((290.0, 369.6009975062344), (990.0, 350.3990024937656), swipe_time=450),
        call((290.0, 427.7865612648221), (990.0, 292.2134387351779), swipe_time=450),
    ]
    assert len(swipe.call_args_list) == 3
    tap.assert_called_once_with((640.0, 400.0))


def test_station_label_rejects_partial_low_confidence_and_hud_candidates():
    items = [
        {**box("目的地：target", 600, 300, 700, 330), "score": 0.99},
        {**box("target", 600, 300, 680, 330), "score": 0.5},
        box("target", 600, 20, 680, 50),
        box("middle", 610, 350, 670, 390),
    ]

    assert presets._station_label_center(items, ("target", "middle")) == (
        "middle",
        640.0,
        370.0,
    )


def test_click_station_stops_without_departure_after_pan_attempts_are_exhausted():
    initial = MagicMock()
    initial.match_template.return_value = True
    start_map = MagicMock()
    start_map.ocr.return_value = [box("start", 610, 350, 670, 390)]
    probes = []
    for _ in range(presets.WORLD_MAP_MAX_PAN_ATTEMPTS):
        probe = MagicMock()
        probe.match_template.return_value = None
        probe.ocr.return_value = []
        probes.append(probe)
    final = MagicMock()
    final.match_template.return_value = None
    final.ocr.return_value = []

    with patch.object(
        presets, "STATION_POS_DATA", {"start": (0, 0), "target": (5000, 0)}
    ), patch.object(
        presets,
        "STATION_DIFFERENCES",
        presets.calculate_station_differences({"start": (0, 0), "target": (5000, 0)}),
    ), patch.object(
        presets, "STATION_NAME2PNG", {"target": "target.png"}
    ), patch.object(
        presets, "screenshot", side_effect=[initial, start_map, *probes, final]
    ), patch.object(
        presets, "_open_world_map_at_default_zoom"
    ), patch.object(
        presets, "go_home"
    ), patch.object(
        presets, "input_swipe"
    ), patch.object(
        presets, "wait_stopped"
    ), patch.object(
        presets, "click_image"
    ) as click_image, patch.object(
        presets, "_wait_for_departure"
    ) as departure, patch.object(
        presets.time, "sleep"
    ):
        result = presets.click_station("target", cur_station="start")

    assert bool(result) is False
    click_image.assert_not_called()
    departure.assert_not_called()


def test_click_station_stops_long_pan_when_target_template_is_visible():
    initial = MagicMock()
    initial.match_template.return_value = True
    start_map = MagicMock()
    start_map.ocr.return_value = [box("start", 610, 350, 670, 390)]
    target_match = MagicMock()
    target_match.loc = (777, 333)
    target_map = MagicMock()
    target_map.match_template.return_value = target_match
    target_map.ocr.return_value = []

    station_data = {"start": (0, 0), "target": (5000, 0)}
    with patch.object(presets, "STATION_POS_DATA", station_data), patch.object(
        presets,
        "STATION_DIFFERENCES",
        presets.calculate_station_differences(station_data),
    ), patch.object(
        presets, "STATION_NAME2PNG", {"target": "target.png"}
    ), patch.object(
        presets, "screenshot", side_effect=[initial, start_map, target_map]
    ), patch.object(
        presets, "_open_world_map_at_default_zoom"
    ), patch.object(
        presets, "go_home"
    ), patch.object(
        presets, "input_swipe"
    ) as swipe, patch.object(
        presets, "input_tap"
    ) as tap, patch.object(
        presets, "wait_stopped"
    ), patch.object(
        presets, "click_image", return_value=True
    ), patch.object(
        presets, "_wait_for_departure", return_value=True
    ), patch.object(
        presets.time, "sleep"
    ):
        result = presets.click_station("target", cur_station="start")

    assert bool(result) is True
    assert len(swipe.call_args_list) == 1
    tap.assert_called_once_with((777, 333))


def test_world_map_step_keeps_vertical_swipe_inside_map_area():
    step_x, step_y = presets._world_map_step(100.0, 2000.0)

    assert step_y == presets.WORLD_MAP_MAX_PAN_STEP_Y
    assert step_x == 25.0
    assert 100 <= presets.WORLD_MAP_GESTURE_CENTER[1] - step_y / 2
    assert presets.WORLD_MAP_GESTURE_CENTER[1] + step_y / 2 <= 620
