import ast
from pathlib import Path
from unittest.mock import call, patch

import auto.passenger_carriage_build as passenger_build


def _box(x, y, text, width=80, height=25):
    return {
        "text": text,
        "position": ((x, y), (x + width, y), (x + width, y + height), (x, y + height)),
    }


def _load_parser():
    source = Path("auto/passenger_carriage_build.py").read_text(encoding="utf-8")
    module = ast.parse(source)
    function = next(node for node in module.body if isinstance(node, ast.FunctionDef) and node.name == "parse_build_remaining")
    isolated = ast.Module(body=[ast.Import(names=[ast.alias(name="re")]), function], type_ignores=[])
    isolated = ast.fix_missing_locations(isolated)
    namespace = {}
    exec(compile(isolated, "parse_build_remaining", "exec"), namespace)
    return namespace["parse_build_remaining"]


def test_parses_in_game_build_countdown():
    parse = _load_parser()
    assert parse(["施工剩余时长：", "05:55:15", "立刻完成"]) == 21_315
    assert parse(["施工剩余时长 5：05：09"]) == 18_309
    assert parse(["工坊空置中，暂无建造任务"]) is None


def test_build_dialog_requires_passenger_stats_and_start_button():
    assert passenger_build._is_passenger_build_dialog(
        ["客厢", "载客量", "64", "开始施工"]
    )
    assert not passenger_build._is_passenger_build_dialog(
        ["货厢", "载货量", "50", "开始施工"]
    )
    assert not passenger_build._is_passenger_build_dialog(
        ["客厢", "载客量", "64"]
    )


def test_screen_predicates_keep_navigation_stages_separate():
    assert passenger_build._is_train_management_screen(["维护", "改装", "编组"])
    assert not passenger_build._is_train_management_screen(["整备列车"])
    assert passenger_build._is_workshop_screen(
        ["维护", "编组", "工坊空置中，暂无建造任务", "建造车厢"]
    )
    assert passenger_build._is_workshop_screen(
        ["维护", "编组", "施工已完成，等待列车长提取", "建造完成"]
    )
    assert passenger_build._is_completed_build_screen(
        ["编组", "施工已完成，等待列车长提取", "建造完成"]
    )
    assert passenger_build._is_carriage_build_dialog(
        ["建造所需时长：06:00:00", "消耗材料", "开始施工"]
    )
    assert not passenger_build._is_carriage_build_dialog(
        ["工坊空置中，暂无建造任务", "建造车厢"]
    )


def test_passenger_inventory_counts_initial_plus_garage_and_derives_seat_groups():
    workshop = [
        _box(600, 20, "编组"),
        _box(400, 120, "施工剩余时长：03:55:38"),
        _box(900, 480, "车库容量"),
        _box(50, 210, "标准货厢"),
        _box(350, 210, "基础客厢"),
        _box(20, 674, "标准客厢"),
        _box(185, 674, "标准客厢"),
        _box(350, 674, "标准客厢"),
        _box(520, 674, "重型货厢"),
    ]
    overview = [
        _box(1000, 324, "载客总量"),
        _box(1210, 326, "0/64"),
        _box(1000, 360, "载货总量"),
        _box(1170, 360, "1197/1121"),
    ]

    result = passenger_build.parse_passenger_build_inventory(workshop, overview)

    assert result.installed_passenger_carriages == 1
    assert result.garage_standard_carriages == 3
    assert result.total_passenger_carriages == 4
    assert result.built_extra_passenger_carriages == 3
    assert result.installed_seat_groups == 64


def test_passenger_inventory_rejects_unconfirmed_or_nonstandard_capacity():
    workshop = [
        _box(600, 20, "编组"),
        _box(400, 120, "施工剩余时长：03:55:38"),
        _box(900, 480, "车库容量"),
        _box(20, 674, "标准客厢"),
    ]
    assert passenger_build.parse_passenger_build_inventory(
        workshop, [_box(1000, 324, "载客总量"), _box(1210, 326, "0/96")]
    ) is None


def test_build_flow_uses_distinct_entry_and_confirmation_actions():
    idle = passenger_build.BuildScreenState(False)
    building = passenger_build.BuildScreenState(True, 21_600)

    with patch.object(passenger_build, "_wait_for_game", return_value=True), patch.object(
        passenger_build, "go_home"
    ), patch.object(
        passenger_build, "_click_until_ready", return_value=True
    ) as click_until_ready, patch.object(
        passenger_build, "inspect_build_screen", return_value=idle
    ), patch.object(
        passenger_build, "read_passenger_build_inventory_on_workshop", return_value=None
    ), patch.object(
        passenger_build,
        "_read_screen_texts",
        return_value=["编组", "工坊空置中，暂无建造任务", "建造车厢"],
    ), patch.object(
        passenger_build, "_wait_for_passenger_build_dialog", return_value=True
    ), patch.object(
        passenger_build, "_wait_for_build_started", return_value=building
    ), patch.object(
        passenger_build, "blurry_ocr_click", side_effect=[False, True]
    ) as click_text:
        with patch.object(passenger_build, "input_tap") as tap:
            result = passenger_build.start_next_passenger_carriage()

    assert result == building
    assert click_until_ready.call_args_list == [
        call("整备列车", passenger_build._is_train_management_screen, score=0.6),
        call("编组", passenger_build._is_workshop_screen, score=0.6),
        call(
            "建造车厢",
            passenger_build._is_carriage_build_dialog,
            score=0.6,
        ),
    ]
    tap.assert_called_once_with((338, 487))
    assert click_text.call_args_list == [
        call(
            "客厢",
            score=0.55,
            trynum=3,
            log=False,
        ),
        call(
            "开始施工",
            score=0.6,
            trynum=6,
            log=False,
        ),
    ]


def test_claim_completed_carriage_dismisses_result_and_waits_for_idle():
    completed = ["编组", "施工已完成，等待列车长提取", "建造完成"]
    result = ["建造成功", "标准客厢"]
    idle = ["编组", "工坊空置中，暂无建造任务", "建造车厢"]

    with patch.object(
        passenger_build,
        "_read_screen_texts",
        side_effect=[completed, result, idle],
    ), patch.object(
        passenger_build, "blurry_ocr_click", return_value=True
    ) as click_text, patch.object(
        passenger_build, "input_tap"
    ) as tap, patch.object(
        passenger_build.time, "sleep"
    ):
        assert passenger_build._claim_completed_carriage()

    click_text.assert_called_once_with(
        "建造完成", score=0.55, trynum=3, log=False
    )
    tap.assert_called_once_with((1100, 650))
