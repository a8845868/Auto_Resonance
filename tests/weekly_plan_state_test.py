import json
from datetime import date, timedelta
from unittest.mock import patch

import core.services.weekly_plan_state as weekly_state


def test_expired_weekly_plan_rolls_forward_and_resets_only_progress(tmp_path):
    path = tmp_path / "weekly_plan.json"
    this_week = date.today() - timedelta(days=date.today().weekday())
    previous_week = this_week - timedelta(days=7)
    path.write_text(
        json.dumps(
            {
                "week_start": previous_week.isoformat(),
                "cycle": ["岚心城", "武林源"],
                "total_runs": 8,
                "runs": [{"岚心城": 0, "武林源": 0}] * 8,
                "completed_runs": 5,
                "completed_books": 9,
                "price_time": "2026-07-12 20:20:01",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    with patch.object(weekly_state, "STATE_PATH", path):
        rolled = weekly_state.roll_weekly_plan_forward()

    assert rolled["week_start"] == this_week.isoformat()
    assert rolled["cycle"] == ["岚心城", "武林源"]
    assert rolled["completed_runs"] == 0
    assert rolled["completed_books"] == 0
    assert rolled["price_time"] == "2026-07-12 20:20:01"
    assert rolled["needs_reoptimization"] is True
