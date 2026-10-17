
import os
import time
import html
import logging
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests
from dotenv import load_dotenv

load_dotenv()

# ============================================================
# SOLVEEASE 5G LIVE ACCUMULATOR SCANNER
# Live odds analysis + Telegram alerts. No automatic betting.
# ============================================================

ODDS_API_KEY = os.getenv("ODDS_API_KEY", "").strip()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

REGION = os.getenv("ODDS_REGION", "uk").strip()
POLL_DELAY_SECONDS = max(
    3600, int(os.getenv("POLL_DELAY_SECONDS", "14400"))
)
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
log = logging.getLogger("solveease-scanner")

session = requests.Session()
session.headers.update({"User-Agent": "SolveEaseScanner/1.0"})

cache_lock = threading.Lock()
sent_combinations = set()
last_cache_cleanup = datetime.now(timezone.utc)

# Stops concurrent manual/background scans from running together.
scan_lock = threading.Lock()
quota_exhausted = threading.Event()


# ============================================================
# TELEGRAM
# ============================================================

def send_telegram_message(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.error(
            "Telegram configuration missing: set "
            "TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID."
        )
        return False

    url = (
        f"{TELEGRAM_BASE_URL}/bot"
        f"{TELEGRAM_BOT_TOKEN}/sendMessage"
    )

    try:
        response = session.post(
            url,
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=15,
        )

        try:
            data = response.json()
        except ValueError:
            data = {}

        if (
            response.ok
            and data.get("ok") is True
            and data.get("result", {}).get("message_id")
        ):
            log.info(
                "Telegram delivery confirmed. Message ID: %s",
                data["result"]["message_id"],
            )
            return True

        log.error(
            "Telegram delivery rejected: HTTP %s — %s",
            response.status_code,
            data.get("description", response.text[:300]),
        )
        return False

    except requests.RequestException as exc:
        log.error("Telegram request failed: %s", exc)
        return False


def send_startup_test():
    return send_telegram_message(
        "🟢 <b>SolveEase 5G Scanner is online</b>\n\n"
        "Telegram delivery test successful.\n"
        "Mode: analysis and alerts only.\n"
        "No winning outcome or profit is guaranteed."
    )


# ============================================================
# ODDS API
# ============================================================

def fetch_league_odds(sport_key):
    """
    Return a list of events on success.
    Return None on API/quota failure so the caller stops scanning.
    """

    if quota_exhausted.is_set():
        return None

    if not ODDS_API_KEY:
        log.error("ODDS_API_KEY is missing.")
        return None

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
    except requests.RequestException as exc:
        log.error("Odds API network error for %s: %s", sport_key, exc)
        return None

    remaining_header = response.headers.get("x-requests-remaining")
    used_header = response.headers.get("x-requests-used")

    try:
        remaining = (
            int(remaining_header)
            if remaining_header is not None
            else None
        )
    except (ValueError, TypeError):
        remaining = None

    log.info(
        "Odds API quota: used=%s remaining=%s",
        used_header or "unknown",
        remaining if remaining is not None else "unknown",
    )

    if remaining == 0 or response.status_code == 429:
        quota_exhausted.set()
        log.error(
            "Odds API quota/rate limit reached. "
            "Stopping requests for this scan."
        )
        return None

    if response.status_code in (401, 403):
        log.error(
            "Odds API rejected the request (HTTP %s). "
            "Check key validity, account status and quota. Response: %s",
            response.status_code,
            response.text[:300],
        )
        return None

    if response.status_code != 200:
        log.error(
            "Odds API error for %s: HTTP %s — %s",
            sport_key,
            response.status_code,
            response.text[:300],
        )
        return None

    try:
        data = response.json()
    except ValueError:
        log.error("Odds API returned invalid JSON for %s.", sport_key)
        return None

    if not isinstance(data, list):
        log.error("Unexpected Odds API response for %s.", sport_key)
        return None

    return data


# ============================================================
# EVENT PARSING
# ============================================================

def parse_event(event, sport_key, now_utc, cutoff_utc):
    commence_time = event.get("commence_time")
    home_team = event.get("home_team")
    away_team = event.get("away_team")
    event_id = event.get("id")

    if not all((commence_time, home_team, away_team, event_id)):
        return None

    try:
        kickoff = datetime.fromisoformat(
            commence_time.replace("Z", "+00:00")
        )
        if kickoff.tzinfo is None:
            kickoff = kickoff.replace(tzinfo=timezone.utc)
        kickoff = kickoff.astimezone(timezone.utc)
    except (ValueError, TypeError, AttributeError):
        log.warning("Skipping event with invalid kickoff time.")
        return None

    if kickoff <= now_utc or kickoff > cutoff_utc:
        return None

    bookmakers = event.get("bookmakers", [])
    if not isinstance(bookmakers, list):
        return None

    best_price = None
    best_bookmaker = None

    for bookmaker in bookmakers:
        markets = bookmaker.get("markets", [])
        if not isinstance(markets, list):
            continue

        for market in markets:
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

                if best_price is None or price > best_price:
                    best_price = price
                    best_bookmaker = bookmaker.get(
                        "title",
                        bookmaker.get("key", "Unknown"),
                    )

    if best_price is None or best_bookmaker is None:
        return None

    if not MIN_HOME_ODDS <= best_price <= MAX_HOME_ODDS:
        return None

    return {
        "id": str(event_id),
        "name": f"{home_team} vs {away_team}",
        "league": sport_key.removeprefix("soccer_").replace("_", " "),
        "home_odds": best_price,
        "bookmaker": best_bookmaker,
        "kickoff": kickoff,
        "kickoff_date": kickoff.strftime("%Y-%m-%d"),
    }


# ============================================================
# ALERT FORMAT
# ============================================================

def format_alert(match1, match2, combined_odds):
    date_text = datetime.strptime(
        match1["kickoff_date"], "%Y-%m-%d"
    ).strftime("%B %d, %Y")

    return (
        "⚽ <b>SOLVEEASE — TWO-MATCH CANDIDATE</b>\n\n"
        f"📅 <b>UTC match date:</b> {html.escape(date_text)}\n"
        f"📊 <b>Combined decimal odds:</b> {combined_odds:.2f}\n\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"1️⃣ <b>{html.escape(match1['name'])}</b>\n"
        f"🏆 {html.escape(match1['league'])}\n"
        "Selection: Home win (1X2)\n"
        f"Odds: {match1['home_odds']:.2f}\n"
        f"Bookmaker: {html.escape(match1['bookmaker'])}\n\n"
        f"2️⃣ <b>{html.escape(match2['name'])}</b>\n"
        f"🏆 {html.escape(match2['league'])}\n"
        "Selection: Home win (1X2)\n"
        f"Odds: {match2['home_odds']:.2f}\n"
        f"Bookmaker: {html.escape(match2['bookmaker'])}\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        "ℹ️ Prices are from The Odds API and may not be available "
        "at SportyBet. Confirm current odds and markets independently.\n"
        "⚠️ Candidate only. No outcome or profit is guaranteed."
    )


# ============================================================
# SCAN ENGINE
# ============================================================

def clean_cache(now_utc):
    global last_cache_cleanup

    with cache_lock:
        if now_utc - last_cache_cleanup >= timedelta(hours=24):
            sent_combinations.clear()
            last_cache_cleanup = now_utc
            log.info("Daily duplicate-alert cache cleared.")


def run_free_pipeline():
    if not scan_lock.acquire(blocking=False):
        log.warning("A scan is already running; skipping this request.")
        return

    try:
        # Reset the per-scan quota flag.
        quota_exhausted.clear()

        now_utc = datetime.now(timezone.utc)
        cutoff_utc = now_utc + timedelta(hours=LOOKAHEAD_HOURS)
        clean_cache(now_utc)

        candidates = []
        log.info("Starting scan of %d leagues.", len(EUROPEAN_LEAGUES))

        for league in EUROPEAN_LEAGUES:
            events = fetch_league_odds(league)

            if events is None:
                log.warning(
                    "Scan stopped at %s because an API request failed "
                    "or the quota was exhausted.",
                    league,
                )
                break

            for event in events:
                candidate = parse_event(
                    event, league, now_utc, cutoff_utc
                )
                if candidate:
                    candidates.append(candidate)

            time.sleep(1)

        # Deduplicate events returned by the provider.
        unique = {item["id"]: item for item in candidates}
        candidates = sorted(
            unique.values(),
            key=lambda item: item["kickoff"],
        )

        log.info(
            "Found %d qualifying individual matches.",
            len(candidates),
        )

        combinations_found = 0
        alerts_sent = 0

        for i in range(len(candidates)):
            for j in range(i + 1, len(candidates)):
                match1 = candidates[i]
                match2 = candidates[j]

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
                    if signature in sent_combinations:
                        continue

                if send_telegram_message(
                    format_alert(match1, match2, combined_odds)
                ):
                    with cache_lock:
                        sent_combinations.add(signature)
                    alerts_sent += 1
                    time.sleep(1)

        log.info(
            "Scan complete. Candidates=%d combinations=%d alerts_sent=%d",
            len(candidates),
            combinations_found,
            alerts_sent,
        )

    finally:
        scan_lock.release()


# ============================================================
# RENDER HEALTH SERVER
# ============================================================

class RenderHealthServer(BaseHTTPRequestHandler):

    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()

    def do_GET(self):
        if self.path in ("/", "/health"):
            body = (
                "SolveEase 5G Scanner is running.\n"
                "Mode: analysis and Telegram alerts only.\n"
            ).encode("utf-8")
            status = 200

        elif self.path == "/test-telegram":
            ok = send_telegram_message(
                "✅ <b>Telegram test successful</b>\n"
                "Your scanner can send Telegram messages."
            )
            body = (
                b"Telegram test succeeded.\n"
                if ok
                else b"Telegram test failed. Check Render logs.\n"
            )
            status = 200 if ok else 502

        elif self.path == "/scan-now":
            threading.Thread(
                target=run_free_pipeline,
                daemon=True,
            ).start()
            body = b"Scan requested. Check Render logs.\n"
            status = 202

        else:
            body = b"Not found.\n"
            status = 404

        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()

        if self.command != "HEAD":
            self.wfile.write(body)

    def log_message(self, fmt, *args):
        log.info("Health server: " + fmt, *args)


def start_port_listener():
    port = int(os.getenv("PORT", "10000"))
    server = ThreadingHTTPServer(
        ("0.0.0.0", port),
        RenderHealthServer,
    )
    log.info("Health server listening on port %s.", port)
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
        errors.append("Invalid home odds range.")

    for error in errors:
        log.error("CONFIGURATION: %s", error)

    return not errors


def main_scanner_loop():
    log.info("Background scanner started.")

    while True:
        try:
            run_free_pipeline()
        except Exception:
            log.exception("Unexpected scanner failure.")

        log.info(
            "Next scan in %d seconds.",
            POLL_DELAY_SECONDS,
        )
        time.sleep(POLL_DELAY_SECONDS)


if __name__ == "__main__":
    if not validate_config():
        raise SystemExit("Fix the environment variables and redeploy.")

    # GitHub Actions can request a single scan without starting a server.
    if os.getenv("SCAN_ONCE", "0") == "1":
        log.info("Single-scan mode activated.")
        run_free_pipeline()
        log.info("Single scan finished.")

    else:
        threading.Thread(
            target=start_port_listener,
            daemon=True,
        ).start()

        send_startup_test()
        main_scanner_loop()
