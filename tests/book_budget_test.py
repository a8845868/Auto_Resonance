import unittest

from core.services.book_budget import BOOK_SOURCES, calculate_book_budget, weekly_equivalent


class BookBudgetTest(unittest.TestCase):
    def test_periods_are_normalized_to_week(self):
        self.assertEqual(weekly_equivalent("daily", 2), 14)
        self.assertAlmostEqual(weekly_equivalent("monthly", 52), 12)
        self.assertEqual(weekly_equivalent("weekly", 5), 5)

    def test_disabled_sources_do_not_enter_budget(self):
        result = calculate_book_budget(
            [
                {"enabled": True, "period": "weekly", "amount": 5},
                {"enabled": True, "period": "daily", "amount": 1},
                {"enabled": False, "period": "monthly", "amount": 100},
            ],
            current_inventory=3,
        )
        self.assertEqual(result["weekly_income"], 12)
        self.assertEqual(result["available_this_week"], 15)

    def test_fixed_free_sources_match_documented_caps(self):
        fixed = {source.key: source.default_amount for source in BOOK_SOURCES if not source.editable}
        self.assertEqual(fixed["weekly_black_moon"], 6)
        self.assertEqual(fixed["weekly_mileage"], 5)
        self.assertEqual(fixed["weekly_iron"], 5)
        self.assertEqual(fixed["monthly_mileage"], 10)
        self.assertEqual(fixed["monthly_iron"], 10)


if __name__ == "__main__":
    unittest.main()
