from __future__ import annotations

import json
from pathlib import Path

from core.services.personal_action_budget import EpisodeActionBudget
from core.services.personal_automation_entry import PersonalAutomationEntryConfig
from core.services.personal_city_target import PersonalCityTarget
from core.services.personal_runtime_episode import (
    ActionExecutor,
    ActionPlanner,
    CoordinateTransform,
    EpisodePolicy,
    PersonalAutomationEpisode,
    RuntimeAction,
    RuntimeState,
    StateDetector,
)
from tests.personal_runtime_fixtures import Frame, city_frame, home_frame, item


def _run_generic(frame):
    clicks = []
    episode = PersonalAutomationEpisode(
        frame_provider=lambda: frame,
        detector=StateDetector(),
        planner=ActionPlanner(),
        executor=ActionExecutor(lambda point: clicks.append(point) or True),
        transform_provider=lambda detected: CoordinateTransform(
            detected.frame_dimensions,
            (853, 480),
            detected.frame_dimensions,
            (0, 0),
        ),
        budget=EpisodeActionBudget(),
        policy=EpisodePolicy(maximum_observations=1),
    )
    return episode.run(), clicks


def test_default_entry_config_has_no_target_city():
    assert PersonalAutomationEntryConfig().target_city_id is None


def test_default_home_ready_is_terminal_task_ready_without_city_input():
    result, clicks = _run_generic(home_frame())
    assert result.status == "PASS"
    assert result.final_state is RuntimeState.HOME_READY
    assert result.reason == "task_ready_home_reached"
    assert clicks == []


def test_default_city_detail_is_terminal_without_forcing_any_city():
    result, clicks = _run_generic(city_frame())
    assert result.status == "PASS"
    assert result.final_state is RuntimeState.CITY_DETAIL
    assert clicks == []


def test_default_planner_never_enters_city_but_explicit_target_still_can():
    budget = EpisodeActionBudget()
    generic = StateDetector().detect(home_frame())
    assert ActionPlanner().plan(generic, budget=budget).action is RuntimeAction.STOP

    explicit = StateDetector(
        city_target=PersonalCityTarget(city_id="岚心城")
    ).detect(home_frame())
    assert (
        ActionPlanner(target_city_id="岚心城").plan(explicit, budget=budget).action
        is RuntimeAction.ENTER_CITY
    )


def test_generic_city_detail_reports_other_known_city_without_navigation():
    frame = city_frame()
    frame._texts[0] = item("海角城", (176, 535, 279, 572))
    detected = StateDetector().detect(frame)
    assert detected.state is RuntimeState.CITY_DETAIL
    assert detected.current_city_id == "海角城"
    assert ActionPlanner().plan(
        detected, budget=EpisodeActionBudget()
    ).action is RuntimeAction.STOP


def test_generic_city_detail_allows_unknown_city_but_does_not_guess_it():
    frame = city_frame()
    frame._texts[0] = item("新城市", (176, 535, 279, 572))
    detected = StateDetector().detect(frame)
    assert detected.state is RuntimeState.CITY_DETAIL
    assert detected.current_city_id is None


def test_legacy_config_migration_preserves_only_enabled_flag(tmp_path):
    from app.common.config import migrate_personal_startup_config

    path = tmp_path / "app.json"
    path.write_text(
        json.dumps(
            {
                "PersonalAutomation": {
                    "PrepareGameAndEnterLanxinBeforeTasks": True,
                    "AutoConfirmResourceUpdate": True,
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    assert migrate_personal_startup_config(path) is True
    migrated = json.loads(path.read_text(encoding="utf-8"))
    section = migrated["PersonalAutomation"]
    assert section["PrepareGameBeforeTasks"] is True
    assert "PrepareGameAndEnterLanxinBeforeTasks" not in section
    assert not any("city" in key.lower() for key in section)


def test_product_surfaces_do_not_describe_fixed_city_startup():
    root = Path(__file__).resolve().parents[1]
    for relative in (
        "app/view/dashboard_interface.py",
        "app/view/setting_interface.py",
        "core/services/personal_automation_entry.py",
    ):
        text = (root / relative).read_text(encoding="utf-8")
        assert "自动准备游戏并进入岚心城" not in text
