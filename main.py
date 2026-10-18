#!/usr/bin/env python3
"""SolveEase Surebet Intelligence — single-file, standard-library-only service.
Render start command: python main.py
Read-only: never logs into bookmakers or places bets.
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
TIMEOUT = max(2.0, min(30.0, float(
    os.environ.get("REQUEST_TIMEOUT_SECONDS", "12")
)))
BANKROLL = max(0.0, float(os.environ.get("BANKROLL", "100000")))
STALE_AFTER_SECONDS = max(
    30, int(os.environ.get("FEED_STALE_AFTER_SECONDS", "300"))
)

MAX_FEED_BYTES = 3_000_000
MAX_LOOKUP_BYTES = 2_000_000
MAX_POST_BYTES = 16_000

SPORTYBET_SHARE_URL = (
    "https://www.sportybet.com/api/ng/orders/share/"
)

BOOKMAKERS = {
    "sportybet": {
        "name": "SportyBet",
        "feed_url": os.environ.get(
            "SPORTYBET_ODDS_FEED_URL", ""
        ).strip(),
    },
    "bet9ja": {
        "name": "Bet9ja",
        "feed_url": os.environ.get(
            "BET9JA_ODDS_FEED_URL", ""
        ).strip(),
    },
    "1xbet": {
        "name": "1xBet",
        "feed_url": os.environ.get(
            "ONEXBET_ODDS_FEED_URL", ""
        ).strip(),
    },
    "betway": {
        "name": "Betway",
        "feed_url": os.environ.get(
            "BETWAY_ODDS_FEED_URL", ""
        ).strip(),
    },
}

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(message)s",
)

STARTED_AT = time.time()
STATE_LOCK = threading.Lock()
SCAN_LOCK = threading.Lock()

LAST_SCAN = None
LAST_LOOKUP = None
LAST_FEED_STATUS = None


# ---------------------------------------------------------
# General helpers
# ---------------------------------------------------------

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
    value = "".join(
        char for char in value
        if not unicodedata.combining(char)
    )
    value = value.lower().replace("&", " and ")
    value = re.sub(r"[^a-z0-9.+-]+", " ", value)

    return " ".join(value.split())


def parse_timestamp(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            stamp = float(value)

            # Accept Unix seconds or Unix milliseconds.
            if stamp > 10_000_000_000:
                stamp /= 1000

            return datetime.fromtimestamp(stamp, timezone.utc)

        except (ValueError, OverflowError, OSError):
            return None

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


def diag_template():
    return {
        "http_status": None,
        "content_type": None,
        "response_bytes": 0,
        "fetch_ms": 0,
        "json_valid": False,
        "xml_valid": False,
        "schema_valid": False,
        "raw_events": 0,
        "events_loaded": 0,
        "events_rejected": 0,
        "rejection_reasons": {},
        "last_successful_fetch": None,
        "data_updated_at": None,
        "freshness_seconds": None,
        "stale": None,
        "detail": "",
        "status": "not configured",
    }


# ---------------------------------------------------------
# Safe HTTP and data fetching
# ---------------------------------------------------------

def fetch_bytes(url, label, extra_headers=None, limit=MAX_FEED_BYTES):
    diag = diag_template()
    started = time.monotonic()

    if not url:
        diag["detail"] = (
            "No feed URL configured; this feed has not been tested"
        )
        return None, diag

    try:
        parsed = urlsplit(url)

        if parsed.scheme != "https" or not parsed.hostname:
            diag.update(
                status="invalid configuration",
                detail="Feed URL must be a valid HTTPS URL",
            )
            return None, diag

    except ValueError:
        diag.update(
            status="invalid configuration",
            detail="Malformed feed URL",
        )
        return None, diag

    headers = {
        "Accept": (
            "application/json, text/plain, "
            "application/xml, text/xml, */*"
        ),
        "User-Agent": (
            "Mozilla/5.0 (compatible; SolveEaseSurebet/4.0)"
        ),
    }

    if extra_headers:
        headers.update(extra_headers)

    try:
        request = Request(url, headers=headers, method="GET")

        with urlopen(request, timeout=TIMEOUT) as response:
            diag["http_status"] = int(response.status)
            diag["content_type"] = (
                response.headers.get("Content-Type", "")[:160]
            )

            body = response.read(limit + 1)
            diag["response_bytes"] = len(body)

            if len(body) > limit:
                diag.update(
                    status="response too large",
                    detail=f"Response exceeded {limit} byte limit",
                )
                return None, diag

            if not 200 <= response.status < 300:
                diag.update(
                    status="http error",
                    detail=f"HTTP {response.status}",
                )
                return None, diag

        diag["fetch_ms"] = round(
            (time.monotonic() - started) * 1000
        )

        return body, diag

    except HTTPError as exc:
        diag["http_status"] = int(exc.code)
        diag.update(
            status="http error",
            detail=f"HTTP {exc.code}",
        )

    except (URLError, TimeoutError, OSError, ValueError):
        diag.update(
            status="fetch failed",
            detail="Network request failed or timed out",
        )

    finally:
        if not diag["fetch_ms"]:
            diag["fetch_ms"] = round(
                (time.monotonic() - started) * 1000
            )

    return None, diag


def safe_fetch_json(url, label, extra_headers=None):
    body, diag = fetch_bytes(url, label, extra_headers)

    if body is None:
        return None, diag

    try:
        payload = json.loads(body.decode("utf-8-sig"))
        diag["json_valid"] = True

    except (json.JSONDecodeError, UnicodeDecodeError):
        diag.update(
            status="invalid response",
            detail="Response is not valid UTF-8 JSON",
        )
        return None, diag

    if not isinstance(payload, (dict, list)):
        diag.update(
            status="invalid response",
            detail="JSON root must be an object or array",
        )
        return None, diag

    diag["last_successful_fetch"] = now_iso()
    diag["status"] = "json received"

    return payload, diag


def safe_fetch_xml(url, label, extra_headers=None):
    body, diag = fetch_bytes(url, label, extra_headers)

    if body is None:
        return None, diag

    try:
        root = ET.fromstring(body)
        diag["xml_valid"] = True
        diag["last_successful_fetch"] = now_iso()
        diag["status"] = "xml received"

        return root, diag

    except ET.ParseError:
        diag.update(
            status="invalid response",
            detail="Response is not valid XML",
        )
        return None, diag


# ---------------------------------------------------------
# SportyBet read-only ticket lookup
# ---------------------------------------------------------

def lookup_sportybet(code):
    code = str(code or "").strip()

    if (
        not code
        or len(code) > 200
        or not re.fullmatch(r"[A-Za-z0-9_-]+", code)
    ):
        return {
            "ok": False,
            "error": (
                "Enter a valid booking/share code "
                "(letters, numbers, _ or -)."
            ),
        }

    url = SPORTYBET_SHARE_URL + quote(code, safe="")

    headers = {
        "Accept": "application/json, text/plain, */*",
        "User-Agent": (
            "Mozilla/5.0 (Linux; Android 15) "
            "AppleWebKit/537.36 Chrome/131.0 Mobile Safari/537.36"
        ),
        "Current-Country": "NG",
        "Referer": "https://www.sportybet.com/ng/",
    }

    body, diag = fetch_bytes(
        url, "SportyBet lookup", headers, MAX_LOOKUP_BYTES
    )

    if body is None:
        return {
            "ok": False,
            "error": diag.get("detail") or "SportyBet lookup failed",
            "diagnostics": diag,
        }

    try:
        payload = json.loads(body.decode("utf-8-sig"))

    except (json.JSONDecodeError, UnicodeDecodeError):
        return {
            "ok": False,
            "error": "SportyBet returned invalid JSON",
            "diagnostics": diag,
        }

    if not isinstance(payload, dict):
        return {
            "ok": False,
            "error": "Unexpected SportyBet response",
        }

    biz_code = payload.get("bizCode")

    if biz_code is not None and str(biz_code) != "10000":
        return {
            "ok": False,
            "error": str(
                first_value(
                    payload,
                    ("message", "msg", "errorMsg"),
                    "SportyBet did not accept this code",
                )
            )[:300],
        }

    data = (
        payload.get("data")
        if isinstance(payload.get("data"), dict)
        else {}
    )

    ticket = (
        data.get("ticket")
        if isinstance(data.get("ticket"), dict)
        else {}
    )

    raw_legs = (
        data.get("outcomes")
        if isinstance(data.get("outcomes"), list)
        else []
    )

    legs = []

    for index, item in enumerate(raw_legs):
        if not isinstance(item, dict):
            continue

        event = first_value(
            item,
            (
                "eventName",
                "event_name",
                "matchName",
                "match",
                "fixtureName",
            ),
            None,
        )

        if not event:
            home = first_value(item, ("homeTeamName",), "")
            away = first_value(item, ("awayTeamName",), "")

            event = (
                f"{home} vs {away}"
                if home and away
                else "Event unavailable"
            )

        candidates = []
        selected = []

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

                    odds = finite_number(
                        outcome.get("odds"),
                        minimum=1.000001,
                    )

                    if odds is None:
                        continue

                    pair = (market, outcome, odds)
                    candidates.append(pair)

                    marker = first_value(
                        outcome,
                        (
                            "isSelected",
                            "selected",
                            "chosen",
                            "betSelected",
                        ),
                        None,
                    )

                    if (
                        marker is True
                        or marker == 1
                        or str(marker).lower() == "true"
                    ):
                        selected.append(pair)

        chosen = (
            selected[0]
            if len(selected) == 1
            else (
                candidates[0]
                if not selected and len(candidates) == 1
                else None
            )
        )

        if chosen:
            market, outcome, odds = chosen

            market_name = first_value(
                market,
                ("desc", "marketName", "marketDesc", "name"),
                "Market unavailable",
            )

            selection_name = first_value(
                outcome,
                ("desc", "outcomeName", "selectionName", "name"),
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
                item,
                (
                    "estimateStartTime",
                    "matchTime",
                    "startTime",
                    "eventTime",
                ),
                None,
            ),
        })

    if not legs:
        return {
            "ok": False,
            "error": (
                "No recognizable selections returned. "
                "The code may be invalid or the response format "
                "may have changed."
            ),
        }

    source_count_raw = ticket.get("selections")

    source_count = (
        len(source_count_raw)
        if isinstance(source_count_raw, list) and source_count_raw
        else len(legs)
    )

    odds_list = [
        leg["odds"]
        for leg in legs
        if leg["odds_verified"]
    ]

    calculated = (
        math.prod(odds_list)
        if len(odds_list) == len(legs)
        else None
    )

    source_total = finite_number(
        ticket.get("displayTotalOdds"),
        minimum=1.000001,
    )

    result = {
        "ok": True,
        "bookmaker": "SportyBet",
        "bookmaker_key": "sportybet",
        "bookmaker_identification": (
            "Confirmed by successful SportyBet lookup"
        ),
        "displayed_selections_count": len(legs),
        "source_selection_count": source_count,
        "legs": legs,
        "source_combined_odds": source_total,
        "calculated_combined_odds": calculated,
        "odds_agree": (
            None
            if source_total is None or calculated is None
            else math.isclose(
                source_total, calculated, rel_tol=0.005
            )
        ),
        "status": first_value(
            ticket,
            ("status", "orderStatus", "betStatus", "state"),
            "Not provided",
        ),
        "stake": scalar(ticket.get("totalStake")),
        "potential_return": scalar(
            ticket.get("potentialWinnings")
        ),
        "currency": None,
        "retrieved_at": now_iso(),
        "note": (
            "Read-only lookup from an unofficial endpoint; "
            "returned details may be incomplete."
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


# ---------------------------------------------------------
# Bookmaker feed configuration and adapters
# ---------------------------------------------------------

def prepare_feed_request(bookmaker_key, configured_url):
    if not configured_url:
        return configured_url, {}

    try:
        parsed = urlsplit(configured_url)
    except ValueError:
        return configured_url, {}

    host = (parsed.hostname or "").lower()
    path = parsed.path.rstrip("/").lower()

    if bookmaker_key == "sportybet":
        is_site = (
            host == "sportybet.com"
            or host.endswith(".sportybet.com")
        )

        if is_site and "/api/" not in path:
            params = {
                "sportId": "sr:sport:1",
                "marketId": "1,18,10,29,11,26,36,14,60100",
                "pageSize": "100",
                "pageNum": "1",
                "todayGames": "false",
                "_t": str(int(time.time() * 1000)),
            }

            return (
                "https://www.sportybet.com/api/ng/"
                "factsCenter/pcUpcomingEvents?"
                + urlencode(params),
                {
                    "Accept": "application/json, text/plain, */*",
                    "Current-Country": "NG",
                    "Origin": "https://www.sportybet.com",
                    "Referer": "https://www.sportybet.com/ng/",
                },
            )

        return configured_url, {
            "Current-Country": "NG",
            "Referer": "https://www.sportybet.com/ng/",
        }

    if (
        bookmaker_key == "bet9ja"
        and host in ("web.bet9ja.com", "www.bet9ja.com")
        and (
            "marketws.asmx" in path
            or path in ("/sport/odds", "")
        )
    ):
        return (
            "https://web.bet9ja.com/Controls/MarketWS.asmx",
            {
                "Referer": "https://web.bet9ja.com/Sport/Odds",
                "Accept": "text/xml, application/xml, */*",
            },
        )

    if (
        bookmaker_key == "1xbet"
        and (
            host == "1xbet.ng"
            or host.endswith(".1xbet.ng")
        )
        and "/linefeed/" not in path
    ):
        return (
            "https://1xbet.ng/LineFeed/BestGamesExtZip?"
            + urlencode({
                "sports": "1",
                "count": "1000",
                "lng": "en",
                "mode": "4",
                "country": "1",
            }),
            {
                "Accept": "application/json, text/plain, */*",
                "Referer": "https://1xbet.ng/en",
            },
        )

    if (
        bookmaker_key == "betway"
        and (
            host == "betway.com.ng"
            or host.endswith(".betway.com.ng")
        )
        and "/sportsapi/" not in path
    ):
        return (
            "https://www.betway.com.ng/"
            "sportsapi/br/v1/BetBook/Highlights/",
            {
                "Accept": "application/json, text/plain, */*",
                "Referer": "https://www.betway.com.ng/",
            },
        )

    return configured_url, {}


def adapt_sportybet_payload(payload):
    if not isinstance(payload, dict):
        return None, 0, "SportyBet JSON root is not an object"

    data = payload.get("data")

    if (
        not isinstance(data, dict)
        or not isinstance(data.get("tournaments"), list)
    ):
        return None, 0, "Expected data.tournaments array"

    normalized = []
    count = 0

    for tournament in data["tournaments"]:
        if not isinstance(tournament, dict):
            continue

        league = str(
            tournament.get("name")
            or tournament.get("categoryName")
            or "Unknown league"
        )

        events = tournament.get("events", [])

        if not isinstance(events, list):
            continue

        for event in events:
            if not isinstance(event, dict):
                continue

            home = str(
                event.get("homeTeamName") or ""
            ).strip()

            away = str(
                event.get("awayTeamName") or ""
            ).strip()

            if not home or not away:
                continue

            markets = event.get("markets", [])

            if not isinstance(markets, list):
                continue

            for market in markets:
                if not isinstance(market, dict):
                    continue

                count += 1
                outcomes = market.get("outcomes", [])
                parsed_outcomes = []

                if not isinstance(outcomes, list):
                    outcomes = []

                for outcome in outcomes:
                    if not isinstance(outcome, dict):
                        continue

                    odds = finite_number(
                        outcome.get("odds"),
                        minimum=1.000001,
                    )

                    name = str(
                        outcome.get("desc")
                        or outcome.get("name")
                        or ""
                    ).strip()

                    if (
                        odds is not None
                        and name
                        and outcome.get("isActive") is not False
                    ):
                        parsed_outcomes.append({
                            "name": name,
                            "odds": odds,
                        })

                market_name = str(
                    market.get("desc")
                    or market.get("name")
                    or ""
                ).strip()

                specifier = str(
                    market.get("specifier") or ""
                ).strip()

                if specifier:
                    market_name = (
                        f"{market_name} {specifier}"
                    ).strip()

                if market_name:
                    normalized.append({
                        "sport": "Football",
                        "event": f"{home} vs {away}",
                        "market": market_name,
                        "outcomes": parsed_outcomes,
                        "updated_at": first_value(
                            market,
                            ("updated_at", "updatedAt"),
                        ),
                        "league": league,
                    })

    return (
        normalized,
        count,
        "SportyBet nested schema recognized",
    )


def walk_dicts(value, depth=0):
    if depth > 12:
        return

    if isinstance(value, dict):
        yield value

        for child in value.values():
            if isinstance(child, (dict, list)):
                yield from walk_dicts(child, depth + 1)

    elif isinstance(value, list):
        for child in value[:20000]:
            if isinstance(child, (dict, list)):
                yield from walk_dicts(child, depth + 1)


def adapt_generic_json(payload):
    """Best-effort parser; not a guarantee of bookmaker schema support."""
    rows = []
    seen = set()
    raw_count = 0

    for obj in walk_dicts(payload):
        home = first_value(
            obj,
            ("homeTeamName", "homeTeam", "home_name", "home", "team1"),
        )

        away = first_value(
            obj,
            ("awayTeamName", "awayTeam", "away_name", "away", "team2"),
        )

        event = first_value(
            obj,
            (
                "eventName",
                "fixtureName",
                "matchName",
                "event_name",
                "fixture",
                "match",
            ),
        )

        if not event and home and away:
            event = f"{home} vs {away}"

        markets = next(
            (
                obj.get(key)
                for key in (
                    "markets",
                    "marketList",
                    "betMarkets",
                    "oddsMarkets",
                )
                if isinstance(obj.get(key), list)
            ),
            None,
        )

        if not event or not isinstance(markets, list):
            continue

        for market in markets:
            if not isinstance(market, dict):
                continue

            raw_count += 1

            market_name = first_value(
                market,
                (
                    "marketName",
                    "marketDesc",
                    "desc",
                    "name",
                    "type",
                    "market",
                ),
            )

            outcomes_raw = next(
                (
                    market.get(key)
                    for key in (
                        "outcomes",
                        "selections",
                        "runners",
                        "prices",
                        "odds",
                    )
                    if isinstance(market.get(key), (list, dict))
                ),
                None,
            )

            if isinstance(outcomes_raw, dict):
                outcomes_raw = [
                    (
                        {"name": key, "odds": value}
                        if not isinstance(value, dict)
                        else dict(value, name=value.get("name", key))
                    )
                    for key, value in outcomes_raw.items()
                ]

            outcomes = []

            if isinstance(outcomes_raw, list):
                for outcome in outcomes_raw:
                    if isinstance(outcome, dict):
                        name = first_value(
                            outcome,
                            (
                                "name",
                                "desc",
                                "outcomeName",
                                "selectionName",
                                "label",
                                "runnerName",
                                "key",
                            ),
                        )

                        odd = first_value(
                            outcome,
                            (
                                "odds",
                                "price",
                                "decimalOdds",
                                "value",
                                "odd",
                            ),
                        )

                    else:
                        name, odd = None, outcome

                    odds = finite_number(odd, minimum=1.000001)

                    if name and odds:
                        outcomes.append({
                            "name": str(name),
                            "odds": odds,
                        })

            if not market_name or len(outcomes) < 2:
                continue

            updated = first_value(
                market,
                (
                    "updated_at",
                    "updatedAt",
                    "lastUpdated",
                    "timestamp",
                    "updateTime",
                ),
                first_value(
                    obj,
                    (
                        "updated_at",
                        "updatedAt",
                        "lastUpdated",
                        "timestamp",
                        "updateTime",
                    ),
                ),
            )

            row = {
                "sport": str(
                    first_value(
                        obj,
                        ("sport", "sportName", "categoryName"),
                        "Football",
                    )
                ),
                "event": str(event),
                "market": str(market_name),
                "outcomes": outcomes,
                "updated_at": updated,
            }

            signature = (
                normalize_text(row["event"]),
                normalize_text(row["market"]),
                tuple(
                    (
                        normalize_text(outcome["name"]),
                        outcome["odds"],
                    )
                    for outcome in outcomes
                ),
            )

            if signature not in seen:
                seen.add(signature)
                rows.append(row)

    if not rows:
        return (
            None,
            raw_count,
            "JSON received but no supported event/market/outcome "
            "structure was recognized",
        )

    return (
        rows,
        raw_count,
        "Best-effort generic JSON adapter recognized events; "
        "verify schema and market semantics",
    )


def xml_tag(element):
    return element.tag.rsplit("}", 1)[-1].lower()


def xml_child_text(element, names):
    for child in list(element):
        if (
            xml_tag(child) in names
            and child.text
            and child.text.strip()
        ):
            return child.text.strip()

    return None


def adapt_generic_xml(root):
    rows = []
    raw_count = 0

    event_tags = {"event", "match", "fixture", "game"}
    market_tags = {"market", "bettingmarket", "bettype"}
    outcome_tags = {"outcome", "selection", "runner", "odd", "price"}

    for event_element in root.iter():
        if xml_tag(event_element) not in event_tags:
            continue

        attrs = {
            str(key).lower(): value
            for key, value in event_element.attrib.items()
        }

        home = (
            attrs.get("home")
            or attrs.get("hometeam")
            or xml_child_text(
                event_element,
                {"home", "hometeam", "hometeamname"},
            )
        )

        away = (
            attrs.get("away")
            or attrs.get("awayteam")
            or xml_child_text(
                event_element,
                {"away", "awayteam", "awayteamname"},
            )
        )

        name = (
            attrs.get("name")
            or attrs.get("eventname")
            or xml_child_text(
                event_element,
                {"name", "eventname", "description"},
            )
        )

        event = (
            f"{home} vs {away}"
            if home and away
            else name
        )

        if not event:
            continue

        for market_element in event_element.iter():
            if (
                market_element is event_element
                or xml_tag(market_element) not in market_tags
            ):
                continue

            market_attrs = {
                str(key).lower(): value
                for key, value in market_element.attrib.items()
            }

            market_name = (
                market_attrs.get("name")
                or market_attrs.get("description")
                or market_attrs.get("type")
                or xml_child_text(
                    market_element,
                    {"name", "description", "marketname"},
                )
            )

            outcomes = []

            for outcome_element in market_element.iter():
                if (
                    outcome_element is market_element
                    or xml_tag(outcome_element) not in outcome_tags
                ):
                    continue

                outcome_attrs = {
                    str(key).lower(): value
                    for key, value in outcome_element.attrib.items()
                }

                name_value = (
                    outcome_attrs.get("name")
                    or outcome_attrs.get("label")
                    or outcome_attrs.get("description")
                    or xml_child_text(
                        outcome_element,
                        {"name", "label", "description"},
                    )
                )

                odd_value = (
                    outcome_attrs.get("odds")
                    or outcome_attrs.get("price")
                    or outcome_attrs.get("value")
                    or xml_child_text(
                        outcome_element,
                        {"odds", "price", "value"},
                    )
                )

                odds = finite_number(
                    odd_value,
                    minimum=1.000001,
                )

                if name_value and odds:
                    outcomes.append({
                        "name": name_value,
                        "odds": odds,
                    })

            if market_name and len(outcomes) >= 2:
                raw_count += 1

                rows.append({
                    "sport": "Football",
                    "event": event,
                    "market": market_name,
                    "outcomes": outcomes,
                    "updated_at": attrs.get("updatedat"),
                })

    if not rows:
        return (
            None,
            raw_count,
            "XML received but no supported event/market/outcome "
            "structure was recognized",
        )

    return (
        rows,
        raw_count,
        "Best-effort generic XML adapter recognized markets; "
        "verify schema and market semantics",
    )


# ---------------------------------------------------------
# Event, market and outcome normalization
# ---------------------------------------------------------

def split_event_teams(event):
    parts = re.split(
        r"\s+(?:vs\.?|v\.?|versus)\s+|\s+[-–—]\s+",
        str(event or "").strip(),
        maxsplit=1,
        flags=re.IGNORECASE,
    )

    if len(parts) == 2 and all(part.strip() for part in parts):
        return parts[0].strip(), parts[1].strip()

    return None, None


def canonical_event_key(event):
    home, away = split_event_teams(event)

    if home and away:
        # Preserve team order because home/away can change market meaning.
        return (
            normalize_text(home)
            + " vs "
            + normalize_text(away)
        )

    return normalize_text(event)


def canonical_outcome_key(
    name, home=None, away=None, allow_1x2=False
):
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
    if (
        not isinstance(raw, dict)
        or bookmaker_key not in BOOKMAKERS
    ):
        return None

    sport = str(raw.get("sport") or "Unknown sport").strip()
    event = str(
        raw.get("event") or raw.get("fixture") or ""
    ).strip()

    market = str(raw.get("market") or "").strip()
    outcomes = raw.get("outcomes")

    if not event or not market or not isinstance(outcomes, list):
        return None

    home, away = split_event_teams(event)
    market_key = normalize_text(market)

    if (
        "double chance" in market_key
        or market_key in {"1x", "12", "x2"}
    ):
        return None

    valid = []

    for outcome in outcomes:
        if not isinstance(outcome, dict):
            continue

        name = str(
            outcome.get("name") or outcome.get("selection") or ""
        ).strip()

        odds = finite_number(
            outcome.get("odds"),
            minimum=1.000001,
        )

        if not name or odds is None:
            continue

        valid.append({
            "name": name,
            "odds": odds,
            "estimated_probability": finite_number(
                outcome.get("estimated_probability"),
                minimum=0,
                maximum=1,
            ),
        })

    raw_keys = [normalize_text(item["name"]) for item in valid]

    if len(raw_keys) != len(set(raw_keys)):
        return None

    labels = set(raw_keys)

    market_suggests_1x2 = bool(re.search(
        r"\b(1x2|match result|full time result|"
        r"90 minutes result|three way result|moneyline)\b",
        market_key,
    ))

    has_draw = bool(
        labels & {"x", "draw", "tie", "match drawn"}
    )

    is_1x2 = (
        len(valid) == 3
        and (
            market_suggests_1x2
            or (
                has_draw
                and bool(labels & {"1", "home"})
                and bool(labels & {"2", "away"})
            )
        )
    )

    if market_suggests_1x2 and len(valid) != 3:
        return None

    normalized_outcomes = []

    for item in valid:
        key = canonical_outcome_key(
            item["name"], home, away, is_1x2
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
        item["name_key"]
        for item in normalized_outcomes
    ]

    if len(outcome_keys) != len(set(outcome_keys)):
        return None

    if is_1x2:
        if set(outcome_keys) != {"home", "draw", "away"}:
            return None

        market_key = "football_1x2"

    updated_at = raw.get("updated_at")
    stamp = parse_timestamp(updated_at)

    age = (
        (datetime.now(timezone.utc) - stamp).total_seconds()
        if stamp
        else None
    )

    stale = (
        age > STALE_AFTER_SECONDS
        if age is not None
        else None
    )

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
        "age_seconds": round(age) if age is not None else None,
        "stale": stale,
    }


# ---------------------------------------------------------
# Fetch and normalize bookmaker feeds
# ---------------------------------------------------------

def fetch_all_feeds():
    rows = []
    health = []

    for key, config in BOOKMAKERS.items():
        request_url, headers = prepare_feed_request(
            key, config["feed_url"]
        )

        if not request_url:
            diag = diag_template()
            diag.update(
                bookmaker=config["name"],
                detail="No feed URL configured; endpoint not tested",
            )
            health.append(diag)
            continue

        if (
            key == "bet9ja"
            and (
                "marketws.asmx" in urlsplit(request_url).path.lower()
                or "xml" in headers.get("Accept", "")
            )
        ):
            payload, diag = safe_fetch_xml(
                request_url, config["name"], headers
            )

            if payload is not None:
                raw_events, raw_count, detail = (
                    adapt_generic_xml(payload)
                )
                diag["xml_valid"] = True
            else:
                raw_events = None
                raw_count = 0
                detail = diag.get("detail", "XML fetch failed")

        else:
            payload, diag = safe_fetch_json(
                request_url, config["name"], headers
            )

            if payload is None:
                raw_events = None
                raw_count = 0
                detail = diag.get("detail", "JSON fetch failed")

            elif key == "sportybet":
                raw_events, raw_count, detail = (
                    adapt_sportybet_payload(payload)
                )

            else:
                raw_events, raw_count, detail = (
                    adapt_generic_json(payload)
                )

        diag["bookmaker"] = config["name"]
        diag["detail"] = detail

        if raw_events is None or not isinstance(raw_events, list):
            diag.update(
                status=(
                    "invalid schema"
                    if payload is not None
                    else diag["status"]
                ),
                schema_valid=False,
            )
            health.append(diag)
            continue

        diag["schema_valid"] = True
        diag["raw_events"] = raw_count

        for raw in raw_events:
            row = normalize_feed_event(key, raw)

            if row:
                rows.append(row)
                diag["events_loaded"] += 1
            else:
                diag["events_rejected"] += 1

        if diag["events_loaded"]:
            diag["status"] = "usable markets loaded"

        elif diag["status"] not in (
            "http error",
            "fetch failed",
            "invalid response",
            "response too large",
        ):
            diag["status"] = "feed reached; no usable markets"

            if not diag["detail"]:
                diag["detail"] = (
                    "Data arrived but no complete supported markets "
                    "were extracted"
                )

        health.append(diag)

    return rows, health


# ---------------------------------------------------------
# Surebet calculations and evidence labels
# ---------------------------------------------------------

def find_arbitrage(rows, bankroll=None):
    bankroll = (
        BANKROLL
        if bankroll is None
        else max(0.0, float(bankroll))
    )

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
            row["bookmaker_key"]
            for row in market_rows
        }) < 2:
            continue

        outcome_sets = [
            frozenset(
                outcome["name_key"]
                for outcome in row["outcomes"]
            )
            for row in market_rows
        ]

        if not outcome_sets or len(set(outcome_sets)) != 1:
            continue

        outcome_names = set(outcome_sets[0])

        if len(outcome_names) not in (2, 3):
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
                            "stale": row.get("stale"),
                            "updated_at": row.get("updated_at"),
                        })

            if quotes:
                best[outcome_name] = max(
                    quotes,
                    key=lambda quote_item: quote_item["odds"],
                )

        if set(best) != outcome_names:
            continue

        implied_sum = sum(
            1.0 / quote_item["odds"]
            for quote_item in best.values()
        )

        # The theoretical arbitrage condition is strict.
        if implied_sum >= 1.0:
            continue

        legs = []

        for outcome_name in sorted(outcome_names):
            quote_item = dict(best[outcome_name])

            quote_item["stake_fraction"] = (
                (1.0 / quote_item["odds"]) / implied_sum
            )

            quote_item["stake_amount"] = round(
                bankroll * quote_item["stake_fraction"], 2
            )

            legs.append(quote_item)

        theoretical_return = (
            bankroll / implied_sum
            if implied_sum > 0
            else 0.0
        )

        profit = theoretical_return - bankroll

        all_timestamped = all(
            parse_timestamp(item.get("updated_at")) is not None
            for item in best.values()
        )

        all_fresh = all(
            item.get("stale") is False
            for item in best.values()
        )

        unique_books = len({
            item["bookmaker_key"]
            for item in legs
        })

        if (
            all_timestamped
            and all_fresh
            and unique_books == len(legs)
        ):
            evidence = (
                "Recently timestamped data; still verify odds "
                "at each bookmaker"
            )

        elif all_timestamped:
            evidence = (
                "Timestamped data; freshness or cross-source "
                "confirmation needs review"
            )

        else:
            evidence = (
                "Unverified freshness: source timestamps "
                "missing or incomplete"
            )

        # A transparent heuristic score, not a probability
        # that the opportunity will succeed.
        evidence_score = (
            (40 if all_timestamped else 15)
            + (20 if all_fresh else 0)
            + (20 if unique_books == len(legs) else 0)
            + (20 if len(legs) == len(outcome_names) else 0)
        )

        opportunities.append({
            "sport": market_rows[0]["sport"],
            "event": market_rows[0]["event"],
            "market": market_rows[0]["market"],
            "outcome_count": len(outcome_names),
            "implied_probability_sum_percent": round(
                implied_sum * 100, 4
            ),
            "theoretical_margin_percent": round(
                (1.0 - implied_sum) * 100, 4
            ),
            "theoretical_bankroll": round(bankroll, 2),
            "theoretical_gross_return": round(
                theoretical_return, 2
            ),
            "theoretical_profit": round(profit, 2),
            "theoretical_roi_percent": (
                round(profit / bankroll * 100, 4)
                if bankroll > 0
                else None
            ),
            "evidence_label": evidence,
            "evidence_score": evidence_score,
            "legs": legs,
            "warning": (
                "Mathematical estimate only. Not a guarantee or "
                "instruction to bet. Confirm the same event and "
                "market, current odds, limits, settlement rules, "
                "fees and availability before relying on it."
            ),
        })

    opportunities.sort(
        key=lambda item: item["theoretical_margin_percent"],
        reverse=True,
    )

    return opportunities


def run_scan():
    global LAST_SCAN, LAST_FEED_STATUS

    if not SCAN_LOCK.acquire(blocking=False):
        with STATE_LOCK:
            if LAST_SCAN:
                result = dict(LAST_SCAN)
                result["scan_note"] = (
                    "A scan was already running; returning "
                    "the most recent result"
                )
                return result

        return {
            "ok": False,
            "error": "A scan is already running; retry shortly",
        }

    try:
        rows, health = fetch_all_feeds()
        arbitrage = find_arbitrage(rows)

        result = {
            "ok": True,
            "scanned_at": now_iso(),
            "bookmakers": health,
            "configured_feeds": sum(
                bool(config["feed_url"])
                for config in BOOKMAKERS.values()
            ),
            "feeds_reached": sum(
                1
                for item in health
                if item.get("http_status")
                and 200 <= item["http_status"] < 300
            ),
            "feeds_with_usable_markets": sum(
                1
                for item in health
                if item.get("events_loaded", 0) > 0
            ),
            "usable_markets": len(rows),
            "arbitrage_count": len(arbitrage),
            "arbitrage": arbitrage,
            "note": (
                "No opportunities means none were detected in "
                "the supported, successfully parsed feed data; "
                "it does not prove no arbitrage exists anywhere."
            ),
        }

        with STATE_LOCK:
            LAST_SCAN = result
            LAST_FEED_STATUS = health

        logging.info(
            "Scan complete: configured=%s reached=%s "
            "usable_markets=%s opportunities=%s",
            result["configured_feeds"],
            result["feeds_reached"],
            len(rows),
            len(arbitrage),
        )

        return result

    finally:
        SCAN_LOCK.release()


# ---------------------------------------------------------
# Mobile-friendly dashboard
# ---------------------------------------------------------

HTML = r'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="description" content="SolveEase Surebet Intelligence: read-only odds comparison, transparent calculations and feed diagnostics.">
<title>SolveEase Surebet Intelligence</title>
<style>
:root{color-scheme:dark;--bg:#09111f;--panel:#121e31;--line:#2b3b54;--text:#edf4ff;--muted:#a9b9d0;--accent:#73e0b2;--warn:#ffd27a}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:16px/1.5 system-ui,-apple-system,sans-serif}
main{width:min(1120px,100%);margin:auto;padding:20px 14px 48px}
h1{font-size:clamp(1.6rem,5vw,2.35rem);line-height:1.15;margin:.5rem 0}
h2{font-size:1.18rem;margin:.1rem 0 .7rem}
.muted,.small{color:var(--muted)}
.small{font-size:.88rem}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:16px;padding:16px;margin:14px 0;min-width:0}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,260px),1fr));gap:12px}
.row{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
label{display:block;font-size:.9rem;color:var(--muted);margin:8px 0 4px}
input,button{font:inherit;width:100%;min-height:48px;border-radius:10px;padding:11px 12px}
input{border:1px solid var(--line);background:#07101d;color:var(--text)}
button{border:0;background:var(--accent);color:#071b15;font-weight:800;cursor:pointer}
button:disabled{opacity:.6;cursor:wait}
.row button{width:auto;min-width:130px}
.status{padding:10px;border-radius:10px;background:#0a1424;border:1px solid var(--line);overflow-wrap:anywhere}
.good{color:var(--accent)}
.warn{color:var(--warn)}
.card{padding:12px;border:1px solid var(--line);border-radius:12px;margin:10px 0;overflow-wrap:anywhere}
.legs{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,210px),1fr));gap:8px}
.pill{display:inline-block;padding:3px 8px;border:1px solid var(--line);border-radius:999px;font-size:.82rem;color:var(--muted)}
pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:.84rem}
footer{margin-top:20px;color:var(--muted);font-size:.84rem}
@media(min-width:700px){main{padding-top:30px}.panel{padding:20px}}
</style>
</head>
<body>
<main>
<p class="pill">READ-ONLY · NO AUTOMATED BETTING</p>
<h1>SolveEase Surebet Intelligence</h1>
<p class="muted">Compare supported feed data, inspect evidence, and calculate theoretical stake allocation. A server being online does not mean odds feeds are working.</p>

<section class="panel">
<div class="row">
<div style="flex:1">
<h2>Scanner</h2>
<p class="small">The scan can take several seconds. It only uses configured feed URLs.</p>
</div>
<button id="scanBtn" onclick="scan()">Scan feeds</button>
</div>
<div id="scanStatus" class="status" role="status">No scan run in this browser session.</div>
<div id="summary" class="grid"></div>
<div id="opportunities"></div>
<details>
<summary>Feed diagnostics</summary>
<div id="feeds"></div>
</details>
</section>

<section class="panel">
<h2>SportyBet ticket lookup</h2>
<p class="small">Enter a share/booking code. This uses an unofficial read-only endpoint; codes may not resolve, and selections may not be identifiable.</p>
<label for="code">Share code</label>
<input id="code" autocomplete="off" maxlength="200" placeholder="Enter SportyBet share code">
<div class="row" style="margin-top:10px">
<button id="lookupBtn" onclick="lookup()">Look up ticket</button>
</div>
<div id="lookupResult" aria-live="polite"></div>
</section>

<section class="panel">
<h2>Stake calculator</h2>
<p class="small">For decimal odds covering every outcome of the same market. These are mathematical estimates, not betting advice.</p>
<label for="bankroll">Total stake / bankroll</label>
<input id="bankroll" type="number" min="0.01" step="0.01" value="100000">
<label for="odds">Decimal odds (comma-separated; e.g. 2.15, 3.6, 3.4)</label>
<input id="odds" placeholder="2.15, 3.6">
<button style="margin-top:10px" onclick="calculate()">Calculate stakes</button>
<div id="calcResult" class="status" style="margin-top:10px">Enter at least two valid decimal odds.</div>
</section>

<footer>Evidence labels describe the available feed data only. Prices can change, markets can differ, and an apparent margin can disappear due to delays, limits, settlement rules, or fees. Never share passwords, bookmaker logins, or API secrets here.</footer>
</main>

<script>
const el=id=>document.getElementById(id);
const money=n=>Number(n).toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2});

async function post(path,data={}){
 const r=await fetch(path,{
  method:'POST',
  headers:{'Content-Type':'application/json'},
  body:JSON.stringify(data)
 });
 const j=await r.json();
 if(!r.ok)throw new Error(j.error||'Request failed');
 return j;
}

function escapeHtml(s){
 return String(s??'').replace(/[&<>"']/g,c=>({
  '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'
 }[c]));
}

async function scan(){
 const b=el('scanBtn');
 b.disabled=true;
 b.textContent='Scanning…';
 el('scanStatus').textContent='Fetching configured feeds and parsing supported market formats…';

 try{
  const d=await post('/api/scan');

  el('scanStatus').innerHTML='<span class="good">Scan finished:</span> '+escapeHtml(d.scanned_at||'time unavailable');

  el('summary').innerHTML=[
   ['Configured feeds',d.configured_feeds],
   ['Feeds reached',d.feeds_reached],
   ['Usable markets',d.usable_markets],
   ['Detected opportunities',d.arbitrage_count]
  ].map(x=>'<div class="card"><div class="small">'+x[0]+'</div><strong>'+escapeHtml(x[1])+'</strong></div>').join('');

  el('opportunities').innerHTML=d.arbitrage?.length
   ?d.arbitrage.map(o=>'<div class="card"><h3>'+escapeHtml(o.event)+'</h3><div>'+escapeHtml(o.market)+' · '+o.outcome_count+' outcomes</div><p><strong class="good">Theoretical margin: '+money(o.theoretical_margin_percent)+'%</strong> · ROI: '+(o.theoretical_roi_percent===null?'—':money(o.theoretical_roi_percent)+'%')+'</p><p>Stake total: '+money(o.theoretical_bankroll)+' · Gross return: '+money(o.theoretical_gross_return)+' · Estimated profit: '+money(o.theoretical_profit)+'</p><p class="small">Evidence score '+escapeHtml(o.evidence_score)+'/100 — '+escapeHtml(o.evidence_label)+'</p><div class="legs">'+o.legs.map(l=>'<div class="status"><strong>'+escapeHtml(l.selection)+'</strong><br>Odds '+money(l.odds)+'<br>Stake '+money(l.stake_amount)+'<br><span class="small">'+escapeHtml(l.bookmaker)+'</span></div>').join('')+'</div><p class="small">'+escapeHtml(o.warning)+'</p></div>').join('')
   :'<div class="status">No surebets detected in the supported data returned by these feeds. This is not proof that no arbitrage exists elsewhere.</div>';

  el('feeds').innerHTML=(d.bookmakers||[]).map(f=>'<div class="card"><strong>'+escapeHtml(f.bookmaker)+'</strong> · '+escapeHtml(f.status)+'<p class="small">HTTP: '+escapeHtml(f.http_status??'—')+' · JSON: '+!!f.json_valid+' · XML: '+!!f.xml_valid+' · Schema: '+!!f.schema_valid+' · Markets loaded: '+escapeHtml(f.events_loaded||0)+' · rejected: '+escapeHtml(f.events_rejected||0)+'</p><p class="small">'+escapeHtml(f.detail||'No further details')+'</p></div>').join('');

 }catch(e){
  el('scanStatus').textContent='Scan request failed: '+e.message;
 }finally{
  b.disabled=false;
  b.textContent='Scan feeds';
 }
}

async function lookup(){
 const code=el('code').value.trim();

 if(!code){
  el('lookupResult').textContent='Enter a share code first.';
  return;
 }

 const b=el('lookupBtn');
 b.disabled=true;
 b.textContent='Looking up…';
 el('lookupResult').textContent='Contacting SportyBet lookup endpoint…';

 try{
  const d=await post('/api/lookup',{code});

  if(!d.ok){
   el('lookupResult').innerHTML='<div class="status warn">'+escapeHtml(d.error||'Lookup failed')+'</div>';
   return;
  }

  el('lookupResult').innerHTML='<div class="card"><p><strong>'+escapeHtml(d.bookmaker)+'</strong> · '+escapeHtml(d.status)+'</p><p>Selections returned: '+escapeHtml(d.displayed_selections_count)+' · source count: '+escapeHtml(d.source_selection_count)+'</p><p>Source combined odds: '+escapeHtml(d.source_combined_odds??'—')+' · calculated: '+escapeHtml(d.calculated_combined_odds??'—')+' · agreement: '+escapeHtml(d.odds_agree??'not verifiable')+'</p>'+(d.legs||[]).map(l=>'<div class="status"><strong>'+escapeHtml(l.event)+'</strong><br>'+escapeHtml(l.market)+' — '+escapeHtml(l.selection)+'<br>Odds: '+escapeHtml(l.odds??'not identified')+' · kickoff: '+escapeHtml(l.kickoff??'—')+'</div>').join('')+'<p class="small">'+escapeHtml(d.note||'')+'</p></div>';

 }catch(e){
  el('lookupResult').textContent='Lookup request failed: '+e.message;
 }finally{
  b.disabled=false;
  b.textContent='Look up ticket';
 }
}

function calculate(){
 const b=Number(el('bankroll').value);
 const odds=el('odds').value.split(',').map(x=>Number(x.trim()));

 if(!Number.isFinite(b)||b<=0||odds.length<2||odds.some(x=>!Number.isFinite(x)||x<=1)){
  el('calcResult').textContent='Enter a positive total stake and at least two decimal odds greater than 1.';
  return;
 }

 const s=odds.reduce((a,o)=>a+1/o,0);
 const stakes=odds.map(o=>b*(1/o)/s);
 const ret=b/s;
 const profit=ret-b;

 el('calcResult').innerHTML='<p>Implied probability sum: <strong>'+money(s*100)+'%</strong></p><p>Result: <strong class="'+(s<1?'good':'warn')+'">'+(s<1?'Theoretical arbitrage condition met':'No theoretical arbitrage (sum must be below 100%)')+'</strong></p><p>Estimated gross return: '+money(ret)+'<br>Estimated profit: '+money(profit)+'<br>ROI: '+money(profit/b*100)+'%</p>'+stakes.map((x,i)=>'<div class="status">Outcome '+(i+1)+': stake '+money(x)+' at odds '+money(odds[i])+'</div>').join('')+'<p class="small">Estimates exclude fees, rounding, odds movement, limits, voids, and settlement differences.</p>';
}
</script>
</body>
</html>'''


# ---------------------------------------------------------
# HTTP server and API
# ---------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "SolveEase/4.0"

    def log_message(self, fmt, *args):
        logging.info(
            "%s - %s",
            self.address_string(),
            fmt.split('"')[0].strip(),
        )

    def send_bytes(self, status, body, content_type):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

        if self.command != "HEAD":
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def send_json(self, status, data):
        try:
            body = json.dumps(
                data,
                ensure_ascii=False,
                allow_nan=False,
            ).encode("utf-8")

        except (TypeError, ValueError):
            status = 500
            body = (
                b'{"ok":false,'
                b'"error":"Could not serialize response"}'
            )

        self.send_bytes(
            status,
            body,
            "application/json; charset=utf-8",
        )

    def read_json(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return None, "Invalid Content-Length"

        if length < 0 or length > MAX_POST_BYTES:
            return None, "Request body is too large"

        raw = self.rfile.read(length) if length else b"{}"

        try:
            value = json.loads(raw.decode("utf-8"))

        except (json.JSONDecodeError, UnicodeDecodeError):
            return None, "Request body must be valid JSON"

        if not isinstance(value, dict):
            return None, "JSON body must be an object"

        return value, None

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        path = urlsplit(self.path).path.rstrip("/") or "/"

        if path == "/":
            return self.send_bytes(
                200,
                HTML.encode("utf-8"),
                "text/html; charset=utf-8",
            )

        if path == "/health":
            with STATE_LOCK:
                scan = LAST_SCAN
                lookup_result = LAST_LOOKUP

            configured = sum(
                bool(item["feed_url"])
                for item in BOOKMAKERS.values()
            )

            return self.send_json(200, {
                "status": "online",
                "version": "4.0",
                "service": "SolveEase Surebet Intelligence",
                "uptime_seconds": round(time.time() - STARTED_AT),
                "configured_feeds": configured,
                "last_scan_at": (
                    scan.get("scanned_at") if scan else None
                ),
                "last_scan_usable_markets": (
                    scan.get("usable_markets") if scan else None
                ),
                "last_scan_opportunities": (
                    scan.get("arbitrage_count") if scan else None
                ),
                "last_lookup_at": (
                    lookup_result.get("at")
                    if lookup_result else None
                ),
                "note": (
                    "Online status confirms only that this HTTP "
                    "service is responding; it does not prove "
                    "feed availability."
                ),
            })

        if path == "/api/status":
            with STATE_LOCK:
                scan = LAST_SCAN

            return self.send_json(200, {
                "ok": True,
                "last_scan": scan,
            })

        return self.send_json(404, {
            "ok": False,
            "error": "Route not found",
        })

    def do_POST(self):
        path = urlsplit(self.path).path

        if path not in ("/api/scan", "/api/lookup"):
            return self.send_json(404, {
                "ok": False,
                "error": "Route not found",
            })

        body, error = self.read_json()

        if error:
            return self.send_json(400, {
                "ok": False,
                "error": error,
            })

        if path == "/api/scan":
            result = run_scan()

            return self.send_json(
                200 if result.get("ok") else 409,
                result,
            )

        result = lookup_sportybet(body.get("code", ""))

        return self.send_json(
            200 if result.get("ok") else 400,
            result,
        )

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header(
            "Allow", "GET, HEAD, POST, OPTIONS"
        )
        self.send_header("Content-Length", "0")
        self.end_headers()


def main():
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    server.daemon_threads = True

    logging.info(
        "SolveEase Surebet Intelligence v4.0 listening on port %s",
        PORT,
    )

    try:
        server.serve_forever()

    except KeyboardInterrupt:
        pass

    finally:
        server.server_close()


if __name__ == "__main__":
    main()
