import unittest

from core.services.passenger_planner import PassengerPlanConfig, estimate_passenger_plan, route_reference


class PassengerPlannerTest(unittest.TestCase):
    def test_fixed_64_seats_scale_from_current_full_train_reference(self):
        result = estimate_passenger_plan()
        self.assertEqual(result["trip_revenue"], 736_815)
        self.assertEqual(result["weekly_revenue"], 5_157_705)
        self.assertEqual(result["weekly_fatigue"], 665)

    def test_occupancy_and_invalid_values_are_bounded(self):
        result = estimate_passenger_plan(
            PassengerPlanConfig(seats=64, trips_per_week=2, occupancy_percent=50)
        )
        self.assertEqual(result["passengers_per_trip"], 32)
        self.assertEqual(result["weekly_revenue"], 736_814)

    def test_directional_route_reference_is_not_locked(self):
        outbound = route_reference("武林源", "岚心城")
        inbound = route_reference("岚心城", "武林源")
        self.assertEqual(outbound["revenue"], 6_003_222)
        self.assertEqual(inbound["revenue"], 5_785_811)
        self.assertIsNone(route_reference("修格里城", "塔图站"))


if __name__ == "__main__":
    unittest.main()
