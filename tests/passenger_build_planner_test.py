import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from core.services.passenger_build_planner import (
    build_monitor_summary,
    calculate_passenger_build_plan,
    create_build_monitor_plan,
    load_build_monitor_plan,
    record_carriage_completed,
    record_carriage_started,
    resync_active_build,
)


class PassengerBuildPlannerTest(unittest.TestCase):
    def test_monitor_records_real_start_and_six_hour_due_time(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "plan.json"
            start = datetime(2026, 7, 12, 8, 15, tzinfo=timezone.utc)
            state = create_build_monitor_plan(
                target_carriages=8, current_carriages=1, path=path
            )
            state = record_carriage_started(state, started_at=start, path=path)
            self.assertEqual(
                datetime.fromisoformat(state["active_due_at"]), start + timedelta(hours=6)
            )
            self.assertEqual(load_build_monitor_plan(path)["history"][0]["carriage_number"], 2)
            self.assertFalse(build_monitor_summary(state, start + timedelta(hours=5))["due_now"])
            self.assertTrue(build_monitor_summary(state, start + timedelta(hours=6))["due_now"])

    def test_monitor_advances_without_losing_history(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "plan.json"
            start = datetime(2026, 7, 12, 8, 0, tzinfo=timezone.utc)
            state = create_build_monitor_plan(
                target_carriages=3, current_carriages=1, path=path
            )
            state = record_carriage_started(state, started_at=start, path=path)
            state = record_carriage_completed(
                state, completed_at=start + timedelta(hours=6), path=path
            )
            self.assertEqual(state["completed_carriages"], 2)
            self.assertEqual(state["status"], "pending")
            self.assertIsNotNone(state["history"][0]["completed_at"])

    def test_game_countdown_resyncs_local_due_time(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "plan.json"
            start = datetime(2026, 7, 12, 8, 0, tzinfo=timezone.utc)
            state = create_build_monitor_plan(
                target_carriages=8, current_carriages=1, path=path
            )
            state = record_carriage_started(state, started_at=start, path=path)
            state = resync_active_build(
                state,
                remaining_seconds=5 * 3600 + 55 * 60 + 15,
                observed_at=start + timedelta(minutes=4),
                path=path,
            )
            self.assertEqual(
                datetime.fromisoformat(state["active_due_at"]),
                start + timedelta(hours=5, minutes=59, seconds=15),
            )
    def test_full_passenger_consist(self):
        result = calculate_passenger_build_plan(
            target_passenger_carriages=8,
            built_extra_passenger_carriages=0,
            installed_seat_groups=0,
            current_iron=0,
        )
        self.assertEqual(result["passenger_carriages"], 8)
        self.assertEqual(result["freight_carriages"], 1)
        self.assertEqual(result["function_carriages"], 2)
        self.assertEqual(result["seats"], 512)
        self.assertEqual(result["required_seat_groups"], 128)
        self.assertEqual(result["iron_shortfall"], 185_272_000)

    def test_five_passenger_consist_is_five_four_two(self):
        result = calculate_passenger_build_plan(
            target_passenger_carriages=5,
            built_extra_passenger_carriages=4,
            installed_seat_groups=80,
            current_iron=0,
            comfort=42_000,
            food=7_000,
            entertainment=7_000,
            pets=7_000,
            aquarium=7_000,
            plants=7_000,
            medical=7_000,
        )
        self.assertEqual((result["passenger_carriages"], result["freight_carriages"], result["function_carriages"]), (5, 4, 2))
        self.assertTrue(result["build_ready"])
        self.assertTrue(result["rating_ready"])


if __name__ == "__main__":
    unittest.main()
