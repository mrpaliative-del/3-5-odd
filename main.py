
"""
SolveEase Surebet Intelligence v6.0.0
Read-only football market intelligence and accumulator analysis.

Run:
    python main.py

Required production settings:
    HOST=0.0.0.0
    PORT=10000

Optional:
    MARKET_FEED_URLS=https://feed-one.example/data.json;https://feed-two.example/data.json
    REQUEST_TIMEOUT_SECONDS=8
    MAX_RESPONSE_BYTES=4000000
    MAX_FEEDS=10
    ENABLE_SPORTYBET_LOOKUP=true

Market feed format:
{
  "markets": [
    {
      "sport": "Football",
      "event": "Home FC vs Away FC",
      "event_id": "stable-event-id",
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

This application does not place bets or promise profitable outcomes.
"""

import json
import os
import re
import time
import math
import threading
import unicodedata
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse, unquote
from urllib.request import Request, urlopen


APP_NAME = "SolveEase Surebet Intelligence"
VERSION = "6.0.0"
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "10000"))
REQUEST_TIMEOUT = max(2, min(30, int(os.getenv("REQUEST_TIMEOUT_SECONDS", "8"))))
MAX_RESPONSE_BYTES = max(
    100_000, min(10_000_000, int(os.getenv("MAX_RESPONSE_BYTES", "4000000")))
)
MAX_FEEDS = max(1, min(20, int(os.getenv("MAX_FEEDS", "10"))))
ENABLE_SPORTYBET_LOOKUP = (
    os.getenv("ENABLE_SPORTYBET_LOOKUP", "true").strip().lower()
    in ("1", "true", "yes", "on")
)
FEED_URLS = [
    u.strip()
    for u in os.getenv("MARKET_FEED_URLS", "").split(";")
    if u.strip()
][:MAX_FEEDS]

SPORTYBET_SHARE_ENDPOINT = (
    "https://www.sportybet.com/api/ng/orders/share/"
)

STATE_LOCK = threading.RLock()
STATE = {
    "started_at": datetime.now(timezone.utc).isoformat(),
    "last_lookup_status": "not_run",
    "last_lookup_at": None,
    "last_scan_status": "not_run",
    "last_scan_at": None,
    "last_scan_summary": None,
    "last_feed_health": [],
    "last_error": None,
}

USER_AGENT = "SolveEase-Surebet-Intelligence/6.0 (read-only market analysis)"


# ---------------------------------------------------------------------------
# General helpers
# ---------------------------------------------------------------------------

def utc_now():
    return datetime.now(timezone.utc).isoformat()


def safe_float(value):
    try:
        result = float(value)
        if math.isfinite(result):
            return result
    except (TypeError, ValueError, OverflowError):
        pass
    return None


def normalise_text(value):
    """Normalize text for conservative event/market comparisons."""
    if value is None:
        return ""
    value = unicodedata.normalize("NFKD", str(value))
    value = "".join(c for c in value if not unicodedata.combining(c))
    value = value.lower().replace("&", " and ")
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def same_text(a, b):
    return normalise_text(a) == normalise_text(b)


def valid_odds(value):
    odds = safe_float(value)
    return odds is not None and 1.0 < odds <= 10000


def implied_probability(odds):
    odds = safe_float(odds)
    if not valid_odds(odds):
        return None
    return round(100.0 / odds, 3)


def clean_string(value, limit=300):
    if value is None:
        return None
    result = str(value).strip()
    return result[:limit] if result else None


def json_response(handler, status, payload):
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(raw)))
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("X-Content-Type-Options", "nosniff")
    handler.end_headers()
    handler.wfile.write(raw)


def text_response(handler, status, body, content_type="text/plain; charset=utf-8"):
    raw = body.encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(raw)))
    handler.send_header("X-Content-Type-Options", "nosniff")
    handler.end_headers()
    handler.wfile.write(raw)


def fetch_json(url):
    """
    Fetch an authorized JSON feed.

    Production deployments should configure only trusted HTTPS feed hosts.
    This function intentionally refuses non-HTTPS remote feeds.
    """
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError("Feed URL must be a valid HTTPS URL.")

    request = Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
        },
        method="GET",
    )
    with urlopen(request, timeout=REQUEST_TIMEOUT) as response:
        content_type = response.headers.get("Content-Type", "")
        body = response.read(MAX_RESPONSE_BYTES + 1)

    if len(body) > MAX_RESPONSE_BYTES:
        raise ValueError("Feed response exceeded the configured size limit.")
    if not body:
        raise ValueError("Feed returned an empty response.")

    # Some authorized JSON endpoints use a generic content type.
    try:
        return json.loads(body.decode("utf-8-sig")), content_type
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Feed did not return valid UTF-8 JSON.") from exc


# ---------------------------------------------------------------------------
# Market feed normalization
# ---------------------------------------------------------------------------

def extract_feed_quotes(payload, source_url):
    """
    Accept either:
      {"markets": [{"event": "...", "market": "...", "outcomes": [...]}]}
    or:
      {"quotes": [{"event": "...", "market": "...", "selection": "..."}]}

    A market's exhaustive_two_outcome flag is retained only when the source
    explicitly declares it. It is not inferred from the number of outcomes.
    """
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

    quotes = []
    for record in records[:10000]:
        if not isinstance(record, dict):
            continue

        event = clean_string(
            record.get("event")
            or record.get("event_name")
            or record.get("fixture")
            or record.get("match"),
            300,
        )
        market = clean_string(
            record.get("market")
            or record.get("market_name")
            or record.get("marketDesc"),
            200,
        )
        sport = clean_string(record.get("sport") or "Football", 80)
        bookmaker = clean_string(
            record.get("bookmaker")
            or record.get("bookmaker_name")
            or record.get("source"),
            120,
        )
        event_id = clean_string(
            record.get("event_id") or record.get("fixture_id"), 160
        )
        updated_at = clean_string(
            record.get("updated_at")
            or record.get("timestamp")
            or record.get("last_updated"),
            100,
        )

        exhaustive = record.get("exhaustive_two_outcome") is True

        if not event or not market:
            continue

        outcomes = record.get("outcomes")
        if isinstance(outcomes, list):
            for outcome in outcomes:
                if not isinstance(outcome, dict):
                    continue
                selection = clean_string(
                    outcome.get("selection")
                    or outcome.get("name")
                    or outcome.get("label"),
                    200,
                )
                odds = safe_float(
                    outcome.get("odds")
                    or outcome.get("price")
                    or outcome.get("decimal_odds")
                )
                if not selection or not valid_odds(odds):
                    continue
                quotes.append({
                    "sport": sport,
                    "event": event,
                    "event_id": event_id,
                    "market": market,
                    "selection": selection,
                    "odds": odds,
                    "bookmaker": bookmaker,
                    "updated_at": updated_at,
                    "source_url": source_url,
                    "exhaustive_two_outcome": exhaustive,
                })
        else:
            selection = clean_string(
                record.get("selection") or record.get("outcome"), 200
            )
            odds = safe_float(
                record.get("odds")
                or record.get("price")
                or record.get("decimal_odds")
            )
            if selection and valid_odds(odds):
                quotes.append({
                    "sport": sport,
                    "event": event,
                    "event_id": event_id,
                    "market": market,
                    "selection": selection,
                    "odds": odds,
                    "bookmaker": bookmaker,
                    "updated_at": updated_at,
                    "source_url": source_url,
                    "exhaustive_two_outcome": exhaustive,
                })

    return quotes


def fetch_configured_markets():
    all_quotes = []
    health = []

    for url in FEED_URLS:
        started = time.time()
        try:
            payload, _content_type = fetch_json(url)
            quotes = extract_feed_quotes(payload, url)
            all_quotes.extend(quotes)
            health.append({
                "source": urlparse(url).hostname or "configured feed",
                "status": "ok" if quotes else "empty",
                "quotes": len(quotes),
                "elapsed_ms": round((time.time() - started) * 1000),
                "checked_at": utc_now(),
            })
        except (ValueError, HTTPError, URLError, TimeoutError, OSError) as exc:
            health.append({
                "source": urlparse(url).hostname or "configured feed",
                "status": "error",
                "quotes": 0,
                "error": str(exc)[:240],
                "checked_at": utc_now(),
            })
        except Exception as exc:
            health.append({
                "source": urlparse(url).hostname or "configured feed",
                "status": "error",
                "quotes": 0,
                "error": type(exc).__name__,
                "checked_at": utc_now(),
            })

    with STATE_LOCK:
        STATE["last_feed_health"] = health

    return all_quotes, health


# ---------------------------------------------------------------------------
# SportyBet share-code lookup
# ---------------------------------------------------------------------------

def walk_dicts(value, depth=0):
    """Yield nested dictionaries while avoiding pathological recursion."""
    if depth > 12:
        return
    if isinstance(value, dict):
        yield value
        for child in value.values():
            if isinstance(child, (dict, list)):
                yield from walk_dicts(child, depth + 1)
    elif isinstance(value, list):
        for child in value[:5000]:
            if isinstance(child, (dict, list)):
                yield from walk_dicts(child, depth + 1)


def first_value(obj, keys):
    for key in keys:
        value = obj.get(key)
        if value is not None and value != "":
            return value
    return None


def parse_sportybet_outcomes(payload):
    """
    Best-effort parser for the share response.
    The bookmaker may change its undocumented response schema.
    """
    candidates = []
    seen = set()

    home_keys = ("homeTeamName", "homeTeam", "home_name", "home")
    away_keys = ("awayTeamName", "awayTeam", "away_name", "away")
    market_keys = ("marketDesc", "marketName", "market_name", "market")
    selection_keys = (
        "selectedOutcome", "outcomeName", "selectionName",
        "selection", "outcome",
    )
    odds_keys = ("odds", "odd", "decimalOdds", "price")
    time_keys = ("estimateStartTime", "startTime", "kickoff", "eventTime")

    for item in walk_dicts(payload):
        home = first_value(item, home_keys)
        away = first_value(item, away_keys)
        market = first_value(item, market_keys)
        selection = first_value(item, selection_keys)
        odds = safe_float(first_value(item, odds_keys))

        if not (home and away and market and selection and valid_odds(odds)):
            continue

        event = f"{home} vs {away}"
        key = (
            normalise_text(event),
            normalise_text(market),
            normalise_text(selection),
            odds,
        )
        if key in seen:
            continue
        seen.add(key)

        candidates.append({
            "sport": "Football",
            "event": event,
            "market": clean_string(market, 200),
            "selection": clean_string(selection, 200),
            "odds": odds,
            "implied_probability_percent": implied_probability(odds),
            "kickoff": first_value(item, time_keys),
            "bookmaker": "SportyBet",
            "source": "SportyBet share-code response",
            "data_quality": "parsed; not independently verified",
        })

    return candidates


def lookup_sportybet(code):
    if not ENABLE_SPORTYBET_LOOKUP:
        return {
            "ok": False,
            "status": "disabled",
            "message": "SportyBet lookup is disabled by server configuration.",
        }

    code = clean_string(code, 160)
    if not code or not re.fullmatch(r"[A-Za-z0-9_-]+", code):
        return {
            "ok": False,
            "status": "invalid_code",
            "message": "Enter a valid-looking share code without spaces or URLs.",
        }

    url = SPORTYBET_SHARE_ENDPOINT + code
    request = Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
        },
        method="GET",
    )

    try:
        with urlopen(request, timeout=REQUEST_TIMEOUT) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
            http_status = response.status

        if len(raw) > MAX_RESPONSE_BYTES:
            return {
                "ok": False,
                "status": "response_too_large",
                "message": "The bookmaker response exceeded the size limit.",
            }

        try:
            payload = json.loads(raw.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return {
                "ok": False,
                "status": "invalid_response",
                "http_status": http_status,
                "message": "The endpoint did not return valid JSON.",
            }

        legs = parse_sportybet_outcomes(payload)
        if not legs:
            return {
                "ok": False,
                "status": "schema_unrecognized",
                "http_status": http_status,
                "message": (
                    "A response was received, but no complete accumulator "
                    "legs could be extracted. The endpoint may have changed "
                    "or the code may be invalid or expired."
                ),
                "parseable_response_received": True,
                "endpoint_live_verified": False,
            }

        combined_odds = 1.0
        for leg in legs:
            combined_odds *= leg["odds"]

        ticket = {
            "schema": "solveease.normalized-ticket.v1",
            "bookmaker": "SportyBet",
            "booking_code": code,
            "decoded_at": utc_now(),
            "legs": legs,
            "leg_count": len(legs),
            "combined_odds_from_parsed_legs": round(combined_odds, 6),
            "combined_odds_note": (
                "Calculated from extracted legs; not independently verified "
                "against the bookmaker's displayed total."
            ),
            "read_only": True,
        }

        return {
            "ok": True,
            "status": "decoded",
            "parseable_response_received": True,
            "endpoint_live_verified": False,
            "ticket": ticket,
        }

    except HTTPError as exc:
        return {
            "ok": False,
            "status": "http_error",
            "http_status": exc.code,
            "message": (
                "The bookmaker endpoint returned an HTTP error. The code "
                "may be invalid, expired, or unavailable in this region."
            ),
        }
    except (URLError, TimeoutError, OSError) as exc:
        return {
            "ok": False,
            "status": "connection_error",
            "message": "Could not reach the bookmaker endpoint.",
            "detail": type(exc).__name__,
        }
    except Exception as exc:
        return {
            "ok": False,
            "status": "lookup_error",
            "message": "Unexpected lookup error.",
            "detail": type(exc).__name__,
        }


# ---------------------------------------------------------------------------
# Ticket analysis and alternative picks
# ---------------------------------------------------------------------------

def split_event(event):
    text = str(event or "")
    for separator in (" vs ", " VS ", " v ", " - ", " @ "):
        if separator in text:
            parts = text.split(separator, 1)
            if len(parts) == 2 and parts[0].strip() and parts[1].strip():
                return normalise_text(parts[0]), normalise_text(parts[1])
    return normalise_text(text), ""


def event_matches(a, b, event_id_a=None, event_id_b=None):
    if event_id_a and event_id_b:
        return str(event_id_a) == str(event_id_b)

    a_home, a_away = split_event(a)
    b_home, b_away = split_event(b)
    if a_home and a_away and b_home and b_away:
        return (a_home == b_home and a_away == b_away) or (
            a_home == b_away and a_away == b_home
        )
    return normalise_text(a) == normalise_text(b)


def market_matches(a, b):
    return normalise_text(a) == normalise_text(b)


def selection_matches(a, b):
    return normalise_text(a) == normalise_text(b)


def inspect_ticket(ticket, quotes):
    legs = ticket.get("legs", []) if isinstance(ticket, dict) else []
    results = []
    combined = 1.0
    combined_valid = True

    for index, leg in enumerate(legs, start=1):
        if not isinstance(leg, dict):
            continue

        odds = safe_float(leg.get("odds"))
        if valid_odds(odds):
            combined *= odds
        else:
            combined_valid = False

        event = leg.get("event")
        market = leg.get("market")
        selection = leg.get("selection")

        comparable = []
        for quote in quotes:
            if not event_matches(event, quote.get("event")):
                continue
            if not market_matches(market, quote.get("market")):
                continue
            if not selection_matches(selection, quote.get("selection")):
                continue
            comparable.append(quote)

        comparable.sort(key=lambda q: q["odds"], reverse=True)
        best = comparable[0] if comparable else None

        alternatives = []
        seen_selections = set()
        for quote in quotes:
            if not event_matches(event, quote.get("event")):
                continue
            if not market_matches(market, quote.get("market")):
                continue
            if selection_matches(selection, quote.get("selection")):
                continue

            key = normalise_text(quote.get("selection"))
            if not key or key in seen_selections:
                continue
            seen_selections.add(key)

            alternatives.append({
                "selection": quote.get("selection"),
                "odds": quote.get("odds"),
                "bookmaker": quote.get("bookmaker"),
                "updated_at": quote.get("updated_at"),
                "source_url": quote.get("source_url"),
                "implied_probability_percent": implied_probability(
                    quote.get("odds")
                ),
                "note": (
                    "Alternative outcome only; not a recommendation or "
                    "claim that it is safer."
                ),
            })

        alternatives.sort(key=lambda x: x["odds"], reverse=True)

        results.append({
            "leg_number": index,
            "event": event,
            "market": market,
            "selection": selection,
            "original_odds": odds,
            "original_implied_probability_percent": implied_probability(odds),
            "matching_quote_count": len(comparable),
            "best_observed_matching_price": (
                {
                    "odds": best.get("odds"),
                    "bookmaker": best.get("bookmaker"),
                    "updated_at": best.get("updated_at"),
                    "source_url": best.get("source_url"),
                }
                if best else None
            ),
            "alternative_outcomes": alternatives[:10],
            "comparison_status": (
                "comparable_quotes_found" if comparable
                else "no_comparable_quotes"
            ),
        })

    return {
        "status": "analysed",
        "analysed_at": utc_now(),
        "leg_count": len(results),
        "legs": results,
        "combined_odds_from_parsed_legs": (
            round(combined, 6) if combined_valid and results else None
        ),
        "combined_implied_probability_percent": (
            round(100 / combined, 6)
            if combined_valid and results and combined > 0
            else None
        ),
        "interpretation": (
            "Combined implied probability from multiplied decimal odds is "
            "not a calibrated probability of winning. Leg probabilities may "
            "be dependent, particularly for selections from the same event."
        ),
        "market_data_available": bool(quotes),
        "market_data_note": (
            "Comparisons use only configured feed quotes that matched event, "
            "market and selection labels exactly after text normalization. "
            "Missing matches are not filled with invented prices."
        ),
    }


def scan_arbitrage(quotes):
    """
    Conservative two-outcome screening.

    Each outcome must have an explicit exhaustive_two_outcome=True flag,
    exactly two distinct outcomes for the same event and market, and prices
    from two distinct named bookmakers. This is a screening signal, not a
    guarantee of executable arbitrage: settlement rules, limits, fees, price
    movement and account restrictions still require manual verification.
    """
    groups = {}

    for quote in quotes:
        if quote.get("exhaustive_two_outcome") is not True:
            continue
        if not quote.get("bookmaker") or not quote.get("selection"):
            continue

        event_key = (
            quote.get("event_id")
            or normalise_text(quote.get("event"))
        )
        key = (event_key, normalise_text(quote.get("market")))
        groups.setdefault(key, []).append(quote)

    opportunities = []
    for (_event_key, _market_key), group in groups.items():
        event_names = list(dict.fromkeys(q.get("event") for q in group if q.get("event")))
        market_names = list(dict.fromkeys(q.get("market") for q in group if q.get("market")))
        if not event_names or not market_names:
            continue

        selections = list(dict.fromkeys(
            normalise_text(q.get("selection"))
            for q in group if q.get("selection")
        ))
        if len(selections) != 2:
            continue

        best_by_selection = {}
        for selection_key in selections:
            candidates = [
                q for q in group
                if normalise_text(q.get("selection")) == selection_key
                and valid_odds(q.get("odds"))
            ]
            candidates.sort(key=lambda q: q["odds"], reverse=True)

            # Keep the best price from a distinct bookmaker.
            distinct = []
            used = set()
            for q in candidates:
                book = normalise_text(q.get("bookmaker"))
                if book and book not in used:
                    distinct.append(q)
                    used.add(book)
            if distinct:
                best_by_selection[selection_key] = distinct[0]

        if len(best_by_selection) != 2:
            continue

        selected = list(best_by_selection.values())
        if normalise_text(selected[0]["bookmaker"]) == normalise_text(
            selected[1]["bookmaker"]
        ):
            continue

        inverse_sum = sum(1.0 / q["odds"] for q in selected)
        if inverse_sum >= 1.0:
            continue

        opportunities.append({
            "event": event_names[0],
            "market": market_names[0],
            "observed_outcomes": [
                {
                    "selection": q["selection"],
                    "odds": q["odds"],
                    "bookmaker": q["bookmaker"],
                    "updated_at": q.get("updated_at"),
                }
                for q in selected
            ],
            "implied_probability_sum_percent": round(inverse_sum * 100, 4),
            "theoretical_margin_percent": round((1 - inverse_sum) * 100, 4),
            "status": "candidate_requires_manual_verification",
            "warning": (
                "Confirm identical settlement rules, event timing, stake "
                "limits, price availability and actual executable odds."
            ),
        })

    return opportunities


def run_scan():
    quotes, health = fetch_configured_markets()
    opportunities = scan_arbitrage(quotes)

    result = {
        "status": "complete" if FEED_URLS else "no_feeds_configured",
        "scanned_at": utc_now(),
        "configured_feed_count": len(FEED_URLS),
        "usable_quote_count": len(quotes),
        "candidate_count": len(opportunities),
        "candidates": opportunities[:100],
        "feed_health": health,
        "message": (
            "No market feeds are configured. Add authorized HTTPS JSON feeds "
            "using MARKET_FEED_URLS."
            if not FEED_URLS else
            "Scan completed. Candidates require manual price and settlement verification."
        ),
    }

    with STATE_LOCK:
        STATE["last_scan_status"] = result["status"]
        STATE["last_scan_at"] = result["scanned_at"]
        STATE["last_scan_summary"] = {
            "configured_feed_count": len(FEED_URLS),
            "usable_quote_count": len(quotes),
            "candidate_count": len(opportunities),
        }

    return result


# ---------------------------------------------------------------------------
# Progressive Web App resources
# ---------------------------------------------------------------------------

MANIFEST = {
    "id": "/",
    "name": APP_NAME,
    "short_name": "SolveEase",
    "description": "Read-only football market and accumulator analysis.",
    "start_url": "/",
    "scope": "/",
    "display": "standalone",
    "background_color": "#0b1020",
    "theme_color": "#0b1020",
    "orientation": "portrait-primary",
    "icons": [
        {
            "src": "/icon.svg",
            "sizes": "any",
            "type": "image/svg+xml",
            "purpose": "any maskable",
        }
    ],
}

SERVICE_WORKER = r"""
const CACHE = "solveease-static-v6";
const APP_SHELL = ["/", "/manifest.webmanifest", "/icon.svg"];

self.addEventListener("install", event => {
  event.waitUntil(
    caches.open(CACHE).then(cache => cache.addAll(APP_SHELL))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", event => {
  event.waitUntil(
    caches.keys().then(keys => Promise.all(
      keys.filter(key => key !== CACHE).map(key => caches.delete(key))
    )).then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", event => {
  const url = new URL(event.request.url);

  // Never cache API responses, booking codes, or market data.
  if (url.origin !== self.location.origin ||
      url.pathname.startsWith("/api/") ||
      event.request.method !== "GET") {
    return;
  }

  event.respondWith(
    fetch(event.request).then(response => {
      if (response.ok) {
        const copy = response.clone();
        caches.open(CACHE).then(cache => cache.put(event.request, copy));
      }
      return response;
    }).catch(async () => {
      const cached = await caches.match(event.request);
      if (cached) return cached;
      return new Response(
        "<!doctype html><meta name='viewport' content='width=device-width'>" +
        "<h1>SolveEase is offline</h1><p>Reconnect to refresh live markets.</p>",
        { headers: { "Content-Type": "text/html; charset=utf-8" } }
      );
    })
  );
});
"""

ICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">
<rect width="512" height="512" rx="100" fill="#0b1020"/>
<circle cx="256" cy="256" r="152" fill="none" stroke="#60a5fa" stroke-width="22"/>
<path d="M256 150l65 47-25 77h-80l-25-77z" fill="#60a5fa"/>
<path d="M191 197l-67-4m172 4l67-4M216 274l-33 63m113-63l33 63M256 150v-48"
stroke="#60a5fa" stroke-width="18" fill="none" stroke-linecap="round"/>
</svg>"""


# ---------------------------------------------------------------------------
# Mobile-first dashboard
# ---------------------------------------------------------------------------

HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#0b1020">
<meta name="description" content="Read-only football market intelligence, accumulator decoding and odds comparison.">
<link rel="manifest" href="/manifest.webmanifest">
<link rel="icon" href="/icon.svg" type="image/svg+xml">
<title>SolveEase Surebet Intelligence</title>
<style>
:root {
  color-scheme: dark;
  --bg:#0b1020; --panel:#121a2c; --panel2:#182238;
  --line:#29364d; --text:#edf3ff; --muted:#9caec7;
  --blue:#60a5fa; --green:#34d399; --amber:#fbbf24;
  --red:#f87171; --radius:16px;
}
* { box-sizing:border-box; }
html { scroll-behavior:smooth; -webkit-text-size-adjust:100%; }
body {
  margin:0; padding:0 0 calc(26px + env(safe-area-inset-bottom));
  background:var(--bg); color:var(--text);
  font:16px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
}
button,input,select { font:inherit; }
button { touch-action:manipulation; cursor:pointer; }
a { color:var(--blue); }
header {
  padding:calc(15px + env(safe-area-inset-top)) 16px 14px;
  background:#0e1628; border-bottom:1px solid var(--line);
  position:sticky; top:0; z-index:5;
}
.header-inner,.wrap { max-width:1120px; margin:auto; }
.brand { display:flex; align-items:center; gap:12px; }
.logo {
  width:42px;height:42px;border-radius:13px;display:grid;place-items:center;
  background:#1e3a5f;color:#bfdbfe;font-weight:900;font-size:20px;
}
h1 { font-size:18px; line-height:1.2; margin:0; }
.eyebrow { color:var(--muted);font-size:11px;letter-spacing:.14em;font-weight:800; }
.topline { display:flex;align-items:center;justify-content:space-between;gap:10px; }
.pill {
  border:1px solid #31506e;border-radius:999px;padding:4px 9px;
  color:#bfdbfe;font-size:11px;font-weight:800;white-space:nowrap;
}
.wrap { padding:18px 14px 0; }
.hero { padding:6px 2px 18px; }
.hero h2 { font-size:clamp(25px,5vw,38px);line-height:1.12;margin:0 0 9px; }
.hero p { color:var(--muted);max-width:690px;margin:0; }
.grid { display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px; }
.metric,.panel,.feature-card {
  min-width:0;background:var(--panel);border:1px solid var(--line);
  border-radius:var(--radius);padding:15px;
}
.metric .label { color:var(--muted);font-size:12px; }
.metric .value { font-size:22px;font-weight:800;line-height:1.35;overflow-wrap:anywhere; }
.metric .sub { color:var(--muted);font-size:11px; }
.tabs {
  display:flex;gap:7px;overflow-x:auto;padding:13px 0 6px;
  scrollbar-width:thin;position:sticky;top:71px;background:var(--bg);z-index:4;
}
.tab {
  flex:0 0 auto;border:1px solid var(--line);border-radius:999px;
  background:var(--panel);color:var(--muted);padding:9px 13px;min-height:42px;
}
.tab.active { color:#fff;background:#1d3c60;border-color:#37689a; }
.section { margin-top:15px;scroll-margin-top:132px; }
.section-head { display:flex;align-items:start;justify-content:space-between;gap:12px;margin-bottom:10px; }
h2.section-title { font-size:19px;margin:0; }
h3 { font-size:16px;margin:0 0 7px; }
p { overflow-wrap:anywhere; }
.muted { color:var(--muted); }
.small { font-size:12px; }
label { display:block;color:var(--muted);font-size:13px;margin:12px 0 6px; }
input,select {
  display:block;width:100%;min-height:48px;padding:11px 12px;
  border:1px solid #35445d;border-radius:11px;background:#0d1526;color:var(--text);
}
.actions { display:flex;flex-wrap:wrap;gap:8px;margin-top:12px; }
.btn {
  min-height:46px;padding:11px 15px;border-radius:11px;
  border:1px solid #36516f;background:#1e3a5f;color:#eff6ff;font-weight:750;
}
.btn.secondary { background:#192338;border-color:var(--line);color:var(--text); }
.btn:disabled { opacity:.55;cursor:wait; }
.notice {
  margin-top:11px;padding:11px 12px;background:#0c1526;border:1px solid var(--line);
  border-radius:10px;color:var(--muted);font-size:13px;overflow-wrap:anywhere;
}
.result { white-space:normal;overflow-wrap:anywhere; }
.leg,.item {
  border:1px solid var(--line);border-radius:12px;background:#0d1526;
  padding:12px;margin-top:9px;
}
.row { display:flex;justify-content:space-between;align-items:start;gap:10px; }
.price { font-weight:850;font-size:19px; }
.good { color:var(--green); }
.warn { color:var(--amber); }
.bad { color:var(--red); }
.features { display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px; }
.feature-card h3 { font-size:14px; }
.feature-card p { color:var(--muted);font-size:12px;margin:0 0 9px; }
.feature-status { font-size:11px;color:var(--amber); }
footer { margin:26px 0 0;padding:18px 4px;border-top:1px solid var(--line);color:var(--muted);font-size:12px; }
[hidden] { display:none!important; }
@media(min-width:760px) {
  .wrap { padding-left:22px;padding-right:22px; }
  .grid.metrics { grid-template-columns:repeat(4,minmax(0,1fr)); }
  .features { grid-template-columns:repeat(3,minmax(0,1fr)); }
  .main-grid { display:grid;grid-template-columns:1.15fr .85fr;gap:14px;align-items:start; }
  .tabs { top:71px; }
}
@media(max-width:380px) {
  .grid,.features { grid-template-columns:1fr; }
  .pill { font-size:10px; }
  .wrap { padding-left:10px;padding-right:10px; }
}
@media(prefers-reduced-motion:reduce) { html { scroll-behavior:auto; } }
</style>
</head>
<body>
<header>
 <div class="header-inner topline">
  <div class="brand">
   <div class="logo" aria-hidden="true">S</div>
   <div><h1>SolveEase Surebet Intelligence</h1><div class="eyebrow">FOOTBALL MARKET ANALYSIS</div></div>
  </div>
  <span class="pill">READ-ONLY</span>
 </div>
</header>

<main class="wrap">
 <section class="hero">
  <h2>See the evidence behind every pick.</h2>
  <p>Decode supported accumulators, inspect individual legs, compare available prices and explore match intelligence. No bookmaker login or bet placement.</p>
 </section>

 <section class="grid metrics" aria-label="Service status">
  <div class="metric"><div class="label">Fixtures & markets</div><div class="value" id="mQuotes">—</div><div class="sub" id="mQuotesSub">Waiting for scan</div></div>
  <div class="metric"><div class="label">Configured feeds</div><div class="value" id="mFeeds">—</div><div class="sub">Server configuration</div></div>
  <div class="metric"><div class="label">Arbitrage candidates</div><div class="value" id="mArb">—</div><div class="sub">Requires manual verification</div></div>
  <div class="metric"><div class="label">Data health</div><div class="value warn" id="mHealth">Checking</div><div class="sub" id="mHealthSub">Source status is not a prediction</div></div>
 </section>

 <nav class="tabs" aria-label="Dashboard navigation">
  <button class="tab active" data-target="scanner">Code scanner</button>
  <button class="tab" data-target="fixtures">Match intelligence</button>
  <button class="tab" data-target="markets">Market scanner</button>
  <button class="tab" data-target="products">All tools</button>
 </nav>

 <div class="main-grid">
  <section class="panel section" id="scanner">
   <div class="section-head"><div><h2 class="section-title">1. Decode an accumulator</h2><div class="muted small">Supported share-code lookup</div></div></div>
   <label for="bookmaker">Bookmaker</label>
   <select id="bookmaker"><option value="sportybet">SportyBet</option></select>
   <label for="code">Shared booking code</label>
   <input id="code" maxlength="160" autocomplete="off" autocapitalize="none" spellcheck="false" placeholder="Paste a booking code">
   <div class="actions">
    <button class="btn" id="decode">Decode and analyse</button>
    <button class="btn secondary" id="clear">Clear</button>
    <button class="btn secondary" id="share" hidden>Share analysis</button>
   </div>
   <div class="notice" id="lookupStatus" role="status" aria-live="polite">Ready. Enter a code to begin.</div>
   <div id="ticketResult" class="result" aria-live="polite"></div>
   <p class="muted small">Only the supported bookmaker's share endpoint is queried. Code format alone cannot reliably identify a bookmaker.</p>
  </section>

  <section class="panel section" id="fixtures">
   <div class="section-head"><div><h2 class="section-title">2. Match intelligence</h2><div class="muted small">Fixture and model data, when available</div></div></div>
   <div class="notice">The current deployment has no configured market feeds. Fixture lists, team form and probabilities will appear only when a suitable source or validated model is connected.</div>
   <div id="fixtureResult" class="result"></div>
   <div class="actions"><button class="btn secondary" id="refresh">Refresh service status</button></div>
  </section>
 </div>

 <section class="panel section" id="markets">
  <div class="section-head"><div><h2 class="section-title">3. Market scanner</h2><div class="muted small">Server-side feed scan</div></div></div>
  <p class="muted">The scanner compares quotes from configured, authorized JSON feeds. It does not search private APIs or substitute invented prices.</p>
  <div class="actions">
   <button class="btn" id="scan">Run configured feed scan</button>
  </div>
  <div class="notice" id="scanStatus" role="status" aria-live="polite">Scan has not been requested from this page.</div>
  <div id="scanResult" class="result"></div>
 </section>

 <section class="section" id="products">
  <div class="section-head"><div><h2 class="section-title">4. Intelligence toolkit</h2><div class="muted small">Designed for future Free, Pro and Business tiers</div></div></div>
  <div class="features">
   <article class="feature-card"><h3>Accumulator Risk Lab</h3><p>Combined odds, implied probabilities, leg quality and correlated outcomes.</p><div class="feature-status">Core analysis</div></article>
   <article class="feature-card"><h3>Alternative Pick Finder</h3><p>Compare actual alternative outcomes for the same event and market.</p><div class="feature-status">Requires matching quotes</div></article>
   <article class="feature-card"><h3>Today's Fixtures</h3><p>Search matches by competition, team and kickoff time.</p><div class="feature-status">Requires fixture source</div></article>
   <article class="feature-card"><h3>Team Form & H2H</h3><p>Recent results, home/away records, goals and sample sizes.</p><div class="feature-status">Requires match history</div></article>
   <article class="feature-card"><h3>Probability Explorer</h3><p>Home/draw/away and supported goals-market estimates.</p><div class="feature-status">Requires validated model</div></article>
   <article class="feature-card"><h3>Best Odds Finder</h3><p>Compare prices for the same selection across identified sources.</p><div class="feature-status">Requires odds feeds</div></article>
   <article class="feature-card"><h3>Fair Odds & Value</h3><p>Compare offered prices against documented model probabilities.</p><div class="feature-status">Requires validated model</div></article>
   <article class="feature-card"><h3>Odds Movement</h3><p>Timestamped price history and market movement charts.</p><div class="feature-status">Requires stored snapshots</div></article>
   <article class="feature-card"><h3>Goals & Scoreline Lab</h3><p>Expected goals and plausible scoreline distributions.</p><div class="feature-status">Requires suitable data</div></article>
   <article class="feature-card"><h3>Prediction Track Record</h3><p>Historical accuracy, calibration and independently recorded results.</p><div class="feature-status">Requires stored forecasts</div></article>
   <article class="feature-card"><h3>Watchlist & Alerts</h3><p>Monitor chosen matches and notify on configured changes.</p><div class="feature-status">Future monitoring service</div></article>
   <article class="feature-card"><h3>Research & Export</h3><p>Historical backtests, analysis reports and permitted CSV exports.</p><div class="feature-status">Future Pro / Business</div></article>
  </div>
 </section>

 <section class="panel section" id="install">
  <h2 class="section-title">Use SolveEase like an app</h2>
  <p class="muted small">Responsive for iPhone Safari, Android and desktop. On supported browsers, use the browser's Add to Home Screen or Install option. Live prices and APIs still require internet access.</p>
  <div class="actions"><button class="btn secondary" id="installHelp">Installation help</button></div>
  <div class="notice" id="installStatus" hidden></div>
 </section>

 <footer>
  SolveEase Surebet Intelligence · Read-only · No automated staking.
  Prices can change. Estimates are not guarantees. All comparisons require source,
  timing and settlement verification. Offline mode does not cache private codes,
  API responses or live market prices.
 </footer>
</main>

<script>
"use strict";
const $ = id => document.getElementById(id);
let lastTicket = null;
let lastShareText = "";
let installPrompt = null;

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, c => ({
    "&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"
  })[c]);
}
function fmt(value, digits=2) {
  const n = Number(value);
  return Number.isFinite(n) ? n.toFixed(digits) : "—";
}
async function api(path, options={}) {
  const response = await fetch(path, {
    cache:"no-store",
    headers:{"Content-Type":"application/json",...(options.headers||{})},
    ...options
  });
  const data = await response.json().catch(() => ({message:"Invalid server response"}));
  if (!response.ok) throw new Error(data.message || `HTTP ${response.status}`);
  return data;
}
function status(id, text, cls="") {
  const el = $(id); el.textContent = text; el.className = "notice " + cls;
}
function renderTicket(data) {
  const ticket = data.ticket;
  lastTicket = ticket;
  $("share").hidden = false;
  $("ticketResult").innerHTML = "";
  const summary = document.createElement("div");
  summary.className = "item";
  summary.innerHTML = `<div class="row"><strong>${escapeHtml(ticket.bookmaker || "Unknown")}</strong><span class="pill">${escapeHtml(ticket.leg_count)} legs</span></div>
  <p class="muted small">Combined odds from parsed legs: <strong>${fmt(ticket.combined_odds_from_parsed_legs,4)}</strong></p>
  <p class="muted small">This calculation is not independently verified against the bookmaker's displayed total.</p>`;
  $("ticketResult").appendChild(summary);

  (ticket.legs || []).forEach((leg, i) => {
    const card = document.createElement("article");
    card.className = "leg";
    card.innerHTML = `<div class="row"><strong>Leg ${i+1}: ${escapeHtml(leg.event)}</strong><span class="price">${fmt(leg.odds,2)}</span></div>
    <div>${escapeHtml(leg.market)} · ${escapeHtml(leg.selection)}</div>
    <div class="muted small">Implied probability before margin: ${fmt(leg.implied_probability_percent)}%</div>`;
    $("ticketResult").appendChild(card);
  });

  if (data.analysis) renderAnalysis(data.analysis);
  lastShareText = JSON.stringify({ticket,analysis:data.analysis||null},null,2);
}
function renderAnalysis(analysis) {
  const section = document.createElement("div");
  section.className = "item";
  section.innerHTML = `<h3>Market comparison</h3><p class="muted small">${analysis.market_data_available ? "Configured market quotes were available." : "No configured market quotes were available; no alternatives can be verified."}</p>`;
  (analysis.legs || []).forEach(leg => {
    const card = document.createElement("div");
    card.className = "leg";
    const best = leg.best_observed_matching_price;
    card.innerHTML = `<strong>Leg ${leg.leg_number}: ${escapeHtml(leg.event)}</strong>
    <div class="muted small">${escapeHtml(leg.market)} · ${escapeHtml(leg.selection)}</div>
    <div>Original odds: <strong>${fmt(leg.original_odds)}</strong></div>
    <div>Best observed same-selection price: <strong>${best ? fmt(best.odds) : "No comparable quote"}</strong></div>
    <div class="muted small">${best ? "Source: "+escapeHtml(best.bookmaker||"Unattributed")+" · Updated: "+escapeHtml(best.updated_at||"not supplied") : "Matching event, market and selection quotes were not found."}</div>`;
    if (leg.alternative_outcomes && leg.alternative_outcomes.length) {
      const list = document.createElement("div");
      list.className = "notice";
      list.innerHTML = "<strong>Other outcomes in this market (not recommendations)</strong>" +
        leg.alternative_outcomes.map(a => `<div>${escapeHtml(a.selection)} · ${fmt(a.odds)} · ${escapeHtml(a.bookmaker||"Source not attributed")}</div>`).join("");
      card.appendChild(list);
    }
    section.appendChild(card);
  });
  $("ticketResult").appendChild(section);
}
async function decode() {
  const code = $("code").value.trim();
  if (!code) { status("lookupStatus","Enter a booking code first.","warn"); return; }
  $("decode").disabled = true;
  $("ticketResult").replaceChildren();
  $("share").hidden = true;
  status("lookupStatus","Contacting the selected share-code endpoint…");
  try {
    const data = await api("/api/lookup",{
      method:"POST",body:JSON.stringify({code,bookmaker:$("bookmaker").value})
    });
    if (!data.ok) {
      status("lookupStatus",data.message || data.status || "Lookup failed.","warn");
      $("ticketResult").textContent = JSON.stringify(data,null,2);
      return;
    }
    status("lookupStatus","Ticket decoded. Review each leg and the data-quality notes.","good");
    renderTicket(data);
  } catch (e) {
    status("lookupStatus",e.message || "Lookup failed.","bad");
  } finally { $("decode").disabled = false; }
}
async function refreshStatus() {
  try {
    const h = await api("/health");
    $("mFeeds").textContent = h.configured_market_feed_count;
    $("mHealth").textContent = h.configured_market_feed_count ? "Configured" : "Not configured";
    $("mHealth").className = "value " + (h.configured_market_feed_count ? "good" : "warn");
    $("mHealthSub").textContent = h.configured_market_feed_count ? "Feed endpoints configured" : "No market feeds configured";
    $("fixtureResult").textContent = h.configured_market_feed_count
      ? "Feeds are configured. Run the scanner to check availability and usable quote counts."
      : "No fixture or market source is configured yet.";
  } catch(e) {
    $("mHealth").textContent = "Unavailable";
    $("mHealthSub").textContent = e.message;
  }
}
async function scan() {
  $("scan").disabled = true;
  status("scanStatus","Fetching configured feeds…");
  $("scanResult").replaceChildren();
  try {
    const data = await api("/api/scan",{method:"POST",body:"{}"});
    $("mFeeds").textContent = data.configured_feed_count;
    $("mQuotes").textContent = data.usable_quote_count;
    $("mQuotesSub").textContent = "Usable quotes returned";
    $("mArb").textContent = data.candidate_count;
    $("mHealth").textContent = data.configured_feed_count ? "Checked" : "Not configured";
    $("mHealth").className = "value " + (data.configured_feed_count ? "good" : "warn");
    status("scanStatus",data.message,data.configured_feed_count ? "" : "warn");

    const summary = document.createElement("div");
    summary.className = "item";
    summary.innerHTML = `<strong>Scan summary</strong><p class="muted small">Configured feeds: ${data.configured_feed_count} · Usable quotes: ${data.usable_quote_count} · Candidates: ${data.candidate_count}</p>`;
    $("scanResult").appendChild(summary);
    (data.candidates||[]).forEach(c => {
      const el = document.createElement("article");
      el.className = "leg";
      el.innerHTML = `<strong>${escapeHtml(c.event)}</strong><div>${escapeHtml(c.market)}</div>
      <div class="muted small">Theoretical margin: ${fmt(c.theoretical_margin_percent,4)}%</div>
      <div class="warn small">${escapeHtml(c.warning)}</div>`;
      $("scanResult").appendChild(el);
    });
    (data.feed_health||[]).forEach(f => {
      const el = document.createElement("div");
      el.className = "item";
      el.innerHTML = `<div class="row"><strong>${escapeHtml(f.source)}</strong><span class="${f.status==="ok"?"good":"warn"}">${escapeHtml(f.status)}</span></div>
      <div class="muted small">Quotes: ${f.quotes} · ${escapeHtml(f.error||"Checked "+(f.elapsed_ms??"")+" ms")}</div>`;
      $("scanResult").appendChild(el);
    });
  } catch(e) {
    status("scanStatus",e.message || "Scan failed.","bad");
  } finally { $("scan").disabled = false; }
}
$("decode").addEventListener("click",decode);
$("code").addEventListener("keydown",e=>{if(e.key==="Enter")decode();});
$("clear").addEventListener("click",()=>{
  $("code").value=""; $("ticketResult").replaceChildren();
  $("share").hidden=true; lastTicket=null; lastShareText="";
  status("lookupStatus","Cleared. Ready for a new code.");
});
$("scan").addEventListener("click",scan);
$("refresh").addEventListener("click",refreshStatus);
$("share").addEventListener("click",async()=>{
  const text = lastShareText || "";
  if (!text) return;
  try {
    if (navigator.share) await navigator.share({title:"SolveEase analysis",text});
    else if (navigator.clipboard) { await navigator.clipboard.writeText(text); status("lookupStatus","Analysis JSON copied to clipboard."); }
    else { status("lookupStatus","Sharing is not supported by this browser.","warn"); }
  } catch(e) {
    if (e.name!=="AbortError") status("lookupStatus","Could not share this analysis.","warn");
  }
});
$("installHelp").addEventListener("click",()=>{
  const el=$("installStatus"); el.hidden=false;
  el.textContent=/iphone|ipad|ipod/i.test(navigator.userAgent)
    ? "On iPhone: open this site in Safari, tap Share, then Add to Home Screen. Menu wording may vary by iOS version."
    : "In a supported browser, open the browser menu and choose Install app or Add to Home Screen if offered.";
});
document.querySelectorAll(".tab").forEach(tab=>tab.addEventListener("click",()=>{
  document.querySelectorAll(".tab").forEach(t=>t.classList.toggle("active",t===tab));
  $(tab.dataset.target).scrollIntoView({behavior:"smooth",block:"start"});
}));
window.addEventListener("beforeinstallprompt",event=>{
  event.preventDefault(); installPrompt=event;
});
if ("serviceWorker" in navigator) {
  window.addEventListener("load",()=>navigator.serviceWorker.register("/sw.js").catch(()=>{}));
}
refreshStatus();
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# HTTP API
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "SolveEase/6.0.0"

    def log_message(self, fmt, *args):
        # Avoid logging submitted booking codes or request bodies.
        message = fmt % args
        print(f"{self.log_date_time_string()} {self.client_address[0]} {message}")

    def read_json_body(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise ValueError("Invalid Content-Length.")

        if length < 0 or length > 1_000_000:
            raise ValueError("Request body is too large.")

        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("Request body must be valid JSON.") from exc

        if not isinstance(payload, dict):
            raise ValueError("JSON request body must be an object.")
        return payload

    def do_HEAD(self):
        path = urlparse(self.path).path
        if path in ("/", "/health", "/healthz"):
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()
        else:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/":
            return text_response(self, 200, HTML, "text/html; charset=utf-8")
        if path == "/health" or path == "/healthz":
            with STATE_LOCK:
                state = dict(STATE)
            return json_response(self, 200, {
                "status": "online",
                "service": APP_NAME,
                "version": VERSION,
                "read_only": True,
                "sportybet_lookup_implemented": True,
                "configured_market_feed_count": len(FEED_URLS),
                "last_lookup_status": state["last_lookup_status"],
                "last_lookup_at": state["last_lookup_at"],
                "last_scan_status": state["last_scan_status"],
                "last_scan_at": state["last_scan_at"],
                "note": (
                    "Online status does not prove bookmaker endpoints or "
                    "market feeds are available."
                ),
            })
        if path == "/state":
            with STATE_LOCK:
                state = dict(STATE)
            return json_response(self, 200, {
                "service": APP_NAME,
                "version": VERSION,
                "state": state,
                "configured_feed_count": len(FEED_URLS),
            })
        if path == "/manifest.webmanifest":
            return json_response(self, 200, MANIFEST)
        if path == "/sw.js":
            return text_response(
                self, 200, SERVICE_WORKER,
                "application/javascript; charset=utf-8"
            )
        if path == "/icon.svg":
            return text_response(self, 200, ICON_SVG, "image/svg+xml")
        if path == "/api/scan":
            return json_response(self, 200, run_scan())
        return json_response(self, 404, {"error": "not_found"})

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            payload = self.read_json_body()
        except ValueError as exc:
            return json_response(self, 400, {
                "ok": False,
                "status": "bad_request",
                "message": str(exc),
            })

        if path == "/api/lookup":
            bookmaker = clean_string(payload.get("bookmaker"), 40) or "sportybet"
            if bookmaker.lower() not in ("sportybet", "sporty_bet"):
                return json_response(self, 400, {
                    "ok": False,
                    "status": "unsupported_bookmaker",
                    "message": (
                        "Only SportyBet share-code lookup is implemented. "
                        "A code alone cannot reliably identify a bookmaker."
                    ),
                })

            result = lookup_sportybet(payload.get("code"))
            with STATE_LOCK:
                STATE["last_lookup_status"] = result.get("status", "error")
                STATE["last_lookup_at"] = utc_now()

            if result.get("ok") and result.get("ticket"):
                quotes, _health = fetch_configured_markets()
                result["analysis"] = inspect_ticket(result["ticket"], quotes)
                result["analysis"]["market_feed_count"] = len(FEED_URLS)
                if not quotes:
                    result["analysis"]["market_data_note"] = (
                        "No usable quotes were retrieved from configured feeds. "
                        "No alternative prices have been invented."
                    )
            return json_response(self, 200, result)

        if path == "/api/analyse":
            # Internal/manual JSON analysis endpoint; deliberately not exposed
            # as a manual quote editor in the public dashboard.
            quotes = payload.get("quotes", [])
            if not isinstance(quotes, list):
                return json_response(self, 400, {
                    "ok": False,
                    "message": "quotes must be a list",
                })
            safe_quotes = []
            for q in quotes[:10000]:
                if not isinstance(q, dict):
                    continue
                odds = safe_float(q.get("odds"))
                if not valid_odds(odds):
                    continue
                safe_quotes.append({
                    "sport": clean_string(q.get("sport") or "Football", 80),
                    "event": clean_string(q.get("event"), 300),
                    "event_id": clean_string(q.get("event_id"), 160),
                    "market": clean_string(q.get("market"), 200),
                    "selection": clean_string(q.get("selection"), 200),
                    "odds": odds,
                    "bookmaker": clean_string(q.get("bookmaker"), 120),
                    "updated_at": clean_string(q.get("updated_at"), 100),
                    "source_url": None,
                    "exhaustive_two_outcome": (
                        q.get("exhaustive_two_outcome") is True
                    ),
                })
            return json_response(self, 200, {
                "ok": True,
                "quote_count": len(safe_quotes),
                "arbitrage_candidates": scan_arbitrage(safe_quotes),
                "note": "Analysis uses only supplied quotes; their accuracy is not verified.",
            })

        if path == "/api/scan":
            return json_response(self, 200, run_scan())

        return json_response(self, 404, {"error": "not_found"})


def main():
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    server.daemon_threads = True
    print(f"{APP_NAME} v{VERSION} starting on {HOST}:{PORT}")
    print(f"Configured market feed URLs: {len(FEED_URLS)}")
    print("Read-only mode enabled; no bet placement or automated staking.")
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        print("Server stopped.")


if __name__ == "__main__":
    main()
