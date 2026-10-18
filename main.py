
#!/usr/bin/env python3
"""
SolveEase Surebet Intelligence
Read-only accumulator decoder, market comparator and arbitrage scanner.

Runtime: Python 3.10+
Dependencies: Python standard library only.

Routes:
  GET  /                 Mobile-friendly dashboard
  GET  /health            Health and configuration status
  GET  /state             Scanner state and capabilities
  POST /api/lookup        Decode a shared betting code
  POST /api/analyse       Analyse supplied market quotes
  POST /api/scan          Scan configured market feeds
  GET  /api/scan          Same as POST /api/scan
  GET  /api/lookup?code=  Optional lookup via query parameter

Optional environment variables:
  PORT=10000
  HOST=0.0.0.0
  SCAN_INTERVAL_SECONDS=900
  MARKET_FEED_URLS=https://example.org/feed.json;https://example.net/feed.json
  REQUEST_TIMEOUT_SECONDS=12
  MAX_RESPONSE_BYTES=4000000

Configured feeds must return JSON in the documented generic format
shown in the dashboard help. The app never places bets or requests
bookmaker credentials.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import threading
import time
import unicodedata
from datetime import datetime, timezone
from difflib import SequenceMatcher
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit, parse_qs
from urllib.request import Request, urlopen


APP_NAME = "SolveEase Surebet Intelligence"
APP_VERSION = "5.0.0"

HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "10000"))
REQUEST_TIMEOUT = max(3, min(60, int(os.getenv("REQUEST_TIMEOUT_SECONDS", "12"))))
MAX_RESPONSE_BYTES = max(
    100_000, min(20_000_000, int(os.getenv("MAX_RESPONSE_BYTES", "4000000")))
)
SCAN_INTERVAL_SECONDS = max(
    60, min(86400, int(os.getenv("SCAN_INTERVAL_SECONDS", "900")))
)

SPORTYBET_SHARE_URL = "https://www.sportybet.com/api/ng/orders/share/"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0 Safari/537.36"
)

SUPPORTED_BOOKMAKERS = (
    "SportyBet",
    "Bet9ja",
    "BetKing",
    "1xBet",
    "Betway",
    "Unknown",
)

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger(APP_NAME)

STATE_LOCK = threading.Lock()
STATE: dict[str, Any] = {
    "started_at": datetime.now(timezone.utc).isoformat(),
    "last_scan_at": None,
    "last_scan_status": "not_run",
    "last_scan_error": None,
    "last_scan_markets": 0,
    "last_scan_arbitrage_count": 0,
    "last_scan_feed_results": [],
    "last_lookup_at": None,
    "last_lookup_status": "not_run",
}


# ---------------------------------------------------------------------------
# General helpers
# ---------------------------------------------------------------------------

def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def as_dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def as_list(value: Any) -> list:
    return value if isinstance(value, list) else []


def first_value(*values: Any) -> Any:
    for value in values:
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        if isinstance(value, (dict, list)):
            continue
        return value
    return None


def to_float(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(str(value).replace(",", "").strip())
        if math.isfinite(number) and number > 1.0:
            return number
    except (TypeError, ValueError, OverflowError):
        pass
    return None


def normalize_text(value: Any) -> str:
    if value is None:
        return ""
    text = unicodedata.normalize("NFKD", str(value))
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.lower().replace("&", " and ")
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def similarity(left: Any, right: Any) -> float:
    a = normalize_text(left)
    b = normalize_text(right)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    return SequenceMatcher(None, a, b).ratio()


def safe_json_bytes(data: bytes) -> Any:
    text = data.decode("utf-8-sig", errors="strict")
    return json.loads(text)


def fetch_bytes(url: str, headers: dict | None = None) -> tuple[bytes, int]:
    """
    Read-only HTTP GET. Restricts schemes to HTTP(S), response size,
    and request duration. Returns response bytes and status code.
    """
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("Only valid HTTP(S) URLs are allowed.")

    request_headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json, text/plain, */*",
    }
    if headers:
        request_headers.update(headers)

    request = Request(url, headers=request_headers, method="GET")
    try:
        with urlopen(request, timeout=REQUEST_TIMEOUT) as response:
            data = response.read(MAX_RESPONSE_BYTES + 1)
            if len(data) > MAX_RESPONSE_BYTES:
                raise ValueError("The response exceeded the configured size limit.")
            return data, int(response.status)
    except HTTPError as exc:
        # Preserve the status code and bounded response body for diagnostics.
        data = exc.read(min(MAX_RESPONSE_BYTES, 200_000))
        raise RuntimeError(
            f"Remote server returned HTTP {exc.code}: "
            f"{data.decode('utf-8', errors='replace')[:300]}"
        ) from exc
    except URLError as exc:
        raise RuntimeError(f"Remote request failed: {exc.reason}") from exc


def parse_json_body(handler: BaseHTTPRequestHandler) -> dict:
    raw_length = handler.headers.get("Content-Length", "0")
    try:
        length = int(raw_length)
    except ValueError:
        raise ValueError("Invalid Content-Length.")

    if length < 0 or length > 1_000_000:
        raise ValueError("Request body is too large.")

    raw = handler.rfile.read(length) if length else b"{}"
    try:
        result = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Request body must be valid JSON.") from exc

    if not isinstance(result, dict):
        raise ValueError("JSON request body must be an object.")
    return result


def identify_bookmaker(code: str, explicit: str | None = None) -> dict:
    """
    A bookmaker name is a best-effort identification, not proof that
    a booking code is valid or that a bookmaker owns that code format.
    """
    supplied = normalize_text(explicit)
    aliases = {
        "sportybet": "SportyBet",
        "sporty": "SportyBet",
        "bet9ja": "Bet9ja",
        "betking": "BetKing",
        "1xbet": "1xBet",
        "xbet": "1xBet",
        "betway": "Betway",
    }

    if supplied in aliases:
        return {
            "bookmaker": aliases[supplied],
            "method": "user_supplied",
            "confidence": "unverified",
        }

    # This application can actively attempt the SportyBet share endpoint.
    # The code's characters alone are not sufficient proof of its origin.
    if code and re.fullmatch(r"[A-Za-z0-9_-]{3,200}", code):
        return {
            "bookmaker": "SportyBet",
            "method": "supported_share_endpoint_attempt",
            "confidence": "tentative",
        }

    return {
        "bookmaker": "Unknown",
        "method": "not_identified",
        "confidence": "unknown",
    }


# ---------------------------------------------------------------------------
# SportyBet shared accumulator lookup
# ---------------------------------------------------------------------------

def extract_outcomes_container(data: dict, ticket: dict) -> list:
    """
    SportyBet response shapes can change. Support common flat structures
    without assuming every undocumented response has the same schema.
    """
    candidates = [
        data.get("outcomes"),
        ticket.get("outcomes"),
        data.get("selections"),
        ticket.get("selections"),
    ]
    for candidate in candidates:
        if isinstance(candidate, list):
            return candidate
    return []


def extract_explicit_selection(item: dict) -> dict:
    """
    Convert one source outcome/leg into a normalized representation.

    Crucially, a market with multiple candidate outcomes is not guessed.
    """
    home = first_value(
        item.get("homeTeamName"),
        item.get("homeTeam"),
        item.get("home"),
        item.get("homeName"),
    )
    away = first_value(
        item.get("awayTeamName"),
        item.get("awayTeam"),
        item.get("away"),
        item.get("awayName"),
    )
    event = first_value(
        item.get("eventName"),
        item.get("matchName"),
        item.get("fixtureName"),
        item.get("event"),
    )
    if not event and (home or away):
        event = f"{home or 'Unknown home'} vs {away or 'Unknown away'}"

    market = first_value(
        item.get("marketDesc"),
        item.get("marketName"),
        item.get("market"),
        item.get("marketType"),
        item.get("marketDescription"),
    )

    selection = first_value(
        item.get("selectedOutcome"),
        item.get("selectionName"),
        item.get("outcomeName"),
        item.get("selection"),
        item.get("pick"),
        item.get("outcome"),
    )

    odds = to_float(
        first_value(
            item.get("odds"),
            item.get("odd"),
            item.get("decimalOdds"),
            item.get("price"),
            item.get("oddsValue"),
        )
    )

    start_time = first_value(
        item.get("estimateStartTime"),
        item.get("startTime"),
        item.get("kickoff"),
        item.get("eventStartTime"),
    )

    event_id = first_value(
        item.get("eventId"),
        item.get("matchId"),
        item.get("fixtureId"),
    )

    # Some sources wrap a selected outcome in a nested market.
    if selection is None:
        markets = as_list(item.get("markets"))
        selected_candidates = []

        for market_item in markets:
            market_item = as_dict(market_item)
            market_name = first_value(
                market_item.get("marketDesc"),
                market_item.get("marketName"),
                market_item.get("name"),
                market_item.get("desc"),
                market,
            )

            outcomes = as_list(
                first_value(
                    market_item.get("outcomes"),
                    market_item.get("selections"),
                    [],
                )
            )

            for outcome_item in outcomes:
                outcome_item = as_dict(outcome_item)
                marker = first_value(
                    outcome_item.get("selected"),
                    outcome_item.get("isSelected"),
                    outcome_item.get("checked"),
                )
                if marker is True or str(marker).lower() in ("true", "1", "yes"):
                    selected_candidates.append(
                        (market_name, outcome_item)
                    )

        # Only an explicit selected marker is accepted.
        if len(selected_candidates) == 1:
            market, chosen = selected_candidates[0]
            selection = first_value(
                chosen.get("desc"),
                chosen.get("name"),
                chosen.get("outcomeName"),
                chosen.get("selectionName"),
                chosen.get("label"),
            )
            odds = to_float(
                first_value(
                    chosen.get("odds"),
                    chosen.get("odd"),
                    chosen.get("price"),
                    odds,
                )
            )

    complete = bool(event and market and selection and odds)

    return {
        "event": str(event) if event is not None else None,
        "home": str(home) if home is not None else None,
        "away": str(away) if away is not None else None,
        "event_id": str(event_id) if event_id is not None else None,
        "market": str(market) if market is not None else None,
        "selection": str(selection) if selection is not None else None,
        "odds": odds,
        "start_time": start_time,
        "bookmaker": "SportyBet",
        "complete": complete,
        "warnings": [] if complete else [
            "One or more required fields could not be verified from the response."
        ],
    }


def lookup_sportybet(code: str) -> dict:
    code = str(code or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{3,200}", code):
        return {
            "ok": False,
            "status": "invalid_code_format",
            "error": "Enter a valid-looking booking code using letters, numbers, _ or -.",
            "bookmaker": "SportyBet",
        }

    url = SPORTYBET_SHARE_URL + quote(code, safe="")
    headers = {
        "Current-Country": "NG",
        "Referer": "https://www.sportybet.com/ng/",
        "Origin": "https://www.sportybet.com",
    }

    try:
        raw, status_code = fetch_bytes(url, headers=headers)
        payload = safe_json_bytes(raw)
        if not isinstance(payload, dict):
            raise ValueError("The response JSON root was not an object.")
    except Exception as exc:
        with STATE_LOCK:
            STATE["last_lookup_at"] = utc_now()
            STATE["last_lookup_status"] = "request_failed"
        return {
            "ok": False,
            "status": "lookup_failed",
            "bookmaker": "SportyBet",
            "code": code,
            "error": str(exc),
            "endpoint_live_verified": False,
            "note": "The request failed; this does not establish whether the code is valid.",
        }

    business_code = payload.get("bizCode")
    message = first_value(payload.get("message"), payload.get("msg"))
    data = as_dict(payload.get("data"))

    # A non-success business response may still arrive over HTTP 200.
    if business_code is not None and str(business_code) not in ("10000", "0"):
        with STATE_LOCK:
            STATE["last_lookup_at"] = utc_now()
            STATE["last_lookup_status"] = "remote_rejected"
        return {
            "ok": False,
            "status": "remote_rejected",
            "bookmaker": "SportyBet",
            "code": code,
            "http_status": status_code,
            "business_code": business_code,
            "message": message,
            "endpoint_live_verified": False,
            "note": "The endpoint returned a rejection or non-success business code.",
        }

    ticket = as_dict(data.get("ticket"))
    if not ticket:
        ticket = as_dict(data.get("order"))

    raw_outcomes = extract_outcomes_container(data, ticket)
    legs = [
        extract_explicit_selection(as_dict(item))
        for item in raw_outcomes
        if isinstance(item, dict)
    ]

    # Preserve source counts when available; do not claim all legs decoded
    # just because one or more objects could be parsed.
    source_count_value = first_value(
        ticket.get("selectionCount"),
        ticket.get("outcomeCount"),
        data.get("selectionCount"),
        data.get("outcomeCount"),
    )
    try:
        source_count = int(source_count_value) if source_count_value is not None else None
    except (TypeError, ValueError):
        source_count = None

    if source_count is None and isinstance(raw_outcomes, list):
        source_count = len(raw_outcomes)

    complete_count = sum(1 for leg in legs if leg["complete"])
    all_complete = bool(legs) and complete_count == len(legs)
    source_count_matches = (
        source_count is not None and source_count == len(legs)
    )
    fully_verified = all_complete and source_count_matches

    combined_odds = None
    if fully_verified:
        combined_odds = math.prod(leg["odds"] for leg in legs)

    ticket_id = first_value(
        ticket.get("ticketId"),
        ticket.get("orderId"),
        ticket.get("id"),
        data.get("ticketId"),
        data.get("orderId"),
    )
    stake = first_value(
        ticket.get("stake"),
        ticket.get("betAmount"),
        ticket.get("amount"),
        data.get("stake"),
    )
    potential_return = first_value(
        ticket.get("potentialReturn"),
        ticket.get("possibleWin"),
        ticket.get("returnAmount"),
        data.get("potentialReturn"),
    )

    normalized = {
        "schema": "solveease.normalized-ticket.v1",
        "bookmaker": "SportyBet",
        "source_code": code,
        "currency": None,
        "ticket_id": str(ticket_id) if ticket_id is not None else None,
        "stake": stake,
        "potential_return": potential_return,
        "selection_count_reported": source_count,
        "selection_count_decoded": len(legs),
        "selection_count_complete": complete_count,
        "combined_odds_calculated": combined_odds,
        "legs": legs,
        "created_at": utc_now(),
    }

    with STATE_LOCK:
        STATE["last_lookup_at"] = utc_now()
        STATE["last_lookup_status"] = (
            "decoded_complete" if fully_verified
            else "decoded_partial" if legs
            else "no_selections_found"
        )

    return {
        "ok": bool(legs),
        "status": (
            "decoded_complete" if fully_verified
            else "decoded_partial" if legs
            else "no_selections_found"
        ),
        "bookmaker": "SportyBet",
        "code": code,
        "http_status": status_code,
        "message": message,
        "ticket": normalized,
        "legs": legs,
        "selection_count": len(legs),
        "complete_selection_count": complete_count,
        "combined_odds": combined_odds,
        "fully_decoded": fully_verified,
        "endpoint_live_verified": False,
        "lookup_response_verified": True,
        "note": (
            "A parseable response was received for this request. "
            "This does not prove persistent endpoint availability, code validity "
            "beyond the returned payload, or that odds remain current."
        ),
    }


# ---------------------------------------------------------------------------
# Market normalization and matching
# ---------------------------------------------------------------------------

def normalize_quote(raw: dict, feed_bookmaker: str | None = None) -> dict | None:
    raw = as_dict(raw)

    event = first_value(
        raw.get("event"),
        raw.get("eventName"),
        raw.get("match"),
        raw.get("fixture"),
    )
    home = first_value(raw.get("home"), raw.get("homeTeam"), raw.get("homeTeamName"))
    away = first_value(raw.get("away"), raw.get("awayTeam"), raw.get("awayTeamName"))
    if not event and (home or away):
        event = f"{home or 'Unknown home'} vs {away or 'Unknown away'}"

    market = first_value(
        raw.get("market"),
        raw.get("marketName"),
        raw.get("marketDesc"),
        raw.get("marketType"),
    )
    selection = first_value(
        raw.get("selection"),
        raw.get("outcome"),
        raw.get("outcomeName"),
        raw.get("selectedOutcome"),
        raw.get("name"),
    )
    odds = to_float(
        first_value(raw.get("odds"), raw.get("odd"), raw.get("price"))
    )

    if not event or not market or not selection or not odds:
        return None

    bookmaker = first_value(
        raw.get("bookmaker"),
        raw.get("provider"),
        feed_bookmaker,
        "Unknown",
    )

    return {
        "sport": str(first_value(raw.get("sport"), raw.get("sportName"), "Unknown")),
        "event": str(event),
        "home": str(home) if home else None,
        "away": str(away) if away else None,
        "event_id": str(first_value(raw.get("event_id"), raw.get("eventId"), "")) or None,
        "market": str(market),
        "selection": str(selection),
        "odds": odds,
        "bookmaker": str(bookmaker),
        "start_time": first_value(raw.get("start_time"), raw.get("startTime"), raw.get("kickoff")),
        "source": str(first_value(raw.get("source"), feed_bookmaker, "configured_feed")),
        "fetched_at": utc_now(),
    }


def extract_feed_quotes(payload: Any, feed_name: str) -> list[dict]:
    """
    Generic feed contract:
      {"markets":[
        {"sport":"Football","event":"Team A vs Team B",
         "market":"Match Winner","outcomes":[
           {"selection":"Team A","odds":2.1},
           {"selection":"Draw","odds":3.2},
           {"selection":"Team B","odds":3.5}
         ]}
      ]}

    Also accepts flat quotes:
      {"quotes":[{"sport":"Football","event":"A vs B",
                  "market":"Match Winner","selection":"A","odds":2.1}]}
    """
    payload = as_dict(payload)
    result = []

    flat = payload.get("quotes")
    if isinstance(flat, list):
        for item in flat:
            quote_item = normalize_quote(as_dict(item), feed_name)
            if quote_item:
                result.append(quote_item)

    markets = payload.get("markets")
    if not isinstance(markets, list):
        markets = payload.get("events", [])

    for market_item in as_list(markets):
        market_item = as_dict(market_item)
        base = {
            "sport": first_value(market_item.get("sport"), market_item.get("sportName")),
            "event": first_value(
                market_item.get("event"),
                market_item.get("eventName"),
                market_item.get("match"),
            ),
            "home": first_value(market_item.get("home"), market_item.get("homeTeam")),
            "away": first_value(market_item.get("away"), market_item.get("awayTeam")),
            "event_id": first_value(market_item.get("event_id"), market_item.get("eventId")),
            "market": first_value(
                market_item.get("market"),
                market_item.get("marketName"),
                market_item.get("marketDesc"),
            ),
            "start_time": first_value(
                market_item.get("start_time"),
                market_item.get("startTime"),
                market_item.get("kickoff"),
            ),
            "bookmaker": first_value(market_item.get("bookmaker"), feed_name),
        }

        outcomes = first_value(
            market_item.get("outcomes"),
            market_item.get("selections"),
            [],
        )
        for outcome_item in as_list(outcomes):
            outcome_item = as_dict(outcome_item)
            merged = dict(base)
            merged.update(outcome_item)
            quote_item = normalize_quote(merged, feed_name)
            if quote_item:
                result.append(quote_item)

        # Also accept a flat market object containing selection + odds.
        if market_item.get("selection") is not None or market_item.get("outcome") is not None:
            quote_item = normalize_quote(market_item, feed_name)
            if quote_item:
                result.append(quote_item)

    return result


def get_feed_urls() -> list[str]:
    raw = os.getenv("MARKET_FEED_URLS", "").strip()
    if not raw:
        return []
    urls = []
    for part in raw.split(";"):
        url = part.strip()
        if not url:
            continue
        parts = urlsplit(url)
        if parts.scheme in ("http", "https") and parts.hostname:
            urls.append(url)
    return urls[:20]


def fetch_configured_markets() -> tuple[list[dict], list[dict]]:
    all_quotes: list[dict] = []
    feed_results: list[dict] = []

    for index, url in enumerate(get_feed_urls(), start=1):
        feed_name = f"ConfiguredFeed{index}"
        try:
            raw, http_status = fetch_bytes(url)
            payload = safe_json_bytes(raw)
            quotes = extract_feed_quotes(payload, feed_name)
            all_quotes.extend(quotes)
            feed_results.append({
                "feed": feed_name,
                "status": "ok" if quotes else "no_supported_quotes",
                "http_status": http_status,
                "quotes": len(quotes),
            })
        except Exception as exc:
            log.warning("Feed %s failed: %s", feed_name, exc)
            feed_results.append({
                "feed": feed_name,
                "status": "error",
                "error": str(exc)[:300],
            })

    return all_quotes, feed_results


def event_matches(a: dict, b: dict) -> bool:
    aid = a.get("event_id")
    bid = b.get("event_id")
    if aid and bid and str(aid) == str(bid):
        return True

    if a.get("home") and a.get("away") and b.get("home") and b.get("away"):
        direct = (
            similarity(a["home"], b["home"]) >= 0.86
            and similarity(a["away"], b["away"]) >= 0.86
        )
        reversed_teams = (
            similarity(a["home"], b["away"]) >= 0.86
            and similarity(a["away"], b["home"]) >= 0.86
        )
        if direct or reversed_teams:
            return True

    return similarity(a.get("event"), b.get("event")) >= 0.90


def market_matches(a: dict, b: dict) -> bool:
    return similarity(a.get("market"), b.get("market")) >= 0.88


def same_selection(a: Any, b: Any) -> bool:
    return similarity(a, b) >= 0.90


def find_alternatives(leg: dict, quotes: list[dict]) -> list[dict]:
    target = {
        "event": leg.get("event"),
        "home": leg.get("home"),
        "away": leg.get("away"),
        "event_id": leg.get("event_id"),
        "market": leg.get("market"),
    }
    matches = []
    for quote_item in quotes:
        if not event_matches(target, quote_item):
            continue
        if not market_matches(target, quote_item):
            continue

        same_pick = same_selection(leg.get("selection"), quote_item.get("selection"))
        implied = 1.0 / quote_item["odds"] if quote_item.get("odds") else None
        matches.append({
            **quote_item,
            "same_selection_as_original": same_pick,
            "implied_probability": round(implied * 100, 3) if implied is not None else None,
            "comparison_note": (
                "Same selection; compare price only."
                if same_pick
                else "Different selection in a similarly named market; verify settlement rules."
            ),
        })

    # Highest odds first, but label whether it is the same selection.
    return sorted(matches, key=lambda q: q.get("odds", 0), reverse=True)[:30]


def analyse_ticket(ticket: dict, quotes: list[dict]) -> dict:
    legs = as_list(ticket.get("legs"))
    report_legs = []

    for index, raw_leg in enumerate(legs, start=1):
        leg = as_dict(raw_leg)
        alternatives = find_alternatives(leg, quotes) if leg.get("complete") else []
        same_pick_quotes = [
            item for item in alternatives
            if item.get("same_selection_as_original")
        ]
        best_same_pick = same_pick_quotes[0] if same_pick_quotes else None

        warnings = list(as_list(leg.get("warnings")))
        if not leg.get("complete"):
            warnings.append("Original selection is incomplete or ambiguous.")
        if not alternatives:
            warnings.append("No comparable external market quote was found.")
        if alternatives and not same_pick_quotes:
            warnings.append(
                "Comparable event/market candidates exist, but none clearly matches the original selection."
            )

        report_legs.append({
            "leg_number": index,
            "original": leg,
            "alternative_count": len(alternatives),
            "same_selection_quote_count": len(same_pick_quotes),
            "best_same_selection_quote": best_same_pick,
            "alternatives": alternatives,
            "recommendation": (
                "review_original"
                if not leg.get("complete")
                else "compare_prices"
                if best_same_pick
                else "insufficient_comparable_data"
            ),
            "warnings": warnings,
        })

    return {
        "schema": "solveease.ticket-analysis.v1",
        "generated_at": utc_now(),
        "selection_count": len(legs),
        "legs": report_legs,
        "market_quotes_used": len(quotes),
        "note": (
            "Alternatives are comparisons, not predictions. Similar market labels "
            "may have different settlement rules. Missing data is not evidence of safety."
        ),
    }


# ---------------------------------------------------------------------------
# Arbitrage calculations
# ---------------------------------------------------------------------------

def calculate_two_outcome_arbitrage(quotes: list[dict]) -> list[dict]:
    """
    This intentionally considers only markets explicitly marked exhaustive
    with exactly two distinct outcomes. Three-way markets, Asian handicaps,
    totals, props and incomplete markets are excluded.

    Feed contract: market objects may set:
      "exhaustive_two_outcome": true

    That flag is a feed assertion and should only be used when the market's
    settlement rules genuinely define exactly two mutually exclusive outcomes.
    """
    grouped: dict[tuple, list[dict]] = {}

    for quote_item in quotes:
        # Only quote records carrying the explicit two-outcome assertion qualify.
        if quote_item.get("exhaustive_two_outcome") is not True:
            continue
        key = (
            normalize_text(quote_item.get("sport")),
            normalize_text(quote_item.get("event")),
            normalize_text(quote_item.get("market")),
        )
        grouped.setdefault(key, []).append(quote_item)

    opportunities = []

    for key, items in grouped.items():
        selections = {}
        for item in items:
            selection_key = normalize_text(item.get("selection"))
            if not selection_key:
                continue
            existing = selections.get(selection_key)
            if existing is None or item.get("odds", 0) > existing.get("odds", 0):
                selections[selection_key] = item

        if len(selections) != 2:
            continue

        best = sorted(selections.values(), key=lambda x: x.get("odds", 0), reverse=True)
        if len({normalize_text(x.get("selection")) for x in best}) != 2:
            continue

        # Require independent bookmakers, not just two quotes from one operator.
        if normalize_text(best[0].get("bookmaker")) == normalize_text(best[1].get("bookmaker")):
            continue

        odds = [best[0]["odds"], best[1]["odds"]]
        if not all(to_float(x) for x in odds):
            continue

        implied_sum = sum(1.0 / x for x in odds)
        if implied_sum >= 1.0:
            continue

        theoretical_roi = (1.0 / implied_sum - 1.0) * 100.0
        opportunities.append({
            "sport": best[0].get("sport"),
            "event": best[0].get("event"),
            "market": best[0].get("market"),
            "outcomes": best,
            "implied_probability_sum_percent": round(implied_sum * 100, 4),
            "theoretical_roi_percent": round(theoretical_roi, 4),
            "status": "candidate_requires_manual_verification",
            "warnings": [
                "Candidate only: verify identical settlement rules, current prices, "
                "limits, fees, and outcome exhaustiveness before drawing conclusions.",
                "Feed assertion of exhaustive two-outcome market has not been independently verified.",
            ],
        })

    return sorted(
        opportunities,
        key=lambda item: item.get("theoretical_roi_percent", 0),
        reverse=True,
    )


def analyse_supplied_quotes(body: dict) -> dict:
    raw_quotes = as_list(body.get("quotes"))
    quotes = []

    for item in raw_quotes:
        item = as_dict(item)
        normalized = normalize_quote(item, item.get("bookmaker"))
        if normalized:
            # Preserve this explicit assertion only when the caller supplies
            # a boolean true. Never infer exhaustiveness from two outcomes.
            normalized["exhaustive_two_outcome"] = (
                item.get("exhaustive_two_outcome") is True
            )
            quotes.append(normalized)

    arbitrage = calculate_two_outcome_arbitrage(quotes)
    return {
        "schema": "solveease.market-analysis.v1",
        "generated_at": utc_now(),
        "input_quotes": len(raw_quotes),
        "usable_quotes": len(quotes),
        "arbitrage_candidates": arbitrage,
        "status": "analysis_complete",
        "note": (
            "Only explicitly asserted exhaustive two-outcome markets are tested. "
            "This is mathematical screening, not a guarantee of executable profit."
        ),
    }


# ---------------------------------------------------------------------------
# Scanner
# ---------------------------------------------------------------------------

def run_scan() -> dict:
    quotes, feed_results = fetch_configured_markets()
    arbitrage = calculate_two_outcome_arbitrage(quotes)

    with STATE_LOCK:
        STATE["last_scan_at"] = utc_now()
        STATE["last_scan_status"] = "complete" if feed_results else "no_feeds_configured"
        STATE["last_scan_error"] = None
        STATE["last_scan_markets"] = len(quotes)
        STATE["last_scan_arbitrage_count"] = len(arbitrage)
        STATE["last_scan_feed_results"] = feed_results

    return {
        "schema": "solveease.scan-result.v1",
        "generated_at": utc_now(),
        "status": "complete" if feed_results else "no_feeds_configured",
        "configured_feed_count": len(get_feed_urls()),
        "usable_quote_count": len(quotes),
        "arbitrage_candidate_count": len(arbitrage),
        "feed_results": feed_results,
        "arbitrage_candidates": arbitrage,
        "note": (
            "No feeds are bundled with fabricated endpoints. Configure "
            "MARKET_FEED_URLS with accessible, authorized JSON feeds to scan. "
            "An empty result does not mean that no opportunities exist."
        ),
    }


# ---------------------------------------------------------------------------
# Web dashboard
# ---------------------------------------------------------------------------

HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="theme-color" content="#101827">
<title>SolveEase Surebet Intelligence</title>
<style>
:root{color-scheme:dark;--bg:#0b1220;--panel:#131e30;--line:#2a3b55;
--text:#edf3ff;--muted:#aab8cc;--accent:#7dd3fc;--good:#86efac;--warn:#fcd34d}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);
font:15px/1.5 system-ui,-apple-system,Segoe UI,sans-serif}
main{max-width:1000px;margin:auto;padding:18px}
header{padding:20px 0 12px}h1{font-size:clamp(24px,5vw,36px);line-height:1.15;margin:0 0 8px}
h2{font-size:20px;margin:0 0 12px}h3{font-size:16px;margin:16px 0 6px}
p{color:var(--muted)}.tag{display:inline-block;border:1px solid var(--line);
border-radius:999px;padding:4px 10px;font-size:12px;color:var(--accent)}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:16px;
padding:16px;margin:14px 0}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:12px}
label{display:block;font-size:13px;color:var(--muted);margin:10px 0 5px}
input,select,textarea{width:100%;border:1px solid var(--line);background:#0b1424;
color:var(--text);padding:12px;border-radius:9px;font:inherit}
button{background:#0ea5e9;color:#061421;border:0;border-radius:9px;padding:12px 16px;
font-weight:750;font-size:14px;cursor:pointer;margin-top:12px}
button.secondary{background:#26364c;color:var(--text)}
button:disabled{opacity:.5;cursor:wait}.row{display:flex;gap:8px;flex-wrap:wrap}
.result{white-space:pre-wrap;overflow-wrap:anywhere;font:12px/1.5 ui-monospace,monospace;
background:#08101d;border:1px solid var(--line);padding:12px;border-radius:10px;max-height:500px;overflow:auto}
.small{font-size:12px;color:var(--muted)}.status{padding:10px;border-radius:9px;
background:#08101d;border:1px solid var(--line);margin-top:10px;white-space:pre-wrap}
.good{color:var(--good)}.warn{color:var(--warn)}.muted{color:var(--muted)}
footer{padding:18px 0 32px;color:var(--muted);font-size:12px}
</style>
</head>
<body><main>
<header>
<span class="tag">READ-ONLY ANALYSIS</span>
<h1>SolveEase Surebet Intelligence</h1>
<p>Decode a shared accumulator, inspect every leg, compare available market quotes
and screen supported markets. No bookmaker login or bet placement.</p>
</header>

<section class="panel">
<h2>1. Decode a booking code</h2>
<label for="code">Shared booking code</label>
<input id="code" autocomplete="off" placeholder="Paste a booking code">
<label for="bookmaker">Bookmaker</label>
<select id="bookmaker">
<option value="SportyBet">SportyBet — share-code lookup supported</option>
<option value="Bet9ja">Bet9ja — identification/conversion only</option>
<option value="BetKing">BetKing — identification/conversion only</option>
<option value="1xBet">1xBet — identification/conversion only</option>
<option value="Betway">Betway — identification/conversion only</option>
</select>
<div class="row">
<button id="lookupBtn" onclick="lookup()">Decode and analyse</button>
<button class="secondary" onclick="clearOutput()">Clear</button>
</div>
<div id="lookupStatus" class="status">Ready.</div>
<div id="ticketOutput" class="result">Decoded ticket will appear here.</div>
<div class="row">
<button class="secondary" onclick="copyTicket()">Copy normalized ticket JSON</button>
</div>
<p class="small">Only SportyBet's share endpoint is implemented as a live code lookup.
Other bookmakers need their own verified, permitted code-resolution sources. A
booking code alone cannot reliably identify its bookmaker.</p>
</section>

<section class="panel">
<h2>2. Inspect market data</h2>
<p>Market comparisons require accessible data for the same event and market.
Configure feeds on the server with MARKET_FEED_URLS. Missing quotes are not
replaced with invented prices.</p>
<div class="row">
<button id="scanBtn" onclick="scan()">Run configured feed scan</button>
<button class="secondary" onclick="health()">Refresh service status</button>
</div>
<div id="scanStatus" class="status">Scan has not been requested from this page.</div>
<div id="scanOutput" class="result">Scan results will appear here.</div>
</section>

<section class="panel">
<h2>3. Analyse supplied market quotes</h2>
<p class="small">Paste JSON quotes below. Only mark a market as exhaustive with exactly
two mutually exclusive outcomes when you have verified its settlement rules.</p>
<label for="quotes">Quotes JSON</label>
<textarea id="quotes" rows="9" spellcheck="false">{
  "quotes": [
    {
      "sport": "Football",
      "event": "Example Home vs Example Away",
      "market": "Match Winner - Two Outcome Example",
      "selection": "Example Home",
      "odds": 2.1,
      "bookmaker": "Bookmaker A",
      "exhaustive_two_outcome": true
    },
    {
      "sport": "Football",
      "event": "Example Home vs Example Away",
      "market": "Match Winner - Two Outcome Example",
      "selection": "Example Away",
      "odds": 2.1,
      "bookmaker": "Bookmaker B",
      "exhaustive_two_outcome": true
    }
  ]
}</textarea>
<p class="small">The sample is illustrative only, not a live fixture or recommendation.
It demonstrates the data format and is not a claim that the market is exhaustive.</p>
<button id="analyseBtn" onclick="analyseQuotes()">Analyse quotes</button>
<div id="analyseStatus" class="status">Waiting for input.</div>
<div id="analyseOutput" class="result">Analysis will appear here.</div>
</section>

<section class="panel">
<h2>Feed format for deployment</h2>
<p>Set MARKET_FEED_URLS in your hosting environment to one or more authorized
JSON feed URLs separated by semicolons. A feed can return:</p>
<pre class="result">{
  "markets": [
    {
      "sport": "Football",
      "event": "Example Home vs Example Away",
      "market": "Match Winner",
      "outcomes": [
        {"selection": "Home", "odds": 2.1},
        {"selection": "Draw", "odds": 3.2},
        {"selection": "Away", "odds": 3.5}
      ]
    }
  ]
}</pre>
<p class="small">Quotes without bookmaker attribution or stable event identity may
be unsuitable for comparison. Three-way match-winner markets are not treated as
two-outcome arbitrage opportunities. Feeds must be accessible from the server and
return JSON in this format; this app does not magically discover private APIs.</p>
</section>

<footer>
SolveEase Surebet Intelligence · Read-only · No automated staking ·
Market prices can change. All comparisons require manual verification.
</footer>
</main>
<script>
let lastTicket = null;

async function requestJSON(url, options={}) {
  const response = await fetch(url, {
    ...options,
    headers: {"Content-Type":"application/json", ...(options.headers || {})}
  });
  const text = await response.text();
  let data;
  try { data = JSON.parse(text); }
  catch { throw new Error("Server returned non-JSON output: " + text.slice(0,300)); }
  if (!response.ok) {
    throw new Error(data.error || data.message || ("HTTP " + response.status));
  }
  return data;
}

function pretty(el, value) {
  document.getElementById(el).textContent =
    typeof value === "string" ? value : JSON.stringify(value, null, 2);
}

async function lookup() {
  const code = document.getElementById("code").value.trim();
  const bookmaker = document.getElementById("bookmaker").value;
  const status = document.getElementById("lookupStatus");
  const btn = document.getElementById("lookupBtn");

  if (!code) {
    status.textContent = "Paste a booking code first.";
    return;
  }

  btn.disabled = true;
  status.textContent = "Requesting lookup…";
  try {
    const data = await requestJSON("/api/lookup", {
      method:"POST",
      body:JSON.stringify({code, bookmaker})
    });
    lastTicket = data.ticket || null;
    status.textContent = data.status + " · " +
      (data.complete_selection_count ?? 0) + " complete leg(s) · " +
      (data.selection_count ?? 0) + " decoded leg(s)";
    pretty("ticketOutput", data);
  } catch (error) {
    status.textContent = "Lookup failed: " + error.message;
  } finally {
    btn.disabled = false;
  }
}

async function scan() {
  const status = document.getElementById("scanStatus");
  const btn = document.getElementById("scanBtn");
  btn.disabled = true;
  status.textContent = "Fetching configured feeds…";
  try {
    const data = await requestJSON("/api/scan", {method:"POST", body:"{}"});
    status.textContent = data.status + " · " + data.usable_quote_count +
      " usable quotes · " + data.arbitrage_candidate_count + " candidate(s)";
    pretty("scanOutput", data);
  } catch (error) {
    status.textContent = "Scan failed: " + error.message;
  } finally {
    btn.disabled = false;
  }
}

async function health() {
  try {
    const data = await requestJSON("/health");
    document.getElementById("scanStatus").textContent = "Service health: " +
      JSON.stringify(data);
  } catch (error) {
    document.getElementById("scanStatus").textContent =
      "Health request failed: " + error.message;
  }
}

async function analyseQuotes() {
  const status = document.getElementById("analyseStatus");
  const btn = document.getElementById("analyseBtn");
  let body;
  try { body = JSON.parse(document.getElementById("quotes").value); }
  catch (error) {
    status.textContent = "Invalid JSON: " + error.message;
    return;
  }
  btn.disabled = true;
  status.textContent = "Analysing…";
  try {
    const data = await requestJSON("/api/analyse", {
      method:"POST", body:JSON.stringify(body)
    });
    status.textContent = data.status + " · " + data.usable_quotes +
      " usable quotes · " + data.arbitrage_candidates.length + " candidate(s)";
    pretty("analyseOutput", data);
  } catch (error) {
    status.textContent = "Analysis failed: " + error.message;
  } finally {
    btn.disabled = false;
  }
}

async function copyTicket() {
  const output = document.getElementById("ticketOutput").textContent;
  try {
    await navigator.clipboard.writeText(output);
    document.getElementById("lookupStatus").textContent = "Copied output.";
  } catch {
    document.getElementById("lookupStatus").textContent =
      "Clipboard permission unavailable. Select and copy the output manually.";
  }
}

function clearOutput() {
  lastTicket = null;
  document.getElementById("code").value = "";
  document.getElementById("lookupStatus").textContent = "Ready.";
  document.getElementById("ticketOutput").textContent = "Decoded ticket will appear here.";
  document.getElementById("scanStatus").textContent = "Scan has not been requested.";
  document.getElementById("scanOutput").textContent = "Scan results will appear here.";
  document.getElementById("analyseStatus").textContent = "Waiting for input.";
  document.getElementById("analyseOutput").textContent = "Analysis will appear here.";
}
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# HTTP API
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "SolveEaseSurebet/5.0"
    sys_version = ""

    def log_message(self, fmt: str, *args: Any) -> None:
        log.info("%s - %s", self.address_string(), fmt % args)

    def send_json(self, data: dict, status: int = 200) -> None:
        encoded = json.dumps(
            data, ensure_ascii=False, separators=(",", ":"), default=str
        ).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(encoded)

    def send_html(self, html: str, status: int = 200) -> None:
        encoded = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(encoded)

    def send_error_json(self, message: str, status: int = 400) -> None:
        self.send_json({"ok": False, "error": message}, status)

    def do_HEAD(self) -> None:
        path = urlsplit(self.path).path
        if path in ("/", "/health", "/state"):
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()
        else:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()

    def do_GET(self) -> None:
        parsed = urlsplit(self.path)
        path = parsed.path

        if path == "/":
            self.send_html(HTML)
            return

        if path == "/health":
            with STATE_LOCK:
                lookup_status = STATE["last_lookup_status"]
                scan_status = STATE["last_scan_status"]
            self.send_json({
                "status": "online",
                "service": APP_NAME,
                "version": APP_VERSION,
                "read_only": True,
                "sportybet_lookup_implemented": True,
                "configured_market_feed_count": len(get_feed_urls()),
                "last_lookup_status": lookup_status,
                "last_scan_status": scan_status,
                "note": "Online status does not prove bookmaker endpoints or feeds are available.",
            })
            return

        if path == "/state":
            with STATE_LOCK:
                snapshot = dict(STATE)
            self.send_json({
                "service": APP_NAME,
                "version": APP_VERSION,
                "read_only": True,
                "configured_feed_count": len(get_feed_urls()),
                "state": snapshot,
            })
            return

        if path == "/api/lookup":
            params = parse_qs(parsed.query)
            code = (params.get("code") or [""])[0]
            if not code:
                self.send_error_json("Supply ?code=BOOKING_CODE.", 400)
                return
            result = lookup_sportybet(code)
            self.send_json(result, 200 if result.get("ok") else 422)
            return

        if path == "/api/scan":
            try:
                self.send_json(run_scan())
            except Exception as exc:
                log.exception("Scan failed")
                self.send_error_json(str(exc), 500)
            return

        self.send_error_json("Route not found.", 404)

    def do_POST(self) -> None:
        path = urlsplit(self.path).path

        try:
            body = parse_json_body(self)
        except ValueError as exc:
            self.send_error_json(str(exc), 400)
            return

        if path == "/api/lookup":
            code = str(body.get("code", "")).strip()
            requested_bookmaker = body.get("bookmaker")
            identification = identify_bookmaker(code, requested_bookmaker)

            if identification["bookmaker"] != "SportyBet":
                self.send_json({
                    "ok": False,
                    "status": "lookup_not_implemented_for_bookmaker",
                    "identification": identification,
                    "supported_bookmakers": list(SUPPORTED_BOOKMAKERS),
                    "message": (
                        "Only the SportyBet share-code lookup is implemented. "
                        "For other bookmakers, automatic identification does not "
                        "mean their booking codes can be resolved or converted live."
                    ),
                }, 501)
                return

            result = lookup_sportybet(code)
            result["identification"] = identification
            self.send_json(result, 200 if result.get("ok") else 422)
            return

        if path == "/api/analyse":
            try:
                result = analyse_supplied_quotes(body)
                self.send_json(result)
            except Exception as exc:
                log.exception("Quote analysis failed")
                self.send_error_json(str(exc), 400)
            return

        if path == "/api/scan":
            try:
                result = run_scan()
                self.send_json(result)
            except Exception as exc:
                log.exception("Market scan failed")
                self.send_error_json(str(exc), 500)
            return

        self.send_error_json("Route not found.", 404)


def main() -> None:
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    log.info("%s v%s starting on %s:%s", APP_NAME, APP_VERSION, HOST, PORT)
    log.info("Configured market feed URLs: %s", len(get_feed_urls()))
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        log.info("Shutdown requested.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
