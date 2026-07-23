from __future__ import annotations

import json
import os
import subprocess
import sys

from tools.runtime_scenario_replay import generated_scenarios, replay_all, replay_spec


def test_all_eight_generated_runtime_scenarios_replay():
    results = replay_all()
    assert len(results) == 8
    assert all(item["result"] == "PASS" for item in results)


def test_generated_replay_is_deterministic():
    assert replay_all() == replay_all()


def test_announcement_media_and_width_variants_have_one_planned_safe_target():
    results = {item["scenario"]: item for item in replay_all()}
    for name in ("announcement_png_851", "announcement_jpeg_851", "announcement_png_853"):
        assert results[name]["detected_state"] == "ANNOUNCEMENT_VISIBLE"
        assert results[name]["planned_action"] == "DISMISS_ANNOUNCEMENT"
        assert results[name]["safe_target_count"] == 1
        assert results[name]["announcement_candidates"][0]["edge_density"] <= 0.02


def test_no_safe_region_preserves_announcement_state_but_does_not_act():
    result = next(item for item in replay_all() if item["scenario"] == "announcement_no_safe_region")
    assert result["detected_state"] == "ANNOUNCEMENT_VISIBLE"
    assert result["planned_action"] == "OBSERVE_ONLY"
    assert result["safe_target_count"] == 0


def test_home_city_daily_and_unknown_action_boundaries():
    results = {item["scenario"]: item for item in replay_all()}
    assert results["daily_checkin_claimed"]["planned_action"] == "DISMISS_DAILY_CHECKIN"
    assert results["home_ready"]["planned_action"] == "ENTER_CITY"
    assert results["city_detail"]["planned_action"] == "STOP"
    assert results["transition_unknown"]["planned_action"] == "OBSERVE_ONLY"


def test_cli_all_is_offline_and_emits_eight_results():
    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    completed = subprocess.run(
        [sys.executable, "tools/runtime_scenario_replay.py", "--all"],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )
    results = [
        json.loads(line)
        for line in completed.stdout.splitlines()
        if line.startswith("{")
    ]
    assert len(results) == 8
    assert all(item["result"] == "PASS" for item in results)


def test_scenarios_are_programmatic_and_do_not_reference_media_files():
    for spec in generated_scenarios():
        assert not hasattr(spec, "source_path")
        assert replay_spec(spec)["result"] == "PASS"
