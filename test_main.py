"""Unit tests for SolveEase. Uses synthetic fixtures; no live bookmaker requests."""
import importlib.util
import json
import math
import unittest
from pathlib import Path
from unittest.mock import patch

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

    def test_all_dashboard_pages_exist_and_betslip_ui_is_present(self):
        for page_id in ("homePage", "lookupPanel", "converterPanel", "scannerPanel", "statusPanel"):
            self.assertIn(f'id="{page_id}"', app.HTML)
        self.assertIn("analyzeAccumulator", app.HTML)
        self.assertIn("analyzeBetslip", app.HTML)
        self.assertIn("/api/analyze-betslip", app.HTML)

    def test_sportybet_documented_nested_schema_fixture(self):
        payload = {"bizCode": 10000, "data": {"tournaments": [{"name": "Example League", "events": [{
            "homeTeamName": "Alpha FC", "awayTeamName": "Beta FC", "estimateStartTime": 1790000000000,
            "markets": [{"desc": "Total Goals", "specifier": "total=2.5", "outcomes": [
                {"desc": "Over 2.5", "odds": "2.10", "isActive": True},
                {"desc": "Under 2.5", "odds": "1.90", "isActive": True},
            ]}]
        }]}]}}
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
            "outcomes": [{"id": 30, "marketId": 20, "name": "Over 2.5", "odds": 2.1},
                         {"id": 31, "marketId": 20, "name": "Under 2.5", "odds": 1.9}],
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

    def test_poisson_distribution_is_normalized(self):
        distribution = app.poisson_distribution(1.6)
        self.assertAlmostEqual(sum(distribution), 1.0, places=12)
        self.assertTrue(all(value >= 0 for value in distribution))

    def test_betslip_analysis_ranks_supported_market_and_returns_probability(self):
        result = app.analyze_betslip({"legs": [{
            "event": "Alpha FC vs Beta FC", "market": "Total Goals 2.5", "selection": "Over 2.5",
            "odds": 2.1, "home_goal_rate": 1.6, "away_goal_rate": 1.1,
        }]})
        leg = result["ranked_legs"][0]
        self.assertTrue(leg["model_supported"])
        self.assertGreater(leg["estimated_probability"], 0)
        self.assertLess(leg["estimated_probability"], 1)
        self.assertAlmostEqual(leg["odds_implied_probability"], 1 / 2.1)
        self.assertIn("edge", leg["status"])
        self.assertIn("supplied by user", result["model"])

    def test_betslip_analysis_supports_1x2_and_btts(self):
        result = app.analyze_betslip({"legs": [
            {"event": "A vs B", "market": "1X2", "selection": "Home", "odds": 2.2, "home_goal_rate": 1.7, "away_goal_rate": 0.9},
            {"event": "A vs B", "market": "BTTS", "selection": "Yes", "odds": 1.8, "home_goal_rate": 1.7, "away_goal_rate": 0.9},
        ]})
        self.assertEqual(result["supported_legs"], 2)
        self.assertTrue(all(0 < leg["estimated_probability"] < 1 for leg in result["ranked_legs"]))

    def test_betslip_rejects_invalid_odds_and_goal_rates(self):
        with self.assertRaises(ValueError):
            app.analyze_betslip({"legs": [{"event": "A vs B", "market": "1X2", "selection": "Home", "odds": 1.0, "home_goal_rate": 1.2, "away_goal_rate": 1.0}]})
        with self.assertRaises(ValueError):
            app.analyze_betslip({"legs": [{"event": "A vs B", "market": "1X2", "selection": "Home", "odds": 2.0, "home_goal_rate": 9, "away_goal_rate": 1.0}]})

    def test_unsupported_market_is_not_given_a_fabricated_probability(self):
        result = app.analyze_betslip({"legs": [{
            "event": "A vs B", "market": "First throw-in", "selection": "Home", "odds": 2.0,
            "home_goal_rate": 1.2, "away_goal_rate": 1.0,
        }]})
        self.assertFalse(result["ranked_legs"][0]["model_supported"])
        self.assertIsNone(result["ranked_legs"][0]["estimated_probability"])

    def test_empty_or_oversized_betslip_rejected(self):
        with self.assertRaises(ValueError):
            app.analyze_betslip({"legs": []})
        with self.assertRaises(ValueError):
            app.analyze_betslip({"legs": [{}] * 51})

    def test_double_chance_home_draw_alias_includes_draw_probability(self):
        home_draw, _ = app._selection_probability("Double Chance", "Home/Draw", 1.7, 0.9)
        home_win, _ = app._selection_probability("1X2", "Home", 1.7, 0.9)
        self.assertGreater(home_draw, home_win)

    def test_scan_exposes_bounded_market_catalog_for_selection(self):
        row = {
            "bookmaker_key": "sportybet", "bookmaker": "SportyBet", "sport": "Football",
            "event": "Alpha FC vs Beta FC", "event_key": "alpha fc vs beta fc",
            "market": "Total Goals 2.5", "market_key": "total goals 2.5",
            "updated_at": None, "outcomes": [
                {"name": "Over 2.5", "name_key": "over 2.5", "odds": 2.1, "estimated_probability": None},
                {"name": "Under 2.5", "name_key": "under 2.5", "odds": 1.9, "estimated_probability": None},
            ],
        }
        health = [{"bookmaker": "SportyBet", "status": "usable markets", "events_loaded": 1}]
        with patch.object(app, "fetch_all_feeds", return_value=([row], health)):
            result = app.run_scan()
        self.assertEqual(result["available_markets_count"], 1)
        self.assertEqual(result["available_markets"][0]["event"], "Alpha FC vs Beta FC")
        self.assertEqual(result["available_markets"][0]["outcomes"][0]["odds"], 2.1)
        self.assertFalse(result["available_markets_truncated"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
