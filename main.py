#!/usr/bin/env python3
"""
SolveEase Surebet Intelligence
Version 7.0.0

Read-only football market research:
- Server-rendered multi-page navigation
- SQLite scan history and odds snapshots
- Safe database-path fallback
- Background scheduler with non-overlapping scans
- Configured HTTPS market feeds only
- Conservative two-outcome arbitrage candidate detection
- Best-effort SportyBet share-code lookup (unverified endpoint)
- Bounded HTTP requests and basic per-IP rate limiting
- Responsive mobile UI and installable PWA shell

No betting, bookmaker login, staking, or guaranteed-profit claims.
"""

import os
import re
import json
import time
import html
import sqlite3
import logging
import threading
import ipaddress
import socket
from datetime import datetime, timezone
from urllib.parse import urlparse, parse_qs
from urllib.request import Request, urlopen
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from collections import defaultdict
from itertools import groupby


# ------------------------------------------------------------
# Configuration
# ------------------------------------------------------------

APP_NAME = "SolveEase Surebet Intelligence"
VERSION = "7.0.0"

HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "10000"))

REQUEST_TIMEOUT_SECONDS = max(
    2, min(20, int(os.environ.get("REQUEST_TIMEOUT_SECONDS", "8")))
)
MAX_RESPONSE_BYTES = max(
    100_000,
    min(8_000_000, int(os.environ.get("MAX_RESPONSE_BYTES", "4000000")))
)
MAX_REQUEST_BYTES = 256_000
MAX_FEEDS = max(0, min(10, int(os.environ.get("MAX_FEEDS", "10"))))
SCAN_INTERVAL_SECONDS = max(
    60, min(86_400, int(os.environ.get("SCAN_INTERVAL_SECONDS", "900")))
)
QUOTE_RETENTION_DAYS = max(
    1, min(365, int(os.environ.get("QUOTE_RETENTION_DAYS", "90")))
)
ENABLE_SPORTYBET_LOOKUP = (
    os.environ.get("ENABLE_SPORTYBET_LOOKUP", "true").lower()
    in ("1", "true", "yes", "on")
)

MARKET_FEED_URLS = [
    value.strip()
    for value in os.environ.get("MARKET_FEED_URLS", "").split(";")
    if value.strip()
][:MAX_FEEDS]

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("solveease")

STATE_LOCK = threading.RLock()
SCAN_LOCK = threading.Lock()
RATE_LOCK = threading.Lock()

STATE = {
    "status": "starting",
    "version": VERSION,
    "started_at": None,
    "last_scan_started_at": None,
    "last_scan_finished_at": None,
    "last_scan_status": "not_run",
    "last_lookup_status": "not_run",
    "last_scan_summary": {},
    "configured_market_feed_count": len(MARKET_FEED_URLS),
    "note": "Service health does not prove live odds-feed availability.",
}

# Basic in-memory rate limiter. For a multi-instance deployment, use a
# shared rate limiter at the edge; this one is intentionally lightweight.
RATE_LIMITS = {
    "GET": (120, 60),
    "POST": (20, 60),
}
REQUEST_TIMES = defaultdict(list)


# ------------------------------------------------------------
# General helpers
# ------------------------------------------------------------

def utc_now():
    return datetime.now(timezone.utc).isoformat()


def clean_string(value, limit=250):
    if value is None:
        return ""
    text = str(value).strip()
    text = re.sub(r"[\x00-\x1f\x7f]", " ", text)
    return text[:limit]


def safe_float(value, default=None):
    try:
        number = float(value)
        if number != number or number in (float("inf"), float("-inf")):
            return default
        return number
    except (TypeError, ValueError, OverflowError):
        return default


def valid_odds(value):
    number = safe_float(value)
    return number is not None and 1.01 <= number <= 1000.0


def implied_probability(decimal_odds):
    if not valid_odds(decimal_odds):
        return None
    return 1.0 / float(decimal_odds)


def normalise_text(value):
    return re.sub(r"\s+", " ", clean_string(value, 500).lower()).strip()


def escape(value):
    return html.escape(str(value if value is not None else ""), quote=True)


def json_bytes(data):
    return json.dumps(
        data, ensure_ascii=False, separators=(",", ":"), default=str
    ).encode("utf-8")


def host_of(url):
    try:
        return (urlparse(url).hostname or "")[:200]
    except Exception:
        return ""


def public_https_url(url):
    """
    Conservative URL validation for administrator-configured feeds.

    It rejects non-HTTPS URLs, credentials, localhost and IP literals that
    are not public. For production with high threat exposure, additionally
    restrict feed hostnames to an explicit allowlist and enforce outbound
    network controls to mitigate DNS rebinding.
    """
    try:
        parsed = urlparse(url)
        if parsed.scheme.lower() != "https":
            return False
        if not parsed.hostname or parsed.username or parsed.password:
            return False
        host = parsed.hostname.lower().rstrip(".")
        if host in ("localhost", "localhost.localdomain"):
            return False
        if host.endswith((".local", ".internal", ".localhost")):
            return False

        try:
            ip = ipaddress.ip_address(host.strip("[]"))
            if not ip.is_global:
                return False
        except ValueError:
            # Resolve names and reject private/local destinations.
            try:
                addresses = socket.getaddrinfo(
                    host, parsed.port or 443, type=socket.SOCK_STREAM
                )
                if not addresses:
                    return False
                for address in addresses:
                    resolved = ipaddress.ip_address(address[4][0])
                    if not resolved.is_global:
                        return False
            except OSError:
                return False

        return True
    except Exception:
        return False


def fetch_json(url):
    if not public_https_url(url):
        raise ValueError("Feed URL failed HTTPS/public-host validation.")

    request = Request(
        url,
        headers={
            "User-Agent": "SolveEase-Surebet-Intelligence/7.0",
            "Accept": "application/json",
        },
        method="GET",
    )

    with urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
        content_type = response.headers.get("Content-Type", "").lower()
        body = response.read(MAX_RESPONSE_BYTES + 1)

    if len(body) > MAX_RESPONSE_BYTES:
        raise ValueError("Feed response exceeded configured size limit.")

    # Some providers incorrectly omit the JSON content type; JSON parsing
    # remains the final check.
    try:
        return json.loads(body.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Feed did not return valid JSON.") from exc


# ------------------------------------------------------------
# Database selection and schema
# ------------------------------------------------------------

def resolve_database_path():
    configured = os.environ.get(
        "DATABASE_PATH", "/var/data/solveease.db"
    ).strip() or "/var/data/solveease.db"

    candidates = [configured]

    # A writable temporary fallback prevents a missing Render disk from
    # making the whole service fail to start. It is not persistent.
    fallback = "/tmp/solveease.db"
    if os.path.abspath(configured) != os.path.abspath(fallback):
        candidates.append(fallback)

    for candidate in candidates:
        try:
            parent = os.path.dirname(os.path.abspath(candidate))
            os.makedirs(parent, exist_ok=True)
            probe = os.path.join(parent, ".solveease-write-test")
            with open(probe, "w", encoding="utf-8") as handle:
                handle.write("ok")
            os.remove(probe)

            if candidate == configured:
                log.info("Database path selected: %s", candidate)
            else:
                log.error(
                    "Preferred database path is unavailable: %s. "
                    "Using temporary storage at %s; history may be lost "
                    "when the instance restarts.",
                    configured, candidate,
                )
            return candidate
        except OSError as exc:
            log.error("Database path not writable (%s): %s", candidate, exc)

    # Last-resort in-memory SQLite lets the web service start, but data
    # lasts only for the lifetime of the process.
    log.critical(
        "No writable database path found; using in-memory SQLite. "
        "All stored history will be lost on restart."
    )
    return ":memory:"


DATABASE_PATH = resolve_database_path()
DB_LOCK = threading.RLock()
_MEMORY_CONNECTION = None


def db_connect():
    global _MEMORY_CONNECTION

    if DATABASE_PATH == ":memory:":
        if _MEMORY_CONNECTION is None:
            _MEMORY_CONNECTION = sqlite3.connect(
                ":memory:", timeout=10, check_same_thread=False
            )
            _MEMORY_CONNECTION.row_factory = sqlite3.Row
        return _MEMORY_CONNECTION

    connection = sqlite3.connect(
        DATABASE_PATH, timeout=10, check_same_thread=False
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=10000")
    connection.execute("PRAGMA journal_mode=WAL")
    return connection


def init_db():
    with DB_LOCK:
        connection = db_connect()
        try:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS scan_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    status TEXT NOT NULL,
                    feed_count INTEGER NOT NULL DEFAULT 0,
                    feeds_reached INTEGER NOT NULL DEFAULT 0,
                    quote_count INTEGER NOT NULL DEFAULT 0,
                    candidate_count INTEGER NOT NULL DEFAULT 0,
                    summary_json TEXT NOT NULL DEFAULT '{}'
                );

                CREATE TABLE IF NOT EXISTS market_quotes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event TEXT NOT NULL,
                    event_id TEXT NOT NULL DEFAULT '',
                    sport TEXT NOT NULL DEFAULT 'Football',
                    market TEXT NOT NULL,
                    selection TEXT NOT NULL,
                    odds REAL NOT NULL,
                    bookmaker TEXT NOT NULL DEFAULT '',
                    source_host TEXT NOT NULL DEFAULT '',
                    source_updated_at TEXT NOT NULL DEFAULT '',
                    captured_at TEXT NOT NULL,
                    exhaustive_two_outcome INTEGER NOT NULL DEFAULT 0
                );

                CREATE INDEX IF NOT EXISTS idx_quotes_event
                    ON market_quotes(event_id, event, market);

                CREATE INDEX IF NOT EXISTS idx_quotes_captured
                    ON market_quotes(captured_at);

                CREATE TABLE IF NOT EXISTS lookup_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    outcome_count INTEGER NOT NULL DEFAULT 0,
                    note TEXT NOT NULL DEFAULT ''
                );
            """)
            connection.commit()
        finally:
            if DATABASE_PATH != ":memory:":
                connection.close()


def db_execute(sql, params=()):
    with DB_LOCK:
        connection = db_connect()
        try:
            cursor = connection.execute(sql, params)
            connection.commit()
            return cursor.lastrowid, cursor.rowcount
        finally:
            if DATABASE_PATH != ":memory:":
                connection.close()


def db_query(sql, params=()):
    with DB_LOCK:
        connection = db_connect()
        try:
            rows = connection.execute(sql, params).fetchall()
            return [dict(row) for row in rows]
        finally:
            if DATABASE_PATH != ":memory:":
                connection.close()


def store_scan(started_at, finished_at, status, feed_count,
               feeds_reached, quote_count, candidate_count, summary):
    db_execute(
        """
        INSERT INTO scan_runs
        (started_at, finished_at, status, feed_count, feeds_reached,
         quote_count, candidate_count, summary_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            started_at, finished_at, status, feed_count, feeds_reached,
            quote_count, candidate_count,
            json.dumps(summary, ensure_ascii=False, default=str),
        ),
    )


def store_quotes(quotes):
    if not quotes:
        return 0

    captured_at = utc_now()
    inserted = 0

    with DB_LOCK:
        connection = db_connect()
        try:
            for quote in quotes:
                connection.execute(
                    """
                    INSERT INTO market_quotes
                    (event, event_id, sport, market, selection, odds,
                     bookmaker, source_host, source_updated_at,
                     captured_at, exhaustive_two_outcome)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        quote["event"],
                        quote.get("event_id", ""),
                        quote.get("sport", "Football"),
                        quote["market"],
                        quote["selection"],
                        quote["odds"],
                        quote.get("bookmaker", ""),
                        quote.get("source_host", ""),
                        quote.get("updated_at", ""),
                        captured_at,
                        1 if quote.get("exhaustive_two_outcome") else 0,
                    ),
                )
                inserted += 1

            cutoff = datetime.fromtimestamp(
                time.time() - QUOTE_RETENTION_DAYS * 86400,
                timezone.utc,
            ).isoformat()

            connection.execute(
                "DELETE FROM market_quotes WHERE captured_at < ?", (cutoff,)
            )
            connection.commit()
        finally:
            if DATABASE_PATH != ":memory:":
                connection.close()

    return inserted


# ------------------------------------------------------------
# Market feed parsing
# ------------------------------------------------------------

def first_value(mapping, keys, default=None):
    if not isinstance(mapping, dict):
        return default
    for key in keys:
        if key in mapping and mapping[key] not in (None, ""):
            return mapping[key]
    return default


def extract_feed_quotes(payload, source_host):
    """
    Supports simple JSON feeds with a markets, quotes, or data list.
    A feed must explicitly mark a market as exhaustive_two_outcome=true
    before its two outcomes are eligible for arbitrage-candidate checks.
    """
    if isinstance(payload, list):
        records = payload
    elif isinstance(payload, dict):
        records = first_value(payload, ["markets", "quotes", "data"], [])
        if isinstance(records, dict):
            records = first_value(records, ["markets", "quotes", "data"], [])
    else:
        records = []

    if not isinstance(records, list):
        return []

    output = []

    for item in records[:100_000]:
        if not isinstance(item, dict):
            continue

        event = clean_string(first_value(
            item, ["event", "fixture", "match", "event_name", "name"], ""
        ), 250)
        event_id = clean_string(first_value(
            item, ["event_id", "fixture_id", "id"], ""
        ), 150)
        sport = clean_string(first_value(
            item, ["sport", "sport_name"], "Football"
        ), 60)
        market = clean_string(first_value(
            item, ["market", "market_name", "market_type"], ""
        ), 150)
        bookmaker = clean_string(first_value(
            item, ["bookmaker", "bookmaker_name", "provider"], ""
        ), 120)
        updated_at = clean_string(first_value(
            item, ["updated_at", "last_update", "timestamp"], ""
        ), 80)
        exhaustive = item.get("exhaustive_two_outcome") is True

        outcomes = first_value(item, ["outcomes", "selections"], None)

        if isinstance(outcomes, list):
            for outcome in outcomes[:100]:
                if not isinstance(outcome, dict):
                    continue
                selection = clean_string(first_value(
                    outcome, ["selection", "name", "label", "outcome"], ""
                ), 150)
                odds = first_value(
                    outcome, ["odds", "price", "decimal_odds"], None
                )
                if event and market and selection and valid_odds(odds):
                    output.append({
                        "event": event,
                        "event_id": event_id,
                        "sport": sport or "Football",
                        "market": market,
                        "selection": selection,
                        "odds": float(odds),
                        "bookmaker": bookmaker,
                        "source_host": source_host,
                        "updated_at": updated_at,
                        "exhaustive_two_outcome": exhaustive,
                    })
            continue

        # Flat-quote format.
        selection = clean_string(first_value(
            item, ["selection", "outcome", "runner"], ""
        ), 150)
        odds = first_value(item, ["odds", "price", "decimal_odds"], None)

        if event and market and selection and valid_odds(odds):
            output.append({
                "event": event,
                "event_id": event_id,
                "sport": sport or "Football",
                "market": market,
                "selection": selection,
                "odds": float(odds),
                "bookmaker": bookmaker,
                "source_host": source_host,
                "updated_at": updated_at,
                "exhaustive_two_outcome": exhaustive,
            })

    return output


def fetch_configured_markets():
    all_quotes = []
    health = []
    reached = 0

    for url in MARKET_FEED_URLS:
        host = host_of(url)
        record = {
            "host": host or "unknown",
            "status": "not_reached",
            "quotes": 0,
            "message": "",
        }

        try:
            payload = fetch_json(url)
            quotes = extract_feed_quotes(payload, host)
            all_quotes.extend(quotes)
            reached += 1
            record["status"] = "reached"
            record["quotes"] = len(quotes)
            record["message"] = (
                "JSON parsed; quote usability depends on provider schema."
            )
        except Exception as exc:
            record["status"] = "error"
            # Do not log or return the complete URL; it may contain tokens.
            record["message"] = clean_string(str(exc), 220)
            log.warning("Market feed %s failed: %s", host, record["message"])

        health.append(record)

    return all_quotes, health, reached


# ------------------------------------------------------------
# Conservative arbitrage candidate scan
# ------------------------------------------------------------

def scan_arbitrage(quotes):
    """
    Only checks explicit exhaustive two-outcome markets.

    It does not assume that Home/Draw/Away markets are exhaustive two-way
    markets. It also requires named, different bookmakers and two distinct
    selections. Candidate output is a research lead, not a bet instruction.
    """
    groups = defaultdict(list)

    for quote in quotes:
        if not quote.get("exhaustive_two_outcome"):
            continue

        event_key = quote.get("event_id") or normalise_text(quote["event"])
        key = (
            event_key,
            normalise_text(quote["event"]),
            normalise_text(quote["market"]),
        )
        groups[key].append(quote)

    candidates = []

    for key, group in groups.items():
        selection_quotes = defaultdict(list)
        for quote in group:
            selection_quotes[normalise_text(quote["selection"])].append(quote)

        if len(selection_quotes) != 2:
            continue

        selections = list(selection_quotes.values())
        best = []

        for options in selections:
            # Only use one best price per named bookmaker.
            named = [
                q for q in options
                if clean_string(q.get("bookmaker")) and valid_odds(q["odds"])
            ]
            if not named:
                best = []
                break
            best.append(max(named, key=lambda q: q["odds"]))

        if len(best) != 2:
            continue
        if normalise_text(best[0]["selection"]) == normalise_text(
            best[1]["selection"]
        ):
            continue
        if normalise_text(best[0]["bookmaker"]) == normalise_text(
            best[1]["bookmaker"]
        ):
            continue

        inverse_sum = sum(1.0 / q["odds"] for q in best)
        if inverse_sum >= 1.0:
            continue

        candidates.append({
            "event": best[0]["event"],
            "market": best[0]["market"],
            "outcomes": [
                {
                    "selection": q["selection"],
                    "odds": q["odds"],
                    "bookmaker": q["bookmaker"],
                    "source": q.get("source_host", ""),
                }
                for q in best
            ],
            "implied_probability_sum": round(inverse_sum, 6),
            "theoretical_margin_percent": round(
                (1.0 - inverse_sum) * 100.0, 3
            ),
            "status": "candidate_requires_manual_verification",
            "warning": (
                "Verify market rules, settlement, limits, timestamps, "
                "price availability, and independent bookmaker access."
            ),
        })

    return sorted(
        candidates,
        key=lambda item: item["theoretical_margin_percent"],
        reverse=True,
    )


def run_scan(trigger="manual"):
    if not SCAN_LOCK.acquire(blocking=False):
        return {
            "status": "already_running",
            "message": "A scan is already running.",
        }

    started = utc_now()
    with STATE_LOCK:
        STATE["last_scan_started_at"] = started
        STATE["last_scan_status"] = "running"

    try:
        quotes, feed_health, reached = fetch_configured_markets()
        candidates = scan_arbitrage(quotes)

        stored = store_quotes(quotes)
        finished = utc_now()

        if not MARKET_FEED_URLS:
            status = "no_feeds_configured"
        elif reached == 0:
            status = "feeds_unavailable"
        elif not quotes:
            status = "no_usable_quotes"
        else:
            status = "completed"

        summary = {
            "trigger": trigger,
            "status": status,
            "started_at": started,
            "finished_at": finished,
            "configured_feed_count": len(MARKET_FEED_URLS),
            "feeds_reached": reached,
            "quote_count": len(quotes),
            "snapshots_stored": stored,
            "candidate_count": len(candidates),
            "feed_health": feed_health,
            "candidates": candidates[:100],
        }

        store_scan(
            started, finished, status, len(MARKET_FEED_URLS),
            reached, len(quotes), len(candidates), summary
        )

        with STATE_LOCK:
            STATE["last_scan_finished_at"] = finished
            STATE["last_scan_status"] = status
            STATE["last_scan_summary"] = summary
            STATE["status"] = "online"

        log.info(
            "Scan finished: trigger=%s status=%s feeds=%s/%s quotes=%s "
            "candidates=%s",
            trigger, status, reached, len(MARKET_FEED_URLS),
            len(quotes), len(candidates),
        )
        return summary

    except Exception as exc:
        finished = utc_now()
        summary = {
            "trigger": trigger,
            "status": "error",
            "started_at": started,
            "finished_at": finished,
            "error": clean_string(str(exc), 300),
            "configured_feed_count": len(MARKET_FEED_URLS),
        }

        try:
            store_scan(
                started, finished, "error", len(MARKET_FEED_URLS),
                0, 0, 0, summary
            )
        except Exception:
            log.exception("Could not store failed scan.")

        with STATE_LOCK:
            STATE["last_scan_finished_at"] = finished
            STATE["last_scan_status"] = "error"
            STATE["last_scan_summary"] = summary
            STATE["status"] = "online"

        log.exception("Scan failed.")
        return summary

    finally:
        SCAN_LOCK.release()


def scheduler_loop():
    log.info("Background scan scheduler enabled: interval=%s seconds",
             SCAN_INTERVAL_SECONDS)

    # Start with a brief delay so the HTTP server can initialize.
    time.sleep(5)

    while True:
        try:
            run_scan(trigger="scheduler")
        except Exception:
            log.exception("Scheduler cycle failed.")
        time.sleep(SCAN_INTERVAL_SECONDS)


# ------------------------------------------------------------
# SportyBet best-effort share-code lookup
# ------------------------------------------------------------

def walk_dicts(value, depth=0):
    if depth > 12:
        return
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk_dicts(child, depth + 1)
    elif isinstance(value, list):
        for child in value[:500]:
            yield from walk_dicts(child, depth + 1)


def parse_sportybet_outcomes(payload):
    """
    Heuristic parser only. The share endpoint/schema is unofficial and
    unverified; a response may not be parseable or may change at any time.
    """
    results = []

    for item in walk_dicts(payload):
        event = first_value(
            item, ["eventName", "matchName", "fixtureName", "event_name"]
        )
        market = first_value(
            item, ["marketName", "market_name", "marketDesc"]
        )
        selection = first_value(
            item, ["outcomeName", "selectionName", "specifierName",
                   "outcome_name", "selection"]
        )
        odds = first_value(
            item, ["odds", "oddValue", "oddsValue", "decimalOdds"]
        )

        if event and market and selection and valid_odds(odds):
            results.append({
                "event": clean_string(event, 250),
                "market": clean_string(market, 150),
                "selection": clean_string(selection, 150),
                "odds": float(odds),
            })

    # Remove repeated nested representations.
    unique = {}
    for item in results:
        key = (
            normalise_text(item["event"]),
            normalise_text(item["market"]),
            normalise_text(item["selection"]),
            item["odds"],
        )
        unique[key] = item

    return list(unique.values())[:500]


def lookup_sportybet(code):
    # Never log or persist the submitted share code.
    if not ENABLE_SPORTYBET_LOOKUP:
        return {
            "status": "disabled",
            "message": "SportyBet lookup is disabled by configuration.",
            "outcomes": [],
        }

    code = clean_string(code, 160)
    if not re.fullmatch(r"[A-Za-z0-9_-]{4,160}", code):
        return {
            "status": "invalid_code_format",
            "message": "The share code format was not accepted.",
            "outcomes": [],
        }

    # Unofficial endpoint. Its availability and schema are not verified.
    url = "https://www.sportybet.com/api/ng/orders/share/" + code

    try:
        payload = fetch_json(url)
        outcomes = parse_sportybet_outcomes(payload)
        status = "parsed" if outcomes else "response_unrecognised"

        db_execute(
            "INSERT INTO lookup_events "
            "(created_at, status, outcome_count, note) VALUES (?, ?, ?, ?)",
            (
                utc_now(), status, len(outcomes),
                "Share code not stored; unofficial endpoint unverified.",
            ),
        )

        with STATE_LOCK:
            STATE["last_lookup_status"] = status

        return {
            "status": status,
            "endpoint_live_verified": False,
            "message": (
                "Best-effort response parsing only. This endpoint is "
                "unofficial and may change."
            ),
            "outcomes": outcomes,
        }

    except Exception as exc:
        status = "lookup_failed"
        db_execute(
            "INSERT INTO lookup_events "
            "(created_at, status, outcome_count, note) VALUES (?, ?, ?, ?)",
            (
                utc_now(), status, 0,
                "Share code not stored. " + clean_string(str(exc), 180),
            ),
        )
        with STATE_LOCK:
            STATE["last_lookup_status"] = status

        return {
            "status": status,
            "endpoint_live_verified": False,
            "message": (
                "Lookup failed. The code was not stored. The endpoint "
                "or response format may be unavailable or unsupported."
            ),
            "outcomes": [],
        }


# ------------------------------------------------------------
# Analysis endpoint
# ------------------------------------------------------------

def analyse_ticket(payload):
    """
    Analyse a small user-submitted list of selections without saving it.
    This endpoint does not place bets or verify market availability.
    """
    if not isinstance(payload, dict):
        raise ValueError("Expected a JSON object.")

    picks = payload.get("picks")
    if not isinstance(picks, list) or not 1 <= len(picks) <= 50:
        raise ValueError("Provide between 1 and 50 picks.")

    combined_odds = 1.0
    clean_picks = []

    for pick in picks:
        if not isinstance(pick, dict):
            raise ValueError("Each pick must be an object.")

        odds = safe_float(pick.get("odds"))
        if not valid_odds(odds):
            raise ValueError("Each pick needs decimal odds between 1.01 and 1000.")

        combined_odds *= odds
        clean_picks.append({
            "event": clean_string(pick.get("event"), 200),
            "selection": clean_string(pick.get("selection"), 150),
            "odds": odds,
        })

    return {
        "status": "calculated",
        "pick_count": len(clean_picks),
        "combined_decimal_odds": round(combined_odds, 6),
        "break_even_probability_percent": round(
            100.0 / combined_odds, 6
        ),
        "picks": clean_picks,
        "warning": (
            "Mathematical break-even only. This is not a forecast of "
            "success; selections may be correlated and odds can change."
        ),
        "stored": False,
    }


# ------------------------------------------------------------
# Health, state and history
# ------------------------------------------------------------

def data_health():
    quote_rows = db_query(
        "SELECT COUNT(*) AS n FROM market_quotes"
    )
    scan_rows = db_query(
        "SELECT COUNT(*) AS n FROM scan_runs"
    )
    latest_quotes = db_query(
        "SELECT MAX(captured_at) AS latest FROM market_quotes"
    )

    with STATE_LOCK:
        last_status = STATE["last_scan_status"]
        last_finished = STATE["last_scan_finished_at"]

    return {
        "database_path": DATABASE_PATH,
        "database_persistent": (
            DATABASE_PATH != ":memory:"
            and not DATABASE_PATH.startswith("/tmp/")
        ),
        "configured_market_feed_count": len(MARKET_FEED_URLS),
        "stored_quote_snapshot_rows": quote_rows[0]["n"],
        "stored_scan_runs": scan_rows[0]["n"],
        "latest_quote_capture_at": latest_quotes[0]["latest"],
        "last_scan_status": last_status,
        "last_scan_finished_at": last_finished,
        "quote_retention_days": QUOTE_RETENTION_DAYS,
        "note": (
            "Snapshot history begins when this version stores quotes. "
            "No feed configuration means no live market prices."
        ),
    }


def health_payload():
    with STATE_LOCK:
        state_copy = dict(STATE)

    return {
        "status": "online",
        "service": APP_NAME,
        "version": VERSION,
        "read_only": True,
        "configured_market_feed_count": len(MARKET_FEED_URLS),
        "last_scan_status": state_copy["last_scan_status"],
        "last_lookup_status": state_copy["last_lookup_status"],
        "database_path": DATABASE_PATH,
        "database_persistent": (
            DATABASE_PATH != ":memory:"
            and not DATABASE_PATH.startswith("/tmp/")
        ),
        "note": "Online status does not prove bookmaker endpoint or feed availability.",
    }


def get_history(limit=30):
    limit = max(1, min(100, int(limit)))
    rows = db_query(
        """
        SELECT id, started_at, finished_at, status, feed_count,
               feeds_reached, quote_count, candidate_count, summary_json
        FROM scan_runs
        ORDER BY id DESC
        LIMIT ?
        """,
        (limit,),
    )

    for row in rows:
        try:
            row["summary"] = json.loads(row.pop("summary_json") or "{}")
        except Exception:
            row["summary"] = {}
            row.pop("summary_json", None)

    return rows


# ------------------------------------------------------------
# PWA resources
# ------------------------------------------------------------

MANIFEST = {
    "name": APP_NAME,
    "short_name": "SolveEase",
    "description": "Read-only football market research and odds analysis.",
    "start_url": "/",
    "scope": "/",
    "display": "standalone",
    "background_color": "#08111f",
    "theme_color": "#08111f",
    "icons": [
        {
            "src": "/icon.svg",
            "sizes": "any",
            "type": "image/svg+xml",
            "purpose": "any maskable",
        }
    ],
}

ICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 128 128">
<rect width="128" height="128" rx="28" fill="#08111f"/>
<path d="M30 38h68M30 64h46M30 90h58" stroke="#69e0bb"
stroke-width="9" stroke-linecap="round"/>
<circle cx="94" cy="64" r="12" fill="#69e0bb"/>
</svg>"""

SERVICE_WORKER = r"""
const CACHE = "solveease-shell-v7";
const SHELL = ["/", "/manifest.webmanifest", "/icon.svg"];
self.addEventListener("install", event => {
  event.waitUntil(caches.open(CACHE).then(cache => cache.addAll(SHELL)));
  self.skipWaiting();
});
self.addEventListener("activate", event => {
  event.waitUntil(
    caches.keys().then(keys => Promise.all(
      keys.filter(key => key !== CACHE).map(key => caches.delete(key))
    ))
  );
  self.clients.claim();
});
self.addEventListener("fetch", event => {
  const url = new URL(event.request.url);
  if (url.origin !== self.location.origin) return;
  if (url.pathname.startsWith("/api/") ||
      ["/health", "/healthz", "/state"].includes(url.pathname)) return;

  if (event.request.method !== "GET") return;
  event.respondWith(
    fetch(event.request).then(response => {
      if (response.ok && (url.pathname === "/" ||
          url.pathname === "/manifest.webmanifest" ||
          url.pathname === "/icon.svg")) {
        const copy = response.clone();
        caches.open(CACHE).then(cache => cache.put(event.request, copy));
      }
      return response;
    }).catch(() => caches.match(event.request))
  );
});
"""


# ------------------------------------------------------------
# Server-rendered multi-page website
# ------------------------------------------------------------

NAV_ITEMS = [
    ("/", "Overview"),
    ("/scanner", "Market scanner"),
    ("/matches", "Match intelligence"),
    ("/risk-lab", "Accumulator lab"),
    ("/history", "Scan history"),
    ("/data-health", "Data health"),
    ("/reports", "Research & reports"),
]


def page_content(path):
    if path == "/":
        return "Overview", """
        <section class="hero">
          <p class="eyebrow">READ-ONLY MARKET RESEARCH</p>
          <h1>See the market clearly.</h1>
          <p class="lead">Review configured odds sources, inspect scan results,
          and calculate accumulator mathematics without placing bets.</p>
          <div class="actions">
            <a class="button primary" href="/scanner">Open market scanner</a>
            <a class="button secondary" href="/risk-lab">Open accumulator lab</a>
          </div>
        </section>
        <section class="grid metrics">
          <article class="metric"><span>Service</span><strong id="service-status">Loading…</strong></article>
          <article class="metric"><span>Configured feeds</span><strong id="feed-count">—</strong></article>
          <article class="metric"><span>Last scan</span><strong id="scan-status">—</strong></article>
          <article class="metric"><span>Stored snapshots</span><strong id="snapshot-count">—</strong></article>
        </section>
        <section class="panel">
          <div class="section-heading"><div><p class="eyebrow">CURRENT REALITY</p>
          <h2>What the system can verify</h2></div></div>
          <p id="reality">Checking service data…</p>
          <p class="muted">A live website does not mean a live bookmaker feed is connected.
          Candidate prices require manual verification before any real-world decision.</p>
        </section>
        <section class="grid cards">
          <a class="feature" href="/scanner"><span class="feature-icon">↗</span>
            <h3>Market scanner</h3><p>Inspect feed health and scan for conservative two-outcome candidates.</p></a>
          <a class="feature" href="/matches"><span class="feature-icon">◎</span>
            <h3>Match intelligence</h3><p>Review available market records. Team form and H2H require historical data.</p></a>
          <a class="feature" href="/risk-lab"><span class="feature-icon">∑</span>
            <h3>Accumulator lab</h3><p>Calculate combined decimal odds and break-even probability.</p></a>
          <a class="feature" href="/history"><span class="feature-icon">◷</span>
            <h3>Scan history</h3><p>View saved scan runs and snapshots from this deployment onward.</p></a>
        </section>
        """

    if path == "/scanner":
        return "Market scanner", """
        <section class="page-intro"><p class="eyebrow">MARKET MONITORING</p>
        <h1>Market scanner</h1>
        <p class="lead">Fetch configured feeds and inspect scan results. A manual scan
        may take several seconds depending on provider response times.</p></section>
        <section class="panel">
          <div class="actions"><button class="button primary" id="run-scan">Run scan now</button>
          <button class="button secondary" id="refresh-scan">Refresh status</button></div>
          <p id="scan-message" class="notice">Ready to check feed availability.</p>
          <div id="feed-list"></div>
          <div id="candidate-list"></div>
        </section>
        """

    if path == "/matches":
        return "Match intelligence", """
        <section class="page-intro"><p class="eyebrow">FIXTURES & MARKET CONTEXT</p>
        <h1>Match intelligence</h1>
        <p class="lead">This page only displays information available from configured
        feeds. It does not invent fixtures, team form, head-to-head results, or predictions.</p></section>
        <section class="panel"><h2>Available market records</h2>
        <p id="match-data-note">Loading data health…</p>
        <p class="muted">Fixture lists, team form, H2H and validated probability forecasts
        need suitable authorized sources and validated models; they are not generated here.</p>
        <a class="button secondary" href="/data-health">Inspect data health</a></section>
        """

    if path == "/risk-lab":
        return "Accumulator lab", """
        <section class="page-intro"><p class="eyebrow">MATHEMATICAL TOOL</p>
        <h1>Accumulator lab</h1>
        <p class="lead">Calculate combined odds and theoretical break-even probability.
        This is not a prediction or a recommendation to place a bet.</p></section>
        <section class="panel">
          <label for="pick-count">Number of selections</label>
          <select id="pick-count">
            <option value="1">1 selection</option><option value="2">2 selections</option>
            <option value="3" selected>3 selections</option><option value="4">4 selections</option>
            <option value="5">5 selections</option><option value="6">6 selections</option>
            <option value="7">7 selections</option><option value="8">8 selections</option>
            <option value="9">9 selections</option><option value="10">10 selections</option>
          </select>
          <div id="odds-inputs" class="odds-grid"></div>
          <button class="button primary" id="calculate">Calculate</button>
          <div id="calculation-result" class="result-box" aria-live="polite"></div>
          <p class="muted">Actual outcomes can be dependent, odds can change, and bookmaker
          settlement rules may differ. The calculation does not estimate true win probability.</p>
        </section>
        """

    if path == "/history":
        return "Scan history", """
        <section class="page-intro"><p class="eyebrow">AUDIT TRAIL</p>
        <h1>Scan history</h1>
        <p class="lead">Saved scan summaries, source reachability and quote counts.</p></section>
        <section class="panel"><button class="button secondary" id="refresh-history">Refresh history</button>
        <div id="history-list" class="table-wrap"></div></section>
        """

    if path == "/data-health":
        return "Data health", """
        <section class="page-intro"><p class="eyebrow">SOURCE TRANSPARENCY</p>
        <h1>Data health</h1>
        <p class="lead">Check whether feeds are configured, whether quote snapshots exist,
        and whether the database path is persistent.</p></section>
        <section class="panel"><button class="button secondary" id="refresh-health">Refresh data health</button>
        <pre id="health-json" class="json-view">Loading…</pre>
        <div class="notice">Never paste secrets or API tokens into this page. Configure feeds
        through server environment variables only.</div></section>
        """

    if path == "/reports":
        return "Research & reports", """
        <section class="page-intro"><p class="eyebrow">CAPABILITY REGISTER</p>
        <h1>Research & reports</h1>
        <p class="lead">A clear distinction between working calculations and capabilities
        that need additional data or validated models.</p></section>
        <section class="grid cards">
          <article class="feature"><span class="status-pill active">Available</span>
            <h3>Accumulator mathematics</h3><p>Combined decimal odds and theoretical break-even probability.</p></article>
          <article class="feature"><span class="status-pill active">Conditional</span>
            <h3>Market scanning</h3><p>Only configured feeds with supported JSON formats; arbitrage checks require
            explicit exhaustive two-outcome markets.</p></article>
          <article class="feature"><span class="status-pill pending">Needs source</span>
            <h3>Fixtures, form & H2H</h3><p>Requires authorized fixture and historical match data.</p></article>
          <article class="feature"><span class="status-pill pending">Needs model</span>
            <h3>Fair odds & value</h3><p>Requires documented, validated probability estimates.</p></article>
          <article class="feature"><span class="status-pill pending">Future work</span>
            <h3>Forecast track record</h3><p>Requires stored forecasts and verified final results.</p></article>
          <article class="feature"><span class="status-pill pending">Future work</span>
            <h3>Watchlists & alerts</h3><p>Not implemented in this release.</p></article>
        </section>
        """

    return "Not found", """
    <section class="page-intro"><h1>Page not found</h1>
    <p class="lead">This page does not exist.</p><a class="button primary" href="/">Return to overview</a></section>
    """


CSS = r"""
:root{color-scheme:dark;--bg:#08111f;--panel:#101d2f;--panel2:#14243a;
--text:#edf4ff;--muted:#9eb0c8;--line:#263b55;--mint:#69e0bb;--blue:#9ac7ff;
--warning:#ffd58b;--radius:18px;font-family:Inter,-apple-system,BlinkMacSystemFont,
"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
*{box-sizing:border-box}html{background:var(--bg);scroll-behavior:smooth}
body{margin:0;background:radial-gradient(ellipse at 70% -15%,#183455 0,transparent 45%),var(--bg);
color:var(--text);min-height:100vh;padding-bottom:env(safe-area-inset-bottom)}
a{color:inherit;text-decoration:none}button,input,select{font:inherit}
a:focus-visible,button:focus-visible,input:focus-visible,select:focus-visible{outline:3px solid var(--mint);outline-offset:3px}
.topbar{position:sticky;top:0;z-index:5;background:#08111feF;border-bottom:1px solid var(--line);
backdrop-filter:blur(14px);padding:calc(12px + env(safe-area-inset-top)) max(18px,env(safe-area-inset-right)) 12px max(18px,env(safe-area-inset-left))}
.topinner{max-width:1180px;margin:auto;display:flex;align-items:center;justify-content:space-between;gap:16px}
.brand{display:flex;align-items:center;gap:11px;font-weight:800;letter-spacing:-.03em}
.logo{width:37px;height:37px;border-radius:12px;background:#132b3c;color:var(--mint);display:grid;place-items:center;font-size:23px}
.brand small{display:block;color:var(--muted);font-size:10px;font-weight:600;letter-spacing:.13em;margin-top:3px}
.top-status{font-size:12px;color:var(--mint);display:flex;align-items:center;gap:8px}
.dot{width:8px;height:8px;background:var(--mint);border-radius:50%;display:inline-block}
.layout{max-width:1180px;margin:auto;padding:24px 20px 50px;display:grid;grid-template-columns:205px minmax(0,1fr);gap:26px}
.sidebar{align-self:start;position:sticky;top:85px;display:flex;flex-direction:column;gap:6px}
.navlink{padding:12px 13px;border:1px solid transparent;border-radius:12px;color:var(--muted);font-size:14px}
.navlink:hover,.navlink.current{background:var(--panel);border-color:var(--line);color:var(--text)}
.navlink.current{box-shadow:inset 3px 0 var(--mint)}
.content{min-width:0}.hero,.page-intro{padding:28px 0 24px}
.eyebrow{font-size:10px;font-weight:800;letter-spacing:.17em;color:var(--mint);margin:0 0 12px}
h1{font-size:clamp(31px,5vw,48px);line-height:1.08;letter-spacing:-.045em;margin:0 0 15px}
h2{font-size:21px;letter-spacing:-.025em;margin:0 0 12px}
h3{font-size:17px;margin:13px 0 8px}.lead{color:#c0cee0;line-height:1.7;max-width:720px;font-size:16px}
.muted{color:var(--muted);font-size:13px;line-height:1.65}
.actions{display:flex;flex-wrap:wrap;gap:10px;margin:22px 0}
.button{border:1px solid var(--line);border-radius:12px;padding:12px 16px;min-height:44px;
display:inline-flex;align-items:center;justify-content:center;cursor:pointer;font-weight:700;font-size:13px}
.button.primary{background:var(--mint);color:#082018;border-color:var(--mint)}
.button.secondary{background:var(--panel);color:var(--text)}
.button:disabled{opacity:.5;cursor:wait}
.grid{display:grid;gap:13px}.metrics{grid-template-columns:repeat(4,minmax(0,1fr));margin:8px 0 18px}
.metric,.panel,.feature{border:1px solid var(--line);background:linear-gradient(145deg,#122137,#0d1929);
border-radius:var(--radius);padding:19px;min-width:0}
.metric span{font-size:12px;color:var(--muted);display:block;margin-bottom:11px}
.metric strong{font-size:clamp(16px,2.3vw,24px);overflow-wrap:anywhere}
.panel{margin:14px 0 18px}.section-heading{display:flex;justify-content:space-between;gap:16px}
.cards{grid-template-columns:repeat(2,minmax(0,1fr));margin-top:18px}
.feature{display:block;transition:transform .15s,border-color .15s}.feature:hover{transform:translateY(-2px);border-color:#55718d}
.feature p{color:var(--muted);font-size:13px;line-height:1.65;margin-bottom:0}
.feature-icon{display:grid;place-items:center;width:38px;height:38px;border-radius:12px;background:#1b3548;color:var(--mint);font-size:22px}
.notice{border:1px solid #66512d;background:#2a2418;color:var(--warning);border-radius:12px;padding:13px;line-height:1.6;font-size:13px;margin:15px 0}
.status-pill{display:inline-flex;font-size:10px;letter-spacing:.04em;border-radius:100px;padding:5px 8px}
.status-pill.active{background:#14382f;color:var(--mint)}.status-pill.pending{background:#2a2a32;color:#d4c7a8}
label{display:block;font-size:13px;color:#c4d2e4;margin:16px 0 8px}
input,select{width:100%;min-height:45px;border-radius:11px;border:1px solid var(--line);background:#081422;color:var(--text);padding:10px 12px}
.odds-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px;margin:15px 0}
.result-box{margin-top:18px;border-radius:13px;background:#0a1724;border:1px solid var(--line);padding:17px;line-height:1.8}
.result-big{font-size:28px;color:var(--mint);font-weight:800;overflow-wrap:anywhere}
.json-view{white-space:pre-wrap;overflow-wrap:anywhere;background:#07111d;border:1px solid var(--line);border-radius:12px;padding:15px;color:#c6d8ed;font-size:12px;line-height:1.65}
.table-wrap{overflow-x:auto;margin-top:14px}table{width:100%;border-collapse:collapse;min-width:620px;font-size:12px}
th,td{text-align:left;padding:12px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--muted);font-weight:700}td{color:#dce8f7}.candidate{padding:15px 0;border-bottom:1px solid var(--line)}
.candidate:last-child{border-bottom:0}.candidate strong{color:var(--mint)}
footer{max-width:1180px;margin:auto;padding:0 20px 30px;color:var(--muted);font-size:11px;line-height:1.7}
.mobile-nav{display:none}
@media(max-width:800px){.layout{grid-template-columns:1fr;padding:12px 14px 36px;gap:12px}
.sidebar{position:static;display:flex;flex-direction:row;overflow-x:auto;gap:7px;padding:3px 0 9px;scrollbar-width:thin}
.navlink{white-space:nowrap;padding:10px 12px;font-size:12px}.navlink.current{box-shadow:inset 0 -3px var(--mint)}
.metrics{grid-template-columns:repeat(2,minmax(0,1fr))}.metric{padding:15px}
.hero,.page-intro{padding:22px 0 17px}.top-status{font-size:0}.top-status .dot{font-size:initial}
}
@media(max-width:470px){.cards{grid-template-columns:1fr}.odds-grid{grid-template-columns:repeat(2,minmax(0,1fr))}
.panel{padding:15px}.brand{font-size:13px}.brand small{font-size:9px}.topinner{gap:8px}
h1{font-size:34px}.lead{font-size:14px}.button{padding:11px 13px}}
@media(prefers-reduced-motion:reduce){*{scroll-behavior:auto!important;transition:none!important}}
"""


def render_page(path):
    title, content = page_content(path)
    nav = []
    for href, label in NAV_ITEMS:
        current = " current" if href == path else ""
        current_attr = ' aria-current="page"' if href == path else ""
        nav.append(
            f'<a class="navlink{current}" href="{escape(href)}"'
            f'{current_attr}>{escape(label)}</a>'
        )

    js = r"""
const $ = id => document.getElementById(id);
async function api(path, options={}) {
  const response = await fetch(path, {
    cache: "no-store",
    headers: {"Content-Type":"application/json", ...(options.headers||{})},
    ...options
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
  return data;
}
function pretty(value){return JSON.stringify(value,null,2);}
function safe(value){return String(value ?? "").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));}

async function refreshOverview(){
  try {
    const [health,data] = await Promise.all([api("/health"),api("/api/data-health")]);
    if($("service-status")) $("service-status").textContent = health.status;
    if($("feed-count")) $("feed-count").textContent = data.configured_market_feed_count;
    if($("scan-status")) $("scan-status").textContent = data.last_scan_status;
    if($("snapshot-count")) $("snapshot-count").textContent = data.stored_quote_snapshot_rows;
    if($("reality")) $("reality").textContent =
      data.configured_market_feed_count === 0
      ? "No market feeds are configured. Live prices and arbitrage candidates are unavailable until a supported feed is connected."
      : `${data.configured_market_feed_count} feed(s) configured. Check feed reachability and usable quote counts on the Market Scanner page.`;
    if($("match-data-note")) $("match-data-note").textContent =
      `Configured feeds: ${data.configured_market_feed_count}. Stored quote snapshots: ${data.stored_quote_snapshot_rows}. Latest capture: ${data.latest_quote_capture_at || "none"}.`;
  } catch(e) {
    if($("reality")) $("reality").textContent = "Could not load service data: " + e.message;
  }
}

async function loadScanner(){
  try {
    const data = await api("/api/state");
    const summary = data.last_scan_summary || {};
    if($("scan-message")) $("scan-message").textContent =
      `Status: ${data.last_scan_status}. Feeds configured: ${data.configured_market_feed_count}. Last completed: ${data.last_scan_finished_at || "not yet run"}.`;
    if($("feed-list")) {
      const feeds = summary.feed_health || [];
      $("feed-list").innerHTML = "<h2>Feed health</h2>" +
        (feeds.length ? feeds.map(f=>`<div class="candidate"><strong>${safe(f.host)}</strong><br>Status: ${safe(f.status)} · Quotes: ${safe(f.quotes)}<p class="muted">${safe(f.message)}</p></div>`).join("")
        : "<p class='muted'>No feed results yet. Configure MARKET_FEED_URLS in the service environment.</p>");
    }
    if($("candidate-list")) {
      const candidates = summary.candidates || [];
      $("candidate-list").innerHTML = "<h2>Candidate review</h2>" +
        (candidates.length ? candidates.map(c=>`<div class="candidate"><h3>${safe(c.event)} · ${safe(c.market)}</h3><p>${c.outcomes.map(o=>`${safe(o.selection)} @ ${safe(o.odds)} —${safe(o.bookmaker)}`).join("<br>")}</p><strong>Theoretical margin: ${safe(c.theoretical_margin_percent)}%</strong><p class="muted">${safe(c.warning)}</p></div>`).join("")
        : "<p class='muted'>No eligible candidates in the latest scan. This does not prove that no opportunities exist; feeds may be absent, incomplete, or unsupported.</p>");
    }
  } catch(e) {
    if($("scan-message")) $("scan-message").textContent = "Unable to load scanner: " + e.message;
  }
}
if($("run-scan")) $("run-scan").addEventListener("click", async () => {
  const button=$("run-scan"); button.disabled=true; button.textContent="Scanning…";
  if($("scan-message")) $("scan-message").textContent="Scan started. Waiting for configured sources…";
  try {
    const result=await api("/api/scan",{method:"POST",body:JSON.stringify({})});
    if($("scan-message")) $("scan-message").textContent=`Scan finished: ${result.status}. Quotes: ${result.quote_count ?? 0}. Candidates: ${result.candidate_count ?? 0}.`;
    await loadScanner();
  } catch(e) {
    if($("scan-message")) $("scan-message").textContent="Scan request failed: "+e.message;
  } finally {button.disabled=false;button.textContent="Run scan now";}
});
if($("refresh-scan")) $("refresh-scan").addEventListener("click",loadScanner);

function renderOddsInputs(){
  if(!$("odds-inputs")) return;
  const n=Number($("pick-count").value);
  $("odds-inputs").innerHTML=Array.from({length:n},(_,i)=>
    `<div><label for="odd-${i}">Selection ${i+1} odds</label><input id="odd-${i}" inputmode="decimal" type="number" min="1.01" max="1000" step="0.01" placeholder="e.g. 1.85" value="${i<3?["1.85","2.10","1.65"][i]:""}"></div>`
  ).join("");
}
if($("pick-count")) { $("pick-count").addEventListener("change",renderOddsInputs); renderOddsInputs(); }
if($("calculate")) $("calculate").addEventListener("click",async()=>{
  const n=Number($("pick-count").value);
  const picks=[];
  for(let i=0;i<n;i++){
    const odds=Number($("odd-"+i).value);
    if(!Number.isFinite(odds)||odds<1.01||odds>1000){
      $("calculation-result").textContent=`Enter valid decimal odds for selection ${i+1} (1.01 to 1000).`;return;
    }
    picks.push({event:`Selection ${i+1}`,selection:`Selection ${i+1}`,odds});
  }
  try {
    const result=await api("/api/analyse",{method:"POST",body:JSON.stringify({picks})});
    $("calculation-result").innerHTML=`<div class="muted">Combined decimal odds</div><div class="result-big">${safe(result.combined_decimal_odds)}</div><div>Break-even probability: <strong>${safe(result.break_even_probability_percent)}%</strong></div><p class="muted">${safe(result.warning)}</p>`;
  } catch(e){$("calculation-result").textContent="Calculation failed: "+e.message;}
});

async function loadHistory(){
  if(!$("history-list")) return;
  try {
    const result=await api("/api/history?limit=50");
    const rows=result.runs||[];
    $("history-list").innerHTML=rows.length?`<table><thead><tr><th>Started</th><th>Status</th><th>Feeds</th><th>Quotes</th><th>Candidates</th></tr></thead><tbody>${rows.map(r=>`<tr><td>${safe(r.started_at)}</td><td>${safe(r.status)}</td><td>${safe(r.feeds_reached)}/${safe(r.feed_count)}</td><td>${safe(r.quote_count)}</td><td>${safe(r.candidate_count)}</td></tr>`).join("")}</tbody></table>`:"<p class='muted'>No scan history recorded yet.</p>";
  } catch(e){$("history-list").textContent="Unable to load history: "+e.message;}
}
if($("refresh-history")) $("refresh-history").addEventListener("click",loadHistory);

async function loadDataHealth(){
  if(!$("health-json")) return;
  try {$("health-json").textContent=pretty(await api("/api/data-health"));}
  catch(e){$("health-json").textContent="Unable to load data health: "+e.message;}
}
if($("refresh-health")) $("refresh-health").addEventListener("click",loadDataHealth);

refreshOverview();
if($("run-scan")\vert{}\vert{}$("feed-list")) loadScanner();
if($("history-list")) loadHistory();
if($("health-json")) loadDataHealth();

if("serviceWorker" in navigator) {
  window.addEventListener("load",()=>navigator.serviceWorker.register("/sw.js").catch(()=>{}));
}
"""

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#08111f">
<meta name="description" content="Read-only football market research and odds analysis.">
<link rel="manifest" href="/manifest.webmanifest">
<link rel="icon" href="/icon.svg" type="image/svg+xml">
<title>{escape(title)} · {APP_NAME}</title>
<style>{CSS}</style>
</head>
<body>
<header class="topbar"><div class="topinner">
<a class="brand" href="/"><span class="logo">∑</span><span>SolveEase<small>SUREBET INTELLIGENCE · V{VERSION}</small></span></a>
<span class="top-status"><span class="dot"></span>READ-ONLY SYSTEM</span>
</div></header>
<div class="layout">
<nav class="sidebar" aria-label="Main navigation">{''.join(nav)}</nav>
<main class="content">{content}</main>
</div>
<footer>Read-only research tool. No bets are placed. Prices may be delayed, unavailable, incomplete or incorrect. Always independently verify market availability, settlement rules and timestamps. No profit is guaranteed.</footer>
<script>{js}</script>
</body>
</html>"""


# ------------------------------------------------------------
# HTTP server
# ------------------------------------------------------------

def client_key(handler):
    # Behind a proxy, do not blindly trust arbitrary X-Forwarded-For.
    # Remote address is used to avoid spoofable headers.
    return handler.client_address[0] if handler.client_address else "unknown"


def rate_limited(handler):
    method = handler.command
    limit, window = RATE_LIMITS.get(method, (60, 60))
    key = (client_key(handler), method)
    now = time.time()

    with RATE_LOCK:
        recent = [stamp for stamp in REQUEST_TIMES[key] if now - stamp < window]
        if len(recent) >= limit:
            REQUEST_TIMES[key] = recent
            return True
        recent.append(now)
        REQUEST_TIMES[key] = recent

        # Avoid unbounded growth of old keys.
        if len(REQUEST_TIMES) > 5000:
            for old_key in list(REQUEST_TIMES.keys())[:1000]:
                REQUEST_TIMES.pop(old_key, None)

    return False


class Handler(BaseHTTPRequestHandler):
    server_version = "SolveEaseHTTP/7.0"
    sys_version = ""

    def log_message(self, fmt, *args):
        # Never log query strings or request bodies, which could contain
        # share codes or other user-supplied values.
        path = urlparse(self.path).path
        log.info("%s %s %s", self.client_address[0], self.command, path)

    def send_bytes(self, status, body, content_type="application/json; charset=utf-8",
                   extra_headers=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        self.send_header("Cache-Control", "no-store")
        if extra_headers:
            for key, value in extra_headers.items():
                self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def send_json(self, status, data):
        self.send_bytes(status, json_bytes(data))

    def send_error_json(self, status, message):
        self.send_json(status, {"error": clean_string(message, 300)})

    def read_json_body(self):
        raw_length = self.headers.get("Content-Length", "0")
        try:
            length = int(raw_length)
        except ValueError:
            raise ValueError("Invalid Content-Length.")

        if length < 0 or length > MAX_REQUEST_BYTES:
            raise ValueError("Request body is too large.")

        body = self.rfile.read(length) if length else b"{}"
        try:
            return json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("Request body must be valid JSON.") from exc

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        if rate_limited(self):
            self.send_error_json(429, "Too many requests. Please try again shortly.")
            return

        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = parse_qs(parsed.query)

        if path in ("/health", "/healthz"):
            self.send_json(200, health_payload())
            return

        if path in ("/state", "/api/state"):
            with STATE_LOCK:
                data = dict(STATE)
            # This state contains no feed URLs or submitted share codes.
            self.send_json(200, data)
            return

        if path == "/api/data-health":
            try:
                self.send_json(200, data_health())
            except Exception:
                log.exception("Data-health endpoint failed.")
                self.send_error_json(500, "Could not read data health.")
            return

        if path == "/api/history":
            try:
                limit = int(query.get("limit", ["30"])[0])
                self.send_json(200, {"runs": get_history(limit)})
            except (ValueError, TypeError):
                self.send_error_json(400, "limit must be an integer.")
            except Exception:
                log.exception("History endpoint failed.")
                self.send_error_json(500, "Could not read scan history.")
            return

        if path == "/api/scan":
            with STATE_LOCK:
                result = dict(STATE.get("last_scan_summary") or {})
            self.send_json(200, result or {
                "status": "not_run",
                "message": "No scan has completed yet.",
            })
            return

        if path == "/manifest.webmanifest":
            self.send_bytes(
                200, json_bytes(MANIFEST),
                "application/manifest+json; charset=utf-8"
            )
            return

        if path == "/sw.js":
            self.send_bytes(
                200, SERVICE_WORKER.encode("utf-8"),
                "application/javascript; charset=utf-8",
                {"Service-Worker-Allowed": "/"}
            )
            return

        if path == "/icon.svg":
            self.send_bytes(200, ICON_SVG.encode("utf-8"), "image/svg+xml")
            return

        if path in ("/", "/scanner", "/matches", "/risk-lab",
                    "/history", "/data-health", "/reports"):
            body = render_page(path).encode("utf-8")
            self.send_bytes(200, body, "text/html; charset=utf-8")
            return

        self.send_bytes(
            404,
            render_page("/not-found").encode("utf-8"),
            "text/html; charset=utf-8"
        )

    def do_POST(self):
        if rate_limited(self):
            self.send_error_json(429, "Too many requests. Please try again shortly.")
            return

        path = urlparse(self.path).path.rstrip("/") or "/"

        try:
            payload = self.read_json_body()
        except ValueError as exc:
            self.send_error_json(400, str(exc))
            return

        if path == "/api/scan":
            result = run_scan(trigger="manual")
            code = 200 if result.get("status") != "error" else 500
            self.send_json(code, result)
            return

        if path == "/api/analyse":
            try:
                self.send_json(200, analyse_ticket(payload))
            except ValueError as exc:
                self.send_error_json(400, str(exc))
            except Exception:
                log.exception("Analysis endpoint failed.")
                self.send_error_json(500, "Could not analyse the supplied selections.")
            return

        if path == "/api/lookup":
            if not isinstance(payload, dict):
                self.send_error_json(400, "Expected a JSON object.")
                return

            code = payload.get("code", "")
            result = lookup_sportybet(code)
            status = result.get("status", "")
            http_status = 200 if status in ("parsed", "response_unrecognised") else 400
            if status in ("lookup_failed",):
                http_status = 502
            self.send_json(http_status, result)
            return

        self.send_error_json(404, "API endpoint not found.")


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    init_db()

    with STATE_LOCK:
        STATE["status"] = "online"
        STATE["started_at"] = utc_now()
        STATE["configured_market_feed_count"] = len(MARKET_FEED_URLS)

    log.info("%s v%s starting on %s:%s", APP_NAME, VERSION, HOST, PORT)
    log.info("Configured market feed URLs: %s", len(MARKET_FEED_URLS))
    log.info("Database path: %s", DATABASE_PATH)
    log.info(
        "Database persistent: %s",
        DATABASE_PATH != ":memory:" and not DATABASE_PATH.startswith("/tmp/")
    )

    scheduler = threading.Thread(
        target=scheduler_loop, name="solveease-scheduler", daemon=True
    )
    scheduler.start()

    server = Server((HOST, PORT), Handler)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        log.info("Shutdown requested.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
