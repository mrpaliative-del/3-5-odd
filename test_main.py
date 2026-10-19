import importlib.util
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).with_name("main.py")
spec = importlib.util.spec_from_file_location("solveease_main", MODULE_PATH)
app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app)


def market(bookmaker, over_odds, under_odds):
    return {
        "sport": "Football", "event": "Alpha FC vs Beta FC",
        "event_key": "alpha fc vs beta fc", "market": "Total Goals 2.5",
        "market_key": "total goals 2.5", "bookmaker": bookmaker,
        "bookmaker_key": bookmaker,
        "outcomes": [
            {"name": "Over 2.5", "name_key": "over 2.5", "odds": over_odds, "estimated_probability": None},
            {"name": "Under 2.5", "name_key": "under 2.5", "odds": under_odds, "estimated_probability": None},
        ],
    }


class ScannerTests(unittest.TestCase):
    def test_theoretical_arbitrage_is_found(self):
        rows = [market("sportybet", 2.20, 1.80), market("bet9ja", 2.05, 2.10)]
        self.assertTrue(app.find_arbitrage(rows))

    def test_no_arbitrage_when_implied_sum_is_not_below_one(self):
        rows = [market("sportybet", 1.80, 1.80), market("bet9ja", 1.80, 1.80)]
        self.assertEqual(app.find_arbitrage(rows), [])

    def test_three_outcome_market_is_not_normalized_as_two_way(self):
        row = {"sport": "Football", "event": "A vs B", "market": "1X2", "outcomes": [
            {"name": "Home", "odds": 2.0}, {"name": "Draw", "odds": 3.0}, {"name": "Away", "odds": 4.0}
        ]}
        self.assertIsNone(app.normalize_feed_event("sportybet", row))

    def test_non_finite_number_is_rejected(self):
        self.assertIsNone(app.finite_number(float("nan")))
        self.assertIsNone(app.finite_number(float("inf")))

    def test_all_dashboard_pages_exist(self):
        for page_id in ("homePage", "lookupPanel", "converterPanel", "scannerPanel", "statusPanel"):
            self.assertIn(f'id="{page_id}"', app.HTML)
        self.assertIn("analyzeAccumulator", app.HTML)


if __name__ == "__main__":
    unittest.main(verbosity=2)
