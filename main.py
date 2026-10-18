#!/usr/bin/env python3
"""
SolveEase Booking & Arbitrage Dashboard (v3.5)
Python standard library only.

Render start command:
    python main.py

Features:
- Responsive dashboard for mobile and desktop
- Read-only SportyBet booking-code lookup
- Automatic source identification only after successful supported lookup
- Source-vs-calculated odds diagnostics
- Ticket reconstruction for manual recreation at SportyBet, Bet9ja, or BetKing
- Copyable ticket summary; never fabricates destination booking codes
- Configurable JSON/XML odds feeds for SportyBet, Bet9ja, 1xBet, and Betway
- Two-outcome and three-outcome arbitrage detection across matching event/market/outcome labels
- Feed-supplied probability filter (default 60%)
- Alternative market prioritization (Over/Under, Double Chance, Corners)
- Source health and last-scan reporting
- Privacy-conscious logging (no booking codes or POST bodies logged)
"""

import json
import logging
import math
import os
import re
import threading
import time
import unicodedata
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import Request, urlopen

HOST = "0.0.0.0"
PORT = int(os.environ.get("PORT", "10000"))
TIMEOUT = float(os.environ.get("REQUEST_TIMEOUT_SECONDS", "12"))
MIN_PROBABILITY = float(os.environ.get("MIN_PROBABILITY", "0.60"))
BANKROLL = float(os.environ.get("BANKROLL", "100000"))
MAX_FEED_BYTES = 3_000_000
MAX_LOOKUP_BYTES = 2_000_000
STALE_AFTER_SECONDS = max(30, int(os.environ.get("FEED_STALE_AFTER_SECONDS", "300")))

SPORTYBET_SHARE_URL = "https://www.sportybet.com/api/ng/orders/share/"

BOOKMAKERS = {
    "sportybet": {
        "name": "SportyBet",
        "feed_url": os.environ.get("SPORTYBET_ODDS_FEED_URL", "").strip(),
    },
    "bet9ja": {
        "name": "Bet9ja",
        "feed_url": os.environ.get("BET9JA_ODDS_FEED_URL", "").strip(),
    },
    "1xbet": {
        "name": "1xBet",
        "feed_url": os.environ.get("ONEXBET_ODDS_FEED_URL", "").strip(),
    },
    "betway": {
        "name": "Betway",
        "feed_url": os.environ.get("BETWAY_ODDS_FEED_URL", "").strip(),
    },
}

DESTINATIONS = {
    "sportybet": "SportyBet",
    "bet9ja": "Bet9ja",
    "betking": "BetKing",
}

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(message)s",
)

STARTED_AT = time.time()
STATE_LOCK = threading.Lock()
LAST_SCAN = None
LAST_LOOKUP = None
LAST_FEED_STATUS = None


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def finite_number(value, minimum=None, maximum=None):
    try:
        if isinstance(value, bool) or value is None:
            return None
        number = float(str(value).replace(",", "").strip())
        if not math.isfinite(number):
            return None
        if minimum is not None and number < minimum:
            return None
        if maximum is not None and number > maximum:
            return None
        return number
    except (TypeError, ValueError, OverflowError):
        return None


def scalar(value):
    if isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    return None


def first_value(obj, keys, default=None):
    if not isinstance(obj, dict):
        return default
    for key in keys:
        value = obj.get(key)
        if isinstance(value, (str, int, float, bool)) and value != "":
            return value
    return default


def normalize_text(value):
    value = unicodedata.normalize("NFKD", str(value or ""))
    value = "".join(c for c in value if not unicodedata.combining(c))
    value = value.lower().replace("&", " and ")
    value = re.sub(r"[^a-z0-9.+-]+", " ", value)
    return " ".join(value.split())


def safe_fetch_json(url, label, extra_headers=None):
    """Fetch an HTTPS feed and return JSON plus safe diagnostics."""
    started = time.monotonic()
    diag = {
        "http_status": None, "content_type": None, "response_bytes": 0,
        "fetch_ms": 0, "json_valid": False, "schema_valid": False,
        "raw_events": 0, "events_loaded": 0, "events_rejected": 0,
        "rejection_reasons": {}, "last_successful_fetch": None,
        "data_updated_at": None, "freshness_seconds": None,
        "stale": None, "detail": "", "status": "not configured",
    }
    if not url:
        diag["detail"] = "No feed URL configured; endpoint has not been tested"
        return None, diag

    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname:
        diag.update(status="invalid configuration", detail="Feed URL must be a valid HTTPS URL")
        return None, diag

    headers = {
        "Accept": "application/json, text/plain, */*",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    }
    if extra_headers:
        headers.update(extra_headers)
    request = Request(url, headers=headers, method="GET")
    try:
        with urlopen(request, timeout=TIMEOUT) as response:
            diag["http_status"] = int(response.status)
            diag["content_type"] = response.headers.get("Content-Type", "")[:160]
            body = response.read(MAX_FEED_BYTES + 1)
            diag["response_bytes"] = len(body)
            if len(body) > MAX_FEED_BYTES:
                diag.update(status="response too large", detail=f"Response exceeded {MAX_FEED_BYTES} byte limit")
                return None, diag
            if not 200 <= response.status < 300:
                diag.update(status="http error", detail=f"HTTP {response.status}")
                return None, diag
    except HTTPError as exc:
        diag["http_status"] = int(exc.code)
        diag["content_type"] = exc.headers.get("Content-Type", "")[:160] if exc.headers else ""
        diag.update(status="http error", detail=f"HTTP {exc.code}")
        logging.warning("%s feed returned HTTP %s", label, exc.code)
        return None, diag
    except (URLError, TimeoutError, OSError) as exc:
        diag.update(status="fetch failed", detail=f"Network request failed ({type(exc).__name__})")
        logging.warning("%s feed request failed: %s", label, type(exc).__name__)
        return None, diag
    finally:
        diag["fetch_ms"] = round((time.monotonic() - started) * 1000)

    try:
        payload = json.loads(body.decode("utf-8-sig"))
        diag["json_valid"] = True
    except (json.JSONDecodeError, UnicodeDecodeError):
        diag.update(status="invalid response", detail="Response is not valid UTF-8 JSON; endpoint may return HTML or an anti-bot page")
        return None, diag
    if not isinstance(payload, dict):
        diag.update(status="invalid response", detail="JSON root must be an object")
        return None, diag
    diag["last_successful_fetch"] = now_iso()
    diag["status"] = "json received"
    diag["detail"] = "Valid JSON received; feed schema not yet confirmed"
    return payload, diag


def parse_timestamp(value):
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        return None


# ---------------------------------------------------------------------
# SportyBet booking-code lookup
# ---------------------------------------------------------------------

def lookup_sportybet(code):
    url = SPORTYBET_SHARE_URL + quote(code, safe="")
    request = Request(
        url,
        headers={
            "Accept": "application/json, text/plain, */*",
            "User-Agent": (
                "Mozilla/5.0 (Linux; Android 15) "
                "AppleWebKit/537.36 Chrome/131.0 Mobile Safari/537.36"
            ),
            "Current-Country": "NG",
            "Referer": "https://www.sportybet.com/ng/",
        },
        method="GET",
    )

    try:
        with urlopen(request, timeout=TIMEOUT) as response:
            if response.status != 200:
                return {"ok": False, "error": "SportyBet lookup failed"}
            raw_body = response.read(MAX_LOOKUP_BYTES + 1)
            if len(raw_body) > MAX_LOOKUP_BYTES:
                return {"ok": False, "error": "SportyBet response exceeded size limit"}
            payload = json.loads(raw_body.decode("utf-8", errors="replace"))
    except HTTPError as exc:
        logging.warning("SportyBet lookup HTTP %s", exc.code)
        return {"ok": False, "error": f"SportyBet returned HTTP {exc.code}"}
    except (URLError, TimeoutError, OSError):
        logging.warning("SportyBet lookup network failure")
        return {"ok": False, "error": "Could not reach SportyBet"}
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {"ok": False, "error": "SportyBet returned invalid JSON"}

    if not isinstance(payload, dict):
        return {"ok": False, "error": "Unexpected SportyBet response"}

    biz_code = payload.get("bizCode")
    if biz_code is not None and str(biz_code) != "10000":
        return {
            "ok": False,
            "error": str(first_value(
                payload, ("message", "msg", "errorMsg"),
                "SportyBet did not accept this code",
            )),
        }

    data = payload.get("data")
    if not isinstance(data, dict):
        data = {}
    ticket = data.get("ticket")
    if not isinstance(ticket, dict):
        ticket = {}
    raw_legs = data.get("outcomes")
    if not isinstance(raw_legs, list):
        raw_legs = []

    legs = []
    for index, item in enumerate(raw_legs):
        if not isinstance(item, dict):
            continue

        event = first_value(
            item,
            ("eventName", "event_name", "matchName", "match", "fixtureName"),
            None,
        )
        if not event:
            home = first_value(item, ("homeTeamName",), "")
            away = first_value(item, ("awayTeamName",), "")
            event = f"{home} vs {away}" if home and away else "Event unavailable"

        candidates = []
        selected_candidates = []
        markets = item.get("markets", [])
        if isinstance(markets, list):
            for market in markets:
                if not isinstance(market, dict):
                    continue
                outcomes = market.get("outcomes", [])
                if not isinstance(outcomes, list):
                    continue
                for outcome in outcomes:
                    if not isinstance(outcome, dict):
                        continue
                    odds = finite_number(outcome.get("odds"), minimum=1.000001)
                    if odds is None:
                        continue
                    candidate = (market, outcome, odds)
                    candidates.append(candidate)
                    marker = first_value(
                        outcome, ("isSelected", "selected", "chosen", "betSelected"), None
                    )
                    if marker is True or marker == 1 or str(marker).lower() == "true":
                        selected_candidates.append(candidate)

        chosen = None
        if len(selected_candidates) == 1:
            chosen = selected_candidates[0]
        elif not selected_candidates and len(candidates) == 1:
            chosen = candidates[0]

        if chosen:
            market, outcome, odds = chosen
            market_name = first_value(
                market, ("desc", "marketName", "marketDesc", "name"),
                "Market unavailable",
            )
            selection_name = first_value(
                outcome, ("desc", "outcomeName", "selectionName", "name"),
                "Selection unavailable",
            )
        else:
            market_name = "Selection market needs verification"
            selection_name = "Selected outcome not identifiable"
            odds = None

        legs.append({
            "number": index + 1,
            "event": str(event),
            "market": str(market_name),
            "selection": str(selection_name),
            "odds": odds,
            "odds_verified": odds is not None,
            "kickoff": first_value(
                item, ("estimateStartTime", "matchTime", "startTime", "eventTime"), None
            ),
        })

    if not legs:
        return {
            "ok": False,
            "error": (
                "No recognizable selections returned. The code may be invalid, "
                "or SportyBet's unofficial endpoint may have changed."
            ),
        }

    raw_source_selections = ticket.get("selections")
    source_count = (
        len(raw_source_selections)
        if isinstance(raw_source_selections, list) and raw_source_selections
        else len(legs)
    )
    odds_list = [leg["odds"] for leg in legs if leg["odds_verified"]]
    calculated = (
        math.prod(odds_list)
        if len(odds_list) == len(legs) and legs
        else None
    )
    source_total = finite_number(ticket.get("displayTotalOdds"), minimum=1.000001)
    status = first_value(
        ticket, ("status", "orderStatus", "betStatus", "state"), "Not provided"
    )

    result = {
        "ok": True,
        "bookmaker": "SportyBet",
        "bookmaker_key": "sportybet",
        "bookmaker_identification": "Confirmed by successful SportyBet lookup",
        "displayed_selections_count": len(legs),
        "source_selection_count": source_count,
        "legs": legs,
        "source_combined_odds": source_total,
        "calculated_combined_odds": calculated,
        "odds_agree": (
            None if source_total is None or calculated is None
            else math.isclose(source_total, calculated, rel_tol=0.005)
        ),
        "status": status,
        "stake": scalar(ticket.get("totalStake")),
        "potential_return": scalar(ticket.get("potentialWinnings")),
        "currency": None,
        "retrieved_at": now_iso(),
        "note": (
            "Read-only lookup from an unofficial SportyBet endpoint. "
            "Stake/return units and currency are unverified. "
            "Verify the full ticket on the bookmaker's own site."
        ),
    }

    global LAST_LOOKUP
    with STATE_LOCK:
        LAST_LOOKUP = {
            "at": result["retrieved_at"],
            "displayed_selections": len(legs),
            "source_selections": source_count,
            "bookmaker": "SportyBet",
        }
    return result


# ---------------------------------------------------------------------
# Odds feed normalization and analysis
# ---------------------------------------------------------------------

def prepare_feed_request(bookmaker_key, configured_url):
    """Resolve homepage URLs to source-specific public data candidates."""
    if not configured_url:
        return configured_url, {}
    try:
        parsed = urlsplit(configured_url)
    except ValueError:
        return configured_url, {}
    host = (parsed.hostname or "").lower()
    path = parsed.path.rstrip("/").lower()

    if bookmaker_key == "sportybet":
        is_site = host == "sportybet.com" or host.endswith(".sportybet.com")
        if is_site and "/api/" not in path:
            params = {
                "sportId": "sr:sport:1",
                "marketId": "1,18,10,29,11,26,36,14,60100",
                "pageSize": "100", "pageNum": "1", "todayGames": "false",
                "_t": str(int(time.time() * 1000)),
            }
            url = "https://www.sportybet.com/api/ng/factsCenter/pcUpcomingEvents?" + urlencode(params)
            return url, {
                "Accept": "application/json, text/plain, */*", "Current-Country": "NG",
                "Origin": "https://www.sportybet.com", "Referer": "https://www.sportybet.com/ng/",
            }
        return configured_url, {"Current-Country": "NG", "Referer": "https://www.sportybet.com/ng/"}

    if bookmaker_key == "bet9ja":
        if host in ("web.bet9ja.com", "www.bet9ja.com") and ("marketws.asmx" in path or path == "/sport/odds" or path == ""):
            return "https://web.bet9ja.com/Controls/MarketWS.asmx", {"Referer": "https://web.bet9ja.com/Sport/Odds", "Accept": "text/xml, application/xml, */*"}
        if host.endswith("bet9ja.com") and "/controls/marketws.asmx" in path:
            return configured_url, {"Referer": "https://web.bet9ja.com/Sport/Odds", "Accept": "text/xml, application/xml, */*"}

    if bookmaker_key == "1xbet":
        if host == "1xbet.ng" or host.endswith(".1xbet.ng"):
            if "/linefeed/" not in path:
                params = {"sports": "1", "count": "1000", "lng": "en", "mode": "4", "country": "1"}
                return "https://1xbet.ng/LineFeed/BestGamesExtZip?" + urlencode(params), {"Accept": "application/json, text/plain, */*", "Referer": "https://1xbet.ng/en"}

    if bookmaker_key == "betway":
        if host == "betway.com.ng" or host.endswith(".betway.com.ng"):
            if "/sportsapi/" not in path:
                return "https://www.betway.com.ng/sportsapi/br/v1/BetBook/Highlights/", {"Accept": "application/json, text/plain, */*", "Referer": "https://www.betway.com.ng/"}
    return configured_url, {}


def adapt_sportybet_payload(payload):
    """Convert SportyBet's nested factsCenter response into normalized event-market records."""
    if not isinstance(payload, dict):
        return None, 0, "SportyBet JSON root is not an object"
    biz_code = payload.get("bizCode")
    if biz_code is not None and str(biz_code) not in ("10000", "0"):
        return None, 0, f"SportyBet API returned bizCode {biz_code}"

    data = payload.get("data")
    if not isinstance(data, dict) or not isinstance(data.get("tournaments"), list):
        return None, 0, "JSON received, but expected data.tournaments array from SportyBet factsCenter"

    normalized = []
    raw_market_count = 0
    for tournament in data.get("tournaments", []):
        if not isinstance(tournament, dict):
            continue
        tournament_name = str(tournament.get("name") or tournament.get("categoryName") or "Unknown league")
        events = tournament.get("events")
        if not isinstance(events, list):
            continue
        for event in events:
            if not isinstance(event, dict):
                continue
            home = str(event.get("homeTeamName") or "").strip()
            away = str(event.get("awayTeamName") or "").strip()
            if not home or not away:
                continue
            event_name = f"{home} vs {away}"
            markets = event.get("markets")
            if not isinstance(markets, list):
                continue
            for market in markets:
                if not isinstance(market, dict):
                    continue
                raw_market_count += 1
                outcomes = market.get("outcomes")
                if not isinstance(outcomes, list):
                    continue
                parsed_outcomes = []
                for outcome in outcomes:
                    if not isinstance(outcome, dict):
                        continue
                    odds = finite_number(outcome.get("odds"), minimum=1.000001)
                    name = str(outcome.get("desc") or outcome.get("name") or "").strip()
                    if odds is None or not name or outcome.get("isActive") is False:
                        continue
                    parsed_outcomes.append({"name": name, "odds": odds})
                market_name = str(market.get("desc") or market.get("name") or "").strip()
                specifier = str(market.get("specifier") or "").strip()
                if specifier:
                    market_name = f"{market_name} {specifier}".strip()
                if not market_name:
                    continue
                normalized.append({
                    "sport": "Football",
                    "event": event_name,
                    "market": market_name,
                    "outcomes": parsed_outcomes,
                    "updated_at": first_value(market, ["updated_at", "updatedAt", "lastUpdateTime", "updateTime"]),
                    "league": tournament_name,
                })
    return normalized, raw_market_count, "SportyBet nested schema recognized"


def split_event_teams(event):
    """Split common home-versus-away labels while preserving team order."""
    value = str(event or "").strip()
    parts = re.split(
        r"\s+(?:vs\.?|v\.?|versus)\s+|\s+[-–—]\s+",
        value,
        maxsplit=1,
        flags=re.IGNORECASE,
    )
    if len(parts) == 2 and all(part.strip() for part in parts):
        return parts[0].strip(), parts[1].strip()
    return None, None


def canonical_event_key(event):
    home, away = split_event_teams(event)
    if home and away:
        return normalize_text(home) + " vs " + normalize_text(away)
    return normalize_text(event)


def canonical_outcome_key(name, home=None, away=None, allow_1x2=False):
    """Canonicalize 1X2 labels only when the market is demonstrably 1X2."""
    label = normalize_text(name)
    home_key = normalize_text(home) if home else ""
    away_key = normalize_text(away) if away else ""

    if allow_1x2:
        if label in {"1", "home", "home win", "1x2 home"}:
            return "home"
        if label in {"x", "draw", "tie", "match drawn"}:
            return "draw"
        if label in {"2", "away", "away win", "1x2 away"}:
            return "away"

        if home_key and label in {home_key, home_key + " win"}:
            return "home"
        if away_key and label in {away_key, away_key + " win"}:
            return "away"

    return label


def normalize_feed_event(bookmaker_key, raw):
    if not isinstance(raw, dict):
        return None

    if bookmaker_key not in BOOKMAKERS:
        return None

    sport = str(raw.get("sport") or "Unknown sport").strip()
    event = str(raw.get("event") or raw.get("fixture") or "").strip()
    market = str(raw.get("market") or "").strip()
    outcomes = raw.get("outcomes")

    if not event or not market or not isinstance(outcomes, list):
        return None

    home, away = split_event_teams(event)
    market_key = normalize_text(market)

    # Double Chance outcomes overlap and are not mutually exclusive.
    if (
        "double chance" in market_key
        or market_key in {"1x", "12", "x2"}
        or re.search(r"\bdouble\s+chance\b", market_key)
    ):
        return None

    raw_valid = []
    for outcome in outcomes:
        if not isinstance(outcome, dict):
            continue

        name = str(
            outcome.get("name") or outcome.get("selection") or ""
        ).strip()
        odds = finite_number(
            outcome.get("odds"), minimum=1.000001
        )

        if not name or odds is None:
            continue

        raw_valid.append({
            "name": name,
            "odds": odds,
            "estimated_probability": finite_number(
                outcome.get("estimated_probability"),
                minimum=0,
                maximum=1,
            ),
        })

    # Duplicate labels are ambiguous: do not silently overwrite a price.
    raw_keys = [normalize_text(item["name"]) for item in raw_valid]
    if len(raw_keys) != len(set(raw_keys)):
        return None

    labels = set(raw_keys)
    market_suggests_1x2 = bool(re.search(
        r"\b(1x2|match result|full time result|"
        r"90 minutes result|three way result)\b",
        market_key,
    ))

    draw_labels = {
        "x", "draw", "tie", "match drawn"
    }
    has_draw = bool(labels & draw_labels)

    # A clear three-way set can establish 1X2 even when the
    # source uses a generic market title.
    has_home_label = bool(labels & {"1", "home", "home win"})
    has_away_label = bool(labels & {"2", "away", "away win"})
    is_1x2 = (
        len(raw_valid) == 3
        and (
            market_suggests_1x2
            or (has_draw and has_home_label and has_away_label)
        )
    )

    # Do not accept an ambiguous partial 1X2 market.
    if market_suggests_1x2 and len(raw_valid) != 3:
        return None

    normalized_outcomes = []
    for item in raw_valid:
        key = canonical_outcome_key(
            item["name"], home, away, allow_1x2=is_1x2
        )
        if not key:
            return None

        normalized_outcomes.append({
            "name": item["name"],
            "name_key": key,
            "odds": item["odds"],
            "estimated_probability": item["estimated_probability"],
        })

    if len(normalized_outcomes) not in (2, 3):
        return None

    outcome_keys = [
        item["name_key"] for item in normalized_outcomes
    ]
    if len(outcome_keys) != len(set(outcome_keys)):
        return None

    if is_1x2:
        if set(outcome_keys) != {"home", "draw", "away"}:
            return None
        market_key = "football_1x2"

    # Preserve timestamp information so stale rows can be excluded
    # from theoretical arbitrage calculations.
    updated_at = raw.get("updated_at")
    stamp = parse_timestamp(updated_at)
    stale = None
    if stamp is not None:
        age_seconds = (
            datetime.now(timezone.utc) - stamp
        ).total_seconds()
        stale = age_seconds > STALE_AFTER_SECONDS

    return {
        "bookmaker_key": bookmaker_key,
        "bookmaker": BOOKMAKERS[bookmaker_key]["name"],
        "sport": sport,
        "event": event,
        "event_key": canonical_event_key(event),
        "market": market,
        "market_key": market_key,
        "outcomes": normalized_outcomes,
        "updated_at": updated_at,
        "stale": stale,
    }


def safe_fetch_xml(url, label, extra_headers=None):
    """Fetch public XML without attempting to bypass access controls."""
    started = time.monotonic()
    diag = {
        "http_status": None, "content_type": None, "response_bytes": 0,
        "fetch_ms": 0, "json_valid": False, "xml_valid": False, "schema_valid": False,
        "raw_events": 0, "events_loaded": 0, "events_rejected": 0,
        "rejection_reasons": {}, "last_successful_fetch": None,
        "data_updated_at": None, "freshness_seconds": None, "stale": None,
        "detail": "", "status": "not configured",
    }
    if not url:
        diag["detail"] = "No feed URL configured"
        return None, diag
    headers = {"User-Agent": "Mozilla/5.0 (compatible; SolveEaseFeedMonitor/3.5)", "Accept": "text/xml, application/xml, */*"}
    if extra_headers:
        headers.update(extra_headers)
    request = Request(url, headers=headers, method="GET")
    try:
        with urlopen(request, timeout=TIMEOUT) as response:
            diag["http_status"] = int(response.status)
            diag["content_type"] = response.headers.get("Content-Type", "")[:160]
            body = response.read(MAX_FEED_BYTES + 1)
            diag["response_bytes"] = len(body)
            if len(body) > MAX_FEED_BYTES:
                diag.update(status="response too large", detail=f"XML response exceeded {MAX_FEED_BYTES} byte limit")
                return None, diag
            if not 200 <= response.status < 300:
                diag.update(status="http error", detail=f"HTTP {response.status}")
                return None, diag
    except HTTPError as exc:
        diag["http_status"] = int(exc.code)
        diag["content_type"] = exc.headers.get("Content-Type", "")[:160] if exc.headers else ""
        diag.update(status="http error", detail=f"HTTP {exc.code}")
        return None, diag
    except (URLError, TimeoutError, OSError) as exc:
        diag.update(status="fetch failed", detail=f"Network request failed ({type(exc).__name__})")
        return None, diag
    finally:
        diag["fetch_ms"] = round((time.monotonic() - started) * 1000)
    try:
        root = ET.fromstring(body)
        diag["xml_valid"] = True
        diag["last_successful_fetch"] = now_iso()
        diag["status"] = "XML received"
        return root, diag
    except ET.ParseError:
        diag.update(status="invalid response", detail="Response is not parseable XML; endpoint may return HTML or an access page")
        return None, diag


def _xml_local(tag):
    return tag.rsplit("}", 1)[-1].split(":")[-1].lower()


def _xml_child_text(node, names):
    wanted = {n.lower() for n in names}
    for child in list(node):
        if _xml_local(child.tag) in wanted and child.text and child.text.strip():
            return child.text.strip()
    return ""


def fetch_bet9ja_xml(configured_url, headers):
    """Probe Bet9ja's documented XML service, discover football sport ID, then request odds."""
    base = "https://web.bet9ja.com/Controls/MarketWS.asmx"
    params = {"strQuotaMax": "1000", "tipoVisQuote": "0"}
    sport_url = base + "/GetListSport_OddLessThan?" + urlencode(params)
    root, diag = safe_fetch_xml(sport_url, "Bet9ja sports list", headers)
    if root is None:
        diag["bookmaker"] = "Bet9ja"
        return [], diag
    sport_id = None
    for node in root.iter():
        if _xml_local(node.tag) != "clssportlist":
            continue
        name = _xml_child_text(node, ["Sport"])
        ident = _xml_child_text(node, ["IDSport"])
        if name and ident and any(word in name.lower() for word in ("football", "soccer", "calcio")):
            sport_id = ident
            break
    if not sport_id:
        diag.update(status="invalid schema", schema_valid=False, detail="Bet9ja XML parsed, but no football sport ID was identified")
        diag["bookmaker"] = "Bet9ja"
        return [], diag
    odds_params = {"strQuotaMax": "1000", "strIDSport": sport_id, "typeOrder": "0", "tipoVisQuote": "0"}
    odds_url = base + "/GetListOdds_OddLessThan?" + urlencode(odds_params)
    odds_root, odds_diag = safe_fetch_xml(odds_url, "Bet9ja odds", headers)
    odds_diag["fetch_ms"] += diag.get("fetch_ms", 0)
    odds_diag["response_bytes"] += diag.get("response_bytes", 0)
    odds_diag["bookmaker"] = "Bet9ja"
    if odds_root is None:
        odds_diag["detail"] = "Football sport ID discovered, but Bet9ja odds operation failed: " + odds_diag.get("detail", "unknown error")
        return [], odds_diag

    odds_diag["schema_valid"] = True
    odds_diag["status"] = "XML schema recognized; checking quote fields"
    odds_diag["last_successful_fetch"] = odds_diag.get("last_successful_fetch") or now_iso()
    normalized = []
    for subevent in odds_root.iter():
        if _xml_local(subevent.tag) != "clssubeventslist":
            continue
        event_name = _xml_child_text(subevent, ["Evento", "Event", "EventName"])
        subevent_name = _xml_child_text(subevent, ["SottoEvento", "SubEvent", "Market"])
        quote_parent = next((c for c in list(subevent) if _xml_local(c.tag) == "quote"), None)
        if not event_name or quote_parent is None:
            continue
        selections = []
        for quote_node in quote_parent.iter():
            if quote_node is quote_parent or not list(quote_node):
                continue
            fields = {_xml_local(c.tag): (c.text or "").strip() for c in list(quote_node)}
            odds = None
            for key, value in fields.items():
                if any(term in key for term in ("quota", "odd", "price", "value")):
                    odds = finite_number(value, minimum=1.000001)
                    if odds is not None:
                        break
            label = ""
            for key, value in fields.items():
                if value and any(term in key for term in ("name", "desc", "esito", "selection", "segno", "tipo")):
                    label = value
                    break
            if odds is not None and label:
                selections.append({"name": label, "odds": odds})
        unique = {normalize_text(o["name"]): o for o in selections if normalize_text(o["name"])}
        if len(unique) in (2, 3):
            normalized.append({"sport": "Football", "event": event_name, "market": subevent_name or "Bet9ja market", "outcomes": list(unique.values()), "updated_at": None})
    odds_diag["raw_events"] = sum(1 for n in odds_root.iter() if _xml_local(n.tag) == "clssubeventslist")
    odds_diag["events_loaded"] = len(normalized)
    odds_diag["events_rejected"] = max(0, odds_diag["raw_events"] - len(normalized))
    if normalized:
        odds_diag.update(status="usable markets; freshness unknown", detail="Bet9ja XML returned named multi-outcome markets")
    elif odds_diag["raw_events"]:
        odds_diag.update(status="no usable markets", detail="Bet9ja XML parsed, but quote field names did not yield valid decimal-odds outcomes")
    else:
        odds_diag.update(status="no events", detail="Bet9ja XML schema parsed, but no subevents were returned")
    return normalized, odds_diag


def adapt_betway_payload(payload):
    """Normalize Betway's documented flat event/market/outcome/price arrays."""
    if not isinstance(payload, dict):
        return None, 0, "Betway JSON root is not an object"
    def arr(*names):
        for name in names:
            value = payload.get(name)
            if isinstance(value, list):
                return value
        return []
    events = arr("events", "Events", "eventList", "EventList")
    markets = arr("markets", "Markets", "marketList", "MarketList")
    outcomes = arr("outcomes", "Outcomes", "selections", "Selections")
    prices = arr("prices", "Prices", "odds", "Odds")
    if not events or not markets or not outcomes:
        return None, len(events), "Valid JSON, but expected Betway flat arrays were not all present"
    def pick(obj, *keys):
        return first_value(obj, keys)
    event_by_id = {}
    for e in events:
        eid = pick(e, "id", "eventId", "EventId", "eventID", "ID")
        home = pick(e, "home", "homeName", "Home", "HomeName", "participant1")
        away = pick(e, "away", "awayName", "Away", "AwayName", "participant2")
        name = pick(e, "name", "eventName", "EventName", "caption", "Caption")
        if not name and home and away:
            name = f"{home} vs {away}"
        if eid is not None and name:
            event_by_id[str(eid)] = str(name)
    outcomes_by_market = {}
    price_by_outcome = {}
    for price in prices:
        oid = pick(price, "outcomeId", "OutcomeId", "selectionId", "SelectionId", "id", "Id")
        val = pick(price, "odds", "price", "value", "decimalOdds", "DecimalOdds", "Odd")
        if oid is not None and finite_number(val, minimum=1.000001) is not None:
            price_by_outcome[str(oid)] = finite_number(val, minimum=1.000001)
    for o in outcomes:
        oid = pick(o, "id", "outcomeId", "OutcomeId", "selectionId", "SelectionId", "ID")
        mid = pick(o, "marketId", "MarketId", "marketID", "parentMarketId")
        name = pick(o, "name", "outcomeName", "OutcomeName", "caption", "Caption", "description", "Description")
        odd = finite_number(pick(o, "odds", "price", "value", "decimalOdds", "DecimalOdds", "Odd"), minimum=1.000001)
        if odd is None and oid is not None:
            odd = price_by_outcome.get(str(oid))
        if mid is not None and name and odd is not None:
            outcomes_by_market.setdefault(str(mid), []).append({"name": str(name), "odds": odd})
    normalized = []
    for market in markets:
        mid = pick(market, "id", "marketId", "MarketId", "marketID", "ID")
        eid = pick(market, "eventId", "EventId", "eventID", "parentEventId")
        event_name = event_by_id.get(str(eid))
        market_name = pick(market, "name", "marketName", "MarketName", "caption", "Caption", "description", "Description")
        vals = outcomes_by_market.get(str(mid), [])
        if event_name and market_name and len({normalize_text(x["name"]) for x in vals}) in (2, 3):
            normalized.append({"sport": "Football", "event": event_name, "market": str(market_name), "outcomes": vals, "updated_at": pick(market, "updatedAt", "updated_at", "lastUpdate")})
    return normalized, len(events), "Betway flat-array schema recognized"


def adapt_1xbet_payload(payload):
    """Recognize 1xBet LineFeed fixture payloads."""
    if not isinstance(payload, dict) or not isinstance(payload.get("Value"), list):
        return None, 0, 'Valid JSON, but expected 1xBet LineFeed "Value" array'
    events = payload.get("Value", [])
    normalized = []
    for item in events:
        if not isinstance(item, dict):
            continue
        markets = item.get("markets") or item.get("Markets") or item.get("E")
        if not isinstance(markets, list):
            continue
        home = first_value(item, ["O1", "home", "homeTeam", "HomeTeam"])
        away = first_value(item, ["O2", "away", "awayTeam", "AwayTeam"])
        if not home or not away:
            continue
        for market in markets:
            if not isinstance(market, dict):
                continue
            name = first_value(market, ["name", "N", "marketName", "caption"])
            raw_outcomes = market.get("outcomes") or market.get("Outcomes") or market.get("values")
            if not isinstance(raw_outcomes, list):
                continue
            outs = []
            for out in raw_outcomes:
                if isinstance(out, dict):
                    oname = first_value(out, ["name", "N", "label", "caption"])
                    odd = finite_number(first_value(out, ["odds", "C", "price", "value"]), minimum=1.000001)
                    if oname and odd is not None:
                        outs.append({"name": str(oname), "odds": odd})
            if name and len({normalize_text(o["name"]) for o in outs}) in (2, 3):
                normalized.append({"sport": "Football", "event": f"{home} vs {away}", "market": str(name), "outcomes": outs, "updated_at": first_value(market, ["updatedAt", "updated_at"])})
    if normalized:
        return normalized, len(events), "1xBet explicit market/outcome schema recognized"
    return [], len(events), "1xBet fixtures returned, but no supported multi-outcome market objects found"


def fetch_all_feeds():
    """Fetch configured feeds and retain per-source diagnostics."""
    rows, health = [], []

    for key, config in BOOKMAKERS.items():
        request_url, headers = prepare_feed_request(
            key, config["feed_url"]
        )

        if key == "bet9ja" and ".asmx" in request_url.lower():
            raw_events, diag = fetch_bet9ja_xml(request_url, headers)
            diag["bookmaker"] = config["name"]

            for raw in raw_events:
                row = normalize_feed_event(key, raw)
                if row:
                    rows.append(row)

            diag["events_loaded"] = sum(
                1 for row in rows if row["bookmaker_key"] == key
            )
            diag["events_rejected"] = max(
                0, diag.get("raw_events", 0) - diag["events_loaded"]
            )
            health.append(diag)
            continue

        payload, diag = safe_fetch_json(
            request_url, config["name"], headers
        )
        diag["bookmaker"] = config["name"]

        if payload is None:
            health.append(diag)
            continue

        if key == "sportybet":
            raw_events, raw_count, detail = adapt_sportybet_payload(payload)
        elif key == "betway":
            raw_events, raw_count, detail = adapt_betway_payload(payload)
        elif key == "1xbet":
            raw_events, raw_count, detail = adapt_1xbet_payload(payload)
        else:
            raw_events = payload.get("events")
            raw_count = len(raw_events) if isinstance(raw_events, list) else 0
            detail = (
                "Generic events schema recognized"
                if isinstance(raw_events, list)
                else 'Valid JSON, but expected an "events" array'
            )

        if raw_events is None or not isinstance(raw_events, list):
            diag.update(
                status="invalid schema",
                schema_valid=False,
                detail=detail,
            )
            health.append(diag)
            continue

        diag["schema_valid"] = True
        diag["raw_events"] = raw_count
        diag["adapter_detail"] = detail
        diag["rejection_reasons"] = {}
        newest_timestamp = None

        for raw in raw_events:
            row = normalize_feed_event(key, raw)
            if row:
                rows.append(row)
                diag["events_loaded"] += 1

                stamp = parse_timestamp(row.get("updated_at"))
                if stamp and (
                    newest_timestamp is None or stamp > newest_timestamp
                ):
                    newest_timestamp = stamp
            else:
                diag["events_rejected"] += 1
                reason = (
                    "not an object"
                    if not isinstance(raw, dict)
                    else "invalid event, market, odds, or outcome set"
                )
                reasons = diag["rejection_reasons"]
                reasons[reason] = reasons.get(reason, 0) + 1

        if newest_timestamp:
            age = max(
                0,
                int(
                    (
                        datetime.now(timezone.utc) - newest_timestamp
                    ).total_seconds()
                ),
            )
            diag["data_updated_at"] = newest_timestamp.isoformat()
            diag["freshness_seconds"] = age
            diag["stale"] = age > STALE_AFTER_SECONDS
        else:
            diag["stale"] = None

        if diag["events_loaded"] == 0:
            diag.update(
                status="no usable markets",
                detail=detail,
            )
        elif diag["stale"] is True:
            diag.update(
                status="stale data",
                detail="Usable markets found, but the newest provided timestamp is stale",
            )
        elif newest_timestamp is None:
            diag.update(
                status="usable markets; freshness unknown",
                detail="Markets parsed; no valid odds-update timestamp was supplied",
            )
        else:
            diag.update(
                status="usable markets",
                detail=(
                    "Markets parsed. Odds accuracy and source authenticity "
                    "are not independently verified."
                ),
            )

        health.append(diag)

    return rows, health


def find_arbitrage(rows):
    groups = {}

    for row in rows:
        if row.get("stale") is True:
            continue

        key = (
            normalize_text(row.get("sport")),
            row.get("event_key"),
            row.get("market_key"),
        )
        groups.setdefault(key, []).append(row)

    opportunities = []

    for market_rows in groups.values():
        if len({
            row["bookmaker_key"] for row in market_rows
        }) < 2:
            continue

        outcome_sets = [
            frozenset(
                outcome["name_key"]
                for outcome in row["outcomes"]
            )
            for row in market_rows
        ]

        # Different outcome sets cannot be combined safely.
        if not outcome_sets or len(set(outcome_sets)) != 1:
            continue

        outcome_names = set(outcome_sets[0])
        if len(outcome_names) not in (2, 3):
            continue

        market_label = normalize_text(
            market_rows[0].get("market", "")
        )
        if "double chance" in market_label:
            continue

        best = {}
        for outcome_name in outcome_names:
            quotes = []

            for row in market_rows:
                for outcome in row["outcomes"]:
                    if outcome["name_key"] == outcome_name:
                        quotes.append({
                            "bookmaker": row["bookmaker"],
                            "bookmaker_key": row["bookmaker_key"],
                            "odds": outcome["odds"],
                            "selection": outcome["name"],
                            "updated_at": row.get("updated_at"),
                        })

            if not quotes:
                break

            best[outcome_name] = max(
                quotes, key=lambda quote: quote["odds"]
            )

        if set(best) != outcome_names:
            continue

        implied_sum = sum(
            1.0 / quote["odds"]
            for quote in best.values()
        )

        if implied_sum >= 1.0:
            continue

        legs = []
        for outcome_name in sorted(outcome_names):
            quote = dict(best[outcome_name])
            quote["stake_fraction"] = (
                (1.0 / quote["odds"]) / implied_sum
            )
            quote["stake_amount"] = round(
                BANKROLL * quote["stake_fraction"], 2
            )
            legs.append(quote)

        theoretical_margin = (1.0 - implied_sum) * 100.0

        opportunities.append({
            "sport": market_rows[0]["sport"],
            "event": market_rows[0]["event"],
            "market": market_rows[0]["market"],
            "outcome_count": len(outcome_names),
            "implied_probability_sum": implied_sum * 100.0,
            "theoretical_margin_percent": theoretical_margin,
            "theoretical_bankroll": BANKROLL,
            "legs": legs,
            "warning": (
                "Theoretical only. Confirm the same event, market, "
                "line, settlement rules, current prices, limits, "
                "fees and availability at every bookmaker. "
                "Stakes are illustrative and do not guarantee profit."
            ),
        })

    opportunities.sort(
        key=lambda item: item["theoretical_margin_percent"],
        reverse=True,
    )
    return opportunities


PRIORITY_MARKETS = (
    "over", "under", "total goals", "total points",
    "double chance", "corners", "corner",
)


def is_priority_market(market):
    normalized = normalize_text(market)
    return any(term in normalized for term in PRIORITY_MARKETS)


def filter_probabilities(rows, threshold=MIN_PROBABILITY):
    eligible = []
    for row in rows:
        priority = is_priority_market(row["market"])
        for outcome in row["outcomes"]:
            probability = outcome.get("estimated_probability")
            if probability is None or probability < threshold:
                continue
            eligible.append({
                "sport": row["sport"], "event": row["event"],
                "market": row["market"], "selection": outcome["name"],
                "bookmaker": row["bookmaker"], "odds": outcome["odds"],
                "estimated_probability": probability, "threshold": threshold,
                "priority_market": priority,
                "evidence": "Estimate supplied by configured feed; not independently validated.",
            })
    eligible.sort(
        key=lambda item: (item["priority_market"], item["estimated_probability"]),
        reverse=True,
    )
    return eligible


def alternative_markets(rows, threshold=MIN_PROBABILITY):
    suggestions = {}
    for row in rows:
        if not is_priority_market(row["market"]):
            continue
        for outcome in row["outcomes"]:
            probability = outcome.get("estimated_probability")
            if probability is None or probability < threshold:
                continue
            key = (row["sport"].lower(), row["event_key"])
            suggestions.setdefault(key, []).append({
                "sport": row["sport"], "event": row["event"],
                "market": row["market"], "selection": outcome["name"],
                "bookmaker": row["bookmaker"], "odds": outcome["odds"],
                "estimated_probability": probability,
                "note": "Candidate only. Confirm exact line, market rules, and freshness.",
            })

    result = []
    for items in suggestions.values():
        items.sort(key=lambda item: item["estimated_probability"], reverse=True)
        result.extend(items)
    return result


def run_scan():
    rows, health = fetch_all_feeds()
    arbitrage = find_arbitrage(rows)
    probabilities = filter_probabilities(rows)
    alternatives = alternative_markets(rows)
    result = {
        "ok": True,
        "scanned_at": now_iso(),
        "bookmakers": health,
        "usable_markets": len(rows),
        "usable_two_outcome_markets": sum(
            1 for row in rows if len(row["outcomes"]) == 2
        ),
        "usable_three_outcome_markets": sum(
            1 for row in rows if len(row["outcomes"]) == 3
        ),
        "arbitrage_count": len(arbitrage),
        "arbitrage": arbitrage,
        "probability_filter_threshold": MIN_PROBABILITY,
        "qualified_selections_count": len(probabilities),
        "qualified_selections": probabilities,
        "alternative_markets_count": len(alternatives),
        "alternative_markets": alternatives,
        "notes": [
            "No opportunity is displayed unless configured feed data qualifies.",
            "A feed-provided probability estimate is not a guarantee.",
            "Both outcomes must be exhaustive and use identical settlement rules.",
        ],
    }
    global LAST_SCAN, LAST_FEED_STATUS
    with STATE_LOCK:
        LAST_FEED_STATUS = [dict(item) for item in health]
        LAST_SCAN = {
            "at": result["scanned_at"],
            "feeds_with_usable_markets": sum(1 for item in health if item["events_loaded"] > 0),
            "configured_feeds": sum(1 for config in BOOKMAKERS.values() if config["feed_url"]),
            "markets": len(rows),
            "opportunities": len(arbitrage),
        }
    logging.info(
        "Scan completed: feeds=%s markets=%s opportunities=%s",
        len(health), len(rows), len(arbitrage),
    )
    return result


# ---------------------------------------------------------------------
# Dashboard HTML
# ---------------------------------------------------------------------

HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="theme-color" content="#101827">
<meta name="description" content="SolveEase bookmaker lookup, ticket reconstruction and arbitrage dashboard">
<title>SolveEase | Booking & Arbitrage Dashboard</title>
<style>
:root{color-scheme:dark;--bg:#0b1220;--panel:#121d30;--panel2:#18263c;--text:#eef4ff;--muted:#9eb0ca;--line:#2b3b54;--accent:#69d7ac;--danger:#ff8585;--warning:#ffd479}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:16px/1.5 system-ui,-apple-system,sans-serif}
main{width:min(1060px,100%);margin:auto;padding:20px 14px 44px}
header{margin:8px 0 20px}.brand{color:var(--accent);font-weight:850;letter-spacing:.8px;font-size:13px}
h1{font-size:clamp(27px,5vw,40px);line-height:1.15;margin:8px 0}
h2{font-size:21px;margin:0 0 10px}h3{font-size:16px;margin:0 0 6px}
p{color:var(--muted)}.panel{background:var(--panel);border:1px solid var(--line);border-radius:16px;padding:17px;margin:14px 0}
label{display:block;font-weight:650;margin:10px 0 6px}
input,select{width:100%;min-height:48px;padding:12px;border-radius:10px;border:1px solid var(--line);background:#09101d;color:var(--text);font-size:16px}
button{min-height:48px;padding:12px 16px;border:0;border-radius:10px;background:var(--accent);color:#071b15;font-weight:800;font-size:15px;cursor:pointer}
button:disabled{opacity:.55;cursor:wait}
.actions{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:9px;margin-top:12px}
.secondary{background:var(--panel2);color:var(--text);border:1px solid var(--line)}
.small{font-size:13px;color:var(--muted)}.error{color:var(--danger)}.success{color:var(--accent)}.warning{color:var(--warning)}
.hidden{display:none}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(145px,1fr));gap:10px}
.stat{padding:12px;background:var(--panel2);border-radius:10px;min-width:0}
.stat span{display:block;font-size:12px;color:var(--muted)}.stat strong{display:block;font-size:20px;overflow-wrap:anywhere}
.item{background:var(--panel2);border:1px solid var(--line);border-radius:11px;padding:13px;margin:9px 0}
.item p{margin:5px 0;overflow-wrap:anywhere}.pill{display:inline-block;border:1px solid var(--line);border-radius:20px;padding:3px 9px;margin:3px 4px 3px 0;font-size:12px}
pre{white-space:pre-wrap;overflow-wrap:anywhere;font:14px/1.55 ui-monospace,SFMono-Regular,Consolas,monospace}
.notice{border-left:3px solid var(--warning);padding-left:11px}
footer{font-size:12px;color:var(--muted);margin-top:24px}
@media(min-width:760px){.two-col{display:grid;grid-template-columns:1fr 1fr;gap:14px}.two-col .panel{margin:0}}
</style>
</head>
<body>
<main>
<header>
  <div class="brand">SOLVEEASE · BOOKING TOOLS</div>
  <h1>Booking & Arbitrage Dashboard</h1>
  <p>Read-only ticket lookup, manual ticket reconstruction, odds comparison and probability filters.</p>
</header>

<section class="panel" id="lookupPanel">
  <h2>1. Booking-code lookup</h2>
  <p>Enter a code and the dashboard will verify it using lookup adapters currently available.</p>
  <label for="code">Booking code</label>
  <input id="code" maxlength="120" autocomplete="off" autocapitalize="none" spellcheck="false" placeholder="Enter booking code">
  <div class="actions">
    <button id="lookupBtn" onclick="lookup()">Identify & look up</button>
    <button class="secondary" onclick="clearLookup()">Clear ticket</button>
  </div>
  <p class="small">Currently supported: SportyBet read-only lookup. Bet9ja and BetKing require manual reconstruction.</p>
  <div id="lookupMessage" role="status" aria-live="polite"></div>
  <div id="ticketResult"></div>
</section>

<section class="panel" id="converterPanel">
  <h2>2. Bookmaker ticket converter</h2>
  <p>Prepare a copyable checklist from a successfully retrieved ticket for manual recreation.</p>
  <label for="destinationBookmaker">Destination bookmaker</label>
  <select id="destinationBookmaker">
    <option value="sportybet">SportyBet</option>
    <option value="bet9ja">Bet9ja</option>
    <option value="betking">BetKing</option>
  </select>
  <div class="actions">
    <button id="prepareBtn" onclick="prepareConversion()">Prepare ticket</button>
    <button id="copyBtn" class="secondary" onclick="copyConvertedTicket()">Copy prepared ticket</button>
  </div>
  <div id="converterMessage" role="status" aria-live="polite"></div>
  <div id="converterSummary" class="grid"></div>
  <pre id="convertedTicket" class="item">Look up a supported booking code first.</pre>
  <p class="small notice">Manual reconstruction only. This does not generate a destination booking code.</p>
</section>

<section class="panel" id="scannerPanel">
  <h2>3. Arbitrage scanner</h2>
  <p>Scans permitted JSON odds feeds configured on the server.</p>
  <div class="actions">
    <button id="scanBtn" onclick="scan()">Scan bookmakers now</button>
    <button class="secondary" onclick="loadHealth()">Refresh service status</button>
  </div>
  <div id="scanMessage" role="status" aria-live="polite"></div>
  <div id="sourceHealth"></div>
  <div id="scanSummary" class="grid"></div>
  <h3>Arbitrage opportunities</h3><div id="arbitrage"></div>
  <h3>Probability filter</h3>
  <p class="small">Default threshold: 60%.</p>
  <div id="probabilities"></div>
  <h3>Preferred alternative markets</h3>
  <div id="alternatives"></div>
</section>

<section class="panel">
  <h2>Service status</h2>
  <div id="health">Checking service…</div>
</section>
<footer>SolveEase · Mobile-friendly · Manual confirmation required</footer>
</main>
<script>
"use strict";
const $ = id => document.getElementById(id);
let lastLookupData = null;
let preparedTicketText = "";

function el(tag, text, parent, cls) {
  const node = document.createElement(tag);
  node.textContent = String(text ?? "");
  if (cls) node.className = cls;
  parent.appendChild(node);
  return node;
}
function clear(id) { $(id).replaceChildren(); }
function money(value, digits=2) {
  if (value === null || value === undefined || value === "" || !Number.isFinite(Number(value))) return "Unavailable";
  return Number(value).toLocaleString("en-US", {minimumFractionDigits:digits,maximumFractionDigits:digits});
}
async function api(path, method="GET", body=null) {
  const options = {method, headers:{}};
  if (body !== null) {
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(body);
  }
  const response = await fetch(path, options);
  let data;
  try { data = await response.json(); }
  catch (_) { throw new Error("Server returned an unreadable response."); }
  if (!response.ok || data.ok === false) throw new Error(data.error || "Request failed.");
  return data;
}

function clearLookup() {
  lastLookupData = null;
  preparedTicketText = "";
  $("code").value = "";
  $("lookupMessage").textContent = "";
  $("lookupMessage").className = "";
  clear("ticketResult");
  clear("converterSummary");
  $("converterMessage").textContent = "";
  $("converterMessage").className = "";
  $("convertedTicket").textContent = "Look up a supported booking code first.";
}

async function lookup() {
  const code = $("code").value.trim();
  if (!code) {
    $("lookupMessage").textContent = "Enter a booking code.";
    $("lookupMessage").className = "warning";
    return;
  }
  lastLookupData = null;
  preparedTicketText = "";
  clear("ticketResult");
  clear("converterSummary");
  $("convertedTicket").textContent = "Look up a supported booking code first.";
  $("converterMessage").textContent = "";
  $("lookupBtn").disabled = true;
  $("lookupMessage").textContent = "Checking supported lookup sources…";
  $("lookupMessage").className = "";

  try {
    const data = await api("/api/lookup", "POST", {code, bookmaker:"auto"});
    lastLookupData = data;
    $("lookupMessage").textContent = "Lookup completed and source confirmed.";
    $("lookupMessage").className = "success";
    const box = $("ticketResult");
    const title = el("div", "", box, "item");
    el("h3", "Verified source: " + data.bookmaker, title);
    el("p", data.bookmaker_identification || "Confirmed by successful lookup.", title, "small");

    const stats = el("div", "", box, "grid");
    [
      ["Selections displayed", data.displayed_selections_count],
      ["Source selection count", data.source_selection_count],
      ["Source combined odds", money(data.source_combined_odds)],
      ["Calculated odds", money(data.calculated_combined_odds)],
      ["Odds agree", data.odds_agree === null ? "Cannot verify" : (data.odds_agree ? "Yes" : "No")],
      ["Ticket status", data.status],
      ["Stake", data.stake == null ? "Not provided" : String(data.stake)],
      ["Potential return", data.potential_return == null ? "Not provided" : String(data.potential_return)]
    ].forEach(([label,value]) => {
      const stat = el("div", "", stats, "stat");
      el("span", label, stat);
      el("strong", value ?? "Not provided", stat);
    });

    el("h3", "Ticket selections", box);
    (data.legs || []).forEach(leg => {
      const card = el("div", "", box, "item");
      el("strong", String(leg.number) + ". " + (leg.event || "Event unavailable"), card);
      el("p", "Market: " + (leg.market || "Needs verification"), card);
      el("p", "Selection: " + (leg.selection || "Needs verification"), card);
      el("p", "Source odds: " + (leg.odds_verified ? money(leg.odds, 3) : "Unverified"), card);
    });
  } catch (err) {
    $("lookupMessage").textContent = err.message;
    $("lookupMessage").className = "error";
  } finally {
    $("lookupBtn").disabled = false;
  }
}

function prepareConversion() {
  const data = lastLookupData;
  const destination = $("destinationBookmaker").value;
  const names = {sportybet:"SportyBet",bet9ja:"Bet9ja",betking:"BetKing"};
  const message = $("converterMessage");
  const output = $("convertedTicket");
  clear("converterSummary");
  preparedTicketText = "";

  if (!data || data.ok !== true || !Array.isArray(data.legs) || !data.legs.length) {
    message.textContent = "Look up a supported booking code successfully first.";
    message.className = "warning";
    output.textContent = "No ticket available to reconstruct.";
    return;
  }

  const destinationName = names[destination] || "Selected bookmaker";
  const lines = [
    "SOLVEEASE — MANUAL TICKET RECONSTRUCTION",
    "Source bookmaker: " + (data.bookmaker || "Unknown"),
    "Destination bookmaker: " + destinationName,
    "STATUS: NOT A DESTINATION BOOKING CODE",
    ""
  ];

  data.legs.forEach((leg, index) => {
    lines.push(
      (index + 1) + ". " + String(leg.event || "Event"),
      "   Market: " + String(leg.market || "Market"),
      "   Selection: " + String(leg.selection || "Selection"),
      "   Source odds: " + (leg.odds_verified ? Number(leg.odds).toFixed(3) : "Unverified"),
      ""
    );
  });
  preparedTicketText = lines.join("\n");
  output.textContent = preparedTicketText;
  message.textContent = "Manual reconstruction checklist prepared for " + destinationName + ".";
  message.className = "warning";
}

async function copyConvertedTicket() {
  const message = $("converterMessage");
  if (!preparedTicketText) {
    message.textContent = "Prepare a ticket before copying.";
    message.className = "warning";
    return;
  }
  try {
    await navigator.clipboard.writeText(preparedTicketText);
    message.textContent = "Prepared ticket copied to clipboard.";
    message.className = "success";
  } catch (_) {
    message.textContent = "Clipboard unavailable.";
    message.className = "warning";
  }
}

function renderList(targetId, items, renderer, emptyText) {
  clear(targetId);
  if (!items || !items.length) {
    el("p", emptyText, $(targetId), "small");
    return;
  }
  items.forEach(item => renderer(item, $(targetId)));
}

function renderHealth(items) {
  renderList("sourceHealth", items, (item,parent) => {
    const card = el("div", "", parent, "item");
    el("strong", item.bookmaker + " — " + item.status, card);
    el("p", item.detail || "No diagnostic detail.", card, "small");
  }, "No source health data.");
}

async function scan() {
  $("scanBtn").disabled = true;
  $("scanMessage").textContent = "Fetching configured odds feeds…";
  $("scanMessage").className = "";
  ["sourceHealth","scanSummary","arbitrage","probabilities","alternatives"].forEach(clear);
  try {
    const data = await api("/api/scan", "POST", {});
    $("scanMessage").textContent = "Scan completed at " + data.scanned_at;
    renderHealth(data.bookmakers);
    const summary = $("scanSummary");
    [
      ["Usable markets", data.usable_markets],
      ["2-outcome markets", data.usable_two_outcome_markets],
      ["3-outcome markets", data.usable_three_outcome_markets],
      ["Arbitrage opportunities", data.arbitrage_count]
    ].forEach(([label,value]) => {
      const stat = el("div", "", summary, "stat");
      el("span", label, stat);
      el("strong", value, stat);
    });

    renderList("arbitrage", data.arbitrage, (item,parent) => {
      const card = el("div", "", parent, "item");
      el("h3", item.event + " · " + item.sport, card);
      el("p", "Market: " + item.market + " (" + item.outcome_count + " outcomes)", card);
      el("p", "Theoretical margin: " + money(item.theoretical_margin_percent) + "%", card, "success");
      item.legs.forEach(leg => el("p", leg.selection + ": " + leg.bookmaker + " @ " + money(leg.odds), card));
    }, "No qualifying arbitrage opportunity found.");
  } catch (err) {
    $("scanMessage").textContent = err.message;
    $("scanMessage").className = "error";
  } finally {
    $("scanBtn").disabled = false;
  }
}

async function loadHealth() {
  try {
    const data = await api("/health");
    $("health").textContent = "Service online · " + data.configured_feeds + " configured feeds";
    if (data.feed_status) renderHealth(data.feed_status);
  } catch (_) {
    $("health").textContent = "Could not check service status.";
  }
}

loadHealth();
</script>
</body>
</html>"""


# ---------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "SolveEase/3.5"

    def log_message(self, fmt, *args):
        safe_message = fmt.split('"')[0].strip()
        logging.info("%s - %s", self.address_string(), safe_message)

    def send_bytes(self, status, body, content_type):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
                         "img-src 'self' data:; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'")
        self.end_headers()
        if self.command != "HEAD":
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def send_json(self, status, data):
        body = json.dumps(data, ensure_ascii=False, allow_nan=False).encode("utf-8")
        self.send_bytes(status, body, "application/json; charset=utf-8")

    def read_body(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise ValueError("Invalid request length")
        if length < 1 or length > 16384:
            raise ValueError("Request body is empty or too large")
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ValueError("Request body must be valid JSON")

    def do_HEAD(self):
        path = urlsplit(self.path).path.rstrip("/") or "/"

        if path == "/":
            self.send_bytes(
                200, HTML.encode("utf-8"),
                "text/html; charset=utf-8"
            )
            return

        if path == "/health":
            self.send_json(200, {"status": "online"})
            return

        self.send_json(404, {
            "ok": False,
            "error": "Route not found",
        })

    def do_GET(self):
        path = urlsplit(self.path).path.rstrip("/") or "/"
        if path == "/":
            self.send_bytes(200, HTML.encode("utf-8"), "text/html; charset=utf-8")
            return
        if path == "/favicon.ico":
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if path == "/health":
            configured = sum(1 for config in BOOKMAKERS.values() if config["feed_url"])
            with STATE_LOCK:
                last_scan = dict(LAST_SCAN) if LAST_SCAN else None
                last_lookup = dict(LAST_LOOKUP) if LAST_LOOKUP else None
                last_feed_status = [dict(item) for item in LAST_FEED_STATUS] if LAST_FEED_STATUS is not None else None
            feed_status = last_feed_status if last_feed_status is not None else [
                {
                    "bookmaker": config["name"],
                    "status": "not tested" if config["feed_url"] else "not configured",
                    "detail": "URL configured but not scanned" if config["feed_url"] else "No feed URL configured",
                }
                for config in BOOKMAKERS.values()
            ]
            self.send_json(200, {
                "status": "online",
                "service": "SolveEase Booking & Arbitrage Dashboard",
                "version": "3.5",
                "configured_feeds": configured,
                "feed_status": feed_status,
                "last_scan": last_scan,
                "last_lookup": last_lookup,
                "uptime_seconds": int(time.time() - STARTED_AT),
            })
            return
        self.send_json(404, {"ok": False, "error": "Route not found"})

    def do_POST(self):
        path = urlsplit(self.path).path
        if path not in ("/api/lookup", "/api/scan"):
            self.send_json(404, {"ok": False, "error": "Route not found"})
            return
        if "application/json" not in self.headers.get("Content-Type", "").lower():
            self.send_json(415, {"ok": False, "error": "JSON body required"})
            return
        try:
            body = self.read_body()
        except ValueError as exc:
            self.send_json(400, {"ok": False, "error": str(exc)})
            return
        if not isinstance(body, dict):
            self.send_json(400, {"ok": False, "error": "Expected JSON object"})
            return

        if path == "/api/scan":
            try:
                result = run_scan()
                self.send_json(200, result)
            except Exception:
                logging.exception("Scan failed")
                self.send_json(500, {"ok": False, "error": "Scan failed."})
            return

        code = body.get("code", "")
        bookmaker = body.get("bookmaker", "auto")
        if not isinstance(code, str):
            self.send_json(400, {"ok": False, "error": "Booking code must be text"})
            return
        code = code.strip()
        if not code or len(code) > 120 or re.search(r"[\x00-\x1f\x7f]", code):
            self.send_json(400, {"ok": False, "error": "Invalid booking code"})
            return

        if bookmaker in ("auto", "sportybet"):
            try:
                result = lookup_sportybet(code)
            except Exception:
                logging.exception("Ticket lookup failed")
                self.send_json(500, {"ok": False, "error": "Unexpected ticket lookup error"})
                return
            if result.get("ok"):
                self.send_json(200, result)
            else:
                self.send_json(502, {
                    "ok": False,
                    "error": result.get("error", "SportyBet lookup failed"),
                })
            return

        if bookmaker in ("bet9ja", "betking"):
            self.send_json(501, {
                "ok": False,
                "error": "A verified lookup adapter is not configured for this bookmaker.",
            })
            return

        self.send_json(400, {"ok": False, "error": "Unsupported bookmaker"})


def main():
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    server.daemon_threads = True
    logging.info("SolveEase dashboard v3.5 listening on port %s", PORT)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
