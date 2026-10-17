
import os
import time
import html
import threading
import logging
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests
from dotenv import load_dotenv

load_dotenv()

# ============================================================
# SOLVEASE FOOTBALL SCANNER — REAL ODDS + TELEGRAM ALERTS
# Analysis and alerts only. No automatic betting.
# ============================================================

ODDS_API_KEY = os.getenv("ODDS_API_KEY", "").strip()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

REGION = os.getenv("ODDS_REGION", "uk").strip()
POLL_DELAY_SECONDS = max(3600, int(os.getenv("POLL_DELAY_SECONDS", "43200")))
LOOKAHEAD_HOURS = max(1, int(os.getenv("LOOKAHEAD_HOURS", "48")))

MIN_ACCUMULATOR_ODDS = float(
    os.getenv("MIN_ACCUMULATOR_ODDS", "3.00")
)
MAX_ACCUMULATOR_ODDS = float(
    os.getenv("MAX_ACCUMULATOR_ODDS", "5.20")
)

MIN_HOME_ODDS = float(os.getenv("MIN_HOME_ODDS", "1.15"))
MAX_HOME_ODDS = float(os.getenv("MAX_HOME_ODDS", "3.50"))

API_BASE_URL = "https://api.the-odds-api.com/v4"
TELEGRAM_BASE_URL = "https://api.telegram.org"

EUROPEAN_LEAGUES = [
    "soccer_epl",
    "soccer_spain_la_liga",
    "soccer_italy_serie_a",
    "soccer_germany_bundesliga",
    "soccer_france_ligue_one",
    "soccer_uefa_champs_league",
]

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s UTC | %(levelname)s | %(message)s",
)
log = logging.getLogger("solveease-football")


# ============================================================
# HTTP SESSIONS
# ============================================================

session = requests.Session()
session.headers.update({"User-Agent": "SolveEase-FootballScanner/1.0"})

cache_lock = threading.Lock()
sent_combinations_cache = set()
last_cleanup_time = datetime.now(timezone.utc)


# ============================================================
# TELEGRAM DELIVERY
# ============================================================

def send_telegram_message(message):
    """Return True only when Telegram confirms message delivery."""

    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.error(
            "Telegram not configured. Set TELEGRAM_BOT_TOKEN "
            "and TELEGRAM_CHAT_ID."
        )
        return False

    url = (
        f"{TELEGRAM_BASE_URL}/bot"
        f"{TELEGRAM_BOT_TOKEN}/sendMessage"
    )

    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }

    try:
        response = session.post(url, json=payload, timeout=15)

        try:
            result = response.json()
        except ValueError:
            result = {}

        if (
            response.ok
            and result.get("ok") is True
            and result.get("result", {}).get("message_id")
        ):
            log.info(
                "Telegram delivery confirmed. Message ID: %s",
                result["result"]["message_id"],
            )
            return True

        log.error(
            "Telegram rejected message. HTTP %s: %s",
            response.status_code,
            result.get("description", response.text[:300]),
        )
        return False

    except requests.RequestException:
        log.exception("Telegram network request failed.")
        return False


def send_startup_test():
    message = (
        "🟢 <b>SolveEase Football Scanner</b>\n\n"
        "Telegram connection test successful.\n"
        "Mode: analysis and alerts only.\n"
        "Live odds and qualifying opportunities are checked "
        "by the background scanner.\n\n"
        "⚠️ No profit or winning result is guaranteed."
    )
    return send_telegram_message(message)


def format_alert(match1, match2, combined_odds):
    date_text = datetime.strptime(
        match1["kickoff_date"], "%Y-%m-%d"
    ).strftime("%B %d, %Y")

    match1_name = html.escape(match1["name"])
    match2_name = html.escape(match2["name"])
    league1 = html.escape(match1["league"])
    league2 = html.escape(match2["league"])
    bookmaker1 = html.escape(match1["bookmaker"])
    bookmaker2 = html.escape(match2["bookmaker"])

    return (
        "⚽ <b>SOLVEEASE — TWO-MATCH CANDIDATE</b>\n\n"
        f"📅 <b>UTC match date:</b> {date_text}\n"
        f"📊 <b>Combined decimal odds:</b> "
        f"{combined_odds:.2f}\n\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"1️⃣ <b>{match1_name}</b>\n"
        f"🏆 {league1}\n"
        f"Selection: Home win (1X2)\n"
        f"Odds: {match1['home_odds']:.2f}\n"
        f"Price source: {bookmaker1}\n\n"
        f"2️⃣ <b>{match2_name}</b>\n"
        f"🏆 {league2}\n"
        f"Selection: Home win (1X2)\n"
        f"Odds: {match2['home_odds']:.2f}\n"
        f"Price source: {bookmaker2}\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        "ℹ️ Prices are from The Odds API, not guaranteed "
        "to match SportyBet. Confirm both prices and markets "
        "on your bookmaker before considering a bet.\n"
        "⚠️ Candidate only — not a prediction of guaranteed profit."
    )


# ============================================================
# ODDS API
# ============================================================

def fetch_league_odds(sport_key):
    """Fetch upcoming 1X2 events for one league."""

    url = f"{API_BASE_URL}/sports/{sport_key}/odds/"

    params = {
        "apiKey": ODDS_API_KEY,
        "regions": REGION,
        "markets": "h2h",
        "oddsFormat": "decimal",
        "dateFormat": "iso",
    }

    try:
        response = session.get(url, params=params, timeout=20)

        remaining = response.headers.get("x-requests-remaining")
        used = response.headers.get("x-requests-used")

        if remaining is not None or used is not None:
            log.info(
                "Odds API quota: used=%s remaining=%s",
                used if used is not None else "unknown",
                remaining if remaining is not None else "unknown",
            )

        if response.status_code == 401:
            log.error("Odds API rejected the key. Check ODDS_API_KEY.")
            return []

        if response.status_code == 429:
            log.error("Odds API rate/quota limit reached.")
            return []

        if response.status_code != 200:
            log.error(
                "Odds API error for %s: HTTP %s — %s",
                sport_key,
                response.status_code,
                response.text[:300],
            )
            return []

        data = response.json()

        if not isinstance(data, list):
            log.error("Unexpected Odds API response for %s.", sport_key)
            return []

        return data

    except (requests.RequestException, ValueError):
        log.exception("Failed to retrieve odds for %s.", sport_key)
        return []


def parse_event(event, sport_key, now_utc, cutoff_utc):
    """Extract the best available home-win price from real h2h odds."""

    commence_time = event.get("commence_time")
    home_team = event.get("home_team")
    away_team = event.get("away_team")

    if not commence_time or not home_team or not away_team:
        return None

    try:
        match_time = datetime.fromisoformat(
            commence_time.replace("Z", "+00:00")
        )
        if match_time.tzinfo is None:
            match_time = match_time.replace(tzinfo=timezone.utc)
        match_time = match_time.astimezone(timezone.utc)
    except (ValueError, TypeError):
        log.warning("Skipping event with invalid kickoff time.")
        return None

    if match_time <= now_utc or match_time > cutoff_utc:
        return None

    best_odds = None
    best_bookmaker = None

    bookmakers = event.get("bookmakers", [])
    if not isinstance(bookmakers, list):
        return None

    for bookmaker in bookmakers:
        for market in bookmaker.get("markets", []):
            if market.get("key") != "h2h":
                continue

            for outcome in market.get("outcomes", []):
                if outcome.get("name") != home_team:
                    continue

                try:
                    price = float(outcome.get("price"))
                except (TypeError, ValueError):
                    continue

                if price <= 1.0:
                    continue

                if best_odds is None or price > best_odds:
                    best_odds = price
                    best_bookmaker = bookmaker.get(
                        "title", bookmaker.get("key", "Unknown")
                    )

    if best_odds is None or best_bookmaker is None:
        return None

    if not MIN_HOME_ODDS <= best_odds <= MAX_HOME_ODDS:
        return None

    event_id = event.get("id")
    if not event_id:
        return None

    return {
        "id": str(event_id),
        "name": f"{home_team} vs {away_team}",
        "league": sport_key.replace("soccer_", "").replace("_", " "),
        "home_odds": best_odds,
        "bookmaker": best_bookmaker,
        "kickoff": match_time,
        "kickoff_date": match_time.strftime("%Y-%m-%d"),
    }


# ============================================================
# COMBINATION SCANNER
# ============================================================

def clean_old_cache(now_utc):
    """Clear sent combinations daily to bound memory use."""

    global last_cleanup_time

    with cache_lock:
        if now_utc - last_cleanup_time >= timedelta(hours=24):
            sent_combinations_cache.clear()
            last_cleanup_time = now_utc
            log.info("Daily duplicate-alert cache cleared.")


def run_free_pipeline():
    now_utc = datetime.now(timezone.utc)
    cutoff_utc = now_utc + timedelta(hours=LOOKAHEAD_HOURS)

    clean_old_cache(now_utc)
    candidates = []

    log.info("Starting scan of %d leagues.", len(EUROPEAN_LEAGUES))

    for league in EUROPEAN_LEAGUES:
        events = fetch_league_odds(league)

        for event in events:
            candidate = parse_event(event, league, now_utc, cutoff_utc)
            if candidate:
                candidates.append(candidate)

        # Gentle pacing between requests; not a quota guarantee.
        time.sleep(1)

    log.info("Found %d qualifying individual matches.", len(candidates))

    # Avoid duplicate events and combinations.
    unique_candidates = {}
    for candidate in candidates:
        unique_candidates[candidate["id"]] = candidate

    candidates = list(unique_candidates.values())
    candidates.sort(key=lambda item: item["kickoff"])

    combinations_found = 0
    alerts_sent = 0

    for i in range(len(candidates)):
        for j in range(i + 1, len(candidates)):
            match1 = candidates[i]
            match2 = candidates[j]

            # Keep both matches on the same UTC calendar date.
            if match1["kickoff_date"] != match2["kickoff_date"]:
                continue

            combined_odds = round(
                match1["home_odds"] * match2["home_odds"], 2
            )

            if not (
                MIN_ACCUMULATOR_ODDS
                <= combined_odds
                <= MAX_ACCUMULATOR_ODDS
            ):
                continue

            combinations_found += 1

            signature = "|".join(
                sorted([match1["id"], match2["id"]])
            )

            with cache_lock:
                already_sent = signature in sent_combinations_cache

            if already_sent:
                continue

            message = format_alert(match1, match2, combined_odds)

            # Only mark a combination sent after confirmed delivery.
            if send_telegram_message(message):
                with cache_lock:
                    sent_combinations_cache.add(signature)
                alerts_sent += 1
                time.sleep(1)

    log.info(
        "Scan complete. Candidates=%d combinations=%d alerts_sent=%d",
        len(candidates),
        combinations_found,
        alerts_sent,
    )


# ============================================================
# RENDER HEALTH SERVER
# ============================================================

class RenderHealthServer(BaseHTTPRequestHandler):

    def do_GET(self):
        if self.path in ("/", "/health"):
            body = (
                "SolveEase Football Scanner: running\n"
                "Mode: analysis and Telegram alerts only\n"
            ).encode("utf-8")
            status_code = 200

        elif self.path == "/test-telegram":
            ok = send_telegram_message(
                "✅ <b>Telegram test successful</b>\n"
                "Your SolveEase scanner can send messages."
            )
            body = (
                b"Telegram test sent successfully\n"
                if ok
                else b"Telegram test failed; inspect Render logs\n"
            )
            status_code = 200 if ok else 502

        elif self.path == "/scan-now":
            threading.Thread(
                target=run_free_pipeline, daemon=True
            ).start()
            body = b"Scan requested. Check Render logs and Telegram.\n"
            status_code = 202

        else:
            body = b"Not found\n"
            status_code = 404

        self.send_response(status_code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        log.info("Health server: " + fmt, *args)


def start_port_listener():
    port = int(os.getenv("PORT", "10000"))
    server = ThreadingHTTPServer(("0.0.0.0", port), RenderHealthServer)
    log.info("Render health server listening on port %s.", port)
    server.serve_forever()


# ============================================================
# STARTUP
# ============================================================

def validate_config():
    errors = []

    if not ODDS_API_KEY:
        errors.append("ODDS_API_KEY is missing.")

    if not TELEGRAM_BOT_TOKEN:
        errors.append("TELEGRAM_BOT_TOKEN is missing.")

    if not TELEGRAM_CHAT_ID:
        errors.append("TELEGRAM_CHAT_ID is missing.")

    if MIN_ACCUMULATOR_ODDS <= 1:
        errors.append("MIN_ACCUMULATOR_ODDS must be greater than 1.")

    if MAX_ACCUMULATOR_ODDS < MIN_ACCUMULATOR_ODDS:
        errors.append(
            "MAX_ACCUMULATOR_ODDS must be >= MIN_ACCUMULATOR_ODDS."
        )

    if MIN_HOME_ODDS <= 1 or MAX_HOME_ODDS < MIN_HOME_ODDS:
        errors.append("Invalid home-odds range.")

    if errors:
        for error in errors:
            log.error("CONFIGURATION: %s", error)
        return False

    return True


def main_scanner_loop():
    log.info("Background scanner started.")

    while True:
        try:
            run_free_pipeline()
        except Exception:
            log.exception("Unexpected scanner failure.")

        log.info(
            "Next scan in %d hours (%d seconds).",
            POLL_DELAY_SECONDS // 3600,
            POLL_DELAY_SECONDS,
        )
        time.sleep(POLL_DELAY_SECONDS)


if __name__ == "__main__":
    if not validate_config():
        raise SystemExit("Fix the configuration variables and redeploy.")

    threading.Thread(
        target=start_port_listener, daemon=True
    ).start()

    # Verify Telegram before waiting for the first scan.
    send_startup_test()

    main_scanner_loop()
