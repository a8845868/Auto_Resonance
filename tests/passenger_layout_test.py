import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from core.services.passenger_layout import (
    active_layout,
    auto_place_owned,
    calculate_layout_summary,
    default_layout_state,
    furniture_inventory_from_assets,
    load_layout_state,
    load_furniture_catalog,
    toggle_slot,
)
from core.services.inventory_assets import Asset


class PassengerLayoutTest(unittest.TestCase):
    def test_warehouse_and_placed_counts_are_conserved(self):
        state = default_layout_state()
        state["warehouse"] = {"seat_group": 128}
        auto_place_owned(state)
        summary = calculate_layout_summary(state)
        seat = next(row for row in summary["rows"] if row["key"] == "seat_group")
        self.assertEqual((seat["warehouse"], seat["placed"], seat["owned"]), (0, 128, 128))
        toggle_slot(state, "seat")
        summary = calculate_layout_summary(state)
        seat = next(row for row in summary["rows"] if row["key"] == "seat_group")
        self.assertEqual((seat["warehouse"], seat["placed"], seat["owned"]), (128, 0, 128))

    def test_missing_and_score_update_from_placement(self):
        state = default_layout_state()
        state["warehouse"] = {"green_plum": 344, "silver_queen": 1}
        auto_place_owned(state)
        summary = calculate_layout_summary(state)
        self.assertGreater(summary["scores"]["plants"]["value"], 6990)
        self.assertEqual(summary["scores"]["plants"]["ready"], True)

    def test_known_ocr_assets_map_to_catalog_only(self):
        catalog = load_furniture_catalog()
        found = furniture_inventory_from_assets(
            [Asset("自动售卖机Lv3", 8, "其他"), Asset("完全无关物品", 99, "其他")], catalog
        )
        self.assertEqual(found, {"vending": 8})

    def test_default_layout_is_optimal_profile(self):
        layout = active_layout(default_layout_state())
        self.assertFalse(layout["editable"])
        self.assertEqual(layout["id"], "optimal-2026-05-25")

    def test_fixed_furniture_is_initialized_as_installed(self):
        with TemporaryDirectory() as directory:
            state = load_layout_state(Path(directory) / "missing.json")
        self.assertEqual(state["placements"]["bento"], 1)
        self.assertEqual(state["placements"]["kitchen"], 1)

    def test_fish_score_requires_matching_tank(self):
        state = default_layout_state()
        state["placements"] = {"bigfish": 1}
        summary = calculate_layout_summary(state)
        self.assertEqual(summary["scores"]["aquarium"]["value"], 0)


if __name__ == "__main__":
    unittest.main()
