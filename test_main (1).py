import importlib.util
import unittest
from unittest.mock import patch
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

    def test_sportybet_documented_nested_schema_fixture(self):
        payload = {
            "bizCode": 10000,
            "data": {"tournaments": [{"name": "Example League", "events": [{
                "homeTeamName": "Alpha FC", "awayTeamName": "Beta FC",
                "estimateStartTime": 1790000000000,
                "markets": [{"desc": "Total Goals", "specifier": "total=2.5", "outcomes": [
                    {"desc": "Over 2.5", "odds": "2.10", "isActive": True},
                    {"desc": "Under 2.5", "odds": "1.90", "isActive": True},
                ]}]
            }]}]}
        }
        rows, raw_count, detail = app.adapt_sportybet_payload(payload)
        self.assertEqual(raw_count, 1)
        self.assertIn("schema recognized", detail)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["event"], "Alpha FC vs Beta FC")
        self.assertEqual(rows[0]["outcomes"][0]["odds"], 2.1)
        self.assertIsNone(rows[0]["updated_at"])

    def test_betway_flat_array_schema_fixture(self):
        payload = {
            "events": [{"id": 10, "home": "Alpha FC", "away": "Beta FC"}],
            "markets": [{"id": 20, "eventId": 10, "name": "Total Goals 2.5"}],
            "outcomes": [
                {"id": 30, "marketId": 20, "name": "Over 2.5", "odds": 2.1},
                {"id": 31, "marketId": 20, "name": "Under 2.5", "odds": 1.9},
            ],
            "prices": [],
        }
        rows, raw_count, detail = app.adapt_betway_payload(payload)
        self.assertEqual(raw_count, 1)
        self.assertIn("recognized", detail)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["event"], "Alpha FC vs Beta FC")

    def test_betway_nigeria_url_is_not_redirected_to_south_africa(self):
        url = "https://www.betway.com.ng/"
        prepared, _ = app.prepare_feed_request("betway", url)
        self.assertEqual(prepared, url)

    def test_1xbet_compact_fixture_does_not_invent_odds(self):
        payload = {"Value": [{"LI": 123, "L": "Alpha FC - Beta FC", "O1": "Alpha FC", "O2": "Beta FC"}]}
        rows, count, detail = app.adapt_1xbet_payload(payload)
        self.assertEqual(count, 1)
        self.assertEqual(rows, [])
        self.assertIn("no supported explicit", detail)

    def test_sportybet_documented_nested_schema_fixture(self):
        payload = {
            "bizCode": 10000,
            "data": {"tournaments": [{"name": "Example League", "events": [{
                "homeTeamName": "Alpha FC", "awayTeamName": "Beta FC",
                "estimateStartTime": 1790000000000,
                "markets": [{"desc": "Total Goals", "specifier": "total=2.5", "outcomes": [
                    {"desc": "Over 2.5", "odds": "2.10", "isActive": True},
                    {"desc": "Under 2.5", "odds": "1.90", "isActive": True},
                ]}]
            }]}]}
        }
        rows, raw_count, detail = app.adapt_sportybet_payload(payload)
        self.assertEqual(raw_count, 1)
        self.assertIn("schema recognized", detail)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["event"], "Alpha FC vs Beta FC")
        self.assertEqual(rows[0]["outcomes"][0]["odds"], 2.1)
        self.assertIsNone(rows[0]["updated_at"])

    def test_betway_flat_array_schema_fixture(self):
        payload = {
            "events": [{"id": 10, "home": "Alpha FC", "away": "Beta FC"}],
            "markets": [{"id": 20, "eventId": 10, "name": "Total Goals 2.5"}],
            "outcomes": [
                {"id": 30, "marketId": 20, "name": "Over 2.5", "odds": 2.1},
                {"id": 31, "marketId": 20, "name": "Under 2.5", "odds": 1.9},
            ],
            "prices": [],
        }
        rows, raw_count, detail = app.adapt_betway_payload(payload)
        self.assertEqual(raw_count, 1)
        self.assertIn("recognized", detail)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["event"], "Alpha FC vs Beta FC")

    def test_betway_nigeria_url_is_not_redirected_to_south_africa(self):
        url = "https://www.betway.com.ng/"
        prepared, _ = app.prepare_feed_request("betway", url)
        self.assertEqual(prepared, url)

    def test_1xbet_compact_fixture_does_not_invent_odds(self):
        payload = {"Value": [{"LI": 123, "L": "Alpha FC - Beta FC", "O1": "Alpha FC", "O2": "Beta FC"}]}
        rows, count, detail = app.adapt_1xbet_payload(payload)
        self.assertEqual(count, 1)
        self.assertEqual(rows, [])
        self.assertIn("no supported explicit", detail)

    def test_unconfigured_feeds_are_not_contacted(self):
        original = {key: dict(value) for key, value in app.BOOKMAKERS.items()}
        try:
            for value in app.BOOKMAKERS.values():
                value["feed_url"] = ""
            with patch.object(app, "fetch_bet9ja_xml", side_effect=AssertionError("unexpected network adapter call")), \
                 patch.object(app, "safe_fetch_json", side_effect=AssertionError("unexpected network adapter call")):
                rows, health = app.fetch_all_feeds()
            self.assertEqual(rows, [])
            self.assertEqual(len(health), len(app.BOOKMAKERS))
            self.assertTrue(all(item["status"] == "not configured" for item in health))
        finally:
            app.BOOKMAKERS.clear()
            app.BOOKMAKERS.update(original)


if __name__ == "__main__":
    unittest.main(verbosity=2)
