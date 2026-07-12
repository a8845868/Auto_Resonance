import ast
from pathlib import Path


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
