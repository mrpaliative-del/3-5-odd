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
    def test_statistics_checks_three_seasons_for_coverage(self):
        self.assertEqual(app._season_codes(__import__("datetime").date(2026, 10, 19)), ["2627", "2526", "2425"])

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

    def test_json_response_sanitizes_non_finite_numbers(self):
        handler = app.Handler.__new__(app.Handler)
        captured = {}
        handler.send_bytes = lambda status, body, content_type: captured.update(status=status, body=body, content_type=content_type)
        handler.send_json(200, {"ok": True, "probability": float("nan"), "nested": [float("inf"), 0.7]})
        payload = json.loads(captured["body"].decode("utf-8"))
        self.assertEqual(captured["status"], 200)
        self.assertEqual(captured["content_type"], "application/json; charset=utf-8")
        self.assertIsNone(payload["probability"])
        self.assertIsNone(payload["nested"][0])
        self.assertEqual(payload["nested"][1], 0.7)

    def test_api_helper_reports_http_and_content_type_for_non_json(self):
        self.assertIn('returned HTTP ', app.HTML)
        self.assertIn('not JSON', app.HTML)
        self.assertIn('response.headers.get("content-type")', app.HTML)

    def test_lookup_allocates_every_leg_and_only_keeps_qualified_output(self):
        self.assertIn('api("/api/lookup", "POST"', app.HTML)
        self.assertIn('api("/api/analyze-ticket", "POST"', app.HTML)
        self.assertIn('const threshold = 0.70;', app.HTML)
        self.assertIn('$("code").value = "";', app.HTML)
        self.assertIn('Full-ticket allocation:', app.HTML)
        self.assertIn('decision = "KEEP"', app.HTML)
        self.assertIn('decision = "REVIEW"', app.HTML)
        self.assertIn('api("/api/find-replacements", "POST"', app.HTML)
        self.assertIn('Only KEEP/REPLACED selections go to the converter', app.HTML)
        self.assertIn('rawProbability !== null', app.HTML)
        self.assertIn('Only KEEP/REPLACED selections go to the converter', app.HTML)
        self.assertNotIn("renderTicketMathReview(data, box)", app.HTML)
        self.assertNotIn("Original ticket selections ·", app.HTML)
        self.assertNotIn("Filtered out ·", app.HTML)
        self.assertNotIn("Minimum individual implied probability", app.HTML)

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
        self.assertIn("automatically fetched", result["model"])

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

    def test_early_goal_market_never_reuses_full_match_total_probability(self):
        p, reason = app._selection_probability("Over/Under - Early Goals", "Over 2.5", 1.8, 1.2)
        self.assertIsNone(p)
        self.assertIn("Early-goal", reason)
        result = app.analyze_betslip({"legs": [{
            "event": "Alpha vs Beta", "market": "Over/Under - Early Goals", "selection": "Over 2.5",
            "odds": 1.45, "home_goal_rate": 1.8, "away_goal_rate": 1.2,
        }]})
        self.assertFalse(result["ranked_legs"][0]["model_supported"])
        self.assertIsNone(result["ranked_legs"][0]["estimated_probability"])

    def test_auto_ticket_analysis_flags_special_market_before_history_lookup(self):
        with patch.object(app, "fetch_match_statistics", side_effect=AssertionError("history lookup should not run for unsupported special market")):
            result = app.analyze_ticket_automatically({"legs": [{
                "event": "Alpha vs Beta", "market": "1X2 - 1UP", "selection": "Home", "odds": 1.30,
            }]})
        leg = result["ranked_legs"][0]
        self.assertEqual(leg["status"], "unsupported_market")
        self.assertIsNone(leg["estimated_probability"])
        self.assertIn("1UP/2UP", leg["reason"])

    def test_1up_2up_markets_are_not_treated_as_standard_1x2(self):
        for market in ("1X2 - 1UP", "1X2 - 2UP"):
            p, reason = app._selection_probability(market, "Home", 1.8, 0.8)
            self.assertIsNone(p)
            self.assertIn("1UP/2UP", reason)

    def test_compound_team_or_total_market_is_not_treated_as_plain_total(self):
        p, reason = app._selection_probability("Home Team or Over 2.5", "Yes", 1.8, 0.8)
        self.assertIsNone(p)
        self.assertIn("Compound market", reason)

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


    def test_auto_stats_splits_event_and_parses_public_csv(self):
        self.assertEqual(app._split_event_teams("Alpha FC vs Beta FC"), ("Alpha FC", "Beta FC"))
        text = "Date,HomeTeam,AwayTeam,FTHG,FTAG,FTR\n01/09/26,Alpha FC,Beta FC,2,1,H\n08/09/26,Beta FC,Alpha FC,0,0,D\n"
        rows = app._csv_matches(text, "E0", "2627")
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["hg"], 2)
        self.assertEqual(rows[0]["league"], "England Premier League")

    def test_auto_stats_matches_cross_league_teams_independently(self):
        from unittest.mock import patch
        csv_home = "Date,HomeTeam,AwayTeam,FTHG,FTAG\n01/09/26,Alpha FC,Home Opponent,2,0\n08/09/26,Home Opponent,Alpha FC,1,1\n15/09/26,Alpha FC,Home Opponent 2,3,1\n22/09/26,Alpha FC,Home Opponent 3,1,0\n29/09/26,Home Opponent 4,Alpha FC,0,2\n"
        csv_away = "Date,HomeTeam,AwayTeam,FTHG,FTAG\n02/09/26,Away Opponent,Beta FC,1,1\n09/09/26,Beta FC,Away Opponent 2,2,0\n16/09/26,Away Opponent 3,Beta FC,0,2\n23/09/26,Away Opponent 4,Beta FC,1,3\n30/09/26,Beta FC,Away Opponent 5,1,0\n"
        def fake_csv(season, division):
            if division == "E0":
                return csv_home
            if division == "D1":
                return csv_away
            return None
        with patch.object(app, "_fetch_football_csv", side_effect=fake_csv), \
             patch.object(app, "_xg_rates", return_value=None):
            app.FOOTBALL_STATS_CACHE.clear()
            app.FOOTBALL_DIVISION_MATCH_CACHE.clear()
            app.FOOTBALL_CSV_CACHE.clear()
            result = app.fetch_match_statistics("Alpha FC vs Beta FC")
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["home_league"], "England Premier League")
        self.assertEqual(result["away_league"], "Germany Bundesliga")
        self.assertGreaterEqual(result["home_team_matches_in_sample"], 2)
        self.assertGreaterEqual(result["away_team_matches_in_sample"], 2)
        self.assertIn("Independent domestic histories", result["method"])

    def test_auto_stats_team_summary_reports_venue_form(self):
        history = [
            {"date": "2026-09-01", "venue": "home", "gf": 2, "ga": 1, "opponent": "B"},
            {"date": "2026-09-08", "venue": "away", "gf": 0, "ga": 0, "opponent": "C"},
            {"date": "2026-09-15", "venue": "home", "gf": 1, "ga": 2, "opponent": "D"},
        ]
        summary = app._summarize_team(history)
        self.assertEqual(summary["matches_available"], 3)
        self.assertEqual(summary["home_matches"], 2)
        self.assertEqual(summary["away_matches"], 1)
        self.assertAlmostEqual(summary["recent_goals_for_avg"], 1.0)

    def test_betslip_uses_automatic_statistics_when_goal_rates_absent(self):
        stats = {"ok": True, "status": "estimated", "home_goal_rate": 1.6, "away_goal_rate": 0.9,
                 "source": "fixture", "xg_available": False, "latest_match_date": "2026-10-01", "league": "Test League"}
        with patch.object(app, "fetch_match_statistics", return_value=stats):
            result = app.analyze_betslip({"legs": [{"event": "Alpha FC vs Beta FC", "market": "1X2", "selection": "Home", "odds": 2.1}]})
        self.assertEqual(result["supported_legs"], 1)
        self.assertIsNotNone(result["ranked_legs"][0]["estimated_probability"])
        self.assertEqual(result["ranked_legs"][0]["home_goal_rate"], 1.6)

    def test_auto_ticket_analysis_returns_unavailable_without_fabrication(self):
        with patch.object(app, "fetch_match_statistics", return_value={"ok": False, "status": "unmatched", "reason": "no matching teams"}):
            result = app.analyze_ticket_automatically({"legs": [{"event": "Unknown FC vs Mystery FC", "market": "1X2", "selection": "Home", "odds": 2.2}]})
        self.assertEqual(result["estimated_legs"], 0)
        self.assertIsNone(result["ranked_legs"][0]["estimated_probability"])
        self.assertEqual(result["ranked_legs"][0]["status"], "not_estimated")

    def test_total_goal_probability_can_read_line_from_selection(self):
        probability, method = app._selection_probability("Total Goals", "Over 2.5", 1.5, 1.0)
        self.assertIsNotNone(probability)
        self.assertIn("over 2.5", method)

    def test_auto_stats_runs_and_full_ticket_allocation_is_rendered(self):
        self.assertIn('api("/api/analyze-ticket", "POST"', app.HTML)
        self.assertIn('const threshold = 0.70;', app.HTML)
        self.assertIn('sessionStorage.removeItem("solveease:lastLookup")', app.HTML)
        self.assertIn('Full-ticket allocation:', app.HTML)
        self.assertNotIn('Original ticket selections ·', app.HTML)
        self.assertNotIn('Odds-implied ranking remains available', app.HTML)


    def test_team_names_normalize_accents_and_common_club_prefixes(self):
        self.assertEqual(app._team_key("Nordsjælland"), app._team_key("Nordsjaelland"))
        self.assertEqual(app._team_key("FC Midtjylland"), app._team_key("Midtjylland"))
        self.assertEqual(app._team_key("PFC CSKA Sofia"), app._team_key("CSKA Sofia"))
        self.assertEqual(app._team_key("St. Truidense VV"), app._team_key("Sint-Truiden"))


    def test_team_matching_rejects_ambiguous_substring_collisions(self):
        self.assertIsNone(app._find_team_name("Ajax", ["Jong Ajax", "Ajax U21"]))
        self.assertEqual(app._find_team_name("FC Midtjylland", ["Midtjylland", "Jong Midtjylland"]), "Midtjylland")

    def test_history_selector_requires_minimum_sample_and_venue_evidence(self):
        source = Path(__file__).with_name("main.py").read_text()
        self.assertIn("len(history) >= 5 and venue_count >= 2", source)
        self.assertIn("minimum_sample_required", source)

    def test_missing_probability_guard_does_not_convert_null_to_zero(self):
        self.assertIn('rawProbability !== null && rawProbability !== undefined && rawProbability !== ""', app.HTML)
        self.assertIn('item.probability === null ? "Model probability: unavailable"', app.HTML)

    def test_wider_european_league_coverage_is_configured(self):
        self.assertIn("E2", app.FOOTBALL_DIVISIONS)
        self.assertIn("E3", app.FOOTBALL_DIVISIONS)
        self.assertIn("D2", app.FOOTBALL_DIVISIONS)
        self.assertIn("SP2", app.FOOTBALL_DIVISIONS)
        self.assertIn("F2", app.FOOTBALL_DIVISIONS)

    def test_common_double_chance_labels_are_recognized(self):
        p1, _ = app._selection_probability("Double Chance", "1 or X", 1.7, 0.9)
        p2, _ = app._selection_probability("Double Chance", "Home or Draw", 1.7, 0.9)
        self.assertIsNotNone(p1)
        self.assertIsNotNone(p2)
        self.assertAlmostEqual(p1, p2, places=6)

    def test_common_btts_labels_are_recognized(self):
        yes, _ = app._selection_probability("Both Teams To Score", "GG", 1.7, 0.9)
        no, _ = app._selection_probability("BTTS", "NG", 1.7, 0.9)
        self.assertIsNotNone(yes)
        self.assertIsNotNone(no)
        self.assertAlmostEqual(yes + no, 1.0, places=6)

if __name__ == "__main__":
    unittest.main(verbosity=2)

class AdvancedFeatureTests(unittest.TestCase):
    def test_dixon_coles_matrix_normalizes(self):
        matrix = app.advanced.dc_score_matrix(1.4, 1.1)
        self.assertAlmostEqual(sum(sum(row) for row in matrix), 1.0, places=10)

    def test_bayesian_shrinkage_returns_bounded_rates(self):
        result = app.advanced.shrink_goal_rates(5, 3, 2, 4, 3, 3)
        self.assertGreaterEqual(result["home_goal_rate"], .15)
        self.assertLessEqual(result["home_goal_rate"], 4.5)

    def test_calibration_metrics_on_known_sample(self):
        result = app.advanced.calibration_metrics([.8, .2], [1, 0])
        self.assertEqual(result["n"], 2)
        self.assertAlmostEqual(result["brier_score"], .04)

    def test_probability_threshold_and_evidence_gate(self):
        self.assertTrue(app.advanced.eligible_selection({"estimated_probability": .70, "evidence_quality": "adequate"}))
        self.assertFalse(app.advanced.eligible_selection({"estimated_probability": .99, "evidence_quality": "unknown"}))
        self.assertFalse(app.advanced.eligible_selection({"estimated_probability": .69, "evidence_quality": "high"}))

    def test_optimizer_replaces_only_with_fresh_verified_two_model_candidate(self):
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        candidate = {"event":"A vs B", "market":"Double Chance", "selection":"Home/Draw", "odds":1.35,
                     "odds_verified":True, "odds_source":"configured feed row", "bookmaker":"SportyBet", "bookmaker_key":"sportybet",
                     "updated_at":now, "model_probability":.76, "alternate_model_probability":.75, "evidence_quality":"provisional"}
        result = app.advanced.optimize_ticket([{"original":{"event":"A vs B","selection":"Away","estimated_probability":.4,"evidence_quality":"adequate"},"candidates":[candidate]}])
        self.assertEqual(len(result["replacements"]), 1)
        self.assertEqual(result["qualifying_count"], 1)

    def test_optimizer_rejects_missing_price_provenance_or_timestamp(self):
        candidate = {"event":"A vs B", "market":"Double Chance", "selection":"Home/Draw", "odds":1.35,
                     "odds_verified":True, "model_probability":.76, "alternate_model_probability":.75, "evidence_quality":"provisional"}
        result = app.advanced.optimize_ticket([{"original":{"event":"A vs B","estimated_probability":.4,"evidence_quality":"adequate"},"candidates":[candidate]}])
        self.assertEqual(result["qualifying_count"], 0)
        self.assertEqual(result["replacements"], [])

    def test_optimizer_allows_lower_odds_if_value_gates_pass(self):
        from datetime import datetime, timezone
        candidate = {"event":"A vs B", "market":"Double Chance", "selection":"Home/Draw", "odds":1.30,
                     "odds_verified":True, "odds_source":"configured feed row", "bookmaker":"SportyBet", "bookmaker_key":"sportybet",
                     "updated_at":datetime.now(timezone.utc).isoformat(), "model_probability":.80, "alternate_model_probability":.79,
                     "evidence_quality":"provisional"}
        original = {"event":"A vs B", "selection":"Away", "odds":1.55, "estimated_probability":.4, "evidence_quality":"adequate"}
        result = app.advanced.optimize_ticket([{"original":original,"candidates":[candidate]}])
        self.assertEqual(len(result["replacements"]), 1)
        self.assertEqual(result["replacements"][0]["replacement"]["odds"], 1.30)

    def test_optimizer_excludes_when_no_candidate_qualifies(self):
        result = app.advanced.optimize_ticket([{"original":{"event":"A vs B","estimated_probability":.4,"evidence_quality":"adequate"},"candidates":[]}])
        self.assertEqual(result["qualifying_count"], 0)
        self.assertIn("No original selection or replacement passed", result["exclusions"][0]["reason"])

    def test_replacement_search_requires_fresh_timestamp_and_supported_markets(self):
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        self.assertIsNotNone(app._odds_timestamp(now.isoformat()))
        self.assertIsNotNone(app._odds_timestamp(now.timestamp() * 1000))
        self.assertTrue(app._common_market_supported("Double Chance", "Home or Draw"))
        self.assertTrue(app._common_market_supported("GG/NG", "Yes"))
        self.assertFalse(app._common_market_supported("1X2 - 1UP", "Home"))
        self.assertEqual(app._fixture_key("Nordsjælland vs Atalanta"), app._fixture_key("Nordsjaelland v Atalanta"))

    def test_replacement_engine_blocks_cross_market_substitution(self):
        from datetime import datetime, timezone
        stamp = datetime.now(timezone.utc).isoformat()
        rows = [{"event":"Alpha FC vs Beta FC", "market":"Double Chance", "bookmaker":"SportyBet",
                 "bookmaker_key":"sportybet", "updated_at":stamp,
                 "outcomes":[{"name":"Home or Draw", "odds":1.40}, {"name":"Draw or Away", "odds":2.50}]}]
        health = [{"bookmaker":"SportyBet", "status":"usable markets"}]
        with patch.object(app, "fetch_all_feeds", return_value=(rows, health)):
            result = app.find_market_replacements([{"event":"Alpha FC vs Beta FC", "market":"Early Goals", "selection":"Over 2.5"}])
        self.assertEqual(result["legs_received"], 1)
        self.assertEqual(result["candidates_found"], 0)
        self.assertEqual(result["results"][0]["status"], "unsupported_market")

    def test_replacement_market_signature_requires_same_line_and_outcome(self):
        self.assertEqual(app._market_selection_signature("Total Goals 2.5", "Over 2.5"), ("total_goals", "2.5", "over"))
        self.assertEqual(app._market_selection_signature("Over/Under", "Over 2.5"), ("total_goals", "2.5", "over"))
        self.assertNotEqual(app._market_selection_signature("Total Goals 2.5", "Over 2.5"), app._market_selection_signature("Total Goals 1.5", "Over 1.5"))
        self.assertNotEqual(app._market_selection_signature("Total Goals 2.5", "Over 2.5"), app._market_selection_signature("GG/NG", "Yes"))
        self.assertIsNone(app._market_selection_signature("1X2 - 1UP", "Home"))

    def test_fixture_matching_uses_explicit_aliases_and_preserves_home_away(self):
        self.assertEqual(app._fixture_key("Man Utd vs Wolves"), app._fixture_key("Manchester United v Wolverhampton Wanderers"))
        self.assertNotEqual(app._fixture_key("Man Utd vs Wolves"), app._fixture_key("Wolves vs Man Utd"))

    def test_replacement_only_compares_equivalent_market_across_alias_fixture_names(self):
        from datetime import datetime, timezone
        stamp = datetime.now(timezone.utc).isoformat()
        rows = [
            {"event":"Manchester United vs Wolverhampton Wanderers", "market":"Total Goals 2.5", "bookmaker":"Global feed 1", "bookmaker_key":"global_1", "updated_at":stamp,
             "outcomes":[{"name":"Over 2.5", "odds":2.10}, {"name":"Under 2.5", "odds":1.80}]},
            {"event":"Man Utd vs Wolves", "market":"Total Goals 1.5", "bookmaker":"Global feed 1", "bookmaker_key":"global_1", "updated_at":stamp,
             "outcomes":[{"name":"Over 1.5", "odds":1.15}, {"name":"Under 1.5", "odds":5.50}]},
            {"event":"Man Utd vs Wolves", "market":"GG/NG", "bookmaker":"Global feed 1", "bookmaker_key":"global_1", "updated_at":stamp,
             "outcomes":[{"name":"Yes", "odds":1.70}, {"name":"No", "odds":2.10}]},
        ]
        health = [{"bookmaker":"Global feed 1", "status":"usable markets"}]
        stats = {"ok":True, "home_goal_rate":3.5, "away_goal_rate":2.5, "source":"synthetic test"}
        with patch.object(app, "fetch_all_feeds", return_value=(rows, health)), patch.object(app, "fetch_match_statistics", return_value=stats):
            result = app.find_market_replacements([{"event":"Man Utd vs Wolves", "market":"Total Goals 2.5", "selection":"Over 2.5"}])
        self.assertEqual(result["legs_received"], 1)
        candidates = result["results"][0]["candidates"]
        self.assertTrue(candidates)
        self.assertTrue(all(c["market"] == "Total Goals 2.5" and c["selection"] == "Over 2.5" for c in candidates))
        self.assertTrue(all(app._fixture_key(c["event"]) == app._fixture_key("Manchester United vs Wolverhampton Wanderers") for c in candidates))

    def test_configurable_global_feed_uses_generic_schema(self):
        app.BOOKMAKERS["global_test"] = {"name":"Global test", "feed_url":"https://feed.example/markets.json"}
        try:
            row = app.normalize_feed_event("global_test", {"event":"Man Utd vs Wolves", "market":"Total Goals 2.5", "updated_at":"2026-10-20T10:00:00Z", "outcomes":[{"name":"Over 2.5", "odds":1.8},{"name":"Under 2.5", "odds":2.0}]})
            self.assertIsNotNone(row)
            self.assertEqual(row["bookmaker"], "Global test")
            self.assertEqual(app._fixture_key(row["event"]), app._fixture_key("Manchester United vs Wolverhampton Wanderers"))
        finally:
            app.BOOKMAKERS.pop("global_test", None)

    def test_replacement_engine_rejects_untimestamped_prices(self):
        from datetime import datetime, timezone
        rows = [{"event":"Alpha FC vs Beta FC", "market":"Double Chance", "bookmaker":"SportyBet",
                 "bookmaker_key":"sportybet", "updated_at":None,
                 "outcomes":[{"name":"Home or Draw", "odds":1.40}, {"name":"Draw or Away", "odds":2.50}]}]
        health = [{"bookmaker":"SportyBet", "status":"usable markets"}]
        stats = {"ok":True, "home_goal_rate":2.5, "away_goal_rate":0.5, "source":"synthetic test"}
        with patch.object(app, "fetch_all_feeds", return_value=(rows, health)), patch.object(app, "fetch_match_statistics", return_value=stats):
            result = app.find_market_replacements([{"event":"Alpha FC vs Beta FC", "market":"Unsupported special", "selection":"Yes"}])
        self.assertEqual(result["candidates_found"], 0)

    def test_replacement_endpoint_is_registered_and_documented(self):
        self.assertIn('/api/find-replacements', Path(__file__).with_name('main.py').read_text())
        self.assertIn('configured feeds with fresh timestamped prices', app.HTML)

    def test_daily_builder_filters_date_started_games_and_threshold(self):
        from datetime import datetime, timezone
        now = datetime(2026, 10, 19, 10, 0, tzinfo=timezone.utc)
        fixtures = [
            {"event":"Upcoming","start_time":"2026-10-19T15:00:00+01:00","selections":[{"market":"DC","selection":"1X","odds":1.3,"estimated_probability":.72,"evidence_quality":"adequate"}]},
            {"event":"Started","start_time":"2026-10-19T10:00:00+01:00","selections":[{"estimated_probability":.9,"evidence_quality":"high"}]},
            {"event":"Low confidence","start_time":"2026-10-19T16:00:00+01:00","selections":[{"estimated_probability":.69,"evidence_quality":"high"}]},
            {"event":"Tomorrow","start_time":"2026-10-20T15:00:00+01:00","selections":[{"estimated_probability":.8,"evidence_quality":"high"}]},
        ]
        report = app.advanced.build_daily_accumulator(fixtures, local_date="2026-10-19", timezone_name="Africa/Lagos", now=now)
        self.assertEqual(report["qualifying_count"], 1)
        self.assertEqual(report["shortfall"], 19)
        self.assertEqual(report["selections"][0]["event"], "Upcoming")

    def test_psychology_flags_are_diagnostic_only(self):
        flags = app.advanced.psychology_flags({"event":"Famous FC", "reasoning":"big club and must-win; due a win"})
        names = {x["flag"] for x in flags}
        self.assertIn("reputation_bias_check", names)
        self.assertIn("narrative_bias_check", names)
        self.assertIn("gambler_fallacy_or_chasing", names)

    def test_overround_removal_requires_complete_odds(self):
        result = app.advanced.remove_overround([2.0, 2.0])
        self.assertAlmostEqual(sum(result["margin_removed_probabilities"]), 1.0)
        self.assertFalse(app.advanced.remove_overround([2.0, None])["ok"])

    def test_dashboard_has_advanced_routes_and_alert_controls(self):
        for fragment in ("dailyPanel", "optimizePanel", "alertsPanel", "/api/daily-accumulator", "/api/telegram-test", "/api/model-probability", "/api/calibration-metrics"):
            self.assertIn(fragment, app.HTML if fragment in app.HTML else Path(__file__).with_name("main.py").read_text())


class Release3102HardeningTests(unittest.TestCase):
    def test_replacement_rejects_invalid_probability_threshold(self):
        with self.assertRaises(ValueError):
            app.find_market_replacements([{"event": "A vs B", "market": "Total Goals 2.5", "selection": "Over 2.5"}], threshold=1.5)

    def test_replacement_rejects_nonfinite_disagreement(self):
        with self.assertRaises(ValueError):
            app.find_market_replacements([{"event": "A vs B", "market": "Total Goals 2.5", "selection": "Over 2.5"}], max_disagreement=float("nan"))

    def test_ticket_math_audit_detects_reported_total_odds_mismatch(self):
        result = app.audit_ticket_math({"reported_total_odds": 29043.88, "stake": 100,
            "reported_bonus": 2040363.47, "reported_payout": 4365712.15,
            "legs": [{"event": "A vs B", "odds": 1.42}, {"event": "C vs D", "odds": 1.28}]})
        self.assertEqual(result["audit_status"], "ODDS_MISMATCH")
        self.assertAlmostEqual(result["calculated_combined_odds"], 1.8176, places=4)
        self.assertFalse(result["odds_agree"])
        self.assertAlmostEqual(result["simple_return_at_reported_odds"], 2904388.0, places=2)
        self.assertAlmostEqual(result["reported_payout_minus_simple_return"], 1461324.15, places=2)

    def test_ticket_math_audit_regression_for_supplied_36_leg_slip(self):
        odds = [1.42,1.28,1.43,1.30,1.45,1.28,1.31,1.55,1.30,1.55,1.20,1.29,
                1.51,1.23,1.28,1.15,1.30,1.32,1.36,1.36,1.30,1.49,1.26,1.29,
                1.23,1.20,1.24,1.19,1.22,1.52,1.20,1.30,1.46,1.29,1.25,1.19]
        result = app.audit_ticket_math({"reported_total_odds": 29043.88, "stake": 100,
            "reported_bonus": 2040363.47, "reported_payout": 4365712.15,
            "legs": [{"event": f"Fixture {i}", "odds": odd} for i, odd in enumerate(odds, 1)]})
        self.assertEqual(result["legs_received"], 36)
        self.assertEqual(result["audit_status"], "ODDS_MISMATCH")
        self.assertAlmostEqual(result["calculated_combined_odds"], 19154.11466448355, places=6)
        self.assertAlmostEqual(result["simple_return_at_reported_odds"], 2904388.0, places=2)

    def test_ticket_math_audit_does_not_calculate_with_invalid_leg_odds(self):
        result = app.audit_ticket_math({"legs": [{"odds": 1.8}, {"odds": "not odds"}]})
        self.assertEqual(result["audit_status"], "INCOMPLETE_INPUT")
        self.assertIsNone(result["calculated_combined_odds"])
        self.assertEqual(len(result["invalid_legs"]), 1)

    def test_ticket_math_audit_route_and_ui_are_present(self):
        source = Path(app.__file__).read_text(encoding="utf-8")
        self.assertIn('/api/audit-ticket-math', source)
        self.assertIn('function auditTicketMath()', source)
        self.assertIn('id="ticketMathAuditJson"', source)

    def test_release_version_updated(self):
        self.assertIn("Version: 3.10.7", Path(app.__file__).read_text(encoding="utf-8"))
        self.assertEqual(app.Handler.server_version, "SolveEase/3.10.7")
        self.assertEqual(app._team_key("PSG"), app._team_key("Paris Saint-Germain"))
        self.assertEqual(app._team_key("Man Utd"), app._team_key("Manchester United"))
        self.assertEqual(app._team_key("Wolves"), app._team_key("Wolverhampton Wanderers"))
        self.assertEqual(app._team_key("Lillestroem SK"), app._team_key("Lillestrom"))
        self.assertEqual(app._team_key("Bodoe/Glimt"), app._team_key("Bodo Glimt"))
        self.assertEqual(app._team_key("Galatasaray Istanbul"), app._team_key("Galatasaray"))
        self.assertEqual(app._team_key("Brighton"), app._team_key("Brighton and Hove Albion"))


class Release3106CoverageTests(unittest.TestCase):
    def test_additional_european_divisions_are_configured(self):
        for code in ("AUT", "DNK", "FIN", "IRL", "NOR", "POL", "ROU", "RUS", "SWE", "SWZ"):
            self.assertIn(code, app.FOOTBALL_DIVISIONS)


    def test_extra_league_uses_all_seasons_url_and_correct_code(self):
        self.assertIn("DNK", app.FOOTBALL_ALL_SEASON_CODES)
        self.assertIn("ROU", app.FOOTBALL_ALL_SEASON_CODES)
        with patch.object(app, "urlopen") as mocked:
            mocked.return_value.__enter__.return_value.status = 200
            mocked.return_value.__enter__.return_value.read.return_value = b"Date,HomeTeam,AwayTeam,FTHG,FTAG\n01/08/2025,Alpha,Beta,2,1\n"
            result = app._fetch_football_csv("all", "DNK")
        self.assertIsNotNone(result)
        self.assertIn("/new/DNK.csv", mocked.call_args.args[0].full_url)
        app.FOOTBALL_CSV_CACHE.pop("all:DNK", None)

    def test_all_season_csv_assigns_football_season_from_match_date(self):
        rows = app._csv_matches("Date,HomeTeam,AwayTeam,FTHG,FTAG\n01/08/2025,Alpha,Beta,2,1\n01/03/2026,Beta,Alpha,0,0\n", "DNK", "all")
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["season"], "2526")
        self.assertEqual(rows[1]["season"], "2526")

    def test_health_reports_configured_coverage_and_version(self):
        source = Path(app.__file__).read_text(encoding="utf-8")
        self.assertIn('"version": "3.10.7"', source)
        self.assertIn('"football_stats_configured_divisions": list(FOOTBALL_DIVISIONS.items())', source)
        self.assertIn('"loaded_match_count": loaded_match_count', source)

    def test_coverage_failure_distinguishes_missing_team_from_small_sample(self):
        app.FOOTBALL_STATS_CACHE.clear()
        with patch.object(app, "_season_codes", return_value=["2425"]), \
             patch.object(app, "_fetch_football_csv", return_value="not-a-csv"), \
             patch.object(app, "_csv_matches", return_value=[]):
            result = app.fetch_match_statistics("Unknown Team QZ vs Unknown Team RZ")
        self.assertFalse(result["ok"])
        self.assertEqual(result["loaded_division_count"], 0)
        self.assertEqual(result["team_diagnostics"]["home"]["status"], "TEAM_NOT_FOUND_IN_LOADED_DATA")
        self.assertIn("loaded_match_count", result)

    def test_added_coverage_does_not_enable_special_market_models(self):
        for market_text in ("Over 2.5 Early Goals by 30 minutes", "1X2 - 1UP Home", "Home Team or Over 2.5"):
            self.assertIsNotNone(app._special_settlement_reason(market_text))
