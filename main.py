
#!/usr/bin/env python3
"""
SolveEase Multi-Bookmaker Dashboard
Python standard library only.

Render start command: python main.py

Existing:
- SportyBet read-only booking-code lookup
- Source-vs-calculated odds diagnostics
- No assumed ticket currency
- Privacy-safe request logging

Added:
- Multi-bookmaker JSON odds-feed scanner
- Two-outcome arbitrage detection
- Configurable 60% probability filter
- Alternative-market prioritization
- Source-health reporting
- Ticket reconstruction for manual recreation

IMPORTANT:
Bookmaker share endpoints and odds feeds are not interchangeable.
This application does not place bets or generate destination bookmaker
codes. Configure verified, permitted JSON odds feeds to enable scanning.
"""

import json
import logging
import math
import os
import re
import threading
import time
import unicodedata
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen

HOST = "0.0.0.0"
PORT = int(os.environ.get("PORT", "10000"))
TIMEOUT = float(os.environ.get("REQUEST_TIMEOUT_SECONDS", "12"))
MIN_PROBABILITY = float(os.environ.get("MIN_PROBABILITY", "0.60"))
BANKROLL = float(os.environ.get("BANKROLL", "100000"))

SPORTYBET_SHARE_URL = "https://www.sportybet.com/api/ng/orders/share/"

# Only configure feeds you control or are authorized to access.
# Each URL must return JSON in the schema documented below.
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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)

STARTED_AT = time.time()
STATE_LOCK = threading.Lock()
LAST_SCAN = None
LAST_LOOKUP = None


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


def safe_fetch_json(url, label):
    """
    Fetch only server-configured URLs.
    User input is never used as a network destination.
    """
    if not url:
        return None, "No feed URL configured"

    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname:
        return None, "Feed URL must be a valid HTTPS URL"

    request = Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "SolveEaseDashboard/2.0",
        },
        method="GET",
    )

    try:
        with urlopen(request, timeout=TIMEOUT) as response:
            if response.status != 200:
                return None, f"HTTP {response.status}"
            body = response.read(3_000_000)
            payload = json.loads(body.decode("utf-8", errors="replace"))
            if not isinstance(payload, dict):
                return None, "Feed must return a JSON object"
            return payload, "OK"
    except HTTPError as exc:
        logging.warning("%s feed returned HTTP %s", label, exc.code)
        return None, f"HTTP {exc.code}"
    except (URLError, TimeoutError, OSError) as exc:
        logging.warning("%s feed failed: %s", label, type(exc).__name__)
        return None, "Network request failed"
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None, "Feed returned invalid JSON"


# ------------------------------------------------------------
# SportyBet booking-code lookup
# ------------------------------------------------------------

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
            payload = json.loads(
                response.read(2_000_000).decode("utf-8", errors="replace")
            )
    except HTTPError as exc:
        logging.warning("SportyBet lookup HTTP %s", exc.code)
        return {
            "ok": False,
            "error": f"SportyBet returned HTTP {exc.code}",
        }
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
                payload,
                ("message", "msg", "errorMsg"),
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

        chosen = None
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
                        outcome,
                        ("isSelected", "selected", "chosen", "betSelected"),
                        None,
                    )
                    if marker is True or marker == 1 or str(marker).lower() == "true":
                        selected_candidates.append(candidate)

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
                item,
                ("estimateStartTime", "matchTime", "startTime", "eventTime"),
                None,
            ),
        })

    if not legs:
        return {
            "ok": False,
            "error": (
                "No recognizable selections returned. The code may be invalid "
                "or the unofficial endpoint may have changed."
            ),
        }

    source_count = len(ticket.get("selections", [])) if isinstance(
        ticket.get("selections"), list
    ) and ticket.get("selections") else len(legs)

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

    global LAST_LOOKUP
    with STATE_LOCK:
        LAST_LOOKUP = {
            "at": now_iso(),
            "displayed_selections": len(legs),
            "source_selections": source_count,
        }

    return {
        "ok": True,
        "bookmaker": "SportyBet",
        "bookmaker_identification": "Confirmed by successful SportyBet lookup",
        "displayed_selections_count": len(legs),
        "source_selection_count": source_count,
        "legs": legs,
        "source_combined_odds": source_total,
        "calculated_combined_odds": calculated,
        "odds_agree": (
            source_total is not None
            and calculated is not None
            and math.isclose(source_total, calculated, rel_tol=0.005)
        ),
        "status": status,
        "stake": scalar(ticket.get("totalStake")),
        "potential_return": scalar(ticket.get("potentialWinnings")),
        "currency": None,
        "retrieved_at": now_iso(),
        "note": (
            "Read-only lookup from an unofficial SportyBet endpoint. "
            "Stake/return units and currency are unverified. "
            "Verify all details on the bookmaker's own site."
        ),
    }


# ------------------------------------------------------------
# Odds-feed normalization and arbitrage
# ------------------------------------------------------------

def normalize_feed_event(bookmaker_key, raw):
    """
    Expected JSON feed schema:
    {
      "events": [{
        "sport": "football",
        "event": "Home Team vs Away Team",
        "market": "Total Goals Over/Under 2.5",
        "outcomes": [
          {"name": "Over 2.5", "odds": 2.10,
           "estimated_probability": 0.62},
          {"name": "Under 2.5", "odds": 1.90,
           "estimated_probability": 0.38}
        ],
        "updated_at": "ISO timestamp"
      }]
    }

    estimated_probability must be a separate model estimate from the feed,
    not simply 1/odds. It is not independently validated by this app.
    """
    if not isinstance(raw, dict):
        return None

    sport = str(raw.get("sport") or "Unknown sport").strip()
    event = str(raw.get("event") or raw.get("fixture") or "").strip()
    market = str(raw.get("market") or "").strip()
    outcomes = raw.get("outcomes")

    if not event or not market or not isinstance(outcomes, list):
        return None

    normalized_outcomes = []
    for outcome in outcomes:
        if not isinstance(outcome, dict):
            continue

        name = str(outcome.get("name") or outcome.get("selection") or "").strip()
        odds = finite_number(outcome.get("odds"), minimum=1.000001)
        if not name or odds is None:
            continue

        probability = finite_number(
            outcome.get("estimated_probability"), minimum=0, maximum=1
        )
        normalized_outcomes.append({
            "name": name,
            "name_key": normalize_text(name),
            "odds": odds,
            "estimated_probability": probability,
        })

    # Only exactly two unique outcomes qualify.
    unique = {}
    for outcome in normalized_outcomes:
        unique[outcome["name_key"]] = outcome
    normalized_outcomes = list(unique.values())

    if len(normalized_outcomes) != 2:
        return None

    return {
        "bookmaker_key": bookmaker_key,
        "bookmaker": BOOKMAKERS[bookmaker_key]["name"],
        "sport": sport,
        "event": event,
        "event_key": normalize_text(event),
        "market": market,
        "market_key": normalize_text(market),
        "outcomes": normalized_outcomes,
        "updated_at": raw.get("updated_at"),
    }


def fetch_all_feeds():
    rows = []
    health = []

    for key, config in BOOKMAKERS.items():
        payload, message = safe_fetch_json(config["feed_url"], config["name"])
        if payload is None:
            health.append({
                "bookmaker": config["name"],
                "status": "unavailable",
                "detail": message,
                "events_loaded": 0,
            })
            continue

        raw_events = payload.get("events")
        if not isinstance(raw_events, list):
            health.append({
                "bookmaker": config["name"],
                "status": "invalid feed schema",
                "detail": 'Expected an "events" array',
                "events_loaded": 0,
            })
            continue

        loaded = 0
        for raw in raw_events:
            row = normalize_feed_event(key, raw)
            if row:
                rows.append(row)
                loaded += 1

        health.append({
            "bookmaker": config["name"],
            "status": "online" if loaded else "no usable markets",
            "detail": message,
            "events_loaded": loaded,
        })

    return rows, health


def find_arbitrage(rows):
    groups = {}
    for row in rows:
        key = (row["sport"].lower(), row["event_key"], row["market_key"])
        groups.setdefault(key, []).append(row)

    opportunities = []
    for key, market_rows in groups.items():
        # Need at least two bookmaker records for a comparison.
        if len({r["bookmaker_key"] for r in market_rows}) < 2:
            continue

        outcome_names = set()
        for row in market_rows:
            outcome_names.update(o["name_key"] for o in row["outcomes"])

        if len(outcome_names) != 2:
            continue

        # Require each participating bookmaker record to cover the same
        # two outcome labels. No guessed outcome mapping.
        if any(
            {o["name_key"] for o in row["outcomes"]} != outcome_names
            for row in market_rows
        ):
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
                        })
            if not quotes:
                break
            best[outcome_name] = max(quotes, key=lambda q: q["odds"])

        if len(best) != 2:
            continue

        probability_sum = sum(1.0 / quote["odds"] for quote in best.values())
        if probability_sum >= 1:
            continue

        margin = (1.0 - probability_sum) * 100.0
        legs = []
        for outcome_name, quote in best.items():
            legs.append({
                **quote,
                "stake_fraction": (1.0 / quote["odds"]) / probability_sum,
            })

        opportunities.append({
            "sport": market_rows[0]["sport"],
            "event": market_rows[0]["event"],
            "market": market_rows[0]["market"],
            "implied_probability_sum": probability_sum * 100,
            "theoretical_margin_percent": margin,
            "legs": legs,
            "warning": (
                "Theoretical only. Confirm identical settlement rules, "
                "market lines, odds freshness, limits, and fees."
            ),
        })

    opportunities.sort(
        key=lambda item: item["theoretical_margin_percent"], reverse=True
    )
    return opportunities


PRIORITY_MARKETS = (
    "over", "under", "total goals", "total points",
    "double chance", "corners", "corner",
)


def filter_probabilities(rows, threshold=MIN_PROBABILITY):
    """
    Only feed-provided estimated_probability values are eligible.
    Odds-implied probability is not presented as a model prediction.
    """
    eligible = []
    for row in rows:
        priority = any(
            term in normalize_text(row["market"])
            for term in PRIORITY_MARKETS
        )

        for outcome in row["outcomes"]:
            probability = outcome.get("estimated_probability")
            if probability is None or probability < threshold:
                continue

            eligible.append({
                "sport": row["sport"],
                "event": row["event"],
                "market": row["market"],
                "selection": outcome["name"],
                "bookmaker": row["bookmaker"],
                "odds": outcome["odds"],
                "estimated_probability": probability,
                "threshold": threshold,
                "priority_market": priority,
                "evidence": (
                    "Model estimate supplied by configured feed; "
                    "not independently validated by SolveEase."
                ),
            })

    eligible.sort(
        key=lambda item: (
            item["priority_market"],
            item["estimated_probability"],
        ),
        reverse=True,
    )
    return eligible


def alternative_markets(rows, threshold=MIN_PROBABILITY):
    """
    Suggest alternative markets for an event, prioritizing Over/Under,
    Double Chance and Corners. Suggestions require a feed-provided
    probability estimate at or above threshold.
    """
    suggestions = {}
    for row in rows:
        priority = any(
            term in normalize_text(row["market"])
            for term in PRIORITY_MARKETS
        )
        if not priority:
            continue

        for outcome in row["outcomes"]:
            probability = outcome.get("estimated_probability")
            if probability is None or probability < threshold:
                continue

            key = (row["sport"].lower(), row["event_key"])
            suggestions.setdefault(key, []).append({
                "sport": row["sport"],
                "event": row["event"],
                "market": row["market"],
                "selection": outcome["name"],
                "bookmaker": row["bookmaker"],
                "odds": outcome["odds"],
                "estimated_probability": probability,
                "note": (
                    "Alternative candidate only; compare exact line, "
                    "rules, and source freshness before use."
                ),
            })

    result = []
    for _, items in suggestions.items():
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
        "usable_two_outcome_markets": len(rows),
        "arbitrage_count": len(arbitrage),
        "arbitrage": arbitrage,
        "probability_filter_threshold": MIN_PROBABILITY,
        "qualified_selections_count": len(probabilities),
        "qualified_selections": probabilities,
        "alternative_markets_count": len(alternatives),
        "alternative_markets": alternatives,
        "notes": [
            "No opportunity is displayed unless live feed odds qualify.",
            "A feed-provided probability estimate is not a guarantee.",
            "Two outcomes must be exhaustive and use identical settlement rules.",
            "Unavailable feeds do not count as a negative arbitrage result.",
        ],
    }

    global LAST_SCAN
    with STATE_LOCK:
        LAST_SCAN = {
            "at": result["scanned_at"],
            "feeds_online": sum(1 for h in health if h["status"] == "online"),
            "markets": len(rows),
            "opportunities": len(arbitrage),
        }

    logging.info(
        "Scan completed: feeds=%s markets=%s opportunities=%s",
        len(health), len(rows), len(arbitrage),
    )
    return result


# ------------------------------------------------------------
# Dashboard UI
# ------------------------------------------------------------

HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="theme-color" content="#101827">
<meta name="description" content="SolveEase bookmaker and arbitrage dashboard">
<title>SolveEase | Booking & Arbitrage Dashboard</title>
<style>
:root {
  color-scheme: dark;
  --bg:#0b1220;
  --panel:#121d30;
  --panel2:#18263c;
  --text:#eef4ff;
  --muted:#9eb0ca;
  --line:#2b3b54;
  --accent:#69d7ac;
  --danger:#ff8585;
  --warning:#ffd479;
}
* { box-sizing:border-box; }
body {
  margin:0;
  background:var(--bg);
  color:var(--text);
  font:16px/1.5 system-ui,-apple-system,sans-serif;
}
main { width:min(1000px,100%); margin:auto; padding:20px 14px 44px; }
header { margin:8px 0 20px; }
.brand { color:var(--accent); font-weight:850; letter-spacing:.5px; }
h1 { font-size:clamp(27px,5vw,39px); line-height:1.15; margin:8px 0; }
h2 { font-size:21px; margin-top:0; }
h3 { font-size:16px; margin:0 0 6px; }
p { color:var(--muted); }
.panel { background:var(--panel); border:1px solid var(--line);
  border-radius:16px; padding:17px; margin:14px 0; }
label { display:block; font-weight:650; margin:8px 0; }
input,select {
  width:100%; min-height:48px; padding:12px; border-radius:10px;
  border:1px solid var(--line); background:#09101d; color:var(--text);
  font-size:16px;
}
button {
  min-height:48px; padding:12px 16px; border:0; border-radius:10px;
  background:var(--accent); color:#071b15; font-weight:800;
  font-size:15px; cursor:pointer;
}
button:disabled { opacity:.55; cursor:wait; }
.actions { display:grid; grid-template-columns:repeat(auto-fit,minmax(160px,1fr)); gap:9px; }
.secondary { background:var(--panel2); color:var(--text); border:1px solid var(--line); }
.small { font-size:13px; color:var(--muted); }
.error { color:var(--danger); }
.success { color:var(--accent); }
.warning { color:var(--warning); }
.hidden { display:none; }
.grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(145px,1fr)); gap:10px; }
.stat { padding:12px; background:var(--panel2); border-radius:10px; min-width:0; }
.stat span { display:block; font-size:12px; color:var(--muted); }
.stat strong { display:block; font-size:20px; overflow-wrap:anywhere; }
.item { background:var(--panel2); border:1px solid var(--line); border-radius:11px; padding:13px; margin:9px 0; }
.item p { margin:5px 0; overflow-wrap:anywhere; }
.pill { display:inline-block; border:1px solid var(--line); border-radius:20px; padding:3px 9px; margin:3px 4px 3px 0; font-size:12px; }
pre { white-space:pre-wrap; overflow-wrap:anywhere; }
footer { font-size:12px; color:var(--muted); margin-top:24px; }
</style>
</head>
<body>
<main>
<header>
  <div class="brand">SOLVEEASE</div>
  <h1>Booking & Arbitrage Dashboard</h1>
  <p>Read-only ticket lookup, odds comparison, and statistical filters.</p>
</header>

<section class="panel">
  <h2>1. Booking-code lookup</h2>
  <label for="bookmaker">Bookmaker</label>
  <select id="bookmaker">
    <option value="auto">Try automatic identification</option>
    <option value="sportybet">SportyBet</option>
    <option value="bet9ja">Bet9ja</option>
    <option value="betking">BetKing</option>
  </select>
  <label for="code">Booking code</label>
  <input id="code" maxlength="120" autocomplete="off"
    autocapitalize="none" spellcheck="false" placeholder="Enter code">
  <div class="actions">
    <button id="lookupBtn" onclick="lookup()">Identify & look up</button>
  </div>
  <p class="small">Only SportyBet has a lookup adapter in this version.
  Other bookmakers are not falsely identified from code format alone.</p>
  <div id="lookupMessage" role="status"></div>
  <div id="ticketResult"></div>
</section>

<section class="panel">
  <h2>2. Arbitrage scanner</h2>
  <p>Tap Scan to fetch the configured feeds and compare matching two-outcome markets.</p>
  <div class="actions">
    <button id="scanBtn" onclick="scan()">Scan bookmakers now</button>
    <button class="secondary" onclick="loadHealth()">Refresh source status</button>
  </div>
  <div id="scanMessage" role="status"></div>
  <div id="sourceHealth"></div>
  <div id="scanSummary" class="grid"></div>
  <h3>Arbitrage opportunities</h3>
  <div id="arbitrage"></div>
  <h3>60% probability filter</h3>
  <div id="probabilities"></div>
  <h3>Preferred alternative markets</h3>
  <p class="small">Priorities: Over/Under, Double Chance, Corners.</p>
  <div id="alternatives"></div>
</section>

<section class="panel">
  <h2>Service status</h2>
  <div id="health">Checking…</div>
  <p class="small">The dashboard does not place bets. Odds can change rapidly.
  Verify all selections, settlement rules, limits, and prices with the bookmaker.</p>
</section>
<footer>SolveEase · Read-only · No automatic bet placement · No login credentials requested</footer>
</main>
<script>
const $ = id => document.getElementById(id);

function el(tag, text, parent, cls) {
  const node = document.createElement(tag);
  node.textContent = String(text ?? "");
  if (cls) node.className = cls;
  parent.appendChild(node);
  return node;
}
function money(value, digits=2) {
  if (value === null || value === undefined || !Number.isFinite(Number(value))) return "Unavailable";
  return Number(value).toLocaleString("en-US", {
    minimumFractionDigits: digits, maximumFractionDigits: digits
  });
}
function clear(id) { $(id).replaceChildren(); }

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

async function lookup() {
  const code = $("code").value.trim();
  const bookmaker = $("bookmaker").value;
  if (!code) { $("lookupMessage").textContent = "Enter a booking code."; return; }
  $("lookupBtn").disabled = true;
  $("lookupMessage").textContent = "Checking code…";
  clear("ticketResult");
  try {
    const data = await api("/api/lookup", "POST", {code, bookmaker});
    $("lookupMessage").textContent = "Lookup completed.";
    const box = $("ticketResult");
    el("p", "Bookmaker: " + data.bookmaker, box);
    el("p", data.bookmaker_identification, box, "small");
    const stats = el("div", "", box, "grid");
    [
      ["Selections", data.displayed_selections_count],
      ["Source count", data.source_selection_count],
      ["Source odds", money(data.source_combined_odds)],
      ["Calculated odds", money(data.calculated_combined_odds)],
      ["Odds agree", data.odds_agree === null ? "Cannot verify" : (data.odds_agree ? "Yes" : "No")],
      ["Status", data.status],
      ["Stake", data.stake == null ? "Not provided" : String(data.stake) + " (units unverified)"],
      ["Potential return", data.potential_return == null ? "Not provided" : String(data.potential_return) + " (units unverified)"]
    ].forEach(([label,value]) => {
      const s = el("div", "", stats, "stat");
      el("span", label, s);
      el("strong", value ?? "Not provided", s);
    });
    if (data.source_selection_count !== data.displayed_selections_count) {
      el("p", "Warning: source selection count differs from displayed outcome count.", box, "warning");
    }
    el("h3", "Selections", box);
    (data.legs || []).forEach(leg => {
      const card = el("div", "", box, "item");
      el("strong", `${leg.number}. ${leg.event}`, card);
      el("p", "Market: " + leg.market, card);
      el("p", "Selection: " + leg.selection, card);
      el("p", "Odds: " + money(leg.odds), card);
    });
    el("p", data.note, box, "small");
  } catch (err) {
    $("lookupMessage").textContent = err.message;
  } finally {
    $("lookupBtn").disabled = false;
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

async function scan() {
  $("scanBtn").disabled = true;
  $("scanMessage").textContent = "Fetching configured odds feeds…";
  ["sourceHealth","scanSummary","arbitrage","probabilities","alternatives"].forEach(clear);
  try {
    const data = await api("/api/scan", "POST", {});
    $("scanMessage").textContent = "Scan completed at " + data.scanned_at;
    renderHealth(data.bookmakers);
    const summary = $("scanSummary");
    [
      ["Usable markets", data.usable_two_outcome_markets],
      ["Arbitrage opportunities", data.arbitrage_count],
      ["Selections >= 60%", data.qualified_selections_count],
      ["Alternative candidates", data.alternative_markets_count]
    ].forEach(([label,value]) => {
      const s = el("div", "", summary, "stat");
      el("span", label, s);
      el("strong", value, s);
    });

    renderList("arbitrage", data.arbitrage, (item, parent) => {
      const card = el("div", "", parent, "item");
      el("h3", item.event + " · " + item.sport, card);
      el("p", "Market: " + item.market, card);
      el("p", "Theoretical margin: " + money(item.theoretical_margin_percent) + "%", card, "success");
      el("p", "Implied probability sum: " + money(item.implied_probability_sum) + "%", card);
      item.legs.forEach(leg => el("p",
        `${leg.selection}: ${leg.bookmaker} @ ${money(leg.odds)} · suggested stake share ${money(leg.stake_fraction*100)}%`,
        card));
      el("p", item.warning, card, "small");
    }, "No verified arbitrage opportunity found in the currently available feeds. Unavailable feeds are not proof that no arbitrage exists.");

    renderList("probabilities", data.qualified_selections, (item, parent) => {
      const card = el("div", "", parent, "item");
      el("strong", item.event, card);
      el("p", item.market + " · " + item.selection, card);
      el("p", item.bookmaker + " @ " + money(item.odds), card);
      el("p", "Feed-estimated probability: " + money(item.estimated_probability*100) + "%", card,
        item.estimated_probability >= 0.60 ? "success" : "");
      el("p", item.evidence, card, "small");
    }, "No feed-provided probability estimates reached the configured 60% threshold. Odds alone are not treated as a model prediction.");

    renderList("alternatives", data.alternative_markets, (item, parent) => {
      const card = el("div", "", parent, "item");
      el("strong", item.event, card);
      el("p", item.market + " · " + item.selection, card);
      el("p", item.bookmaker + " @ " + money(item.odds), card);
      el("p", "Feed-estimated probability: " + money(item.estimated_probability*100) + "%", card);
      el("p", item.note, card, "small");
    }, "No qualifying Over/Under, Double Chance, or Corners alternatives are available from the configured feeds.");
  } catch (err) {
    $("scanMessage").textContent = err.message;
  } finally {
    $("scanBtn").disabled = false;
  }
}

function renderHealth(items) {
  renderList("sourceHealth", items, (item, parent) => {
    const card = el("div", "", parent, "item");
    el("strong", item.bookmaker + " — " + item.status, card);
    el("p", item.detail + " · usable markets: " + item.events_loaded, card, "small");
  }, "No source health data.");
}

async function loadHealth() {
  try {
    const data = await api("/health");
    $("health").textContent =
      "Online · " + data.configured_feeds + " configured odds feeds · " +
      "SportyBet ticket lookup available";
    renderHealth(data.feed_status);
  } catch (_) {
    $("health").textContent = "Could not check service status.";
  }
}
loadHealth();
</script>
</body>
</html>"""


# ------------------------------------------------------------
# HTTP server
# ------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "SolveEase/2.0"

    def log_message(self, fmt, *args):
        # Never log POST bodies, booking codes, or query strings.
        logging.info("%s - %s", self.address_string(), fmt.split('"')[0].strip())

    def send_bytes(self, status, body, content_type):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def send_json(self, status, data):
        body = json.dumps(
            data, ensure_ascii=False, allow_nan=False
        ).encode("utf-8")
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
        path = urlsplit(self.path).path
        if path == "/":
            self.send_bytes(200, HTML.encode(), "text/html; charset=utf-8")
        else:
            self.send_json(200, {"status": "online"})

    def do_GET(self):
        path = urlsplit(self.path).path.rstrip("/") or "/"
        if path == "/":
            self.send_bytes(200, HTML.encode(), "text/html; charset=utf-8")
            return

        if path == "/favicon.ico":
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        if path == "/health":
            configured = sum(
                1 for config in BOOKMAKERS.values() if config["feed_url"]
            )
            # Health checks do not repeatedly fetch external feeds.
            self.send_json(200, {
                "status": "online",
                "service": "SolveEase Multi-Bookmaker Dashboard",
                "configured_feeds": configured,
                "feed_status": [
                    {
                        "bookmaker": config["name"],
                        "status": "configured" if config["feed_url"] else "not configured",
                        "detail": (
                            "Feed URL configured; tested during scan"
                            if config["feed_url"]
                            else "No permitted JSON odds feed configured"
                        ),
                        "events_loaded": 0,
                    }
                    for config in BOOKMAKERS.values()
                ],
                "sportybet_ticket_lookup": "unofficial endpoint",
                "last_scan": LAST_SCAN,
                "last_lookup": LAST_LOOKUP,
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
                self.send_json(500, {
                    "ok": False,
                    "error": "Scan failed. Check configured feed schemas and service logs.",
                })
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

        # Only SportyBet is implemented as a ticket lookup adapter.
        # Code shape alone cannot confirm another bookmaker.
        if bookmaker in ("auto", "sportybet"):
            try:
                result = lookup_sportybet(code)
            except Exception:
                logging.exception("Ticket lookup failed")
                self.send_json(500, {
                    "ok": False,
                    "error": "Unexpected ticket lookup error",
                })
                return
            if result.get("ok"):
                self.send_json(200, result)
            else:
                self.send_json(502, {
                    "ok": False,
                    "error": result.get("error", "SportyBet lookup failed"),
                    "bookmaker_identification": (
                        "Not confirmed. Code format alone is insufficient."
                    ),
                })
            return

        if bookmaker in ("bet9ja", "betking"):
            self.send_json(501, {
                "ok": False,
                "error": (
                    "A verified lookup adapter is not configured for this "
                    "bookmaker. The system will not guess its booking-code format."
                ),
                "bookmaker_identification": "Not confirmed",
            })
            return

        self.send_json(400, {"ok": False, "error": "Unsupported bookmaker"})


def main():
    if not math.isfinite(TIMEOUT) or TIMEOUT <= 0:
        raise ValueError("REQUEST_TIMEOUT_SECONDS must be positive")
    if not math.isfinite(MIN_PROBABILITY) or not 0 <= MIN_PROBABILITY <= 1:
        raise ValueError("MIN_PROBABILITY must be between 0 and 1")
    if not math.isfinite(BANKROLL) or BANKROLL < 0:
        raise ValueError("BANKROLL must be non-negative")

    server = ThreadingHTTPServer((HOST, PORT), Handler)
    server.daemon_threads = True
    logging.info("SolveEase listening on port %s", PORT)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
