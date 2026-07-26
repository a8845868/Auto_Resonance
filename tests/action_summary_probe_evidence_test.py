from types import SimpleNamespace

from tools.action_summary_entry_probe import (
    _evidence_for_entry,
    _top_level_stage_points,
)


def test_stage_points_never_alias_action_summary_as_action_terminal():
    summary = SimpleNamespace(
        entry_name="open_action_summary", actual_dispatched_point=(1051, 368)
    )

    points = _top_level_stage_points([summary])

    assert points == {
        "actual_dispatched_point": [1051, 368],
        "action_terminal_device_point": None,
        "action_summary_entry_device_point": [1051, 368],
    }


def test_each_stage_point_is_derived_from_its_own_evidence():
    terminal = SimpleNamespace(
        entry_name="open_action_entry", actual_dispatched_point=(1116, 415)
    )
    summary = SimpleNamespace(
        entry_name="open_action_summary", actual_dispatched_point=(1051, 368)
    )

    points = _top_level_stage_points([terminal, summary])

    assert points["actual_dispatched_point"] == [1051, 368]
    assert points["action_terminal_device_point"] == [1116, 415]
    assert points["action_summary_entry_device_point"] == [1051, 368]


def test_action_terminal_fields_do_not_reuse_first_non_terminal_evidence():
    summary = SimpleNamespace(entry_name="open_action_summary")

    assert _evidence_for_entry([summary], "open_action_entry") is None
