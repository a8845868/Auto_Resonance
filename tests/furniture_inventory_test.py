import unittest

from auto.furniture_inventory import _match_catalog, _visible_card_counts
from core.services.passenger_layout import load_furniture_catalog


def token(text, x, y):
    return {"text": text, "position": ((x - 10, y - 8), (x + 10, y - 8), (x + 10, y + 8), (x - 10, y + 8))}


class FurnitureInventoryTest(unittest.TestCase):
    def test_detail_name_matches_catalog(self):
        match = _match_catalog([token("自动售卖机Lv3", 900, 300)], load_furniture_catalog())
        self.assertEqual(match[0], "vending")

    def test_unrelated_sidebar_label_does_not_match(self):
        match = _match_catalog([token("私人仓库", 1100, 200), token("材料", 1100, 300)], load_furniture_catalog())
        self.assertIsNone(match)

    def test_only_numeric_tokens_in_grid_become_cards(self):
        cards = _visible_card_counts(
            [token("×12", 200, 300), token("材料", 300, 300), token("999", 1100, 300)], 1280, 720
        )
        self.assertEqual(cards, [(200, 300, 12, "×12")])


if __name__ == "__main__":
    unittest.main()
