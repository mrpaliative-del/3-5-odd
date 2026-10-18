
"""
SolveEase Surebet Intelligence v7.0.0
Multi-page, read-only football market analysis.

Run:
    python main.py

Render environment:
    HOST=0.0.0.0
    PORT=10000
    DATABASE_PATH=/var/data/solveease.db
    SCAN_INTERVAL_SECONDS=900
    MARKET_FEED_URLS=https://your-authorized-feed.example/markets.json

Feed format:
{
  "markets": [
    {
      "event": "Home FC vs Away FC",
      "event_id": "event-123",
      "market": "Match Winner",
      "bookmaker": "Example Bookmaker",
      "updated_at": "2026-10-18T12:00:00Z",
      "outcomes": [
        {"selection": "Home", "odds": 2.1},
        {"selection": "Draw", "odds": 3.2},
        {"selection": "Away", "odds": 3.5}
      ]
    }
  ]
}

Only configure feed URLs you are authorized to access.
No bet placement, automated staking, or guaranteed predictions.
"""

import json
import math
import os
import re
import socket
import sqlite3
import threading
import time
import ipaddress
import unicodedata
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

APP_NAME = "SolveEase Surebet Intelligence"
VERSION = "7.0.0"
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "10000"))
DB_PATH = os.getenv("DATABASE_PATH", "./solveease.db")
SCAN_INTERVAL = max(0, min(86400, int(os.getenv("SCAN_INTERVAL_SECONDS", "900"))))
REQUEST_TIMEOUT = max(2, min(20, int(os.getenv("REQUEST_TIMEOUT_SECONDS", "8"))))
MAX_RESPONSE_BYTES = max(100_000, min(10_000_000, int(
    os.getenv("MAX_RESPONSE_BYTES", "4000000")
)))
MAX_FEEDS = max(1, min(20, int(os.getenv("MAX_FEEDS", "10"))))
MAX_AGE_DAYS = max(1, min(3650, int(os.getenv("QUOTE_RETENTION_DAYS", "90"))))
ENABLE_SPORTYBET = os.getenv("ENABLE_SPORTYBET_LOOKUP", "true").lower() in (
    "1", "true", "yes", "on"
)
FEEDS = [
    x.strip() for x in os.getenv("MARKET_FEED_URLS", "").split(";")
    if x.strip()
][:MAX_FEEDS]
SPORTYBET_URL = "https://www.sportybet.com/api/ng/orders/share/"

DB_LOCK = threading.RLock()
SCAN_LOCK = threading.Lock()
STATE_LOCK = threading.RLock()
STATE = {
    "started_at": None,
    "last_scan_at": None,
    "last_scan_status": "not_run",
    "last_lookup_status": "not_run",
    "last_lookup_at": None,
    "last_error": None,
}
RATE_LOCK = threading.Lock()
RATE_BUCKETS = {}
USER_AGENT = "SolveEase-Surebet-Intelligence/7.0 read-only"


def now():
    return datetime.now(timezone.utc).isoformat()


def safe_float(value):
    try:
        n = float(value)
        return n if math.isfinite(n) else None
    except (TypeError, ValueError, OverflowError):
        return None


def odds_ok(value):
    n = safe_float(value)
    return n is not None and 1.0 < n <= 10000


def probability(odds):
    return round(100 / odds, 3) if odds_ok(odds) else None


def clean(value, limit=300):
    if value is None:
        return None
    value = str(value).strip()
    return value[:limit] if value else None


def norm(value):
    value = unicodedata.normalize("NFKD", str(value or ""))
    value = "".join(c for c in value if not unicodedata.combining(c))
    value = value.lower().replace("&", " and ")
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", value)).strip()


def send_json(handler, status, data):
    raw = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode()
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(raw)))
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("X-Content-Type-Options", "nosniff")
    handler.send_header("Referrer-Policy", "no-referrer")
    handler.end_headers()
    handler.wfile.write(raw)


def send_text(handler, status, body, content_type="text/html; charset=utf-8"):
    raw = body.encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(raw)))
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("X-Content-Type-Options", "nosniff")
    handler.send_header("Referrer-Policy", "no-referrer")
    handler.end_headers()
    handler.wfile.write(raw)


# ------------------------------- SQLite -------------------------------------

def db():
    # Each operation gets its own connection. WAL supports concurrent readers.
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=15000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    parent = os.path.dirname(os.path.abspath(DB_PATH))
    os.makedirs(parent, exist_ok=True)
    with DB_LOCK, db() as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS scan_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at TEXT NOT NULL,
            finished_at TEXT NOT NULL,
            status TEXT NOT NULL,
            configured_feeds INTEGER NOT NULL,
            usable_quotes INTEGER NOT NULL,
            candidate_count INTEGER NOT NULL,
            feed_health_json TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS market_quotes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT,
            event TEXT NOT NULL,
            market TEXT NOT NULL,
            selection TEXT NOT NULL,
            bookmaker TEXT,
            odds REAL NOT NULL,
            updated_at TEXT,
            observed_at TEXT NOT NULL,
            source_host TEXT,
            exhaustive_two_outcome INTEGER NOT NULL DEFAULT 0
        );

        CREATE INDEX IF NOT EXISTS idx_quotes_event_market
        ON market_quotes(event, market, observed_at);

        CREATE INDEX IF NOT EXISTS idx_quotes_observed
        ON market_quotes(observed_at);

        CREATE TABLE IF NOT EXISTS forecasts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT,
            event TEXT NOT NULL,
            market TEXT NOT NULL,
            selection TEXT NOT NULL,
            probability REAL NOT NULL,
            model_version TEXT NOT NULL,
            created_at TEXT NOT NULL,
            kickoff TEXT,
            result_status TEXT NOT NULL DEFAULT 'unsettled'
        );

        CREATE TABLE IF NOT EXISTS match_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT,
            event TEXT NOT NULL,
            home_score INTEGER,
            away_score INTEGER,
            status TEXT NOT NULL,
            source TEXT,
            verified_at TEXT
        );

        CREATE TABLE IF NOT EXISTS watchlist (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            client_key TEXT NOT NULL,
            event_id TEXT,
            event TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(client_key, event_id, event)
        );
        """)
        conn.commit()


def store_scan(started, finished, status, quotes, health, candidates):
    observed = finished
    with DB_LOCK, db() as conn:
        conn.execute("BEGIN")
        for q in quotes:
            conn.execute("""
                INSERT INTO market_quotes
                (event_id,event,market,selection,bookmaker,odds,updated_at,
                 observed_at,source_host,exhaustive_two_outcome)
                VALUES (?,?,?,?,?,?,?,?,?,?)
            """, (
                q.get("event_id"), q["event"], q["market"], q["selection"],
                q.get("bookmaker"), q["odds"], q.get("updated_at"), observed,
                q.get("source_host"), int(q.get("exhaustive_two_outcome", False))
            ))
        conn.execute("""
            INSERT INTO scan_runs
            (started_at,finished_at,status,configured_feeds,usable_quotes,
             candidate_count,feed_health_json)
            VALUES (?,?,?,?,?,?,?)
        """, (
            started, finished, status, len(FEEDS), len(quotes),
            len(candidates), json.dumps(health)
        ))
        cutoff = datetime.fromtimestamp(
            time.time() - MAX_AGE_DAYS * 86400, timezone.utc
        ).isoformat()
        conn.execute("DELETE FROM market_quotes WHERE observed_at < ?", (cutoff,))
        conn.commit()


def read_history(limit=30):
    with db() as conn:
        rows = conn.execute("""
            SELECT id, started_at, finished_at, status, configured_feeds,
                   usable_quotes, candidate_count
            FROM scan_runs ORDER BY id DESC LIMIT ?
        """, (max(1, min(100, limit)),)).fetchall()
    return [dict(r) for r in rows]


def read_quotes(limit=100):
    with db() as conn:
        rows = conn.execute("""
            SELECT event_id,event,market,selection,bookmaker,odds,updated_at,
                   observed_at,source_host
            FROM market_quotes ORDER BY id DESC LIMIT ?
        """, (max(1, min(500, limit)),)).fetchall()
    return [dict(r) for r in rows]


def cleanup_old_data():
    cutoff = datetime.fromtimestamp(
        time.time() - MAX_AGE_DAYS * 86400, timezone.utc
    ).isoformat()
    with DB_LOCK, db() as conn:
        conn.execute("DELETE FROM market_quotes WHERE observed_at < ?", (cutoff,))
        conn.commit()


# -------------------------- Feed security & parsing -------------------------

def check_public_https_url(url):
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError("Feed URL must use HTTPS and include a hostname.")
    if parsed.username or parsed.password:
        raise ValueError("Credentials in feed URLs are not permitted.")
    if parsed.port not in (None, 443):
        raise ValueError("Only the standard HTTPS port is permitted.")
    host = parsed.hostname.lower()
    if host in ("localhost",) or host.endswith(".localhost") or host.endswith(".local"):
        raise ValueError("Local hostnames are not permitted.")

    # Reject literal private/reserved IP addresses and hostnames resolving to
    # non-public addresses. Feed URLs must be admin-controlled environment vars.
    try:
        ip = ipaddress.ip_address(host)
        if not ip.is_global:
            raise ValueError("Non-public feed addresses are not permitted.")
    except ValueError as exc:
        if "not permitted" in str(exc):
            raise
        try:
            results = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        except OSError as dns_error:
            raise ValueError("Feed hostname did not resolve.") from dns_error
        if not results:
            raise ValueError("Feed hostname did not resolve.")
        for result in results:
            address = ipaddress.ip_address(result[4][0])
            if not address.is_global:
                raise ValueError("Feed hostname resolves to a non-public address.")
    return parsed


def fetch_json(url):
    check_public_https_url(url)
    request = Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
    }, method="GET")
    with urlopen(request, timeout=REQUEST_TIMEOUT) as response:
        body = response.read(MAX_RESPONSE_BYTES + 1)
    if len(body) > MAX_RESPONSE_BYTES:
        raise ValueError("Response exceeded the configured size limit.")
    if not body:
        raise ValueError("Feed returned an empty response.")
    try:
        return json.loads(body.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Feed did not return valid JSON.") from exc


def extract_quotes(payload, source_url):
    if isinstance(payload, list):
        records = payload
    elif isinstance(payload, dict):
        records = payload.get("markets")
        if not isinstance(records, list):
            records = payload.get("quotes")
        if not isinstance(records, list):
            records = payload.get("data")
        if not isinstance(records, list):
            records = []
    else:
        records = []

    result = []
    source_host = urlparse(source_url).hostname
    for rec in records[:10000]:
        if not isinstance(rec, dict):
            continue
        event = clean(rec.get("event") or rec.get("event_name") or
                      rec.get("fixture") or rec.get("match"))
        market = clean(rec.get("market") or rec.get("market_name") or
                       rec.get("marketDesc"), 200)
        if not event or not market:
            continue
        common = {
            "event": event,
            "event_id": clean(rec.get("event_id") or rec.get("fixture_id"), 160),
            "market": market,
            "sport": clean(rec.get("sport") or "Football", 80),
            "bookmaker": clean(rec.get("bookmaker") or
                               rec.get("bookmaker_name") or rec.get("source"), 120),
            "updated_at": clean(rec.get("updated_at") or
                                rec.get("timestamp") or rec.get("last_updated"), 100),
            "source_host": source_host,
            "exhaustive_two_outcome": rec.get("exhaustive_two_outcome") is True,
        }
        outcomes = rec.get("outcomes")
        if isinstance(outcomes, list):
            for outcome in outcomes:
                if not isinstance(outcome, dict):
                    continue
                selection = clean(outcome.get("selection") or
                                  outcome.get("name") or outcome.get("label"), 200)
                odds = safe_float(outcome.get("odds") or outcome.get("price") or
                                  outcome.get("decimal_odds"))
                if selection and odds_ok(odds):
                    result.append({**common, "selection": selection, "odds": odds})
        else:
            selection = clean(rec.get("selection") or rec.get("outcome"), 200)
            odds = safe_float(rec.get("odds") or rec.get("price") or
                              rec.get("decimal_odds"))
            if selection and odds_ok(odds):
                result.append({**common, "selection": selection, "odds": odds})
    return result


def fetch_markets():
    all_quotes, health = [], []
    for url in FEEDS:
        start = time.monotonic()
        try:
            quotes = extract_quotes(fetch_json(url), url)
            all_quotes.extend(quotes)
            health.append({
                "source": urlparse(url).hostname,
                "status": "ok" if quotes else "empty",
                "quotes": len(quotes),
                "elapsed_ms": round((time.monotonic() - start) * 1000),
                "checked_at": now(),
            })
        except Exception as exc:
            health.append({
                "source": urlparse(url).hostname or "configured feed",
                "status": "error",
                "quotes": 0,
                "error": str(exc)[:200],
                "checked_at": now(),
            })
    return all_quotes, health


# ------------------------------ Market logic --------------------------------

def event_key(q):
    return str(q.get("event_id") or norm(q.get("event")))


def find_candidates(quotes):
    groups = {}
    for q in quotes:
        if not q.get("exhaustive_two_outcome"):
            continue
        if not q.get("bookmaker") or not q.get("selection"):
            continue
        key = (event_key(q), norm(q.get("market")))
        groups.setdefault(key, []).append(q)

    candidates = []
    for group in groups.values():
        selections = list(dict.fromkeys(norm(q["selection"]) for q in group))
        if len(selections) != 2:
            continue
        best = []
        for selection in selections:
            choices = sorted(
                [q for q in group if norm(q["selection"]) == selection],
                key=lambda q: q["odds"], reverse=True
            )
            # Require two distinct named bookmakers across selected outcomes.
            distinct = []
            seen_books = set()
            for q in choices:
                book = norm(q.get("bookmaker"))
                if book and book not in seen_books:
                    seen_books.add(book)
                    distinct.append(q)
            if not distinct:
                break
            best.append(distinct[0])
        if len(best) != 2:
            continue
        if norm(best[0]["bookmaker"]) == norm(best[1]["bookmaker"]):
            continue
        inverse_sum = sum(1 / q["odds"] for q in best)
        if inverse_sum >= 1:
            continue
        candidates.append({
            "event": best[0]["event"],
            "market": best[0]["market"],
            "outcomes": [{
                "selection": q["selection"], "odds": q["odds"],
                "bookmaker": q["bookmaker"], "updated_at": q.get("updated_at"),
                "source": q.get("source_host"),
            } for q in best],
            "implied_probability_sum_percent": round(inverse_sum * 100, 4),
            "theoretical_margin_percent": round((1 - inverse_sum) * 100, 4),
            "status": "candidate_requires_manual_verification",
            "warning": (
                "Only consider this a screening signal. Verify that the market "
                "is genuinely exhaustive, outcomes are mutually exclusive, "
                "settlement rules match, and both prices are executable."
            ),
        })
    return candidates


def run_scan():
    if not SCAN_LOCK.acquire(blocking=False):
        return {"status": "already_running", "message": "A scan is already running."}
    started = now()
    try:
        quotes, health = fetch_markets()
        candidates = find_candidates(quotes)
        finished = now()
        status = "complete" if FEEDS else "no_feeds_configured"
        if FEEDS and not quotes:
            status = "no_usable_quotes"
        store_scan(started, finished, status, quotes, health, candidates)
        with STATE_LOCK:
            STATE["last_scan_at"] = finished
            STATE["last_scan_status"] = status
            STATE["last_error"] = None if quotes else (
                "No feeds configured." if not FEEDS else "No usable quotes returned."
            )
        return {
            "status": status,
            "started_at": started,
            "scanned_at": finished,
            "configured_feed_count": len(FEEDS),
            "usable_quote_count": len(quotes),
            "candidate_count": len(candidates),
            "candidates": candidates[:100],
            "feed_health": health,
            "message": (
                "No market feeds configured. Add authorized HTTPS JSON feeds."
                if not FEEDS else
                "No usable quotes returned. Inspect feed health and provider schema."
                if not quotes else
                "Scan complete. Any candidate requires manual verification."
            ),
        }
    except Exception as exc:
        with STATE_LOCK:
            STATE["last_scan_status"] = "error"
            STATE["last_error"] = type(exc).__name__
        return {"status": "error", "message": "Scan failed.", "detail": type(exc).__name__}
    finally:
        SCAN_LOCK.release()


def scheduler():
    while True:
        time.sleep(max(30, SCAN_INTERVAL))
        if SCAN_INTERVAL:
            try:
                run_scan()
                cleanup_old_data()
            except Exception:
                pass


# --------------------------- Booking-code lookup ----------------------------

def walk_dicts(value, depth=0):
    if depth > 12:
        return
    if isinstance(value, dict):
        yield value
        for child in value.values():
            if isinstance(child, (dict, list)):
                yield from walk_dicts(child, depth + 1)
    elif isinstance(value, list):
        for child in value[:3000]:
            if isinstance(child, (dict, list)):
                yield from walk_dicts(child, depth + 1)


def parse_sportybet(payload):
    found, seen = [], set()
    for item in walk_dicts(payload):
        home = item.get("homeTeamName") or item.get("homeTeam") or item.get("home_name")
        away = item.get("awayTeamName") or item.get("awayTeam") or item.get("away_name")
        market = item.get("marketDesc") or item.get("marketName") or item.get("market")
        selection = (item.get("selectedOutcome") or item.get("outcomeName") or
                     item.get("selectionName") or item.get("selection"))
        odds = safe_float(item.get("odds") or item.get("odd") or
                          item.get("decimalOdds") or item.get("price"))
        if not (home and away and market and selection and odds_ok(odds)):
            continue
        key = (norm(home), norm(away), norm(market), norm(selection), odds)
        if key in seen:
            continue
        seen.add(key)
        found.append({
            "event": f"{home} vs {away}",
            "market": clean(market, 200),
            "selection": clean(selection, 200),
            "odds": odds,
            "implied_probability_percent": probability(odds),
            "bookmaker": "SportyBet",
            "data_quality": "Parsed response; not independently verified",
        })
    return found


def lookup_code(code):
    if not ENABLE_SPORTYBET:
        return {"ok": False, "status": "disabled", "message": "Lookup disabled."}
    code = clean(code, 160)
    if not code or not re.fullmatch(r"[A-Za-z0-9_-]+", code):
        return {"ok": False, "status": "invalid_code",
                "message": "Enter a code without spaces or a URL."}
    req = Request(SPORTYBET_URL + code, headers={
        "User-Agent": USER_AGENT, "Accept": "application/json"
    })
    try:
        with urlopen(req, timeout=REQUEST_TIMEOUT) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
            status_code = response.status
        if len(raw) > MAX_RESPONSE_BYTES:
            return {"ok": False, "status": "response_too_large"}
        payload = json.loads(raw.decode("utf-8-sig"))
        legs = parse_sportybet(payload)
        if not legs:
            return {
                "ok": False, "status": "schema_unrecognized",
                "http_status": status_code,
                "message": "Response received, but no complete legs were recognized.",
                "endpoint_live_verified": False,
            }
        combined = math.prod(x["odds"] for x in legs)
        return {
            "ok": True, "status": "decoded",
            "endpoint_live_verified": False,
            "ticket": {
                "bookmaker": "SportyBet",
                "legs": legs,
                "leg_count": len(legs),
                "combined_odds_from_parsed_legs": round(combined, 6),
                "read_only": True,
            },
            "warning": "Compare the parsed total with the bookmaker's displayed total.",
        }
    except HTTPError as exc:
        return {"ok": False, "status": "http_error", "http_status": exc.code}
    except (URLError, TimeoutError, OSError):
        return {"ok": False, "status": "connection_error",
                "message": "Could not reach the share-code endpoint."}
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {"ok": False, "status": "invalid_response",
                "message": "The endpoint did not return valid JSON."}
    except Exception as exc:
        return {"ok": False, "status": "lookup_error",
                "detail": type(exc).__name__}


# ------------------------------ Multi-page UI -------------------------------

PAGES = {
    "/": ("Home", "Overview and service health"),
    "/scanner": ("Code Scanner", "Decode supported accumulator share codes"),
    "/matches": ("Match Intelligence", "Fixture and model data when available"),
    "/markets": ("Market Scanner", "Compare configured market quotes"),
    "/risk-lab": ("Accumulator Risk Lab", "Combined odds and implied probability"),
    "/watchlist": ("Watchlist", "Monitoring requires a configured data source"),
    "/reports": ("Reports", "Persistent scan history and quote snapshots"),
    "/data-health": ("Data Health", "Feed status, storage and service diagnostics"),
}


def page_html(path):
    title, subtitle = PAGES.get(path, PAGES["/"])
    nav = "".join(
        f'<a class="navlink {"active" if route == path else ""}" href="{route}">'
        f'{name}</a>'
        for route, (name, _) in PAGES.items()
    )
    body = {
        "/": """
          <div class="cards">
            <article><small>Configured feeds</small><strong id="feeds">—</strong></article>
            <article><small>Last scan</small><strong id="lastscan">Not run</strong></article>
            <article><small>Usable quotes</small><strong id="quotes">—</strong></article>
            <article><small>Data health</small><strong id="health">Checking</strong></article>
          </div>
          <p>Use the navigation to open each tool on its own URL. Live results require valid data sources.</p>
          <a class="button" href="/scanner">Open Code Scanner</a>
          <a class="button secondary" href="/markets">Open Market Scanner</a>
        """,
        "/scanner": """
          <label>Supported bookmaker</label><input value="SportyBet" disabled>
          <label for="code">Booking share code</label>
          <input id="code" maxlength="160" autocomplete="off" placeholder="Paste booking code">
          <button id="decode">Decode and analyse</button>
          <div id="lookup" class="notice">Ready. Enter a code to begin.</div>
          <div id="ticket"></div>
          <p class="muted">The share endpoint is unofficial and may change. A decoded response is not independent verification.</p>
        """,
        "/matches": """
          <div class="notice">No fixture source is currently configured. Match lists, form, head-to-head and probabilities will not be fabricated.</div>
          <button id="refresh">Refresh service status</button><pre id="matchStatus"></pre>
        """,
        "/markets": """
          <p>Run a scan against configured authorized HTTPS feeds.</p>
          <button id="scan">Run feed scan</button><div id="scanStatus" class="notice">Not run.</div>
          <div id="candidates"></div><div id="feedHealth"></div>
        """,
        "/risk-lab": """
          <p>Calculate combined decimal odds and the implied probability from your own leg odds. This is arithmetic, not a calibrated chance of winning.</p>
          <label for="odds">Decimal odds, separated by commas</label>
          <input id="odds" placeholder="1.80, 2.10, 1.65">
          <button id="calculate">Calculate</button><div id="riskResult" class="notice">Enter decimal odds above 1.00.</div>
        """,
        "/watchlist": """
          <div class="notice">Watchlist alerts require a monitoring source and client-side identity design. This page does not claim active background alerts.</div>
        """,
        "/reports": """
          <button id="history">Refresh scan history</button><div id="reportStatus" class="notice">History is stored only after scans run.</div>
          <div id="historyRows"></div>
        """,
        "/data-health": """
          <button id="refresh">Refresh diagnostics</button><pre id="diagnostics" class="notice">Loading…</pre>
          <p>SQLite retention is configurable with QUOTE_RETENTION_DAYS. A Render persistent disk must be attached and mounted at /var/data.</p>
        """,
    }.get(path, "<p>Page not found.</p>")

    return r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#0b1020">
<meta name="description" content="SolveEase read-only football market intelligence">
<title>""" + title + r""" · SolveEase</title>
<style>
:root{color-scheme:dark;--bg:#0b1020;--panel:#121a2c;--line:#29364d;--text:#edf3ff;--muted:#9caec7;--blue:#60a5fa;--green:#34d399;--amber:#fbbf24}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:16px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
header{padding:16px;position:sticky;top:0;background:#0e1628;border-bottom:1px solid var(--line);z-index:3}
.wrap{max-width:1050px;margin:auto;padding:18px 14px}
.brand{font-size:18px;font-weight:850}.muted,small{color:var(--muted)}
nav{display:flex;gap:7px;overflow-x:auto;padding-top:12px}
.navlink{flex:0 0 auto;text-decoration:none;color:var(--muted);border:1px solid var(--line);border-radius:999px;padding:8px 12px}
.navlink.active{color:white;background:#1d3c60;border-color:#37689a}
.panel,article,.notice{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:15px;margin:12px 0;overflow-wrap:anywhere}
h1{font-size:clamp(25px,5vw,36px);margin:0 0 4px}p{overflow-wrap:anywhere}
.cards{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px;margin:18px 0}
.cards article{margin:0}.cards strong{display:block;font-size:22px}
label{display:block;color:var(--muted);margin:13px 0 5px}
input{display:block;width:100%;min-height:48px;border:1px solid #35445d;border-radius:10px;padding:11px;background:#0d1526;color:white;font:inherit}
button,.button{display:inline-block;margin:10px 7px 5px 0;padding:11px 15px;min-height:44px;border:1px solid #36516f;border-radius:10px;background:#1e3a5f;color:white;font:inherit;font-weight:750;text-decoration:none;cursor:pointer}
.secondary{background:#192338}button:disabled{opacity:.5}pre{white-space:pre-wrap;overflow-wrap:anywhere}
@media(min-width:760px){.wrap{padding:24px}.cards{grid-template-columns:repeat(4,minmax(0,1fr))}}
</style></head><body>
<header><div class="brand">SolveEase Surebet Intelligence <small>· READ-ONLY</small></div>
<nav aria-label="Main navigation">""" + nav + r"""</nav></header>
<main class="wrap"><h1>""" + title + r"""</h1><p class="muted">""" + subtitle + r"""</p>
<section class="panel">""" + body + r"""</section>
<footer class="muted"><small>Prices can change. Candidates require source, timing and settlement verification. No automated staking.</small></footer>
</main>
<script>
"use strict";
const $=id=>document.getElementById(id);
const esc=s=>String(s??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const fmt=(n,d=3)=>Number.isFinite(Number(n))?Number(n).toFixed(d):"—";
async function api(path,options={}){
 const r=await fetch(path,{cache:"no-store",headers:{"Content-Type":"application/json"},...options});
 const d=await r.json().catch(()=>({message:"Invalid server response"}));
 if(!r.ok)throw Error(d.message||("HTTP "+r.status));return d;
}
async function health(){
 const h=await api("/health");
 if($("feeds"))$("feeds").textContent=h.configured_market_feed_count;
 if($("lastscan"))$("lastscan").textContent=h.last_scan_at||"Not run";
 if($("health"))$("health").textContent=h.data_health;
 if($("diagnostics"))$("diagnostics").textContent=JSON.stringify(h,null,2);
 if($("matchStatus"))$("matchStatus").textContent=JSON.stringify(h,null,2);
}
if($("refresh"))$("refresh").onclick=()=>health().catch(e=>alert(e.message));
if($("decode"))$("decode").onclick=async()=>{
 const code=$("code").value.trim();if(!code){$("lookup").textContent="Enter a booking code.";return}
 $("decode").disabled=true;$("lookup").textContent="Contacting share endpoint…";$("ticket").replaceChildren();
 try{
  const d=await api("/api/lookup",{method:"POST",body:JSON.stringify({code,bookmaker:"sportybet"})});
  $("lookup").textContent=d.ok?"Decoded; review the source and data-quality warnings.":(d.message||d.status);
  if(d.ticket){
   const t=d.ticket;
   $("ticket").innerHTML="<article><b>"+esc(t.leg_count)+" legs</b><p>Combined odds from parsed legs: "+fmt(t.combined_odds_from_parsed_legs,4)+"</p></article>"+
    t.legs.map((l,i)=>"<article><b>Leg "+(i+1)+": "+esc(l.event)+"</b><p>"+esc(l.market)+" · "+esc(l.selection)+"</p><strong>"+fmt(l.odds,2)+"</strong></article>").join("");
  }
 }catch(e){$("lookup").textContent=e.message}finally{$("decode").disabled=false}
};
if($("scan"))$("scan").onclick=async()=>{
 $("scan").disabled=true;$("scanStatus").textContent="Scanning configured feeds…";
 try{
  const d=await api("/api/scan",{method:"POST",body:"{}"});
  $("scanStatus").textContent=d.message+" Quotes: "+d.usable_quote_count+". Candidates: "+d.candidate_count+".";
  $("candidates").innerHTML=(d.candidates||[]).map(c=>"<article><b>"+esc(c.event)+"</b><p>"+esc(c.market)+" · theoretical margin "+fmt(c.theoretical_margin_percent,4)+"%</p><p>"+esc(c.warning)+"</p></article>").join("")||"<p>No candidates returned.</p>";
  $("feedHealth").innerHTML=(d.feed_health||[]).map(f=>"<article><b>"+esc(f.source)+"</b><p>"+esc(f.status)+" · quotes "+f.quotes+"</p><p>"+esc(f.error||"")+"</p></article>").join("");
 }catch(e){$("scanStatus").textContent=e.message}finally{$("scan").disabled=false}
};
if($("calculate"))$("calculate").onclick=()=>{
 const values=$("odds").value.split(",").map(x=>Number(x.trim()));
 if(!values.length||values.some(x=>!Number.isFinite(x)||x<=1)){ $("riskResult").textContent="Every value must be a valid decimal odd greater than 1.";return}
 const combined=values.reduce((a,b)=>a*b,1);
 $("riskResult").textContent="Legs: "+values.length+" · Combined odds: "+combined.toFixed(4)+" · Raw implied probability: "+(100/combined).toFixed(4)+"%. This is not a calibrated probability of winning.";
};
if($("history"))$("history").onclick=async()=>{
 try{const d=await api("/api/history");$("reportStatus").textContent="Stored scan records: "+d.count;
 $("historyRows").innerHTML=d.runs.map(x=>"<article><b>"+esc(x.finished_at)+"</b><p>Status: "+esc(x.status)+" · Feeds: "+x.configured_feeds+" · Quotes: "+x.usable_quotes+" · Candidates: "+x.candidate_count+"</p></article>").join("")||"<p>No scans stored yet.</p>";
 }catch(e){$("reportStatus").textContent=e.message}
};
health().catch(()=>{});
</script></body></html>"""


# ------------------------------- HTTP server --------------------------------

RATE_LIMIT = max(10, min(600, int(os.getenv("RATE_LIMIT_PER_MINUTE", "60"))))


def allowed_request(ip):
    current = time.time()
    with RATE_LOCK:
        bucket = RATE_BUCKETS.get(ip, [])
        bucket = [t for t in bucket if current - t < 60]
        if len(bucket) >= RATE_LIMIT:
            RATE_BUCKETS[ip] = bucket
            return False
        bucket.append(current)
        RATE_BUCKETS[ip] = bucket
        if len(RATE_BUCKETS) > 5000:
            for key in list(RATE_BUCKETS)[:1000]:
                RATE_BUCKETS.pop(key, None)
        return True


class Handler(BaseHTTPRequestHandler):
    server_version = "SolveEase/7.0.0"

    def log_message(self, fmt, *args):
        # Never log POST bodies or booking codes.
        print(f"{self.log_date_time_string()} {self.client_address[0]} {fmt % args}")

    def read_payload(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise ValueError("Invalid Content-Length.")
        if length < 0 or length > 100_000:
            raise ValueError("Request body too large.")
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("Body must be valid JSON.") from exc
        if not isinstance(payload, dict):
            raise ValueError("Body must be a JSON object.")
        return payload

    def _dispatch(self, method):
        if not allowed_request(self.client_address[0]):
            return send_json(self, 429, {"error": "rate_limited",
                                        "message": "Too many requests. Try again shortly."})
        path = urlparse(self.path).path

        if method == "HEAD":
            if path in PAGES or path in ("/health", "/healthz"):
                self.send_response(200)
                self.send_header("Content-Length", "0")
                self.end_headers()
            else:
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()
            return

        if method == "GET":
            if path in PAGES:
                return send_text(self, 200, page_html(path))
            if path in ("/health", "/healthz", "/api/health"):
                with STATE_LOCK:
                    s = dict(STATE)
                try:
                    with db() as conn:
                        quote_count = conn.execute("SELECT COUNT(*) FROM market_quotes").fetchone()[0]
                        scan_count = conn.execute("SELECT COUNT(*) FROM scan_runs").fetchone()[0]
                except Exception:
                    quote_count, scan_count = None, None
                data_health = (
                    "not_configured" if not FEEDS else
                    "no_usable_quotes" if s["last_scan_status"] == "no_usable_quotes" else
                    "awaiting_first_scan" if s["last_scan_status"] == "not_run" else
                    "checked"
                )
                return send_json(self, 200, {
                    "status": "online", "service": APP_NAME, "version": VERSION,
                    "read_only": True, "configured_market_feed_count": len(FEEDS),
                    "database_path_configured": bool(DB_PATH),
                    "stored_quote_count": quote_count, "stored_scan_count": scan_count,
                    "data_health": data_health,
                    "last_scan_at": s["last_scan_at"],
                    "last_scan_status": s["last_scan_status"],
                    "last_lookup_at": s["last_lookup_at"],
                    "last_lookup_status": s["last_lookup_status"],
                    "last_error": s["last_error"],
                    "note": "Online status does not prove live bookmaker or feed availability."
                })
            if path == "/api/history":
                return send_json(self, 200, {"runs": read_history(), "count": len(read_history())})
            if path == "/api/quotes":
                return send_json(self, 200, {"quotes": read_quotes(), "count": len(read_quotes())})
            return send_json(self, 404, {"error": "not_found"})

        if method == "POST":
            try:
                payload = self.read_payload()
            except ValueError as exc:
                return send_json(self, 400, {"error": "bad_request", "message": str(exc)})

            if path == "/api/scan":
                return send_json(self, 200, run_scan())

            if path == "/api/lookup":
                book = clean(payload.get("bookmaker"), 40) or "sportybet"
                if book.lower() not in ("sportybet", "sporty_bet"):
                    return send_json(self, 400, {
                        "ok": False, "status": "unsupported_bookmaker",
                        "message": "Only SportyBet lookup is implemented."
                    })
                result = lookup_code(payload.get("code"))
                with STATE_LOCK:
                    STATE["last_lookup_status"] = result.get("status", "error")
                    STATE["last_lookup_at"] = now()
                # Do not persist or log the submitted code.
                if result.get("ok"):
                    result["market_comparison_available"] = False
                    result["market_comparison_note"] = (
                        "Use the Market Scanner for feed-based comparisons. "
                        "Ticket legs are not matched to market quotes in this build."
                    )
                return send_json(self, 200, result)

            if path == "/api/watchlist":
                # No personal information is required. A client key is supplied
                # by the browser; this endpoint stores only an event label.
                client_key = clean(payload.get("client_key"), 80)
                event = clean(payload.get("event"), 300)
                event_id = clean(payload.get("event_id"), 160) or ""
                if not client_key or not event:
                    return send_json(self, 400, {
                        "error": "client_key_and_event_required"
                    })
                with DB_LOCK, db() as conn:
                    conn.execute("""
                        INSERT OR IGNORE INTO watchlist
                        (client_key,event_id,event,created_at) VALUES (?,?,?,?)
                    """, (client_key, event_id, event, now()))
                    conn.commit()
                return send_json(self, 200, {"ok": True, "status": "saved"})
            return send_json(self, 404, {"error": "not_found"})

    def do_GET(self):
        return self._dispatch("GET")

    def do_HEAD(self):
        return self._dispatch("HEAD")

    def do_POST(self):
        return self._dispatch("POST")


def main():
    init_db()
    with STATE_LOCK:
        STATE["started_at"] = now()

    server = ThreadingHTTPServer((HOST, PORT), Handler)
    server.daemon_threads = True

    if SCAN_INTERVAL:
        thread = threading.Thread(target=scheduler, daemon=True, name="scan-scheduler")
        thread.start()

    print(f"{APP_NAME} v{VERSION} starting on {HOST}:{PORT}")
    print(f"Configured market feeds: {len(FEEDS)}")
    print(f"SQLite path: {DB_PATH}")
    print(f"Scheduled scan interval: {SCAN_INTERVAL} seconds")
    print("Read-only mode; no bet placement or automated staking.")

    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        print("Server stopped.")


if __name__ == "__main__":
    main()
