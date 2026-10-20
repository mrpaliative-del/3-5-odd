#!/usr/bin/env python3
"""
SolveEase Booking & Arbitrage Dashboard
Python standard library only.
Version: 3.16.0

Render start command:
    python main.py

Features:
- Responsive mobile-first app with dedicated routes for Home, lookup, converter, scanner and feed status
- Read-only SportyBet booking-code lookup
- Automatic source identification only after successful supported lookup
- Source-vs-calculated odds diagnostics
- Ticket reconstruction for manual recreation at SportyBet, Bet9ja, or BetKing
- Copyable ticket summary; never fabricates destination booking codes
- Configurable JSON odds feeds for SportyBet, Bet9ja, 1xBet, Betway, and optional global feeds
- Alias-aware fixture alignment and exact supported-market/selection matching for replacements
- Two-outcome arbitrage detection across matching event/market/outcome labels
- Feed-supplied probability filter (default 70%; source estimates are not independently calibrated)
- Broader full-time scoreline market evaluation (1X2, double chance, totals, BTTS, team totals, clean sheets, win-to-nil, parity, correct score)
- Source health and last-scan reporting
- Privacy-conscious logging (no booking codes or POST bodies logged)

Environment variables:
    PORT=10000
    REQUEST_TIMEOUT_SECONDS=12
    MIN_PROBABILITY=0.70
    BANKROLL=100000
    SPORTYBET_ODDS_FEED_URL=https://your-authorized-feed.example/feed.json
    SPORTYBET_MARKET_IDS=1,18,10,11,14,16,26,29,36,45,47,60,60100  # optional verified market-ID override
    BET9JA_ODDS_FEED_URL=...
    ONEXBET_ODDS_FEED_URL=...
    BETWAY_ODDS_FEED_URL=...
    GLOBAL_ODDS_FEED_URLS=https://authorized-feed-1.example/markets.json;https://authorized-feed-2.example/markets.json

Feed schema:
{
  "events": [{
    "sport": "football",
    "event": "Home Team vs Away Team",
    "market": "Total Goals 2.5",
    "updated_at": "2026-10-18T12:00:00Z",
    "outcomes": [
      {"name": "Over 2.5", "odds": 2.10, "estimated_probability": 0.62},
      {"name": "Under 2.5", "odds": 1.90, "estimated_probability": 0.38}
    ]
  }]
}

IMPORTANT:
- Only use feeds you control or are authorized to access.
- The SportyBet share endpoint is unofficial and may change.
- No bets are placed, and no bookmaker login credentials are requested.
- This app does not generate valid destination booking codes.
- Feed-provided probability estimates are not independently validated.
- An empty scan caused by unavailable feeds is not proof that no arbitrage exists.
"""

import json
import csv
import io
import logging
import math
import os
import re
import threading
import time
import unicodedata
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import Request, urlopen

import advanced_features as advanced

HOST = "0.0.0.0"
PORT = int(os.environ.get("PORT", "10000"))
TIMEOUT = float(os.environ.get("REQUEST_TIMEOUT_SECONDS", "12"))
MIN_PROBABILITY = float(os.environ.get("MIN_PROBABILITY", "0.70"))
BANKROLL = float(os.environ.get("BANKROLL", "100000"))
MAX_FEED_BYTES = 12_000_000
MAX_MARKET_OUTCOMES = 100
REPLACEMENT_MAX_MODEL_DISAGREEMENT = 0.08
# Use SportyBet public fixtures/markets by default unless explicitly disabled.
# The endpoint is unofficial and can change; diagnostics report real fetch failures.
SPORTYBET_AUTO_DISCOVERY = os.environ.get("SPORTYBET_AUTO_DISCOVERY", "1").strip().lower() not in ("0", "false", "no", "off")
SPORTYBET_DISCOVERY_HOME = "https://www.sportybet.com/ng/"
SPORTYBET_MARKET_MODE = os.environ.get("SPORTYBET_MARKET_MODE", "both").strip().lower()
if SPORTYBET_MARKET_MODE not in {"both", "scheduled", "live"}:
    SPORTYBET_MARKET_MODE = "both"
SPORTYBET_PAGE_SIZE = max(1, min(100, int(os.environ.get("SPORTYBET_DISCOVERY_PAGE_SIZE", "100"))))
MAX_LOOKUP_BYTES = 2_000_000
STALE_AFTER_SECONDS = max(30, int(os.environ.get("FEED_STALE_AFTER_SECONDS", "300")))
FOOTBALL_STATS_ENABLED = os.environ.get("FOOTBALL_STATS_ENABLED", "1").strip().lower() not in ("0", "false", "no", "off")
FOOTBALL_STATS_TIMEOUT = max(2.0, min(8.0, float(os.environ.get("FOOTBALL_STATS_TIMEOUT_SECONDS", "5"))))
FOOTBALL_STATS_CACHE_TTL = max(300, int(os.environ.get("FOOTBALL_STATS_CACHE_TTL_SECONDS", "21600")))
FOOTBALL_STATS_MAX_AGE_DAYS = max(30, int(os.environ.get("FOOTBALL_STATS_MAX_AGE_DAYS", "540")))
FOOTBALL_STATS_LOCK = threading.Lock()
FOOTBALL_CSV_CACHE = {}
FOOTBALL_DIVISION_MATCH_CACHE = {}
FOOTBALL_STATS_CACHE = {}
UNDERSTAT_CACHE = {}
UNDERSTAT_LEAGUES = {"E0":"EPL", "SP1":"La_Liga", "D1":"Bundesliga", "I1":"Serie_A", "F1":"Ligue_1"}
# Wider European coverage. Availability still depends on the public archive publishing a
# usable CSV for the requested season; configured does not mean live data was retrieved.
FOOTBALL_DIVISIONS = {
    "E0":"England Premier League", "E1":"England Championship", "E2":"England League One",
    "E3":"England League Two", "E4":"England National League",
    "SC0":"Scotland Premiership", "SC1":"Scotland Championship", "SC2":"Scotland League One", "SC3":"Scotland League Two",
    "D1":"Germany Bundesliga", "D2":"Germany 2. Bundesliga",
    "I1":"Italy Serie A", "I2":"Italy Serie B",
    "SP1":"Spain La Liga", "SP2":"Spain Segunda Division",
    "F1":"France Ligue 1", "F2":"France Ligue 2",
    "N1":"Netherlands Eredivisie", "B1":"Belgium First Division A",
    "P1":"Portugal Primeira Liga", "T1":"Turkey Super Lig", "G1":"Greece Super League",
    # Additional divisions commonly needed for European cross-league fixtures.
    # A configured code is only a source attempt; it is not proof that a CSV exists.
    "AUT":"Austria Bundesliga", "DNK":"Denmark Superliga", "FIN":"Finland Veikkausliiga",
    "IRL":"Republic of Ireland Premier Division", "NOR":"Norway Eliteserien",
    "POL":"Poland Ekstraklasa", "ROU":"Romania Liga I", "RUS":"Russia Premier League",
    "SWE":"Sweden Allsvenskan", "SWZ":"Switzerland Super League"
}
# These Football-Data.co.uk extras use /new/{CODE}.csv and combine seasons in one file.
FOOTBALL_ALL_SEASON_CODES = {"ARG", "AUT", "BRA", "CHN", "DNK", "FIN", "IRL", "JPN", "MEX", "NOR", "POL", "ROU", "RUS", "SWE", "SWZ", "USA"}

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
        "feed_url": (os.environ.get("ONEXBET_ODDS_FEED_URL") or os.environ.get("X1BET_ODDS_FEED_URL", "")).strip(),
    },
    "betway": {
        "name": "Betway",
        "feed_url": os.environ.get("BETWAY_ODDS_FEED_URL", "").strip(),
    },
}
# Optional authorized global-market JSON feeds. Provide one or more URLs separated by
# semicolons (commas are also accepted). Each endpoint must expose an {"events": [...]} list
# using the documented generic event/market/outcomes schema. No provider is guessed or
# contacted unless the user explicitly configures its URL.
GLOBAL_ODDS_FEED_URLS = [
    item.strip() for item in re.split(r"[;,]", os.environ.get("GLOBAL_ODDS_FEED_URLS", "")) if item.strip()
][:8]
for index, feed_url in enumerate(GLOBAL_ODDS_FEED_URLS, 1):
    BOOKMAKERS[f"global_{index}"] = {
        "name": f"Global feed {index}",
        "feed_url": feed_url,
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
LAST_DAILY_REPORT = None


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



def poisson_distribution(rate, max_goals=12):
    """Return a normalized finite Poisson distribution for a validated goal rate."""
    rate = finite_number(rate, minimum=0.05, maximum=8.0)
    if rate is None:
        raise ValueError("Goal rates must be finite numbers from 0.05 to 8.0")
    probabilities = []
    for goals in range(max_goals + 1):
        probabilities.append(math.exp(-rate) * (rate ** goals) / math.factorial(goals))
    total = sum(probabilities)
    if total <= 0 or not math.isfinite(total):
        raise ValueError("Could not calculate a stable goal distribution")
    # Normalize the truncated distribution so reported market probabilities sum to 1.
    return [value / total for value in probabilities]


def _special_settlement_reason(market, selection=""):
    """Return a reason when a market needs settlement logic this model does not implement."""
    m = normalize_text(market)
    s = normalize_text(selection)
    combined = f"{m} {s}"
    if re.search(r"\b(?:early goals?|first goal|goal in first|within first|by (?:the )?\d+ minutes?|\d+ min(?:ute)?s?)\b", combined):
        return "Early-goal/time-window market requires minute-by-minute event data and a dedicated settlement model."
    if re.search(r"\b(?:1up|2up|two up|one up)\b", combined):
        return "1UP/2UP market requires the bookmaker's exact lead/settlement rules; standard 1X2 probabilities cannot be reused."
    if any(token in m for token in ("home team or", "away team or", "team to score or", "result or total", "and over", "and under")):
        return "Compound market combines events/outcomes; a single-market probability would be misleading."
    if any(token in m for token in ("corner", "corners", "yellow card", "red card", "cards", "offsides", "offside", "shots", "throw ins", "throw-ins", "penalty", "half time", "half-time", "first half", "1st half", "second half", "2nd half", "period 1", "period 2", "player prop")):
        return "This market needs a dedicated event/player/period model; full-match goal rates cannot estimate it safely."
    return None


def _selection_probability(market, selection, home_rate, away_rate):
    """Estimate explicitly modelled full-time football markets from a score matrix.

    Supported families: 1X2/double chance, full-time totals, BTTS, team totals,
    team-to-score, clean-sheet, win-to-nil, total-goals odd/even and correct score.
    Half-time, corners, cards, time-window, 1UP/2UP and compound markets are rejected.
    These are uncalibrated score-model estimates, not true probabilities.
    """
    market_text = normalize_text(market)
    selection_text = normalize_text(selection)
    special_reason = _special_settlement_reason(market, selection)
    if special_reason:
        return None, special_reason
    home = poisson_distribution(home_rate)
    away = poisson_distribution(away_rate)
    scores = [(h, a, hp * ap) for h, hp in enumerate(home) for a, ap in enumerate(away)]
    compact = re.sub(r"[^a-z0-9]+", "", selection_text)

    if any(term in market_text for term in ("both teams to score", "btts", "both teams score", "gg/ng", "gg ng", "ggng")):
        yes = sum(prob for h, a, prob in scores if h > 0 and a > 0)
        if compact in ("yes", "bttsyes", "bothteamstoscoreyes", "gg"):
            return yes, "Independent-Poisson estimate for BTTS yes"
        if compact in ("no", "bttsno", "bothteamstoscoreno", "ng"):
            return 1.0 - yes, "Independent-Poisson estimate for BTTS no"
        return None, "Unsupported BTTS selection label"

    # Correct/exact score markets have many outcomes; the feed normalizer retains them,
    # but the model assigns only the exact score requested, never an aggregate proxy.
    if any(term in market_text for term in ("correct score", "exact score", "full time score")):
        score = re.fullmatch(r"\s*(\d{1,2})\s*[-:]\s*(\d{1,2})\s*", selection_text)
        if score:
            h, a = int(score.group(1)), int(score.group(2))
            return next((prob for sh, sa, prob in scores if sh == h and sa == a), 0.0), "Independent-Poisson exact-score estimate"
        return None, "Correct-score selection must look like 2-1"

    if any(term in market_text for term in ("clean sheet", "keep a clean sheet", "team to concede")):
        home_market = any(term in market_text for term in ("home", "team 1", "1st team"))
        away_market = any(term in market_text for term in ("away", "team 2", "2nd team"))
        if not home_market and not away_market:
            return None, "Clean-sheet market does not identify home or away team"
        concedes = sum(prob for h, a, prob in scores if (a > 0 if home_market else h > 0))
        yes = 1.0 - concedes
        if compact in ("yes", "true", "will", "keepcleansheet"):
            return yes, "Independent-Poisson clean-sheet estimate"
        if compact in ("no", "false", "wont", "not"):
            return 1.0 - yes, "Independent-Poisson clean-sheet estimate"
        return None, "Unsupported clean-sheet selection label"

    if any(term in market_text for term in ("team to score", "home team score", "away team score", "home to score", "away to score")):
        home_market = any(term in market_text for term in ("home", "team 1", "1st team"))
        away_market = any(term in market_text for term in ("away", "team 2", "2nd team"))
        if not home_market and not away_market:
            return None, "Team-to-score market does not identify home or away team"
        yes = sum(prob for h, a, prob in scores if (h > 0 if home_market else a > 0))
        if compact in ("yes", "willscore", "to score yes"):
            return yes, "Independent-Poisson team-to-score estimate"
        if compact in ("no", "wontscore", "not to score"):
            return 1.0 - yes, "Independent-Poisson team-to-score estimate"
        return None, "Unsupported team-to-score selection label"

    if any(term in market_text for term in ("win to nil", "win-to-nil")):
        home_market = any(term in market_text for term in ("home", "team 1", "1st team"))
        away_market = any(term in market_text for term in ("away", "team 2", "2nd team"))
        if not home_market and not away_market:
            return None, "Win-to-nil market does not identify home or away team"
        yes = sum(prob for h, a, prob in scores if (h > a and a == 0 if home_market else a > h and h == 0))
        if compact in ("yes", "will", "win to nil yes"):
            return yes, "Independent-Poisson win-to-nil estimate"
        if compact in ("no", "wont", "win to nil no"):
            return 1.0 - yes, "Independent-Poisson win-to-nil estimate"
        return None, "Unsupported win-to-nil selection label"

    if any(term in market_text for term in ("odd/even", "odd even", "total goals odd", "total goals even", "goals odd or even")):
        odd = sum(prob for h, a, prob in scores if (h + a) % 2 == 1)
        if compact in ("odd", "totalodd", "goalsodd"):
            return odd, "Independent-Poisson total-goals parity estimate"
        if compact in ("even", "totaleven", "goalseven"):
            return 1.0 - odd, "Independent-Poisson total-goals parity estimate"
        return None, "Unsupported odd/even selection label"

    # Team totals are separate from match totals and use the named team's goal count.
    is_team_total = any(term in market_text for term in ("home team total", "away team total", "home goals", "away goals", "team goals", "team total goals"))
    if is_team_total:
        home_market = any(term in market_text for term in ("home", "team 1", "1st team"))
        away_market = any(term in market_text for term in ("away", "team 2", "2nd team"))
        if not home_market and not away_market:
            return None, "Team-total market does not identify home or away team"
        line_match = re.search(r"(\d+(?:\.\d+)?)", market_text + " " + selection_text)
        if not line_match or not (selection_text.startswith("over") or selection_text.startswith("under")):
            return None, "Team total requires Over/Under and an explicit line"
        line = float(line_match.group(1))
        over = sum(prob for h, a, prob in scores if (h if home_market else a) > line)
        under = sum(prob for h, a, prob in scores if (h if home_market else a) < line)
        return (over if selection_text.startswith("over") else under), f"Independent-Poisson {'home' if home_market else 'away'} team-goals estimate at {line:g}"

    if any(term in market_text for term in ("double chance", "1x2", "match result", "match winner", "full time result", "result")):
        home_win = sum(prob for h, a, prob in scores if h > a)
        draw = sum(prob for h, a, prob in scores if h == a)
        away_win = sum(prob for h, a, prob in scores if h < a)
        if compact in ("1x", "homedraw", "homeordraw", "homeordrawwin", "team1ordraw", "1ordraw", "1orx", "homeordrawdoublechance"):
            return home_win + draw, "Independent-Poisson home-or-draw estimate"
        if compact in ("x2", "drawaway", "draworaway", "draworawaywin", "draworteam2", "drawor2"):
            return draw + away_win, "Independent-Poisson draw-or-away estimate"
        if compact in ("12", "homeoraway", "homeorawaywin", "nodraw", "team1orteam2", "1or2"):
            return home_win + away_win, "Independent-Poisson no-draw estimate"
        if compact in ("home", "homewin", "1"):
            return home_win, "Independent-Poisson home-win estimate"
        if compact in ("draw", "x", "tie"):
            return draw, "Independent-Poisson draw estimate"
        if compact in ("away", "awaywin", "2"):
            return away_win, "Independent-Poisson away-win estimate"
        return None, "Unsupported result selection label"

    combined_market_text = market_text + " " + selection_text
    match = re.search(r"(?:over|under|total(?: goals)?|goals?)\s*(\d+(?:\.\d+)?)|(?:total=)(\d+(?:\.\d+)?)", combined_market_text)
    if match and any(term in market_text for term in ("over", "under", "total", "goal")):
        line = float(match.group(1) or match.group(2))
        over = sum(prob for h, a, prob in scores if h + a > line)
        under = sum(prob for h, a, prob in scores if h + a < line)
        if selection_text.startswith("over"):
            return over, f"Independent-Poisson total goals over {line:g}"
        if selection_text.startswith("under"):
            return under, f"Independent-Poisson total goals under {line:g}"
        return None, "Total-goals selection must be Over or Under with a line"
    return None, "Unsupported market; this market family has no validated settlement calculation"


def _season_codes(today=None):
    today = today or datetime.now(timezone.utc).date()
    start = today.year if today.month >= 7 else today.year - 1
    return [f"{start % 100:02d}{(start + 1) % 100:02d}", f"{(start - 1) % 100:02d}{start % 100:02d}", f"{(start - 2) % 100:02d}{(start - 1) % 100:02d}"]


def _fetch_football_csv(season, division):
    """Fetch a bounded public CSV with process-local caching and conservative timeouts.

    Football-Data.co.uk describes its free files as for private individual use. Do not
    repurpose this adapter for a commercial data product without checking permissions.
    """
    key = f"{season}:{division}"
    now = time.time()
    with FOOTBALL_STATS_LOCK:
        cached = FOOTBALL_CSV_CACHE.get(key)
        cache_limit = FOOTBALL_STATS_CACHE_TTL if cached and cached[1] is not None else min(300, FOOTBALL_STATS_CACHE_TTL)
        if cached and now - cached[0] < cache_limit:
            return cached[1]
    if division in FOOTBALL_ALL_SEASON_CODES:
        url = f"https://www.football-data.co.uk/new/{division}.csv"
    else:
        url = f"https://www.football-data.co.uk/mmz4281/{season}/{division}.csv"
    request = Request(url, headers={"User-Agent": "SolveEase-Football-Stats/3.16.0 (+match-analysis)", "Accept": "text/csv,*/*;q=0.8"})
    data = None
    try:
        with urlopen(request, timeout=FOOTBALL_STATS_TIMEOUT) as response:
            if response.status == 200:
                raw = response.read(2_500_000)
                if raw and len(raw) <= 2_500_000:
                    # Historical files may be UTF-8 or Windows-1252. Prefer UTF-8,
                    # then fall back to cp1252 so accented club names survive matching.
                    try:
                        data = raw.decode("utf-8-sig")
                    except UnicodeDecodeError:
                        data = raw.decode("cp1252", errors="replace")
    except (HTTPError, URLError, TimeoutError, OSError, ValueError):
        data = None
    with FOOTBALL_STATS_LOCK:
        FOOTBALL_CSV_CACHE[key] = (now, data)
    return data


def _csv_matches(text, division, season):
    if not text:
        return []
    try:
        sample = text[:4096]
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    try:
        rows = csv.DictReader(io.StringIO(text), dialect=dialect)
        output = []
        for row in rows:
            lowered = {str(k or "").strip().lower(): v for k, v in row.items()}
            home = str(lowered.get("hometeam") or "").strip()
            away = str(lowered.get("awayteam") or "").strip()
            hg = finite_number(lowered.get("fthg"), minimum=0, maximum=20)
            ag = finite_number(lowered.get("ftag"), minimum=0, maximum=20)
            if not home or not away or hg is None or ag is None:
                continue
            date_text = str(lowered.get("date") or "").strip()
            parsed_date = None
            for fmt in ("%d/%m/%y", "%d/%m/%Y", "%Y-%m-%d"):
                try:
                    parsed_date = datetime.strptime(date_text, fmt).date()
                    if fmt == "%d/%m/%y" and parsed_date.year > datetime.now().year + 1:
                        parsed_date = parsed_date.replace(year=parsed_date.year - 100)
                    break
                except ValueError:
                    pass
            if parsed_date is None:
                continue
            row_season = season
            if season == "all":
                # All-season files span calendar years; football seasons normally start in July.
                start_year = parsed_date.year if parsed_date.month >= 7 else parsed_date.year - 1
                row_season = f"{start_year % 100:02d}{(start_year + 1) % 100:02d}"
            output.append({"date": parsed_date.isoformat(), "home": home, "away": away,
                           "hg": int(hg), "ag": int(ag), "division": division,
                           "league": FOOTBALL_DIVISIONS.get(division, division), "season": row_season})
        return output
    except (csv.Error, UnicodeError, TypeError):
        return []


def _team_key(name):
    """Normalize common football naming variants without fuzzy guessing.

    Unicode accents are removed by normalize_text (so Nordsjælland and
    Nordsjaelland converge). Common club prefixes/suffixes are stripped to
    match source names such as FC Midtjylland vs Midtjylland. Team histories
    are still selected independently and never merged across clubs/leagues.
    """
    name = normalize_text(name).replace(".", "").replace("-", " ")
    name = re.sub(r"^(?:fc|fk|pfc|afc|ac|as|cd|ud|sv|sk|sc|rb|vfb|vfl|if|bk|cf|ks|nk)\s+", "", name)
    for suffix in (" football club", " fc", " afc", " cf", " sc", " club", " vv", " wfc"):
        if name.endswith(suffix):
            name = name[:-len(suffix)].strip()
    name = re.sub(r"\b(the|women|wfc)\b", " ", name).strip()
    # A few well-established alternate spellings/names in public football data.
    aliases = {
        "nordsjaelland": "nordsjaelland",
        "st truidense": "sint truiden",
        "st truidense vv": "sint truiden",
        "sint truiden": "sint truiden",
        "red star belgrade": "crvena zvezda",
        "crvena zvezda": "crvena zvezda",
        "cska sofia": "cska sofia",
        "pfc cska sofia": "cska sofia",
        "fc midtjylland": "midtjylland",
        "midtjylland": "midtjylland",
        "fc nordsjaelland": "nordsjaelland",
        "fc nordsjalland": "nordsjaelland",
        "nordsjaelland": "nordsjaelland",
        "nordsjalland": "nordsjaelland",
        "stuttgart": "stuttgart",
        "vfb stuttgart": "stuttgart",
        "monaco": "monaco",
        "as monaco": "monaco",
        "braga": "braga",
        "sc braga": "braga",
        "ajax": "ajax",
        "afc ajax": "ajax",
        "atalanta": "atalanta",
        "atalanta bc": "atalanta",
        # Common display-name variants seen in booking slips and historical feeds.
        "psg": "paris saint germain",
        "paris sg": "paris saint germain",
        "paris saint germain": "paris saint germain",
        "man utd": "manchester united",
        "man united": "manchester united",
        "manchester utd": "manchester united",
        "manchester united": "manchester united",
        "spurs": "tottenham",
        "tottenham hotspur": "tottenham",
        "tottenham": "tottenham",
        "wolves": "wolverhampton wanderers",
        "wolverhampton": "wolverhampton wanderers",
        "wolverhampton wanderers": "wolverhampton wanderers",
        "bayern": "bayern munich",
        "bayern munich": "bayern munich",
        "inter": "internazionale",
        "internazionale": "internazionale",
        "inter milan": "internazionale",
        "besiktas istanbul": "besiktas",
        "besiktas": "besiktas",
        "fenerbahce istanbul": "fenerbahce",
        "fenerbahce": "fenerbahce",
        "real betis": "betis",
        "betis": "betis",
        "union saint gilloise": "union saint gilloise",
        "union gilloise": "union saint gilloise",
        "sturm graz": "sturm graz",
        "crvena zvezda belgrade": "crvena zvezda",
        "red star": "crvena zvezda",
        "leverkusen": "bayer leverkusen",
        "bayer leverkusen": "bayer leverkusen",
        "real sociedad": "real sociedad",
        "lillestrom": "lillestrom",
        "lillestrom sk": "lillestrom",
        "lillestroem": "lillestrom",
        "lillestroem sk": "lillestrom",
        "ferencvarosi": "ferencvaros",
        "ferencvaros": "ferencvaros",
        "ferencvarosi budapest": "ferencvaros",
        "gnk dinamo zagreb": "dinamo zagreb",
        "dinamo zagreb": "dinamo zagreb",
        "scu torreense": "torreense",
        "torreense": "torreense",
        "heart of midlothian": "heart of midlothian",
        "hearts": "heart of midlothian",
        # Additional explicit aliases from recurring ticket/feed spellings.
        "manchester city": "manchester city", "man city": "manchester city", "manchester city fc": "manchester city",
        "aek athens": "aek athens", "aek": "aek athens",
        "bodo glimt": "bodo glimt", "bodoe glimt": "bodo glimt", "bodo glimt fk": "bodo glimt",
        "psv eindhoven": "psv", "psv": "psv", "psv eindhoven fc": "psv",
        "slovan bratislava": "slovan bratislava", "shakhtar d": "shakhtar donetsk", "shakhtar donetsk": "shakhtar donetsk",
        "galatasaray istanbul": "galatasaray", "galatasaray": "galatasaray",
        "viking fk": "viking", "viking": "viking",
        "lask linz": "lask", "lask": "lask",
        "brighton": "brighton hove albion", "brighton and hove albion": "brighton hove albion", "brighton hove albion": "brighton hove albion",
        "sunderland": "sunderland", "kks lech poznan": "lech poznan", "lech poznan": "lech poznan",
        "union saint gilloise": "union saint gilloise", "union sg": "union saint gilloise",
        "hapoel beer sheva": "hapoel beer sheva", "hapoel beer sheva fc": "hapoel beer sheva",
        "al qadsiah": "al qadsiah", "al qadisiyah": "al qadsiah", "al qadsiah fc": "al qadsiah",
        "al nassr": "al nassr", "al nassr club": "al nassr", "neom": "neom sc", "neom sc": "neom sc",
        "al ahly": "al ahly cairo", "al ahly sc egy": "al ahly cairo", "al ahly cairo": "al ahly cairo",
        "el qanah": "el qanah", "el qanah fc": "el qanah",
        "crystal palace": "crystal palace", "marseille": "marseille", "olympique marseille": "marseille",
        "lyon": "olympique lyonnais", "olympique lyonnais": "olympique lyonnais",
        "arsenal": "arsenal", "barcelona": "barcelona", "real madrid": "real madrid",
        "bayer leverkusen": "bayer leverkusen", "ofI crete": "ofi crete", "ofi crete": "ofi crete",
        "trabzonspor": "trabzonspor", "heart of midlothian fc": "heart of midlothian",
    }
    return aliases.get(name, name)


def _split_event_teams(event):
    text = str(event or "").strip()
    # Prefer explicit separators, preserving team names containing hyphens.
    match = re.match(r"^(.+?)\s+(?:vs?\.?|v\.?|versus|@)\s+(.+?)$", text, re.I)
    if match:
        return match.group(1).strip(" -–—"), match.group(2).strip(" -–—")
    match = re.match(r"^(.+?)\s+[–—]\s+(.+?)$", text)
    if match:
        return match.group(1).strip(), match.group(2).strip()
    return None, None


def _find_team_name(requested, candidates):
    """Match only normalized exact names or explicit aliases; avoid substring collisions.

    Substring matching can accidentally merge first teams with youth/reserve sides
    (for example, a club and its U21 team), which contaminates historical rates.
    """
    target = _team_key(requested)
    exact = {}
    for candidate in candidates:
        key = _team_key(candidate)
        if key and key not in exact:
            exact[key] = candidate
    return exact.get(target)


def _team_match_history(matches, team):
    key = _team_key(team)
    out = []
    for row in matches:
        if _team_key(row["home"]) == key:
            out.append({"date": row["date"], "venue": "home", "gf": row["hg"], "ga": row["ag"], "opponent": row["away"]})
        elif _team_key(row["away"]) == key:
            out.append({"date": row["date"], "venue": "away", "gf": row["ag"], "ga": row["hg"], "opponent": row["home"]})
    out.sort(key=lambda x: x["date"])
    return out


def _avg(values, default):
    return sum(values) / len(values) if values else default


def _summarize_team(history):
    recent = history[-10:]
    home = [m for m in history if m["venue"] == "home"]
    away = [m for m in history if m["venue"] == "away"]
    wins = sum(1 for m in recent if m["gf"] > m["ga"])
    draws = sum(1 for m in recent if m["gf"] == m["ga"])
    return {
        "matches_available": len(history), "recent_matches_used": len(recent),
        "recent_goals_for_avg": round(_avg([m["gf"] for m in recent], 1.2), 3),
        "recent_goals_against_avg": round(_avg([m["ga"] for m in recent], 1.2), 3),
        "recent_win_rate": round(wins / len(recent), 3) if recent else None,
        "recent_draw_rate": round(draws / len(recent), 3) if recent else None,
        "home_matches": len(home), "home_goals_for_avg": round(_avg([m["gf"] for m in home], _avg([m["gf"] for m in history], 1.35)), 3),
        "home_goals_against_avg": round(_avg([m["ga"] for m in home], _avg([m["ga"] for m in history], 1.2)), 3),
        "away_matches": len(away), "away_goals_for_avg": round(_avg([m["gf"] for m in away], _avg([m["gf"] for m in history], 1.1)), 3),
        "away_goals_against_avg": round(_avg([m["ga"] for m in away], _avg([m["ga"] for m in history], 1.35)), 3),
    }


def _fetch_understat_league(league_slug, season_start):
    """Try the public Understat JSON endpoint for xG in supported leagues only.

    This endpoint is unofficial for this app and may change or reject automated access;
    failure is non-fatal and the model falls back to historical goals.
    """
    key = f"{league_slug}:{season_start}"
    now = time.time()
    with FOOTBALL_STATS_LOCK:
        cached = UNDERSTAT_CACHE.get(key)
        cache_limit = FOOTBALL_STATS_CACHE_TTL if cached and cached[1] is not None else min(1800, FOOTBALL_STATS_CACHE_TTL)
        if cached and now - cached[0] < cache_limit:
            return cached[1]
    url = f"https://understat.com/getLeagueData/{league_slug}/{season_start}"
    request = Request(url, headers={
        "User-Agent": "Mozilla/5.0 (compatible; SolveEase/3.7; match-statistics)",
        "X-Requested-With": "XMLHttpRequest", "Referer": "https://understat.com/",
        "Accept": "application/json, text/javascript, */*; q=0.01",
    })
    parsed = None
    try:
        with urlopen(request, timeout=FOOTBALL_STATS_TIMEOUT) as response:
            if response.status == 200:
                raw = response.read(2_000_000)
                payload = json.loads(raw.decode("utf-8", errors="replace"))
                teams = payload.get("teams") if isinstance(payload, dict) else None
                parsed = []
                if isinstance(teams, dict):
                    iterable = teams.values()
                elif isinstance(teams, list):
                    iterable = teams
                else:
                    iterable = []
                for team in iterable:
                    if not isinstance(team, dict):
                        continue
                    name = str(team.get("title") or team.get("name") or "").strip()
                    history = team.get("history")
                    if not name or not isinstance(history, list):
                        continue
                    for match in history:
                        if not isinstance(match, dict):
                            continue
                        xg = finite_number(match.get("xG"), minimum=0, maximum=15)
                        xga = finite_number(match.get("xGA"), minimum=0, maximum=15)
                        venue = str(match.get("h_a") or "").lower()
                        date_text = str(match.get("date") or "")[:10]
                        if xg is None or xga is None or venue not in ("h", "a"):
                            continue
                        parsed.append({"team": name, "venue": "home" if venue == "h" else "away", "xg": xg, "xga": xga, "date": date_text})
                if not parsed:
                    parsed = None
    except (HTTPError, URLError, TimeoutError, OSError, ValueError, TypeError, json.JSONDecodeError):
        parsed = None
    with FOOTBALL_STATS_LOCK:
        UNDERSTAT_CACHE[key] = (now, parsed)
    return parsed


def _xg_rates(division, home_name, away_name):
    league_slug = UNDERSTAT_LEAGUES.get(division)
    if not league_slug:
        return None
    season_start = datetime.now(timezone.utc).year if datetime.now(timezone.utc).month >= 7 else datetime.now(timezone.utc).year - 1
    rows = _fetch_understat_league(league_slug, season_start)
    if not rows:
        return None
    home_key, away_key = _team_key(home_name), _team_key(away_name)
    home_rows = [r for r in rows if _team_key(r["team"]) == home_key and r["venue"] == "home"]
    away_rows = [r for r in rows if _team_key(r["team"]) == away_key and r["venue"] == "away"]
    if len(home_rows) < 3 or len(away_rows) < 3:
        return None
    # Team-level match xG at the correct venue, with opposing venue xGA as defensive signal.
    home_attack = _avg([r["xg"] for r in home_rows[-10:]], 1.4)
    away_defence = _avg([r["xga"] for r in away_rows[-10:]], 1.4)
    away_attack = _avg([r["xg"] for r in away_rows[-10:]], 1.1)
    home_defence = _avg([r["xga"] for r in home_rows[-10:]], 1.2)
    home_rate = max(0.15, min(4.5, (home_attack + away_defence) / 2))
    away_rate = max(0.15, min(4.5, (away_attack + home_defence) / 2))
    return {"home_goal_rate": round(home_rate, 3), "away_goal_rate": round(away_rate, 3),
            "home_xg_matches": len(home_rows), "away_xg_matches": len(away_rows),
            "source": "Understat team match xG/xGA", "xg_available": True}


def fetch_match_statistics(event):
    """Automatically infer expected-goal rates from recent/home-away results.

    This uses historical goals as a fallback model, not official xG. The public CSV
    source has private-use restrictions; coverage is limited to configured divisions.
    """
    if not FOOTBALL_STATS_ENABLED:
        return {"ok": False, "status": "disabled", "reason": "Automatic football statistics are disabled by configuration."}
    home_requested, away_requested = _split_event_teams(event)
    if not home_requested or not away_requested:
        return {"ok": False, "status": "unmatched", "reason": "Could not reliably split this event into home and away teams.", "event": event}
    cache_key = _team_key(home_requested) + "::" + _team_key(away_requested)
    now = time.time()
    with FOOTBALL_STATS_LOCK:
        cached = FOOTBALL_STATS_CACHE.get(cache_key)
        if cached and now - cached[0] < FOOTBALL_STATS_CACHE_TTL:
            return dict(cached[1])
    seasons = _season_codes()
    rows_by_division = {}
    # Fetch up to three seasons for every configured division. A single current-season
    # file often has too few matches early in the season; combining seasons improves
    # coverage while the freshness guard below still rejects stale datasets.
    def fetch_division_seasons(division):
        # Parsing the same 3 seasons for every leg made large tickets do thousands
        # of redundant CSV parses. Cache the combined, deduplicated division data.
        cache_key = f"{division}:{','.join(seasons)}"
        cache_now = time.time()
        with FOOTBALL_STATS_LOCK:
            cached_division = FOOTBALL_DIVISION_MATCH_CACHE.get(cache_key)
            division_ttl = FOOTBALL_STATS_CACHE_TTL if cached_division and cached_division[1] else min(300, FOOTBALL_STATS_CACHE_TTL)
            if cached_division and cache_now - cached_division[0] < division_ttl:
                return division, cached_division[1]
        combined = []
        if division in FOOTBALL_ALL_SEASON_CODES:
            content = _fetch_football_csv("all", division)
            parsed = _csv_matches(content, division, "all")
            if parsed:
                # Keep only the requested rolling window for comparable freshness and sample size.
                allowed_seasons = set(seasons)
                combined.extend(row for row in parsed if row.get("season") in allowed_seasons)
        else:
            for season in seasons:
                content = _fetch_football_csv(season, division)
                parsed = _csv_matches(content, division, season)
                if parsed:
                    combined.extend(parsed)
        # Deduplicate repeated rows if a source file contains duplicate fixtures.
        unique = {}
        for row in combined:
            key = (row["date"], _team_key(row["home"]), _team_key(row["away"]))
            unique[key] = row
        matches = list(unique.values())
        with FOOTBALL_STATS_LOCK:
            FOOTBALL_DIVISION_MATCH_CACHE[cache_key] = (cache_now, matches)
        return division, matches

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(fetch_division_seasons, division) for division in FOOTBALL_DIVISIONS]
        for future in as_completed(futures):
            try:
                division, parsed = future.result()
                if parsed:
                    rows_by_division[division] = parsed
            except Exception:
                continue

    def select_team_source(requested, rows_map):
        """Find a team's own domestic history independently of its opponent's league."""
        candidates = []
        for division, matches in rows_map.items():
            teams = sorted({row["home"] for row in matches} | {row["away"] for row in matches})
            matched = _find_team_name(requested, teams)
            if not matched:
                continue
            history = _team_match_history(matches, matched)
            # Avoid producing a probability from a couple of isolated results.
            # Require a minimally useful sample and at least two matches at the
            # venue relevant to this fixture.
            venue_count = sum(1 for item in history if item["venue"] == ("home" if requested == home_requested else "away"))
            if len(history) >= 5 and venue_count >= 2:
                exact = 1 if _team_key(matched) == _team_key(requested) else 0
                latest = history[-1]["date"] if history else ""
                candidates.append((exact, len(history), latest, division, matches, matched, history))
        # Prefer exact name, then richest recent history. Never merge same-named clubs
        # from different leagues into one fabricated team history.
        return max(candidates, default=None, key=lambda item: (item[0], item[1], item[2]))

    home_source = select_team_source(home_requested, rows_by_division)
    away_source = select_team_source(away_requested, rows_by_division)
    if not home_source or not away_source:
        def diagnose_team(requested, venue):
            seen = []
            for division, matches in rows_by_division.items():
                teams = sorted({row["home"] for row in matches} | {row["away"] for row in matches})
                matched = _find_team_name(requested, teams)
                if not matched:
                    continue
                history = _team_match_history(matches, matched)
                venue_count = sum(1 for item in history if item["venue"] == venue)
                seen.append({"division": division, "league": FOOTBALL_DIVISIONS.get(division, division),
                             "matched_source_name": matched, "matches_found": len(history),
                             "relevant_venue_matches": venue_count})
            if not seen:
                return {"status": "TEAM_NOT_FOUND_IN_LOADED_DATA", "matches_found": 0,
                        "note": "No exact/explicit-alias match was found in the rows successfully downloaded."}
            best = max(seen, key=lambda item: (item["matches_found"], item["relevant_venue_matches"]))
            if best["matches_found"] < 5:
                status = "INSUFFICIENT_MATCH_SAMPLE"
            elif best["relevant_venue_matches"] < 2:
                status = "INSUFFICIENT_VENUE_SAMPLE"
            else:
                status = "TEAM_MATCHED_BUT_SOURCE_SELECTION_FAILED"
            return {"status": status, **best, "other_matches": seen[1:]}

        missing = []
        team_diagnostics = {}
        if not home_source:
            missing.append(f"home team '{home_requested}'")
            team_diagnostics["home"] = diagnose_team(home_requested, "home")
        if not away_source:
            missing.append(f"away team '{away_requested}'")
            team_diagnostics["away"] = diagnose_team(away_requested, "away")
        loaded_match_count = sum(len(rows) for rows in rows_by_division.values())
        reason = ("No usable history was found in the downloaded configured divisions for " + " and ".join(missing) +
                  ". See team_diagnostics to distinguish a missing name from an insufficient sample. Missing coverage is not evidence that a team is weak; no model probability was invented.")
        result = {"ok": False, "status": "unmatched", "event": event,
                  "reason": reason,
                  "source": "Football-Data.co.uk historical match results CSV",
                  "checked_divisions": list(FOOTBALL_DIVISIONS), "loaded_divisions": list(rows_by_division),
                  "configured_division_count": len(FOOTBALL_DIVISIONS),
                  "loaded_division_count": len(rows_by_division), "loaded_match_count": loaded_match_count,
                  "team_diagnostics": team_diagnostics,
                  "seasons_checked": seasons,
                  "coverage_note": "Configured divisions are attempted across up to three seasons, but a code may have no available CSV. Only successfully downloaded, parseable rows count as loaded coverage. Teams with fewer than five matched results, or fewer than two results at the relevant venue, are intentionally not estimated. Some competitions (including many Saudi and Egyptian domestic fixtures) are not covered by this provider.",
                  "minimum_sample_required": {"matches_per_team": 5, "venue_matches_per_team": 2}}
        with FOOTBALL_STATS_LOCK:
            FOOTBALL_STATS_CACHE[cache_key] = (now, result)
        return result

    _, home_count, _, home_division, home_matches, home_name, home_history = home_source
    _, away_count, _, away_division, away_matches, away_name, away_history = away_source
    hsum, asum = _summarize_team(home_history), _summarize_team(away_history)
    home_league_rows = [row for row in home_matches if row["hg"] >= 0 and row["ag"] >= 0]
    away_league_rows = [row for row in away_matches if row["hg"] >= 0 and row["ag"] >= 0]
    home_league_home = _avg([row["hg"] for row in home_league_rows], 1.45)
    home_league_away = _avg([row["ag"] for row in home_league_rows], 1.15)
    away_league_home = _avg([row["hg"] for row in away_league_rows], 1.45)
    away_league_away = _avg([row["ag"] for row in away_league_rows], 1.15)

    # Normalize each team's venue performance against its own league baseline, then
    # scale it to the home/away scoring environment of the home team's league.
    # This is a conservative cross-league goals model, not a calibrated prediction.
    h_n, a_n = hsum["home_matches"], asum["away_matches"]
    h_for_raw = hsum["home_goals_for_avg"]
    a_against_raw = asum["away_goals_against_avg"]
    a_for_raw = asum["away_goals_for_avg"]
    h_against_raw = hsum["home_goals_against_avg"]
    h_for = (h_for_raw * min(h_n, 8) + home_league_home * max(0, 8-h_n)) / max(8, min(h_n, 8) + max(0, 8-h_n))
    a_against = (a_against_raw * min(a_n, 8) + away_league_home * max(0, 8-a_n)) / max(8, min(a_n, 8) + max(0, 8-a_n))
    a_for = (a_for_raw * min(a_n, 8) + away_league_away * max(0, 8-a_n)) / max(8, min(a_n, 8) + max(0, 8-a_n))
    h_against = (h_against_raw * min(h_n, 8) + home_league_away * max(0, 8-h_n)) / max(8, min(h_n, 8) + max(0, 8-h_n))
    home_base = max(0.15, min(4.5, home_league_home * (h_for / max(0.25, home_league_home)) * (a_against / max(0.25, away_league_home))))
    away_base = max(0.15, min(4.5, home_league_away * (a_for / max(0.25, away_league_away)) * (h_against / max(0.25, home_league_away))))
    home_recent = (hsum["recent_goals_for_avg"] + asum["recent_goals_against_avg"]) / 2
    away_recent = (asum["recent_goals_for_avg"] + hsum["recent_goals_against_avg"]) / 2
    home_rate = max(0.15, min(4.5, 0.60 * home_base + 0.40 * home_recent))
    away_rate = max(0.15, min(4.5, 0.60 * away_base + 0.40 * away_recent))

    # Understat is only blended when both teams belong to the same supported league.
    xg = _xg_rates(home_division, home_name, away_name) if home_division == away_division else None
    if xg:
        home_rate = max(0.15, min(4.5, 0.60 * xg["home_goal_rate"] + 0.40 * home_rate))
        away_rate = max(0.15, min(4.5, 0.60 * xg["away_goal_rate"] + 0.40 * away_rate))

    latest_home = max((row["date"] for row in home_matches if row["date"] and (_team_key(row["home"]) == _team_key(home_name) or _team_key(row["away"]) == _team_key(home_name))), default="2000-01-01")
    latest_away = max((row["date"] for row in away_matches if row["date"] and (_team_key(row["home"]) == _team_key(away_name) or _team_key(row["away"]) == _team_key(away_name))), default="2000-01-01")
    latest_date = datetime.fromisoformat(min(latest_home, latest_away)).date()
    data_age_days = (datetime.now(timezone.utc).date() - latest_date).days
    if data_age_days > FOOTBALL_STATS_MAX_AGE_DAYS:
        result = {"ok": False, "status": "stale", "event": event,
                  "reason": f"At least one team's latest available result is {data_age_days} days old, beyond the configured freshness limit.",
                  "home_latest_match_date": latest_home, "away_latest_match_date": latest_away,
                  "source": "Football-Data.co.uk historical match results CSV"}
        with FOOTBALL_STATS_LOCK:
            FOOTBALL_STATS_CACHE[cache_key] = (now, result)
        return result
    latest = {"home": latest_home, "away": latest_away}
    division = home_division if home_division == away_division else f"{home_division}+{away_division}"
    matches = home_matches if home_division == away_division else home_matches + away_matches
    result = {
        "ok": True, "status": "estimated", "event": event,
        "home_team_matched": home_name, "away_team_matched": away_name,
        "league": (FOOTBALL_DIVISIONS.get(home_division, home_division) if home_division == away_division else f"{FOOTBALL_DIVISIONS.get(home_division, home_division)} / {FOOTBALL_DIVISIONS.get(away_division, away_division)}"),
        "division": division,
        "home_league": FOOTBALL_DIVISIONS.get(home_division, home_division),
        "away_league": FOOTBALL_DIVISIONS.get(away_division, away_division),
        "seasons_used": sorted({row["season"] for row in home_matches + away_matches}),
        "source": ("Understat xG/xGA + Football-Data.co.uk results" if xg else "Football-Data.co.uk historical match results CSV; independent team histories"),
        "source_url": f"https://www.football-data.co.uk/mmz4281/{seasons[0]}/{home_division}.csv",
        "source_urls": sorted({f"https://www.football-data.co.uk/mmz4281/{row['season']}/{row['division']}.csv" for row in home_matches + away_matches}),
        "latest_match_date": latest, "home_latest_match_date": latest_home, "away_latest_match_date": latest_away, "data_age_days": data_age_days,
        "home_goal_rate": round(home_rate, 3), "away_goal_rate": round(away_rate, 3),
        "xg_available": bool(xg), "xg_source": xg.get("source") if xg else None,
        "xg_home_matches": xg.get("home_xg_matches") if xg else 0, "xg_away_matches": xg.get("away_xg_matches") if xg else 0,
        "xg_note": ("xG/xGA data matched for supported league; blended with result-based form." if xg else "Official xG was unavailable for this match/league; rates are estimated from goals, venue splits and recent form."),
        "home_stats": hsum, "away_stats": asum,
        "league_baseline_home_goals": round(home_league_home, 3), "league_baseline_away_goals": round(home_league_away, 3),
        "matches_in_league_sample": len(home_league_rows),
        "home_team_matches_in_sample": len(home_history), "away_team_matches_in_sample": len(away_history),
        "method": "Independent domestic histories; each team's venue rates normalized to its own league baseline, then scaled to the home team's scoring environment; 60% venue-adjusted rates + 40% recent goals.",
        "warnings": ["Goal-derived expected rates are not official xG.", "Cross-league estimates are provisional and not calibrated; verify competition, match date and squad context.", "This baseline must be backtested before relying on it.", "Team name matching can fail for reserve, women's, youth or neutral-site fixtures.", "Football-Data.co.uk files have publisher-stated private-use restrictions; check permissions before commercial reuse."],
    }
    with FOOTBALL_STATS_LOCK:
        FOOTBALL_STATS_CACHE[cache_key] = (now, result)
    return result


def _fixture_key(event):
    """Return an alias-aware fixture identity while preserving home/away order.

    Explicit team aliases are shared with historical-data matching. This allows a source
    to call a club “Man Utd” while another calls it “Manchester United”, without using
    fuzzy substring matching or swapping home and away teams.
    """
    home, away = _split_event_teams(event)
    if not home or not away:
        return (normalize_text(event), "")
    return (_team_key(home), _team_key(away))


def _odds_timestamp(value):
    """Parse ISO timestamps or Unix seconds/milliseconds; reject unknown timestamps."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            numeric = float(value)
            if not math.isfinite(numeric):
                return None
            if numeric > 10_000_000_000:
                numeric /= 1000.0
            return datetime.fromtimestamp(numeric, tz=timezone.utc)
        except (ValueError, OverflowError, OSError):
            return None
    return parse_timestamp(value)


def _common_market_supported(market, selection):
    """Return true only for full-time markets with implemented settlement logic."""
    m, s = normalize_text(market), normalize_text(selection)
    if _special_settlement_reason(m, s):
        return False
    if any(t in m for t in ("btts", "both teams to score", "gg ng", "gg/ng", "ggng")):
        return s in ("yes", "no", "gg", "ng", "gg yes", "ng no")
    if "correct score" in m or "exact score" in m or "full time score" in m:
        return re.fullmatch(r"\s*\d{1,2}\s*[-:]\s*\d{1,2}\s*", s) is not None
    if any(t in m for t in ("clean sheet", "keep a clean sheet", "team to score", "home team score", "away team score", "home to score", "away to score", "win to nil")):
        return s in ("yes", "no", "will", "wont", "true", "false", "not", "will score", "wont score", "win to nil yes", "win to nil no")
    if any(t in m for t in ("odd/even", "odd even", "total goals odd", "total goals even", "goals odd or even")):
        return s in ("odd", "even", "total odd", "total even", "goals odd", "goals even")
    if any(t in m for t in ("home team total", "away team total", "home goals", "away goals", "team goals", "team total goals")):
        return bool(re.search(r"\d+(?:\.\d+)?", m + " " + s)) and (s.startswith("over") or s.startswith("under"))
    if "double chance" in m:
        return s in ("1x", "x2", "12", "home draw", "home or draw", "draw away", "draw or away", "home away", "home or away", "home/draw", "draw/away", "home/away")
    if any(t in m for t in ("1x2", "match result", "full time result", "match winner", "result")):
        return s in ("home", "home win", "1", "draw", "x", "away", "away win", "2", "tie")
    if any(t in m for t in ("total", "over under", "goals", "goal")):
        return bool(re.search(r"\d+(?:\.\d+)?", m + " " + s)) and (s.startswith("over") or s.startswith("under"))
    return False


def _market_selection_signature(market, selection):
    """Canonical signature for settlement-equivalent supported selections only."""
    m, s = normalize_text(market), normalize_text(selection)
    if _special_settlement_reason(m, s):
        return None
    if any(t in m for t in ("btts", "both teams to score", "both teams score", "gg/ng", "gg ng", "ggng")):
        if s in ("yes", "gg", "gg yes", "both teams to score yes"):
            return ("btts", "yes")
        if s in ("no", "ng", "ng no", "both teams to score no"):
            return ("btts", "no")
        return None
    if "double chance" in m:
        aliases = {"1x":"1x", "home draw":"1x", "home or draw":"1x", "home/draw":"1x", "x2":"x2", "draw away":"x2", "draw or away":"x2", "draw/away":"x2", "12":"12", "home away":"12", "home or away":"12", "home/away":"12"}
        return ("double_chance", aliases[s]) if s in aliases else None
    if any(t in m for t in ("1x2", "match result", "match winner", "full time result", "result")):
        aliases = {"home":"home", "home win":"home", "1":"home", "draw":"draw", "x":"draw", "tie":"draw", "away":"away", "away win":"away", "2":"away"}
        return ("1x2", aliases[s]) if s in aliases else None
    combined = f"{m} {s}"
    match = re.search(r"(?:over|under|total(?: goals)?|goals?)\s*(\d+(?:\.\d+)?)|(?:total=)(\d+(?:\.\d+)?)", combined)
    line = next((g for g in match.groups() if g), None) if match else None
    if line is not None and ("over" in s or "under" in s):
        side = "over" if s.startswith("over") else "under" if s.startswith("under") else None
        team_family = any(t in m for t in ("home team total", "away team total", "home goals", "away goals", "team goals", "team total goals"))
        if side:
            return ("team_goals", m, f"{float(line):g}", side) if team_family else ("total_goals", f"{float(line):g}", side)
    if "correct score" in m or "exact score" in m or "full time score" in m:
        return ("correct_score", re.sub(r"\s+", "", s.replace(":", "-")))
    if any(t in m for t in ("clean sheet", "keep a clean sheet", "team to score", "home team score", "away team score", "home to score", "away to score", "win to nil")):
        return ("special_full_time", m, s)
    if any(t in m for t in ("odd/even", "odd even", "total goals odd", "total goals even", "goals odd or even")):
        return ("total_parity", s)
    return None


def find_market_replacements(legs, threshold=MIN_PROBABILITY, max_disagreement=REPLACEMENT_MAX_MODEL_DISAGREEMENT, now=None):
    """Find and rank same-fixture replacement candidates using two search tiers.

    Tier 1 compares equivalent market/selection pairs across providers. Tier 2 also
    evaluates other explicitly supported markets on the same fixture when the exact
    market is unavailable or weaker. Cross-market alternatives are labelled clearly;
    they are never represented as settlement-equivalent. Fresh source timestamps,
    two model estimates, a probability threshold and positive estimated value are
    required. Estimates are provisional and no bet is placed.
    """
    if not isinstance(legs, list) or not legs:
        raise ValueError("legs must be a non-empty list")
    if len(legs) > 50:
        raise ValueError("Maximum 50 legs per replacement search")
    threshold = finite_number(threshold, minimum=0.01, maximum=1.0)
    if threshold is None:
        raise ValueError("threshold must be a finite probability between 0.01 and 1.0")
    max_disagreement = finite_number(max_disagreement, minimum=0.0, maximum=1.0)
    if max_disagreement is None:
        raise ValueError("max_disagreement must be a finite number between 0 and 1")
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    rows, health = fetch_all_feeds()
    feed_by_key = {item.get("bookmaker"): item for item in health}
    results = []
    for index, leg in enumerate(legs, 1):
        if not isinstance(leg, dict):
            continue
        event = str(leg.get("event") or "").strip()
        original_market = str(leg.get("market") or "").strip()
        original_selection = str(leg.get("selection") or "").strip()
        original_odds = finite_number(leg.get("odds"), minimum=1.01, maximum=1000)
        target_key = _fixture_key(event)
        record = {
            "index": index, "event": event, "original": {
                "market": original_market, "selection": original_selection, "odds": original_odds,
                "market_supported": _market_selection_signature(original_market, original_selection) is not None,
            },
            "candidates": [], "same_market_candidates": [], "alternative_market_candidates": [],
            "market_coverage": {"fixture_market_rows_seen": 0, "outcomes_seen": 0, "supported_outcomes_modelled": 0, "rejected_or_unsupported_outcomes": 0, "qualifying_candidates": 0},
            "status": "unavailable", "decision": "REVIEW", "recommended_replacement": None,
            "reason": "No verified candidate found."
        }
        if not event or not all(target_key):
            record["reason"] = "Fixture name could not be normalized safely."
            results.append(record)
            continue
        original_signature = _market_selection_signature(original_market, original_selection)
        matching_rows = [row for row in rows if isinstance(row, dict) and _fixture_key(row.get("event", "")) == target_key]
        record["market_coverage"]["fixture_market_rows_seen"] = len(matching_rows)
        record["market_coverage"]["outcomes_seen"] = sum(len(row.get("outcomes", [])) for row in matching_rows if isinstance(row.get("outcomes", []), list))
        stats = fetch_match_statistics(event)
        if not stats.get("ok"):
            record["market_coverage"]["rejected_or_unsupported_outcomes"] = record["market_coverage"]["outcomes_seen"]
            record["reason"] = "Historical team-rate evidence is unavailable; odds alone cannot create a probability. Market discovery counts are still reported, but no candidates are scored without team-rate evidence."
            results.append(record)
            continue
        home_rate = finite_number(stats.get("home_goal_rate"), minimum=0.05, maximum=8.0)
        away_rate = finite_number(stats.get("away_goal_rate"), minimum=0.05, maximum=8.0)
        if home_rate is None or away_rate is None:
            record["reason"] = "Historical goal rates are invalid or missing."
            results.append(record)
            continue

        # Assess the original using the same models where its market is supported.
        original_p1 = original_p2 = original_conservative = original_edge = None
        if original_signature is not None and original_odds is not None:
            original_p1, _ = _selection_probability(original_market, original_selection, home_rate, away_rate)
            original_p2, _ = advanced.market_probability(original_market, original_selection, home_rate, away_rate)
            original_p1 = finite_number(original_p1, minimum=0, maximum=1)
            original_p2 = finite_number(original_p2, minimum=0, maximum=1)
            if original_p1 is not None and original_p2 is not None:
                original_conservative = min(original_p1, original_p2)
                original_edge = (original_conservative * original_odds - 1.0) * 100.0
        record["original"].update({
            "model_probability": original_p1, "alternate_model_probability": original_p2,
            "conservative_probability": original_conservative, "estimated_value_percent": original_edge,
            "passes_threshold": original_conservative is not None and original_conservative >= threshold,
        })

        for row in matching_rows:
            outcomes = row.get("outcomes", [])
            source_health = feed_by_key.get(row.get("bookmaker"), {})
            source_updated = _odds_timestamp(row.get("updated_at"))
            retrieved_at = _odds_timestamp(row.get("retrieved_at"))
            updated = source_updated or retrieved_at
            if updated is None:
                record["market_coverage"]["rejected_or_unsupported_outcomes"] += len(outcomes) if isinstance(outcomes, list) else 0
                continue
            age = (now.astimezone(timezone.utc) - updated).total_seconds()
            if age < -30 or age > STALE_AFTER_SECONDS:
                record["market_coverage"]["rejected_or_unsupported_outcomes"] += len(outcomes) if isinstance(outcomes, list) else 0
                continue
            if source_health.get("status") not in ("usable markets", "usable markets; freshness unknown"):
                record["market_coverage"]["rejected_or_unsupported_outcomes"] += len(outcomes) if isinstance(outcomes, list) else 0
                continue
            if not isinstance(outcomes, list):
                continue
            for outcome in outcomes:
                if not isinstance(outcome, dict):
                    continue
                market = str(row.get("market") or "").strip()
                selection = str(outcome.get("name") or "").strip()
                odds = finite_number(outcome.get("odds"), minimum=1.01, maximum=1000)
                signature = _market_selection_signature(market, selection)
                if odds is None or signature is None or not _common_market_supported(market, selection):
                    record["market_coverage"]["rejected_or_unsupported_outcomes"] += 1
                    continue
                same_market = original_signature is not None and signature == original_signature
                p_poisson, method_poisson = _selection_probability(market, selection, home_rate, away_rate)
                p_dc, method_dc = advanced.market_probability(market, selection, home_rate, away_rate)
                p_poisson = finite_number(p_poisson, minimum=0, maximum=1)
                p_dc = finite_number(p_dc, minimum=0, maximum=1)
                if p_poisson is None or p_dc is None:
                    record["market_coverage"]["rejected_or_unsupported_outcomes"] += 1
                    continue
                record["market_coverage"]["supported_outcomes_modelled"] += 1
                conservative = min(p_poisson, p_dc)
                disagreement = abs(p_poisson - p_dc)
                edge = (conservative * odds - 1.0) * 100.0
                if conservative < threshold or disagreement > max_disagreement or edge <= 0:
                    continue
                candidate = {
                    "event": event, "market": market, "selection": selection, "odds": odds,
                    "same_market_equivalent": same_market,
                    "replacement_type": "equivalent_market" if same_market else "alternative_market_same_fixture",
                    "settlement_warning": None if same_market else "Different market/settlement proposition; review before replacing.",
                    "bookmaker": row.get("bookmaker"), "bookmaker_key": row.get("bookmaker_key"),
                    "odds_verified": True, "odds_source": "configured feed row",
                    "updated_at": updated.isoformat(), "odds_age_seconds": round(max(0, age)),
                    "freshness_basis": "provider_timestamp" if source_updated else "retrieved_at; provider timestamp unavailable",
                    "model_probability": p_poisson, "alternate_model_probability": p_dc,
                    "conservative_probability": conservative, "model_disagreement": disagreement,
                    "estimated_value_percent": edge, "model_methods": [method_poisson, method_dc],
                    "evidence_quality": "provisional", "calibrated": False,
                }
                record["candidates"].append(candidate)
                (record["same_market_candidates"] if same_market else record["alternative_market_candidates"]).append(candidate)

        record["market_coverage"]["qualifying_candidates"] = len(record["candidates"])
        rank = lambda c: (c["conservative_probability"], c["estimated_value_percent"], c["same_market_equivalent"])
        record["same_market_candidates"].sort(key=rank, reverse=True)
        record["alternative_market_candidates"].sort(key=rank, reverse=True)
        # Tier 1: prefer an equivalent market/selection. Tier 2 can outrank it only
        # when the alternative's conservative probability improves by >=5 points.
        # This avoids comparing unlike settlement propositions as if they were equal.
        equivalent_best = record["same_market_candidates"][0] if record["same_market_candidates"] else None
        alternative_best = record["alternative_market_candidates"][0] if record["alternative_market_candidates"] else None
        if equivalent_best and alternative_best and alternative_best["conservative_probability"] >= equivalent_best["conservative_probability"] + 0.05:
            best = alternative_best
        else:
            best = equivalent_best or alternative_best
        if best:
            # Keep the API's first candidate aligned with the actual tiered decision.
            record["candidates"].sort(key=lambda c: (c is best, c["conservative_probability"], c["estimated_value_percent"]), reverse=True)
        else:
            record["candidates"].sort(key=rank, reverse=True)
        if best:
            record["status"] = "candidates_found"
            record["best_filtered_candidate"] = best
            original_qualifies = (
                original_conservative is not None and original_conservative >= threshold
                and original_edge is not None and original_edge > 0
                and abs(original_p1 - original_p2) <= max_disagreement
            )
            # Replace if the original fails the gates, or the selected candidate is
            # materially stronger. Equivalent markets need >=2 points; cross-market
            # alternatives need >=5 points because settlement propositions differ.
            improvement = (best["conservative_probability"] - original_conservative) if original_conservative is not None else None
            required_improvement = 0.05 if not best["same_market_equivalent"] else 0.02
            value_improvement = (best["estimated_value_percent"] - original_edge) if original_edge is not None else None
            same_market_value_upgrade = (
                best["same_market_equivalent"] and original_qualifies
                and value_improvement is not None and value_improvement >= 2.0
                and original_conservative is not None
                and best["conservative_probability"] >= original_conservative - 0.01
            )
            should_replace = (not original_qualifies) or (improvement is not None and improvement >= required_improvement) or same_market_value_upgrade
            record["decision"] = "REPLACE" if should_replace else "KEEP"
            if should_replace:
                record["recommended_replacement"] = best
                record["reason"] = (
                    "Best filtered candidate passed freshness, supported-market, two-model, probability, disagreement and positive-value gates. "
                    + ("Original did not pass all gates." if not original_qualifies else ("Equivalent candidate improves estimated value by at least 2 percentage points without materially lowering probability." if same_market_value_upgrade else "Candidate improves conservative probability by at least the required margin (2 points for equivalent markets; 5 points for cross-market alternatives)."))
                    + (" Alternative market: settlement differs; manual confirmation required." if not best["same_market_equivalent"] else " Equivalent market/selection.")
                )
            else:
                record["reason"] = "Original passes the configured gates and no candidate improves its conservative probability by the required margin; keep the original."
        elif not rows:
            failures = []
            for item in health:
                provider = str(item.get("bookmaker") or "Feed")
                status = str(item.get("status") or "unknown")
                detail = str(item.get("detail") or item.get("adapter_detail") or "No diagnostic detail")
                # Keep per-leg UI readable and avoid exposing feed URLs or credentials.
                detail = re.sub(r"https?://\S+", "[endpoint]", detail)[:180]
                failures.append(f"{provider}: {status} — {detail}")
            diagnostic = "; ".join(failures[:4]) if failures else "No feed diagnostics returned"
            record["reason"] = ("No feed returned usable market rows, so replacement search could not run. "
                                f"Feed diagnostics: {diagnostic}. No alternative odds were invented.")
        elif not matching_rows:
            record["reason"] = ("Feeds returned market rows, but none matched this fixture's normalized home/away names. "
                                "Check fixture-name aliases, competition, kickoff date and feed coverage; no substitute was invented.")
        else:
            record["reason"] = "No candidate passed all gates; stale/untimestamped prices and unsupported markets were excluded."
        results.append(record)
    return {
        "ok": True, "searched_at": now.isoformat(), "threshold": threshold,
        "max_model_disagreement": max_disagreement, "freshness_limit_seconds": STALE_AFTER_SECONDS,
        "legs_received": len(legs), "candidates_found": sum(bool(x["candidates"]) for x in results),
        "fixture_market_rows_seen": sum(x.get("market_coverage", {}).get("fixture_market_rows_seen", 0) for x in results),
        "outcomes_seen": sum(x.get("market_coverage", {}).get("outcomes_seen", 0) for x in results),
        "supported_outcomes_modelled": sum(x.get("market_coverage", {}).get("supported_outcomes_modelled", 0) for x in results),
        "qualifying_candidates": sum(x.get("market_coverage", {}).get("qualifying_candidates", 0) for x in results),
        "replacement_recommended": sum(x.get("decision") == "REPLACE" for x in results),
        "results": results, "feed_health": health,
        "note": "The engine searches all returned fixture markets with implemented full-time scoreline models, including 1X2/double chance, full-time totals, BTTS, team totals, team-to-score, clean-sheet, win-to-nil, total-goals parity and correct-score candidates. Candidates require observed odds within the freshness window, two provisional model estimates, the 70% conservative-probability threshold, <=8-point model disagreement and positive estimated value. Fresh retrieval is distinguished from a provider quote timestamp. Unsupported markets are counted/reported rather than assigned invented probabilities. Models are uncalibrated; this is not proof of profitable value and no bet is placed.",
        "release": "3.16.0",
        "calibration_status": "uncalibrated",
        "approval_policy": "Manual review required; missing or invalid evidence fails closed."
    }


def analyze_ticket_automatically(payload):
    legs = payload.get("legs") if isinstance(payload, dict) else None
    if not isinstance(legs, list) or not legs:
        raise ValueError("Provide a non-empty 'legs' array")
    if len(legs) > 50:
        raise ValueError("Maximum 50 ticket legs per analysis")
    results = []
    for index, leg in enumerate(legs, 1):
        if not isinstance(leg, dict):
            continue
        event = str(leg.get("event") or "").strip()
        market = str(leg.get("market") or "").strip()
        selection = str(leg.get("selection") or "").strip()
        odds = finite_number(leg.get("odds"), minimum=1.01, maximum=1000)
        if not event or not market or not selection:
            results.append({"index": index, "event": event, "market": market, "selection": selection, "status": "unmatched", "reason": "Ticket did not provide a clear event, market and selection."})
            continue
        item = {"index": index, "event": event, "market": market, "selection": selection, "odds": odds}
        special_reason = _special_settlement_reason(market, selection)
        if special_reason:
            item.update({"status": "unsupported_market", "reason": special_reason,
                         "estimated_probability": None, "method": "No model applied: special settlement rules are unsupported."})
            results.append(item)
            continue
        stats = fetch_match_statistics(event)
        item.update({"stats_status": stats.get("status"), "stats": stats})
        if not stats.get("ok"):
            item.update({"status": "not_estimated", "reason": stats.get("reason", "Statistics unavailable."), "estimated_probability": None})
            results.append(item)
            continue
        probability, method = _selection_probability(market, selection, stats["home_goal_rate"], stats["away_goal_rate"])
        implied = 1.0 / odds if odds else None
        edge = (probability * odds - 1) * 100 if probability is not None and odds else None
        item.update({"status": "estimated" if probability is not None else "unsupported_market", "estimated_probability": probability,
                     "odds_implied_probability": implied, "model_fair_odds": (1 / probability if probability and probability > 0 else None),
                     "estimated_edge_percent": edge, "method": method, "home_goal_rate": stats["home_goal_rate"], "away_goal_rate": stats["away_goal_rate"]})
        results.append(item)
    # Sort estimated candidates by edge first, then probability; unresolved legs follow.
    results.sort(key=lambda x: (x.get("status") == "estimated", x.get("estimated_edge_percent") if x.get("estimated_edge_percent") is not None else -1e9,
                                x.get("estimated_probability") if x.get("estimated_probability") is not None else -1), reverse=True)
    return {"ok": True, "analyzed_at": now_iso(), "source": "Automatic historical team-stat lookup", "legs_received": len(legs),
            "estimated_legs": sum(1 for x in results if x.get("status") == "estimated"),
            "unavailable_legs": sum(1 for x in results if x.get("status") != "estimated"), "ranked_legs": results,
            "warnings": ["Automatically estimated rates are goal-history-derived, not official xG.",
                         "No probability is produced when team matching or market interpretation fails.",
                         "This model is uncalibrated; estimated edge is not proof of value or profit.",
                         "Football-Data.co.uk free CSVs are described by their publisher as for private individual use; check permissions before commercial reuse.",
                         "No bet is placed and the original booking is never changed."]}


def audit_ticket_math(payload):
    """Audit reported accumulator odds and payout against every supplied leg."""
    if not isinstance(payload, dict):
        raise ValueError("Expected a JSON object")
    legs = payload.get("legs")
    if not isinstance(legs, list) or not legs:
        raise ValueError("Provide a non-empty 'legs' array")
    if len(legs) > 50:
        raise ValueError("Maximum 50 ticket legs per audit")
    parsed, invalid = [], []
    for index, leg in enumerate(legs, 1):
        if not isinstance(leg, dict):
            invalid.append({"index": index, "reason": "Leg must be an object"})
            continue
        odds = finite_number(leg.get("odds"), minimum=1.01, maximum=1000)
        event = str(leg.get("event") or "").strip()
        if odds is None:
            invalid.append({"index": index, "event": event, "reason": "Odds must be a finite decimal value from 1.01 to 1000"})
        else:
            parsed.append({"index": index, "event": event, "market": str(leg.get("market") or "").strip(),
                           "selection": str(leg.get("selection") or "").strip(), "odds": odds})
    if invalid:
        return {"ok": True, "audit_status": "INCOMPLETE_INPUT", "legs_received": len(legs),
                "valid_legs": len(parsed), "invalid_legs": invalid, "calculated_combined_odds": None,
                "note": "Combined odds are not calculated because one or more leg prices are invalid. No values were inferred."}
    calculated = math.prod(row["odds"] for row in parsed)
    reported = finite_number(payload.get("reported_total_odds"), minimum=1.000001, maximum=1e15)
    stake = finite_number(payload.get("stake"), minimum=0, maximum=1e15)
    bonus = finite_number(payload.get("reported_bonus"), minimum=0, maximum=1e15)
    payout = finite_number(payload.get("reported_payout"), minimum=0, maximum=1e15)
    tolerance = finite_number(payload.get("relative_tolerance"), minimum=0, maximum=0.10)
    if tolerance is None: tolerance = 0.005
    difference = reported - calculated if reported is not None else None
    relative = difference / calculated if reported is not None and calculated else None
    odds_agree = None if reported is None else math.isclose(reported, calculated, rel_tol=tolerance, abs_tol=0.01)
    simple_return = (stake * reported if reported is not None else stake * calculated) if stake is not None else None
    payout_difference = payout - simple_return if payout is not None and simple_return is not None else None
    payout_agree = None if payout_difference is None else math.isclose(payout, simple_return, rel_tol=tolerance, abs_tol=1.0)
    if odds_agree is False: status = "ODDS_MISMATCH"
    elif payout_agree is False: status = "PAYOUT_MISMATCH"
    elif odds_agree is True and (payout_agree is True or payout is None): status = "ARITHMETICALLY_CONSISTENT"
    else: status = "PARTIAL_AUDIT"
    return {"ok": True, "audit_status": status, "legs_received": len(legs), "valid_legs": len(parsed),
            "legs": parsed, "calculated_combined_odds": calculated, "reported_total_odds": reported,
            "reported_minus_calculated_odds": difference,
            "reported_odds_relative_difference_percent": relative * 100 if relative is not None else None,
            "odds_agree": odds_agree, "relative_tolerance": tolerance, "stake": stake,
            "simple_return_at_reported_odds": stake * reported if stake is not None and reported is not None else None,
            "simple_return_at_calculated_odds": stake * calculated if stake is not None else None,
            "reported_bonus": bonus, "reported_payout": payout,
            "reported_payout_minus_simple_return": payout_difference, "payout_agrees_with_simple_return": payout_agree,
            "combined_odds_implied_probability_percent": (1.0 / calculated) * 100 if calculated > 0 else None,
            "note": "Arithmetic audit only. Bookmaker bonus, tax, void-leg rules, boosts and payout definitions may differ; verify the original slip. Implied probability is not a forecast and does not account for accumulator dependence or bookmaker margin."}


def analyze_betslip(payload):
    """Analyze user-selected betslip legs; no external calls or automatic betting."""
    legs = payload.get("legs") if isinstance(payload, dict) else None
    if not isinstance(legs, list) or not legs:
        raise ValueError("Provide a non-empty 'legs' array")
    if len(legs) > 50:
        raise ValueError("Maximum 50 selected legs per analysis")
    analyzed = []
    for index, leg in enumerate(legs, start=1):
        if not isinstance(leg, dict):
            raise ValueError(f"Leg {index} must be an object")
        event = str(leg.get("event", "")).strip()
        market = str(leg.get("market", "")).strip()
        selection = str(leg.get("selection", "")).strip()
        odds = finite_number(leg.get("odds"), minimum=1.01, maximum=1000)
        home_raw, away_raw = leg.get("home_goal_rate"), leg.get("away_goal_rate")
        home_rate = finite_number(home_raw, minimum=0.05, maximum=8.0)
        away_rate = finite_number(away_raw, minimum=0.05, maximum=8.0)
        auto_stats = None
        # Explicit but invalid values are rejected; only omitted/null rates trigger lookup.
        if home_raw not in (None, "") and home_rate is None:
            raise ValueError(f"Leg {index}: home_goal_rate must be from 0.05 to 8.0")
        if away_raw not in (None, "") and away_rate is None:
            raise ValueError(f"Leg {index}: away_goal_rate must be from 0.05 to 8.0")
        if home_rate is None or away_rate is None:
            auto_stats = fetch_match_statistics(event)
            if auto_stats.get("ok"):
                home_rate = auto_stats.get("home_goal_rate")
                away_rate = auto_stats.get("away_goal_rate")
        if not event or len(event) > 180 or not market or len(market) > 100 or not selection or len(selection) > 100:
            raise ValueError(f"Leg {index}: event, market and selection are required and must be short text")
        if odds is None:
            raise ValueError(f"Leg {index}: odds must be a finite decimal value from 1.01 to 1000")
        if home_rate is None or away_rate is None:
            analyzed.append({"event": event, "market": market, "selection": selection, "odds": odds,
                             "home_goal_rate": None, "away_goal_rate": None, "estimated_probability": None,
                             "model_fair_odds": None, "odds_implied_probability": 1.0 / odds,
                             "estimated_edge_percent": None, "model_supported": False,
                             "method": (auto_stats or {}).get("reason", "Automatic team statistics unavailable; no probability fabricated."),
                             "status": "statistics unavailable"})
            continue
        probability, method = _selection_probability(market, selection, home_rate, away_rate)
        implied = 1.0 / odds
        estimated_edge = (probability * odds - 1.0) * 100.0 if probability is not None else None
        analyzed.append({
            "event": event, "market": market, "selection": selection, "odds": odds,
            "home_goal_rate": home_rate, "away_goal_rate": away_rate,
            "estimated_probability": probability,
            "model_fair_odds": (1.0 / probability) if probability and probability > 0 else None,
            "odds_implied_probability": implied,
            "estimated_edge_percent": estimated_edge,
            "model_supported": probability is not None,
            "method": method,
            "stats_source": (auto_stats or {}).get("source") if auto_stats else "user-supplied rates",
            "status": (
                "model suggests positive edge; verify rates, odds and settlement"
                if probability is not None and estimated_edge is not None and estimated_edge > 0
                else "no positive model edge at entered odds; not a betting recommendation"
                if probability is not None
                else "not ranked: unsupported market/selection"
            ),
        })
    supported = [item for item in analyzed if item["model_supported"]]
    supported.sort(key=lambda item: (item["estimated_edge_percent"], item["estimated_probability"]), reverse=True)
    unsupported = [item for item in analyzed if not item["model_supported"]]
    # No combined probability is offered because legs may be correlated and the model is not calibrated.
    return {
        "ok": True,
        "analyzed_at": now_iso(),
        "model": "independent Poisson baseline; automatically fetched goal-history rates when inputs are absent",
        "legs_received": len(analyzed),
        "supported_legs": len(supported),
        "unsupported_legs": len(unsupported),
        "ranked_legs": supported + unsupported,
        "warnings": [
            "Missing goal rates trigger automatic team-stat lookup when enabled; rates are goal-history-derived, not official xG.",
            "The independent-Poisson model is a baseline estimate, not a calibrated or verified prediction.",
            "Estimated edge is only meaningful if the goal rates are credible and odds are current.",
            "No combined accumulator win probability is calculated because legs may be correlated.",
            "This tool is read-only and never places bets.",
        ],
    }

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
    value = str(value or "").replace("æ", "ae").replace("Æ", "Ae").replace("ø", "o").replace("Ø", "O").replace("ł", "l").replace("Ł", "L").replace("ß", "ss")
    value = unicodedata.normalize("NFKD", value)
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
        # A short, sanitized upstream message helps distinguish bad query parameters
        # (for example HTTP 422) from access restrictions without logging large bodies.
        try:
            error_body = exc.read(1024).decode("utf-8", errors="replace")
            error_body = re.sub(r"<[^>]+>", " ", error_body)
            error_body = " ".join(error_body.split())[:220]
        except Exception:
            error_body = ""
        detail = f"HTTP {exc.code}" + (f"; upstream: {error_body}" if error_body else "")
        diag.update(status="http error", detail=detail)
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
    """Resolve homepage URLs to source-specific public data candidates.

    These are candidates, not guarantees: the bookmaker can change or restrict them.
    """
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
                "pageSize": str(SPORTYBET_PAGE_SIZE), "pageNum": "1", "todayGames": "false",
                "timeline": "720", "_t": str(int(time.time() * 1000)),
            }
            # Broad request first: omit marketId unless the user explicitly supplies IDs.
            market_ids = os.environ.get("SPORTYBET_MARKET_IDS", "").strip()
            if market_ids:
                params["marketId"] = market_ids
            url = "https://www.sportybet.com/api/ng/factsCenter/pcUpcomingEvents?" + urlencode(params)
            return url, {
                "Accept": "application/json, text/plain, */*", "Current-Country": "NG",
                "Origin": "https://www.sportybet.com", "Referer": "https://www.sportybet.com/ng/",
            }
        return configured_url, {"Current-Country": "NG", "Referer": "https://www.sportybet.com/ng/"}

    if bookmaker_key == "bet9ja":
        # The configured /Sport/Odds page is HTML. Use the documented MarketWS XML operation.
        if host in ("web.bet9ja.com", "www.bet9ja.com") and ("marketws.asmx" in path or path == "/sport/odds" or path == ""):
            return "https://web.bet9ja.com/Controls/MarketWS.asmx", {"Referer": "https://web.bet9ja.com/Sport/Odds", "Accept": "text/xml, application/xml, */*"}
        if host.endswith("bet9ja.com") and "/controls/marketws.asmx" in path:
            return configured_url, {"Referer": "https://web.bet9ja.com/Sport/Odds", "Accept": "text/xml, application/xml, */*"}

    if bookmaker_key == "1xbet":
        # Public LineFeed endpoint candidate for football fixtures. The adapter only accepts
        # actual market/outcome odds fields; fixture names alone are never treated as odds.
        if host == "1xbet.ng" or host.endswith(".1xbet.ng"):
            if "/linefeed/" not in path:
                params = {"sports": "1", "count": "1000", "lng": "en", "mode": "4", "country": "1"}
                return "https://1xbet.ng/LineFeed/BestGamesExtZip?" + urlencode(params), {"Accept": "application/json, text/plain, */*", "Referer": "https://1xbet.ng/en"}

    if bookmaker_key == "betway":
        # Public evidence found for the flat-array SportsAPI is for Betway South Africa,
        # not Betway Nigeria. Never redirect a Nigeria configuration to another region.
        # A configured URL is fetched as supplied and must pass schema validation.
        return configured_url, {"Accept": "application/json, text/plain, */*"}
    return configured_url, {}


def fetch_sportybet_discovery():
    """Try scheduled and in-play public endpoints; report each result explicitly.

    SportyBet does not publish an official third-party API contract. These site endpoints
    are best-effort only. Broad market retrieval is attempted first, then known market IDs
    are used as a fallback if no usable odds rows are returned.
    """
    headers = {"Accept": "application/json, text/plain, */*", "Current-Country": "NG",
               "Origin": "https://www.sportybet.com", "Referer": "https://www.sportybet.com/ng/"}
    modes = ["live", "scheduled"] if SPORTYBET_MARKET_MODE == "both" else [SPORTYBET_MARKET_MODE]
    tournaments_merged = []
    seen_ids = set()
    mode_diags = []
    combined = {"http_status": None, "content_type": "application/json", "response_bytes": 0,
                "fetch_ms": 0, "json_valid": False, "schema_valid": False, "raw_events": 0,
                "events_loaded": 0, "events_rejected": 0, "rejection_reasons": {},
                "last_successful_fetch": None, "data_updated_at": None, "freshness_seconds": None,
                "stale": None, "detail": "", "status": "fetch failed",
                "source_mode": f"automatic SportyBet {SPORTYBET_MARKET_MODE} market discovery"}
    fallback_ids = os.environ.get("SPORTYBET_MARKET_IDS", "1,18,10,11,14,16,26,29,36,45,47,60,60100").strip()
    for mode in modes:
        endpoint = "pcLiveEvents" if mode == "live" else "pcUpcomingEvents"
        params = {"sportId": "sr:sport:1", "pageSize": str(SPORTYBET_PAGE_SIZE), "pageNum": "1",
                  "todayGames": "false", "timeline": "720", "_t": str(int(time.time() * 1000))}
        configured_ids = os.environ.get("SPORTYBET_MARKET_IDS", "").strip()
        if configured_ids:
            params["marketId"] = configured_ids
        base = f"https://www.sportybet.com/api/ng/factsCenter/{endpoint}?"
        payload, diag = safe_fetch_json(base + urlencode(params), f"SportyBet {mode}", headers)
        broad_diag = {"status": diag.get("status"), "http_status": diag.get("http_status"), "detail": diag.get("detail", "")}
        fallback_used = False
        raw = None
        raw_count = 0
        has_usable = False
        if payload is not None:
            raw, raw_count, _ = adapt_sportybet_payload(payload)
            has_usable = isinstance(raw, list) and any(isinstance(r, dict) and isinstance(r.get("outcomes"), list) and len(r["outcomes"]) >= 2 for r in raw)
        # Crucial recovery: a 4xx/5xx or anti-bot response on the broad request must
        # not suppress the supported market-ID fallback. v3.16 only fell back when
        # the first request returned valid JSON, so HTTP 422 stopped discovery entirely.
        if not has_usable:
            fallback_params = dict(params)
            fallback_params["marketId"] = fallback_ids
            fallback_payload, fallback_diag = safe_fetch_json(base + urlencode(fallback_params), f"SportyBet {mode} fallback", headers)
            fallback_raw, fallback_count, _ = adapt_sportybet_payload(fallback_payload) if fallback_payload is not None else (None, 0, "Fallback request returned no JSON payload")
            fallback_usable = isinstance(fallback_raw, list) and any(isinstance(r, dict) and isinstance(r.get("outcomes"), list) and len(r["outcomes"]) >= 2 for r in fallback_raw)
            if fallback_usable:
                payload, raw, raw_count, diag, fallback_used = fallback_payload, fallback_raw, fallback_count, fallback_diag, True
                diag["broad_request_diagnostics"] = broad_diag
            else:
                diag["fallback_diagnostics"] = {"status": fallback_diag.get("status"), "http_status": fallback_diag.get("http_status"), "detail": fallback_diag.get("detail", "")}
        if isinstance(payload, dict):
            data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
            tournaments = data.get("tournaments") if isinstance(data.get("tournaments"), list) else []
            for tournament in tournaments:
                if not isinstance(tournament, dict):
                    continue
                events = tournament.get("events") if isinstance(tournament.get("events"), list) else []
                kept = []
                for event in events:
                    if not isinstance(event, dict):
                        continue
                    eid = str(event.get("eventId") or event.get("id") or "")
                    if eid and eid in seen_ids:
                        continue
                    if eid:
                        seen_ids.add(eid)
                    kept.append(event)
                if kept:
                    clone = dict(tournament)
                    clone["events"] = kept
                    tournaments_merged.append(clone)
        mode_diags.append({"mode": mode, "endpoint": endpoint, "status": diag.get("status"),
                           "http_status": diag.get("http_status"), "response_bytes": diag.get("response_bytes", 0),
                           "fetch_ms": diag.get("fetch_ms", 0), "json_valid": diag.get("json_valid", False),
                           "fallback_used": fallback_used, "broad_request": broad_diag,
                           "fallback_diagnostics": diag.get("fallback_diagnostics"), "detail": diag.get("detail", "")})
        combined["response_bytes"] += diag.get("response_bytes", 0)
        combined["fetch_ms"] += diag.get("fetch_ms", 0)
        if diag.get("http_status") is not None:
            combined["http_status"] = diag["http_status"]
        if diag.get("json_valid"):
            combined["json_valid"] = True
            combined["last_successful_fetch"] = diag.get("last_successful_fetch") or combined["last_successful_fetch"]
    combined["schema_valid"] = bool(tournaments_merged)
    combined["status"] = "json received" if combined["json_valid"] else "fetch failed"
    combined["detail"] = "; ".join(f"{d['mode']}={d['status']} HTTP={d['http_status']} fallback={d['fallback_used']}" for d in mode_diags)
    combined["mode_diagnostics"] = mode_diags
    if not tournaments_merged:
        return None, combined
    return {"bizCode": 10000, "data": {"tournaments": tournaments_merged}}, combined


def adapt_sportybet_payload(payload):
    """Convert SportyBet's nested factsCenter response into normalized event-market records.

    Documented shape: data.tournaments[].events[].markets[].outcomes[].
    This adapter deliberately does not invent probabilities or update timestamps.
    """
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
                    parsed_outcomes.append({
                        "name": name,
                        "odds": odds,
                        "outcome_id": str(outcome.get("id") or ""),
                        # SportyBet's ordinary odds response does not provide a validated
                        # model probability; never manufacture one from the odds.
                    })
                market_name = str(market.get("desc") or market.get("name") or "").strip()
                specifier = str(market.get("specifier") or "").strip()
                if specifier:
                    market_name = f"{market_name} {specifier}".strip()
                if not market_name:
                    continue
                normalized.append({
                    "sport": "Football",
                    "event": event_name,
                    "event_id": str(event.get("eventId") or event.get("id") or ""),
                    "start_time": first_value(event, ["estimateStartTime", "startTime", "kickoffTime"]),
                    "match_status": str(event.get("matchStatus") or event.get("status") or ""),
                    "market": market_name,
                    "market_id": str(market.get("id") or market.get("marketId") or ""),
                    "market_group": str(market.get("group") or market.get("groupName") or ""),
                    "specifier": specifier,
                    "outcomes": parsed_outcomes,
                    # Do not use scheduled kick-off as an odds-update timestamp.
                    "updated_at": first_value(market, ["updated_at", "updatedAt", "lastUpdateTime", "updateTime"]),
                    "league": tournament_name,
                })
    return normalized, raw_market_count, "SportyBet nested schema recognized"


def normalize_feed_event(bookmaker_key, raw):
    if not isinstance(raw, dict):
        return None
    sport = str(raw.get("sport") or "Unknown sport").strip()
    event = str(raw.get("event") or raw.get("fixture") or "").strip()
    market = str(raw.get("market") or "").strip()
    outcomes = raw.get("outcomes")
    if not event or not market or not isinstance(outcomes, list):
        return None

    unique = {}
    for outcome in outcomes:
        if not isinstance(outcome, dict):
            continue
        name = str(outcome.get("name") or outcome.get("selection") or "").strip()
        odds = finite_number(outcome.get("odds"), minimum=1.000001)
        if not name or odds is None:
            continue
        key = normalize_text(name)
        if not key:
            continue
        probability = finite_number(
            outcome.get("estimated_probability"), minimum=0, maximum=1
        )
        unique[key] = {
            "name": name,
            "name_key": key,
            "odds": odds,
            "estimated_probability": probability,
        }

    normalized_outcomes = list(unique.values())
    # Accept two-way markets and three-way 1X2 markets. Other outcome counts are
    # rejected until a market-specific settlement adapter explicitly supports them.
    if len(normalized_outcomes) < 2 or len(normalized_outcomes) > MAX_MARKET_OUTCOMES:
        return None

    return {
        "bookmaker_key": bookmaker_key,
        "bookmaker": BOOKMAKERS[bookmaker_key]["name"],
        "sport": sport,
        "event": event,
        "event_key": normalize_text(event),
        "market": market,
        "market_key": normalize_text(market),
        "market_id": str(raw.get("market_id") or raw.get("marketId") or ""),
        "market_group": str(raw.get("market_group") or raw.get("marketGroup") or ""),
        "specifier": str(raw.get("specifier") or ""),
        "event_id": str(raw.get("event_id") or raw.get("eventId") or ""),
        "start_time": raw.get("start_time") or raw.get("startTime"),
        "match_status": str(raw.get("match_status") or raw.get("matchStatus") or ""),
        "outcomes": normalized_outcomes,
        "updated_at": raw.get("updated_at"),
    }



def adapt_global_payload(payload):
    """Flatten common global odds JSON shapes into explicit event/market/outcome rows.

    Supports SolveEase's simple {events:[{event, market, outcomes, updated_at}]}
    schema and The Odds API-style events with home_team, away_team, bookmakers,
    markets and outcomes. Source timestamps are preserved, never replaced with fetch time.
    """
    if not isinstance(payload, dict) or not isinstance(payload.get("events"), list):
        return None, 0, "Expected a JSON object containing an events array"
    raw_events = payload["events"]
    normalized = []
    raw_market_count = 0
    for event in raw_events:
        if not isinstance(event, dict):
            continue
        # Already-normalized provider-independent schema.
        if event.get("event") or event.get("fixture"):
            if isinstance(event.get("outcomes"), list) and event.get("market"):
                normalized.append(event)
                raw_market_count += 1
                continue
        home = str(event.get("home_team") or event.get("homeTeam") or event.get("home") or "").strip()
        away = str(event.get("away_team") or event.get("awayTeam") or event.get("away") or "").strip()
        if not home or not away:
            continue
        fixture = f"{home} vs {away}"
        commence = event.get("commence_time") or event.get("start_time") or event.get("kickoff")
        bookmakers = event.get("bookmakers") or event.get("providers") or []
        if not isinstance(bookmakers, list):
            continue
        for bookmaker in bookmakers:
            if not isinstance(bookmaker, dict):
                continue
            markets = bookmaker.get("markets") or []
            if not isinstance(markets, list):
                continue
            for market in markets:
                if not isinstance(market, dict):
                    continue
                raw_market_count += 1
                key = normalize_text(market.get("key") or market.get("name") or market.get("market") or "")
                raw_outcomes = market.get("outcomes")
                if not isinstance(raw_outcomes, list):
                    continue
                updated = market.get("last_update") or market.get("updated_at") or bookmaker.get("last_update") or bookmaker.get("updated_at")
                line_groups = {}
                if key in ("h2h", "1x2", "moneyline", "match result", "match winner"):
                    market_name = "1X2"
                    mapped = []
                    for outcome in raw_outcomes:
                        if not isinstance(outcome, dict):
                            continue
                        name = str(outcome.get("name") or outcome.get("selection") or "").strip()
                        odds = outcome.get("price", outcome.get("odds"))
                        if _team_key(name) == _team_key(home):
                            name = "Home"
                        elif _team_key(name) == _team_key(away):
                            name = "Away"
                        elif normalize_text(name) in ("draw", "tie", "x"):
                            name = "Draw"
                        mapped.append({"name": name, "odds": odds})
                    if mapped:
                        normalized.append({"sport": "Football", "event": fixture, "market": market_name, "outcomes": mapped, "updated_at": updated, "start_time": commence})
                elif key in ("totals", "total goals", "over under", "over/under"):
                    for outcome in raw_outcomes:
                        if not isinstance(outcome, dict):
                            continue
                        point = finite_number(outcome.get("point", outcome.get("line")), minimum=0, maximum=30)
                        name = str(outcome.get("name") or outcome.get("selection") or "").strip().lower()
                        odds = outcome.get("price", outcome.get("odds"))
                        if point is None or name not in ("over", "under"):
                            continue
                        line_groups.setdefault(point, []).append({"name": f"{name.title()} {point:g}", "odds": odds})
                    for point, outs in line_groups.items():
                        normalized.append({"sport": "Football", "event": fixture, "market": f"Total Goals {point:g}", "outcomes": outs, "updated_at": updated, "start_time": commence})
                elif key in ("btts", "both teams to score", "gg/ng", "ggng"):
                    mapped = []
                    for outcome in raw_outcomes:
                        if not isinstance(outcome, dict):
                            continue
                        name = normalize_text(outcome.get("name") or outcome.get("selection") or "")
                        if name in ("yes", "no"):
                            mapped.append({"name": name.title(), "odds": outcome.get("price", outcome.get("odds"))})
                    if mapped:
                        normalized.append({"sport": "Football", "event": fixture, "market": "BTTS", "outcomes": mapped, "updated_at": updated, "start_time": commence})
    return normalized, raw_market_count, "Global feed event/market schemas recognized; source timestamps preserved"

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
    headers = {"User-Agent": "Mozilla/5.0 (compatible; SolveEaseFeedMonitor/3.3)", "Accept": "text/xml, application/xml, */*"}
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
    """Probe Bet9ja's documented XML service, discover football sport ID, then request odds.

    This uses the published service interface only. It does not evade a 403/WAF response.
    """
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
        diag.update(status="invalid schema", schema_valid=False, detail="Bet9ja XML parsed, but no football sport ID was identified in the published service response")
        diag["bookmaker"] = "Bet9ja"
        return [], diag
    odds_params = {"strQuotaMax": "1000", "strIDSport": sport_id, "typeOrder": "0", "tipoVisQuote": "0"}
    odds_url = base + "/GetListOdds_OddLessThan?" + urlencode(odds_params)
    odds_root, odds_diag = safe_fetch_xml(odds_url, "Bet9ja odds", headers)
    # Prefer the odds response diagnostics; include both request durations and sizes.
    odds_diag["fetch_ms"] += diag.get("fetch_ms", 0)
    odds_diag["response_bytes"] += diag.get("response_bytes", 0)
    odds_diag["bookmaker"] = "Bet9ja"
    if odds_root is None:
        odds_diag["detail"] = "Football sport ID discovered, but the Bet9ja odds operation failed: " + odds_diag.get("detail", "unknown error")
        return [], odds_diag

    odds_diag["schema_valid"] = True
    odds_diag["status"] = "XML schema recognized; checking quote fields"
    odds_diag["last_successful_fetch"] = odds_diag.get("last_successful_fetch") or now_iso()
    normalized = []
    # The published service exposes SubEvents, OddClass and Quote/clsQuoteList.
    # Parse conservatively: only emit a market when two named selections have numeric decimal odds.
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
        # Keep unique labels only; skip unclear or 3-way markets.
        unique = {normalize_text(o["name"]): o for o in selections if normalize_text(o["name"])}
        if len(unique) == 2:
            normalized.append({"sport": "Football", "event": event_name, "market": subevent_name or "Bet9ja market", "outcomes": list(unique.values()), "updated_at": None})
    odds_diag["raw_events"] = sum(1 for n in odds_root.iter() if _xml_local(n.tag) == "clssubeventslist")
    odds_diag["events_loaded"] = len(normalized)
    odds_diag["events_rejected"] = max(0, odds_diag["raw_events"] - len(normalized))
    if normalized:
        odds_diag.update(status="usable markets; freshness unknown", detail="Bet9ja XML returned named two-outcome markets; update timestamps were not confirmed")
    elif odds_diag["raw_events"]:
        odds_diag.update(status="no usable markets", detail="Bet9ja XML parsed, but quote field names/values did not yield exactly two named decimal-odds outcomes; adapter needs a sample response to map safely")
    else:
        odds_diag.update(status="no events", detail="Bet9ja XML schema parsed, but no subevents were returned")
    return normalized, odds_diag


def adapt_betway_payload(payload):
    """Normalize Betway's documented flat event/market/outcome/price arrays when present."""
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
        return None, len(events), "Valid JSON, but expected Betway flat arrays (events, markets, outcomes, prices) were not all present"
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
        if event_name and market_name and len({normalize_text(x["name"]) for x in vals}) == 2:
            normalized.append({"sport": "Football", "event": event_name, "market": str(market_name), "outcomes": vals, "updated_at": pick(market, "updatedAt", "updated_at", "lastUpdate")})
    return normalized, len(events), "Betway flat-array schema recognized"


def adapt_1xbet_payload(payload):
    """Recognize 1xBet LineFeed fixture payloads without inventing unsupported odds."""
    if not isinstance(payload, dict) or not isinstance(payload.get("Value"), list):
        return None, 0, 'Valid JSON, but expected 1xBet LineFeed "Value" array'
    events = payload.get("Value", [])
    normalized = []
    for item in events:
        if not isinstance(item, dict):
            continue
        # Only accept explicit market/outcome collections; fixture-level labels are not prices.
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
            if name and len({normalize_text(o["name"]) for o in outs}) == 2:
                normalized.append({"sport": "Football", "event": f"{home} vs {away}", "market": str(name), "outcomes": outs, "updated_at": first_value(market, ["updatedAt", "updated_at"])})
    if normalized:
        return normalized, len(events), "1xBet explicit market/outcome schema recognized"
    return [], len(events), "1xBet fixtures were returned, but no supported explicit two-outcome market/odds objects were found; no odds inferred"


def fetch_all_feeds():
    """Fetch feeds, use source-specific adapters, and retain per-source diagnostics."""
    rows, health = [], []
    for key, config in BOOKMAKERS.items():
        configured_url = str(config.get("feed_url") or "").strip()
        # Treat documentation/example URLs as unset. They must not suppress the built-in
        # SportyBet discovery adapter when copied into a deployment by mistake.
        configured_host = (urlsplit(configured_url).hostname or "").lower() if configured_url else ""
        placeholder_url = bool(configured_url) and (
            configured_host.endswith(".example") or configured_host in {"example.com", "www.example.com"}
            or "your-authorized-feed" in configured_url.lower()
        )
        if placeholder_url:
            configured_url = ""
        auto_discovery = key == "sportybet" and SPORTYBET_AUTO_DISCOVERY and not configured_url
        effective_url = configured_url or (SPORTYBET_DISCOVERY_HOME if auto_discovery else "")
        # SportyBet is the one default discovery adapter because the user workflow is
        # based on loaded SportyBet tickets. Other providers are never contacted unless
        # their feed URL is explicitly configured. Set SPORTYBET_AUTO_DISCOVERY=0 to opt out.
        if not effective_url:
            health.append({
                "bookmaker": config["name"],
                "status": "not configured",
                "detail": "No feed URL configured; endpoint has not been tested",
                "http_status": None,
                "content_type": None,
                "response_bytes": 0,
                "fetch_ms": 0,
                "json_valid": False,
                "xml_valid": False if key == "bet9ja" else None,
                "schema_valid": False,
                "raw_events": 0,
                "events_loaded": 0,
                "events_rejected": 0,
                "rejection_reasons": {},
                "last_successful_fetch": None,
                "data_updated_at": None,
                "freshness_seconds": None,
                "stale": None,
            })
            continue

        request_url, headers = prepare_feed_request(key, effective_url)
        if auto_discovery:
            payload, diag = fetch_sportybet_discovery()
            diag["bookmaker"] = config["name"]
            diag["source_mode"] = f"automatic SportyBet {SPORTYBET_MARKET_MODE} market discovery"
        elif key == "bet9ja":
            raw_events, diag = fetch_bet9ja_xml(request_url, headers)
            if diag.get("schema_valid") and raw_events:
                for raw in raw_events:
                    row = normalize_feed_event(key, raw)
                    if row:
                        rows.append(row)
                # Adapter already counts the XML records; normalized count must match rows accepted.
                diag["events_loaded"] = sum(1 for r in rows if r["bookmaker_key"] == key)
                diag["events_rejected"] = max(0, diag.get("raw_events", 0) - diag["events_loaded"])
            health.append(diag)
            continue

        if not auto_discovery:
            payload, diag = safe_fetch_json(request_url, config["name"], headers)
        diag["bookmaker"] = config["name"]
        if payload is None:
            health.append(diag)
            continue

        if key == "sportybet":
            raw_events, raw_count, adapter_detail = adapt_sportybet_payload(payload)
        elif key == "betway":
            raw_events, raw_count, adapter_detail = adapt_betway_payload(payload)
        elif key == "1xbet":
            raw_events, raw_count, adapter_detail = adapt_1xbet_payload(payload)
        elif key.startswith("global_"):
            raw_events, raw_count, adapter_detail = adapt_global_payload(payload)
        else:
            raw_events, raw_count, adapter_detail = payload.get("events"), 0, "Generic events schema"
            if isinstance(raw_events, list):
                raw_count = len(raw_events)
        if raw_events is None:
            diag.update(status="invalid schema", schema_valid=False, detail=adapter_detail)
            health.append(diag)
            continue
        if not isinstance(raw_events, list):
            diag.update(status="invalid schema", schema_valid=False, detail="Adapter returned an unsupported data structure")
            health.append(diag)
            continue
        diag["schema_valid"] = True
        diag["raw_events"] = raw_count
        diag["adapter_detail"] = adapter_detail
        if auto_discovery:
            diag["adapter_detail"] = f"Automatic SportyBet discovery attempted; {adapter_detail}. Endpoint is unofficial and may change."
        diag["last_successful_fetch"] = diag.get("last_successful_fetch") or now_iso()
        if not raw_events:
            diag.update(status="no usable markets", detail=adapter_detail)
            health.append(diag)
            continue

        rejection_reasons = {}
        newest_timestamp = None
        for raw in raw_events:
            row = normalize_feed_event(key, raw)
            if row:
                # Distinguish fresh retrieval time from provider-reported quote update time.
                # This allows live-observed odds to be evaluated without fabricating source timestamps.
                row["retrieved_at"] = diag.get("last_successful_fetch") or now_iso()
                rows.append(row)
                diag["events_loaded"] += 1
                stamp = parse_timestamp(row.get("updated_at"))
                if stamp and (newest_timestamp is None or stamp > newest_timestamp):
                    newest_timestamp = stamp
            else:
                diag["events_rejected"] += 1
                reason = "not an object" if not isinstance(raw, dict) else "missing event/market/outcomes, invalid odds, or outcome count outside 2..100"
                rejection_reasons[reason] = rejection_reasons.get(reason, 0) + 1
        diag["rejection_reasons"] = rejection_reasons
        if newest_timestamp:
            age = max(0, int((datetime.now(timezone.utc) - newest_timestamp).total_seconds()))
            diag["data_updated_at"] = newest_timestamp.isoformat()
            diag["freshness_seconds"] = age
            diag["stale"] = age > STALE_AFTER_SECONDS
        else:
            diag["stale"] = None
        if diag["events_loaded"] == 0:
            diag.update(status="no usable markets", detail=adapter_detail)
        elif diag["stale"] is True:
            diag.update(status="stale data", detail=f"Usable markets found, but newest event timestamp exceeds {STALE_AFTER_SECONDS}s or may be old")
        elif newest_timestamp is None:
            diag.update(status="usable markets; freshness unknown", detail="Usable markets found; source did not provide a parseable odds-update timestamp, so replacement eligibility uses the separately labelled retrieval time")
        else:
            diag.update(status="usable markets", detail="Source schema recognized and usable market rows parsed; source authenticity and odds accuracy are not independently verified")
        health.append(diag)
    return rows, health


def find_arbitrage(rows):
    """Find theoretical two-outcome arbitrage using distinct bookmakers per leg.

    A market is eligible only when every participating quote contains the same
    two normalized outcomes. Each outcome pair is checked across bookmaker
    quotes; we do not select a best price first and accidentally hide valid
    cross-bookmaker combinations.
    """
    groups = {}
    for row in rows:
        key = (row["sport"].lower(), row["event_key"], row["market_key"])
        groups.setdefault(key, []).append(row)

    opportunities = []
    seen = set()
    for _, market_rows in groups.items():
        outcome_sets = [
            {outcome["name_key"] for outcome in row["outcomes"]}
            for row in market_rows
        ]
        if not outcome_sets:
            continue
        outcome_names = set().union(*outcome_sets)
        if len(outcome_names) != 2 or any(names != outcome_names for names in outcome_sets):
            continue

        quotes_by_outcome = {name: [] for name in outcome_names}
        for row in market_rows:
            for outcome in row["outcomes"]:
                quotes_by_outcome[outcome["name_key"]].append({
                    "bookmaker": row["bookmaker"],
                    "bookmaker_key": row["bookmaker_key"],
                    "odds": outcome["odds"],
                    "selection": outcome["name"],
                })

        outcome_a, outcome_b = sorted(outcome_names)
        for quote_a in quotes_by_outcome[outcome_a]:
            for quote_b in quotes_by_outcome[outcome_b]:
                if quote_a["bookmaker_key"] == quote_b["bookmaker_key"]:
                    continue
                probability_sum = (1.0 / quote_a["odds"]) + (1.0 / quote_b["odds"])
                if probability_sum >= 1.0:
                    continue
                identity = (
                    market_rows[0]["sport"], market_rows[0]["event_key"],
                    market_rows[0]["market_key"], outcome_a,
                    quote_a["bookmaker_key"], quote_a["odds"],
                    quote_b["bookmaker_key"], quote_b["odds"],
                )
                if identity in seen:
                    continue
                seen.add(identity)
                legs = []
                for quote in (quote_a, quote_b):
                    leg = dict(quote)
                    leg["stake_fraction"] = (1.0 / leg["odds"]) / probability_sum
                    legs.append(leg)
                opportunities.append({
                    "sport": market_rows[0]["sport"],
                    "event": market_rows[0]["event"],
                    "market": market_rows[0]["market"],
                    "implied_probability_sum": probability_sum * 100.0,
                    "theoretical_margin_percent": (1.0 - probability_sum) * 100.0,
                    "legs": legs,
                    "warning": (
                        "Theoretical only. Confirm the exact event, market, line, "
                        "settlement rules, odds freshness, limits, fees, and that "
                        "both outcomes are exhaustive and mutually exclusive."
                    ),
                })

    opportunities.sort(key=lambda item: item["theoretical_margin_percent"], reverse=True)
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
                "evidence": (
                    "Estimate supplied by configured feed; not independently "
                    "validated by SolveEase."
                ),
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
                "note": (
                    "Candidate only. Confirm exact line, market rules, and "
                    "source freshness before considering use."
                ),
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
        "usable_two_outcome_markets": len(rows),
        "arbitrage_count": len(arbitrage),
        "arbitrage": arbitrage,
        "probability_filter_threshold": MIN_PROBABILITY,
        "qualified_selections_count": len(probabilities),
        "qualified_selections": probabilities,
        "alternative_markets_count": len(alternatives),
        "alternative_markets": alternatives,
        # A bounded catalog lets the mobile UI select real markets from the latest scan.
        # Goal rates remain explicit user inputs; the scanner does not invent team statistics.
        "available_markets_count": len(rows),
        "available_markets": [
            {
                "sport": row["sport"], "event": row["event"], "market": row["market"],
                "bookmaker": row["bookmaker"], "updated_at": row.get("updated_at"),
                "outcomes": [{"name": outcome["name"], "odds": outcome["odds"]} for outcome in row["outcomes"]],
            }
            for row in rows[:250]
        ],
        "available_markets_truncated": len(rows) > 250,
        "notes": [
            "No opportunity is displayed unless configured feed data qualifies.",
            "A feed-provided probability estimate is not a guarantee.",
            "Both outcomes must be exhaustive and use identical settlement rules.",
            "Unavailable feeds do not count as a negative arbitrage result.",
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
:root{color-scheme:dark;--bg:#080f1c;--panel:#101b2d;--panel2:#16243a;--text:#edf3ff;--muted:#9aabc4;--line:#263750;--accent:#71e0b4;--danger:#ff8585;--warning:#ffd479;--blue:#8db8ff}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:16px/1.5 system-ui,-apple-system,sans-serif}
main{width:min(1180px,100%);margin:auto;padding:22px 16px 48px}
header{margin:8px 0 18px;padding:8px 2px}.brand{color:var(--accent);font-weight:850;letter-spacing:1.5px;font-size:12px}
h1{font-size:clamp(27px,5vw,40px);line-height:1.15;margin:8px 0}
h2{font-size:21px;margin:0 0 10px}h3{font-size:16px;margin:0 0 6px}
p{color:var(--muted)}.panel{background:linear-gradient(180deg,rgba(19,32,52,.98),rgba(15,26,43,.98));border:1px solid var(--line);border-radius:16px;padding:18px;margin:14px 0;box-shadow:0 8px 26px rgba(0,0,0,.12)}
label{display:block;font-weight:650;margin:10px 0 6px}
input,select{width:100%;min-height:48px;padding:12px;border-radius:10px;border:1px solid var(--line);background:#09101d;color:var(--text);font-size:16px}
button{min-height:46px;padding:11px 16px;border:0;border-radius:10px;background:var(--accent);color:#071b15;font-weight:800;font-size:14px;cursor:pointer;transition:transform .15s ease,filter .15s ease}button:hover{filter:brightness(1.05);transform:translateY(-1px)}button:focus-visible,a:focus-visible,input:focus-visible,select:focus-visible,textarea:focus-visible{outline:2px solid var(--blue);outline-offset:2px}
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
.nav{display:flex;gap:7px;flex-wrap:wrap;margin:12px 0 22px;padding:7px;background:#0d1727;border:1px solid var(--line);border-radius:14px;position:sticky;top:6px;z-index:5;backdrop-filter:blur(12px)}
.nav a,.feature-link{display:inline-flex;align-items:center;justify-content:center;text-decoration:none;color:var(--text);background:var(--panel2);border:1px solid var(--line);border-radius:10px;padding:10px 13px;font-weight:750;font-size:14px;min-height:42px}
.nav a[aria-current="page"]{background:var(--accent);color:#071b15;border-color:var(--accent)}
.hero{padding:clamp(20px,4vw,34px);background:radial-gradient(circle at 90% 10%,rgba(113,224,180,.12),transparent 34%),linear-gradient(135deg,#172b43,#101827);border:1px solid var(--line);border-radius:18px;margin:14px 0 18px}
.hero p{max-width:680px}.feature-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:12px}
.feature-card{display:flex;flex-direction:column;gap:8px;padding:17px;background:var(--panel);border:1px solid var(--line);border-radius:15px;min-height:180px}
.feature-card p{margin:0;flex:1}.feature-card h2{font-size:18px}.feature-link{align-self:flex-start;background:var(--accent);color:#071b15}
.dashboard-hero{margin-top:10px}.dashboard-tools{grid-template-columns:repeat(3,minmax(0,1fr));gap:16px}.dashboard-button{position:relative;min-height:250px;text-decoration:none;color:var(--text);transition:border-color .15s ease,transform .15s ease,box-shadow .15s ease}.dashboard-button:hover{border-color:var(--accent);transform:translateY(-3px);box-shadow:0 14px 34px rgba(0,0,0,.2)}.dashboard-button .feature-link{display:inline-flex;gap:12px;align-self:stretch;justify-content:space-between;margin-top:12px}.tool-number{position:absolute;right:15px;top:12px;color:var(--muted);font-size:12px;font-weight:800;letter-spacing:.12em}.tool-icon{display:grid;place-items:center;width:48px;height:48px;border-radius:13px;background:rgba(113,224,180,.12);border:1px solid rgba(113,224,180,.25);color:var(--accent);font-size:28px;font-weight:700}.dashboard-status{margin-top:18px}.nav{display:none}.nav.nav-visible{display:flex}
@media(max-width:720px){.dashboard-tools{grid-template-columns:1fr}.dashboard-button{min-height:200px}.dashboard-hero h1{font-size:30px}}
.page-section{display:none}.page-section:not(.page-hidden){display:block;animation:pageIn .18s ease-out}.page-hidden{display:none!important}.eyebrow{font-size:11px;letter-spacing:.1em;text-transform:uppercase;color:var(--accent);font-weight:800}.page-title{display:flex;align-items:flex-start;justify-content:space-between;gap:12px;flex-wrap:wrap}.page-title p{max-width:760px;margin-top:6px}.compact-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:10px}.summary-card{background:var(--panel2);border:1px solid var(--line);border-radius:12px;padding:14px}.summary-card strong{display:block;font-size:18px}.summary-card span{color:var(--muted);font-size:12px}.subnav{display:flex;flex-wrap:wrap;gap:8px;margin:12px 0}.subnav a{color:var(--blue);font-size:13px;text-decoration:none;font-weight:750}.report-details{margin-top:12px;border:1px solid var(--line);border-radius:10px;padding:11px 13px;background:rgba(9,16,29,.45)}.report-details summary{cursor:pointer;font-weight:750;color:var(--blue)}@keyframes pageIn{from{opacity:.7;transform:translateY(3px)}to{opacity:1;transform:translateY(0)}}
@media(min-width:760px){.two-col{display:grid;grid-template-columns:1fr 1fr;gap:14px}.two-col .panel{margin:0}}

/* v3.8.2 compact booking + filtering workspace */
.lookup-workspace{padding:clamp(14px,2.4vw,24px)}
.lookup-entry{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:10px;align-items:end}
.lookup-entry label{margin:0 0 6px;display:block}
.lookup-entry input{width:100%;min-width:0}
.lookup-entry .actions{margin:0;display:flex;gap:8px;flex-wrap:wrap}
.lookup-entry .actions button{white-space:nowrap}
.lookup-summary{display:grid;grid-template-columns:repeat(auto-fit,minmax(125px,1fr));gap:8px;margin:12px 0}
.lookup-summary .stat{padding:10px 12px;min-width:0}
.lookup-summary .stat strong{font-size:16px;overflow-wrap:anywhere}
.lookup-details{margin:10px 0;border:1px solid var(--line);border-radius:10px;padding:10px 12px;background:var(--panel2)}
.lookup-details>summary{cursor:pointer;font-weight:750;display:flex;align-items:center;justify-content:space-between;gap:10px;list-style:none}
.lookup-details>summary::-webkit-details-marker{display:none}
.lookup-details>summary:after{content:'+';font-size:18px;color:var(--blue)}
.lookup-details[open]>summary:after{content:'−'}
.lookup-details-body{padding-top:10px}
.filter-toolbar{display:grid;grid-template-columns:minmax(160px,240px) minmax(0,1fr);gap:12px;align-items:end;margin:12px 0}
.filter-toolbar label{margin:0}
.filter-toolbar select{width:100%}
.filter-summary{font-weight:750;padding:9px 11px;border-radius:9px;background:var(--panel2);margin:8px 0}
.filtered-leg{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:8px;align-items:center;padding:10px 12px;margin:7px 0;border:1px solid var(--line);border-radius:9px;background:var(--panel2)}
.filtered-leg strong{display:block;overflow-wrap:anywhere}
.filtered-leg p{margin:3px 0 0;font-size:13px;color:var(--muted)}
.filtered-leg .leg-metric{font-weight:800;white-space:nowrap;font-variant-numeric:tabular-nums}
.lookup-filter-panel{margin-top:12px}
.lookup-filter-panel>h3{margin-bottom:5px}
.lookup-filter-panel>.small{max-width:850px}
.lookup-action{position:sticky;bottom:8px;z-index:3;display:flex;align-items:center;gap:10px;flex-wrap:wrap;padding:10px;margin:10px 0;border:1px solid var(--line);border-radius:12px;background:var(--panel);box-shadow:0 5px 22px rgba(0,0,0,.18)}
.lookup-action p{margin:0;flex:1;min-width:160px}
.lookup-action button{min-height:44px}
.lookup-advanced{margin-top:10px}
.lookup-advanced>summary{cursor:pointer;font-weight:750;padding:10px;border:1px solid var(--line);border-radius:9px;background:var(--panel2)}
.lookup-advanced[open]>summary{margin-bottom:10px}
@media(max-width:600px){.lookup-entry{grid-template-columns:1fr}.lookup-entry .actions button{flex:1}.filter-toolbar{grid-template-columns:1fr}.filtered-leg{grid-template-columns:minmax(0,1fr) auto}.lookup-action{position:static}.lookup-summary{grid-template-columns:repeat(2,minmax(0,1fr))}}

</style>
</head>
<body>
<main>
<header>
  <div class="brand">SOLVEEASE · BOOKING TOOLS</div>
  <h1>Booking & Arbitrage Dashboard</h1>
  <p>One workspace per task: retrieve a ticket, compare markets, review concise recommendations, and check accumulator maths.</p>
</header>
<nav class="nav" id="toolNav" aria-label="Main navigation">
  <a href="/lookup" data-page="lookup">Booking code & filter</a>
  <a href="/converter" data-page="converter">Ticket converter</a>
  <a href="/scanner" data-page="scanner">Arbitrage scanner</a>
</nav>

<section id="homePage" class="page-section">
  <div class="hero dashboard-hero">
    <div class="eyebrow">SOLVEEASE · CONTROL CENTRE</div>
    <h1>One dashboard. Three tools.</h1>
    <p>Choose what you need. Review filtered selections before using the reconstruction checklist. This service does not place bets or manufacture bookmaker booking codes.</p>
  </div>
  <div class="feature-grid dashboard-tools">
    <a class="feature-card dashboard-button" href="/lookup"><span class="tool-number">01</span><span class="tool-icon">⌕</span><h2>Booking Code & Filter</h2><p>Look up a supported ticket, analyze its selections and apply the probability filter.</p><span class="feature-link">Open tool <span aria-hidden="true">→</span></span></a>
    <a class="feature-card dashboard-button" href="/converter"><span class="tool-number">02</span><span class="tool-icon">⇄</span><h2>Ticket Converter</h2><p>Prepare a clean reconstruction checklist using only the selections you retained.</p><span class="feature-link">Open tool <span aria-hidden="true">→</span></span></a>
    <a class="feature-card dashboard-button" href="/scanner"><span class="tool-number">03</span><span class="tool-icon">↗</span><h2>Arbitrage Scanner</h2><p>Scan supported bookmaker feeds and inspect available price differences.</p><span class="feature-link">Open tool <span aria-hidden="true">→</span></span></a>
  </div>
  <section class="panel dashboard-status"><h2>Service status</h2><div id="homeHealth">Checking service…</div><p class="small">Online status does not guarantee that odds feeds are available, fresh or accurate.</p></section>
</section>

<section class="panel page-section lookup-workspace" id="lookupPanel">
  <div class="page-title"><div><div class="eyebrow">Tool 01 · Ticket intelligence</div><h2>Booking code & filter</h2><p>Look up once, review the compact summary, then retain only the selections that pass your chosen screen.</p></div></div>
  <div class="lookup-entry"><div><label for="code">Booking code</label><input id="code" maxlength="120" autocomplete="off" autocapitalize="none" spellcheck="false" placeholder="Enter booking code"></div>
  <div class="actions"><button id="lookupBtn" onclick="lookup()">Look up & filter</button><button class="secondary" onclick="clearLookup()">Clear</button></div></div>
  <p class="small">Currently supported: SportyBet read-only lookup. Bet9ja and BetKing are not confirmed from code format alone and do not yet have lookup adapters.</p>
  <div id="lookupMessage" role="status" aria-live="polite"></div>
  <div id="ticketResult"></div>
</section>

<section class="panel page-section" id="converterPanel">
  <h2>2. Bookmaker ticket converter</h2>
  <p>Convert either a retrieved ticket or manually supplied ticket JSON into a copyable reconstruction checklist. This tool is independent of the prediction and arbitrage tools.</p>
  <label for="manualTicketJson">Optional manual ticket JSON (use if you do not have a supported booking lookup)</label>
  <textarea id="manualTicketJson" rows="6" placeholder='{"bookmaker":"Unknown source","legs":[{"event":"Home FC vs Away FC","market":"Double Chance","selection":"Home/Draw","odds":1.35}]}'></textarea>
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
  <p class="small notice">Manual reconstruction only. This does not generate a destination booking code or prove that equivalent markets are available. Verify each event, market, selection, line and current price yourself.</p>
</section>

<section class="panel page-section" id="scannerPanel">
  <div class="page-title"><div><div class="eyebrow">Market intelligence</div><h2>Market scanner</h2><p>Scan available odds feeds and inspect usable markets. Detailed recommendations and accumulator maths have their own workspaces.</p></div></div>
  <div class="subnav"><a href="/recommendations">Go to recommendations →</a><a href="/accumulator">Open accumulator calculator →</a><a href="/status">Check feed health →</a></div>
  <p>Scans only the permitted JSON odds feeds configured on the server. Missing feeds are reported as unavailable, not as proof that no opportunity exists.</p>
  <div class="actions">
    <button id="scanBtn" onclick="scan()">Scan bookmakers now</button>
    <button class="secondary" onclick="loadHealth()">Refresh service status</button>
  </div>
  <div id="scanMessage" role="status" aria-live="polite"></div>
  <div id="sourceHealth"></div>
  <div id="scanSummary" class="grid"></div>
  <section class="panel" aria-labelledby="marketCatalogTitle">
    <h3 id="marketCatalogTitle">Markets from the latest scan</h3>
    <p class="small">Choose an outcome from the latest usable feed markets. Analysis can automatically fetch recent results and estimate goal rates where team matching and data coverage are sufficient.</p>
    <div id="marketCatalogNote" class="small" role="status" aria-live="polite">Run a scan to load available markets.</div>
    <div id="marketCatalog"></div>
  </section>
  <h3>Arbitrage opportunities</h3><div id="arbitrage"></div>
  <h3>Probability filter</h3>
  <p class="small">Default threshold: 70%. Uses estimates supplied by the configured feed, not odds-implied probabilities; feed estimates are not independently calibrated.</p>
  <div id="probabilities"></div>
  <h3>Preferred alternative markets</h3>
  <p class="small">Priorities include Over/Under, Double Chance and Corners where supported by feed data.</p>
  <div id="alternatives"></div>
</section>

<section class="panel page-section page-hidden" id="recommendationsPanel">
  <div class="eyebrow">Decision support</div><h2>Market recommendations</h2>
  <p>Review candidate markets for selected fixtures. The report is intentionally concise; open the supporting details only when needed. Recommendations require usable odds and sufficient historical data.</p>
  <div class="subnav"><a href="/lookup">← Booking lookup</a><a href="/scanner">Scan bookmaker markets →</a><a href="/accumulator">Accumulator calculator →</a></div>
  <section class="panel" aria-labelledby="betslipAnalysisTitle">
    <h3 id="betslipAnalysisTitle">Selected betslip market analysis</h3>
    <p class="small">Paste selected legs as JSON. Goal-rate fields are optional: the engine tries automatic historical team-stat lookup when they are omitted. Supported markets: 1X2, double chance, totals with a line, and both-teams-to-score.</p>
    <label for="betslipJson">Selected games and markets (JSON)</label>
    <textarea id="betslipJson" rows="10" maxlength="16000" spellcheck="false">{
  "legs": [
    {"event":"Arsenal vs Chelsea","market":"1X2","selection":"Home","odds":2.10},
    {"event":"Arsenal vs Chelsea","market":"Total Goals 2.5","selection":"Over 2.5","odds":1.90}
  ]
}</textarea>
    <div class="actions"><button type="button" id="analyzeBetslipBtn" onclick="analyzeBetslip()">Analyze selected markets</button><button type="button" class="secondary" onclick="clearBetslipAnalysis()">Clear analysis</button></div>
    <div id="betslipAnalysisMessage" role="status" aria-live="polite"></div>
    <div id="betslipAnalysisReport"></div>
    <p class="small notice">Summary first: compare recommended market, estimated probability, odds and data quality. Model estimates are not verified forecasts; confirm settlement rules and live prices independently.</p>
  </section>
</section>

<section class="panel page-section page-hidden" id="accumulatorPanel">
  <div class="eyebrow">Ticket maths</div><h2>Accumulator calculator</h2>
  <p>Check combined odds and potential returns. This calculator does not estimate the probability that the full accumulator wins.</p>
  <div class="subnav"><a href="/recommendations">← Review recommendations</a><a href="/scanner">Scan markets →</a></div>
  <section class="panel" aria-labelledby="ticketMathAuditTitle">
    <h3 id="ticketMathAuditTitle">Booking slip arithmetic audit</h3>
    <p class="small">Paste all selections and the bookmaker-reported total odds, stake and payout. Arithmetic only; this does not verify the booking code or special-market settlement.</p>
    <label for="ticketMathAuditJson">Ticket audit JSON</label>
    <textarea id="ticketMathAuditJson" rows="7" maxlength="16000" spellcheck="false">
{
  "reported_total_odds": 29043.88,
  "stake": 100,
  "reported_bonus": 2040363.47,
  "reported_payout": 4365712.15,
  "legs": [
    {
      "event": "Liverpool vs Villarreal",
      "market": "Over 2.5 (Early Goals: 2 goals by 30 min)",
      "selection": "Over 2.5",
      "odds": 1.42
    },
    {
      "event": "Man City vs AEK Athens",
      "market": "Over 2.5",
      "selection": "Over 2.5",
      "odds": 1.28
    },
    {
      "event": "Napoli vs Besiktas/Gent",
      "market": "Over 2.5 (Early Goals: 2 goals by 30 min)",
      "selection": "Over 2.5",
      "odds": 1.43
    },
    {
      "event": "Porto vs PSV/Eindhoven",
      "market": "Over 2.5",
      "selection": "Over 2.5",
      "odds": 1.3
    },
    {
      "event": "PSG vs Barcelona",
      "market": "Over 3.5 (Early Goals: 2 goals by 30 min)",
      "selection": "Over 3.5",
      "odds": 1.45
    },
    {
      "event": "Roma vs Slovan Bratislava",
      "market": "Over 2.5",
      "selection": "Over 2.5",
      "odds": 1.28
    },
    {
      "event": "Genoa vs Man Utd",
      "market": "Yes (Home Team Over 0.5)",
      "selection": "Yes",
      "odds": 1.31
    },
    {
      "event": "Lille vs Galatasaray Istanbul",
      "market": "Yes (GG/NG)",
      "selection": "Yes",
      "odds": 1.55
    },
    {
      "event": "Aston Villa vs Viking FK",
      "market": "Over 2.5",
      "selection": "Over 2.5",
      "odds": 1.3
    },
    {
      "event": "Bayern Munich vs Arsenal",
      "market": "Yes (GG/NG)",
      "selection": "Yes",
      "odds": 1.55
    },
    {
      "event": "Real Betis vs Feyenoord",
      "market": "Home (1X2 - 1UP)",
      "selection": "Home",
      "odds": 1.2
    },
    {
      "event": "Inter vs Shakhtar Donetsk",
      "market": "Over 2.5",
      "selection": "Over 2.5",
      "odds": 1.29
    },
    {
      "event": "Real Madrid vs RB Leipzig",
      "market": "Over 3.5 (Early Goals: 1 goal by 30 min)",
      "selection": "Over 3.5",
      "odds": 1.51
    },
    {
      "event": "Sporting vs LASK Linz",
      "market": "Over 1.5 (Sporting Over/Under)",
      "selection": "Over 1.5",
      "odds": 1.23
    },
    {
      "event": "FK Jablonec vs Brighton",
      "market": "Away (1X2 - 2UP)",
      "selection": "Away",
      "odds": 1.28
    },
    {
      "event": "Ajax vs Atalanta",
      "market": "Yes (GG/NG)",
      "selection": "Yes",
      "odds": 1.15
    },
    {
      "event": "Ferencvárosi Budapest vs SCU Torreense",
      "market": "Home (1X2 - 1UP)",
      "selection": "Home",
      "odds": 1.3
    },
    {
      "event": "GNK Dinamo Zagreb vs NEC Nijmegen",
      "market": "Home (1X2 - 1UP)",
      "selection": "Home",
      "odds": 1.32
    },
    {
      "event": "Lech Poznań vs Sunderland AFC",
      "market": "Away (1X2 - 2UP)",
      "selection": "Away",
      "odds": 1.36
    },
    {
      "event": "VfB Greuther Fürth vs Bayer Leverkusen",
      "market": "Over 2.5",
      "selection": "Over 2.5",
      "odds": 1.36
    },
    {
      "event": "Union Saint-Gilloise vs Hapoel Be'er Sheva FC",
      "market": "Home (1X2 - 1UP)",
      "selection": "Home",
      "odds": 1.3
    },
    {
      "event": "Hoffenheim vs Lyon",
      "market": "Yes (GG/NG)",
      "selection": "Yes",
      "odds": 1.49
    },
    {
      "event": "Lillestrøm SK vs Real Sociedad",
      "market": "Away (1X2 - 2UP)",
      "selection": "Away",
      "odds": 1.26
    },
    {
      "event": "Olympiacos vs Sparta Prague",
      "market": "Home (1X2 - 1UP)",
      "selection": "Home",
      "odds": 1.29
    },
    {
      "event": "Al Fayha FC vs Al Qadisiya",
      "market": "Away (1X2 - 2UP)",
      "selection": "Away",
      "odds": 1.23
    },
    {
      "event": "Al Nassr Club vs Neom SC",
      "market": "Home (1X2 - 1UP)",
      "selection": "Home",
      "odds": 1.2
    },
    {
      "event": "Al Ahly SC (EGY) vs Al Qarah FC",
      "market": "Home (1X2 - 2UP)",
      "selection": "Home",
      "odds": 1.24
    },
    {
      "event": "Sturm Graz vs Marseille",
      "market": "Over 1.5 (Early Goals: 1 goal by 10 min)",
      "selection": "Over 1.5",
      "odds": 1.19
    },
    {
      "event": "Wolves vs Portsmouth FC",
      "market": "Over 1.5 (Early Goals: 1 goal by 10 min)",
      "selection": "Over 1.5",
      "odds": 1.22
    },
    {
      "event": "Beşiktaş Istanbul vs Crystal Palace",
      "market": "Over 2.5 (Early Goals: 2 goals by 30 min)",
      "selection": "Over 2.5",
      "odds": 1.52
    },
    {
      "event": "Juventus vs Rennes",
      "market": "Over 1.5 (Early Goals: 1 goal by 10 min)",
      "selection": "Over 1.5",
      "odds": 1.2
    },
    {
      "event": "Fenerbahçe Istanbul vs Slavia Prague",
      "market": "Home (1X2 - 1UP)",
      "selection": "Home",
      "odds": 1.3
    },
    {
      "event": "Club Brugge vs Lens",
      "market": "Over 2.5 (Early Goals: 2 goals by 30 min)",
      "selection": "Over 2.5",
      "odds": 1.46
    },
    {
      "event": "Crvena Zvezda vs Copenhagen",
      "market": "Over 1.5 (Early Goals: 1 goal by 10 min)",
      "selection": "Over 1.5",
      "odds": 1.29
    },
    {
      "event": "Bournemouth vs AC Milan",
      "market": "Over 1.5 (Early Goals: 1 goal by 10 min)",
      "selection": "Over 1.5",
      "odds": 1.25
    },
    {
      "event": "Motherwell vs Heart of Midlothian FC",
      "market": "Home (1X2 - 1UP)",
      "selection": "Home",
      "odds": 1.19
    }
  ]
}
    </textarea>
    <div class="actions"><button type="button" onclick="auditTicketMath()">Audit reported ticket maths</button><button type="button" class="secondary" onclick="$('ticketMathAuditReport').textContent='No audit run yet.'">Clear report</button></div>
    <div id="ticketMathAuditMessage" role="status" aria-live="polite"></div>
    <pre id="ticketMathAuditReport" class="item">No audit run yet.</pre>
  </section>
  <section class="panel" aria-labelledby="accumulatorTitle">
    <h3 id="accumulatorTitle">Accumulator calculator & report</h3>
    <p class="small">Enter one selection per line as <code>Selection label | decimal odds</code>. This calculates the ticket maths only; it does not estimate whether the selections will win.</p>
    <label for="accumulatorLegs">Selections and decimal odds</label>
    <textarea id="accumulatorLegs" rows="5" maxlength="6000" placeholder="Team A to win | 1.80&#10;Over 1.5 goals | 1.35&#10;Team C or draw | 1.50"></textarea>
    <label for="accumulatorStake">Stake amount</label>
    <input id="accumulatorStake" type="number" min="0.01" max="1000000000" step="0.01" value="1000" inputmode="decimal">
    <div class="actions"><button type="button" onclick="analyzeAccumulator()">Calculate accumulator</button><button type="button" class="secondary" onclick="clearAccumulator()">Clear</button></div>
    <div id="accumulatorMessage" role="status" aria-live="polite"></div>
    <div id="accumulatorReport"></div>
  </section>
</section>

<section class="panel page-section" id="statusPanel">
  <div class="eyebrow">Monitoring</div><h2>Feed and service status</h2>
  <div id="health">Checking service…</div>
  <h3>Configured bookmaker feeds</h3><div id="statusFeedHealth"></div>
  <p class="small">Read-only tools. No automatic bet placement and no bookmaker login credentials requested. Odds and availability can change rapidly.</p>
</section>
<section class="panel page-section page-hidden" id="dailyPanel">
  <div class="eyebrow">Daily fixtures</div><h2>Daily accumulator builder</h2>
  <p>Use a configured HTTPS fixture feed, or paste fixture JSON below. Each fixture uses <code>event</code>, <code>start_time</code> (ISO-8601), and a <code>selections</code> array with estimated_probability and evidence_quality. Only upcoming fixtures in the selected local date are considered.</p>
  <label for="dailyDate">Fixture date (local)</label><input id="dailyDate" placeholder="YYYY-MM-DD (blank = today)">
  <label for="dailyFixtures">Fixture JSON (optional when FIXTURES_FEED_URL is configured)</label><textarea id="dailyFixtures" rows="8" placeholder='{"fixtures":[{"event":"Home FC vs Away FC","start_time":"2026-10-19T20:00:00+01:00","selections":[{"market":"Double Chance","selection":"Home/Draw","odds":1.35,"estimated_probability":0.74,"evidence_quality":"adequate","reasoning":"Model plus current verified stats"}]}]}'></textarea>
  <div class="actions"><button onclick="runDailyBuilder()">Build today’s accumulator</button><button class="secondary" onclick="sendDailyTelegram()">Send report to Telegram</button></div><div id="dailyMessage" role="status" aria-live="polite"></div><pre id="dailyReport" class="item">No daily scan run yet.</pre>
  <p class="small notice">The threshold is fixed at 70%; target 20 and maximum 40. If fewer qualify, the report shows the shortfall. No automatic bets are placed. Probability estimates are not guarantees.</p>
</section>
<section class="panel page-section page-hidden" id="optimizePanel">
  <div class="eyebrow">Same-fixture replacement</div><h2>Booking lookup & intelligence optimizer</h2>
  <p>Paste a JSON object with a <code>legs</code> array. Each leg can include an <code>original</code> selection and same-fixture <code>candidates</code>. A candidate must have estimated_probability ≥ 0.70 and adequate/high/verified evidence quality or calibrated=true.</p>
  <label for="optimizerJson">Ticket analysis JSON</label><textarea id="optimizerJson" rows="9" placeholder='{"legs":[{"original":{"event":"Home FC vs Away FC","market":"1X2","selection":"Away","odds":2.6,"estimated_probability":0.48,"evidence_quality":"adequate"},"candidates":[{"event":"Home FC vs Away FC","market":"Double Chance","selection":"Home/Draw","odds":1.4,"estimated_probability":0.74,"evidence_quality":"adequate","reasoning":"Recent and longer form agree"}]}]}'></textarea>
  <div class="actions"><button onclick="runOptimizer()">Analyze and optimize</button><button class="secondary" onclick="runPsychologyCheck()">Check decision biases</button></div><div id="optimizerMessage" role="status" aria-live="polite"></div><pre id="optimizerReport" class="item">No optimization run yet.</pre>
  <p class="small notice">Global JSON feeds may use SolveEase's normalized schema or a supported bookmaker/markets/outcomes schema. Only genuinely supplied candidates are considered. “No qualifying alternative found” is an allowed result. This tool does not generate booking codes or place bets.</p>
</section>
<section class="panel page-section page-hidden" id="alertsPanel">
  <div class="eyebrow">Monitoring</div><h2>Professional Telegram alerts</h2>
  <p>Configure <code>TELEGRAM_BOT_TOKEN</code> and <code>TELEGRAM_CHAT_ID</code> in Render’s private environment variables. The bot token is never requested in this page or printed to logs.</p>
  <div class="actions"><button onclick="testTelegram()">Send test alert</button><button class="secondary" onclick="checkTelegramStatus()">Check configuration status</button></div><div id="telegramMessage" role="status" aria-live="polite"></div><pre id="telegramReport" class="item">Telegram delivery has not been tested in this process.</pre>
  <p class="small notice">A configured token does not prove delivery. Activation is confirmed only after a successful test message is received in the intended Telegram chat.</p>
</section>
<footer>SolveEase · Mobile-friendly · Manual confirmation required · No automatic bet placement</footer>
</main>
<script>
"use strict";
const $ = id => document.getElementById(id);
let lastLookupData = null;
let preparedTicketText = "";

// Keep in-progress workspace inputs when the user follows a dedicated page link.
// Values are session-only and stay in this browser tab.
function persistWorkspace() {
  try {
    ["betslipJson", "accumulatorLegs", "accumulatorStake"].forEach(id => {
      const node = $(id);
      if (node) sessionStorage.setItem("solveease:workspace:" + id, node.value);
    });
  } catch (_) { /* Storage can be disabled; the app remains usable. */ }
}
function restoreWorkspace() {
  try {
    ["betslipJson", "accumulatorLegs", "accumulatorStake"].forEach(id => {
      const node = $(id);
      const saved = sessionStorage.getItem("solveease:workspace:" + id);
      if (node && saved !== null) node.value = saved;
    });
  } catch (_) { /* Storage can be disabled; the app remains usable. */ }
}
function activatePage() {
  restoreWorkspace();
  const raw = location.pathname.replace(/\/$/, "") || "/";
  const page = ({"/":"home","/lookup":"lookup","/converter":"converter","/scanner":"scanner","/recommendations":"recommendations","/accumulator":"accumulator","/status":"status","/daily":"daily","/optimize":"optimize","/alerts":"alerts"})[raw];
  if (!page) { location.replace("/"); return; }
  const map = {home:"homePage",lookup:"lookupPanel",converter:"converterPanel",scanner:"scannerPanel",recommendations:"recommendationsPanel",accumulator:"accumulatorPanel",status:"statusPanel",daily:"dailyPanel",optimize:"optimizePanel",alerts:"alertsPanel"};
  Object.entries(map).forEach(([key,id]) => $(id).classList.toggle("page-hidden", key !== page));
  const toolNav = $("toolNav");
  if (toolNav) toolNav.classList.toggle("nav-visible", page !== "home");
  document.querySelectorAll(".nav a").forEach(a => {
    if (a.dataset.page === page) a.setAttribute("aria-current","page");
    else a.removeAttribute("aria-current");
  });
  const titles = {home:"Home",lookup:"Booking-code lookup",converter:"Ticket converter",scanner:"Market scanner",recommendations:"Market recommendations",accumulator:"Accumulator calculator",status:"Feed and service status",daily:"Daily accumulator builder",optimize:"Booking optimizer",alerts:"Telegram alerts"};
  document.title = "SolveEase | " + titles[page];
  if (page === "home" || page === "status") loadHealth();
  if (page === "converter") restoreLookup();
}
function restoreLookup() {
  try { lastLookupData = JSON.parse(sessionStorage.getItem("solveease:lastLookup") || "null"); }
  catch (_) { lastLookupData = null; }
  // Only restore filtered output. Remove legacy session data that may contain an unfiltered ticket.
  if (lastLookupData && (lastLookupData.ok !== true || lastLookupData.filtered_ticket !== true || !Array.isArray(lastLookupData.legs))) {
    lastLookupData = null;
    try { sessionStorage.removeItem("solveease:lastLookup"); } catch (_) {}
  }
  if (lastLookupData && lastLookupData.ok === true && lastLookupData.filtered_ticket === true) {
    $("converterMessage").textContent = "";
    $("converterMessage").className = "";
  }
}

function el(tag, text, parent, cls) {
  const node = document.createElement(tag);
  node.textContent = String(text ?? "");
  if (cls) node.className = cls;
  parent.appendChild(node);
  return node;
}
function clear(id) { $(id).replaceChildren(); }
function clearAccumulator() {
  $("accumulatorLegs").value = "";
  $("accumulatorStake").value = "1000";
  $("accumulatorMessage").textContent = "";
  $("accumulatorMessage").className = "";
  clear("accumulatorReport");
}
async function auditTicketMath(){
  const message=$("ticketMathAuditMessage"), report=$("ticketMathAuditReport");
  try {
    const payload=JSON.parse($("ticketMathAuditJson").value);
    const data=await api("/api/audit-ticket-math","POST",payload);
    report.textContent=JSON.stringify(data,null,2);
    message.textContent=data.audit_status ? ("Audit status: "+data.audit_status+". Check the reported differences and caveats.") : (data.error||"Audit completed.");
    message.className=data.audit_status==="ARITHMETICALLY_CONSISTENT"?"success":"warning";
  } catch(e) { message.textContent=e.message; message.className="error"; }
}
function analyzeAccumulator() {
  const raw = $("accumulatorLegs").value.trim();
  const stake = Number($("accumulatorStake").value);
  const message = $("accumulatorMessage");
  const report = $("accumulatorReport");
  clear("accumulatorReport");
  message.className = "";
  if (!raw) {
    message.textContent = "Add at least one selection and decimal odd.";
    message.className = "warning";
    return;
  }
  if (!Number.isFinite(stake) || stake <= 0 || stake > 1000000000) {
    message.textContent = "Enter a valid positive stake up to 1,000,000,000.";
    message.className = "error";
    return;
  }
  const lines = raw.split(/\r?\n/).map(line => line.trim()).filter(Boolean);
  if (lines.length > 30) {
    message.textContent = "Maximum 30 selections per accumulator report.";
    message.className = "error";
    return;
  }
  const legs = [];
  for (let i=0; i<lines.length; i++) {
    const parts = lines[i].split("|");
    const label = (parts.shift() || "").trim();
    const oddText = parts.join("|").trim();
    const odds = Number(oddText);
    if (!label || label.length > 180 || !oddText || !Number.isFinite(odds) || odds <= 1 || odds > 1000) {
      message.textContent = "Line " + (i+1) + " is invalid. Use Selection label | decimal odds greater than 1 and no more than 1000.";
      message.className = "error";
      return;
    }
    legs.push({label, odds});
  }
  const combined = legs.reduce((product, leg) => product * leg.odds, 1);
  if (!Number.isFinite(combined) || combined <= 1) {
    message.textContent = "Combined odds exceed the calculator's safe numeric range.";
    message.className = "error";
    return;
  }
  const gross = stake * combined;
  const net = gross - stake;
  const implied = 100 / combined;
  const statGrid = el("div", "", report, "grid");
  [["Selections", String(legs.length)], ["Combined decimal odds", combined.toLocaleString("en-US", {maximumFractionDigits:4})], ["Raw implied probability", implied.toFixed(4) + "%"], ["Potential gross return", money(gross)], ["Potential net profit", money(net)]].forEach(([label,value]) => {
    const card = el("div", "", statGrid, "stat"); el("span", label, card); el("strong", value, card);
  });
  const list = el("div", "", report, "item");
  el("strong", "Leg-by-leg audit", list);
  legs.forEach((leg, index) => el("p", (index+1) + ". " + leg.label + " — odds " + leg.odds.toLocaleString("en-US", {maximumFractionDigits:4}) + " — individual raw implied probability " + (100/leg.odds).toFixed(2) + "%", list));
  el("p", "These are arithmetic calculations from the odds you entered. The combined implied probability is not a calibrated forecast and does not account for bookmaker margin, correlation between legs, void rules, limits, price changes or settlement disputes. The stake can be lost in full.", report, "small notice");
  message.textContent = "Calculation complete. No prediction or bet placement was performed.";
  message.className = "success";
}
function money(value, digits=2) {
  if (value === null || value === undefined || value === "" || !Number.isFinite(Number(value))) return "Unavailable";
  return Number(value).toLocaleString("en-US", {minimumFractionDigits:digits,maximumFractionDigits:digits});
}
async function api(path, method="GET", body=null) {
  const options = {method, headers:{"Accept":"application/json"}};
  if (body !== null) {
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(body);
  }
  let response;
  try {
    response = await fetch(path, options);
  } catch (err) {
    // Identify the endpoint whose request failed before an HTTP response arrived.
    // Do not include the request body, booking code, or any secrets in the error.
    const kind = err && err.name ? String(err.name) : "NetworkError";
    throw new Error("Network request to " + path + " failed before an HTTP response (" + kind + "). Check the service connection and Render logs.");
  }
  const contentType = (response.headers.get("content-type") || "").toLowerCase();
  const raw = await response.text();
  let data;
  try { data = JSON.parse(raw); }
  catch (_) {
    const excerpt = raw.replace(/\s+/g, " ").trim().slice(0, 140);
    const type = contentType || "unknown content type";
    throw new Error("API " + path + " returned HTTP " + response.status + " with " + type + ", not JSON" + (excerpt ? ": " + excerpt : " (empty response)"));
  }
  if (!response.ok || data.ok === false) throw new Error(data.error || ("Request failed (HTTP " + response.status + ")."));
  return data;
}

function clearLookup() {
  lastLookupData = null;
  try { sessionStorage.removeItem("solveease:lastLookup"); } catch (_) {}
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
  if (!code) return;
  // The booking code is a one-time input: clear it immediately and never persist it.
  $("code").value = "";
  lastLookupData = null;
  preparedTicketText = "";
  try { sessionStorage.removeItem("solveease:lastLookup"); } catch (_) {}
  clear("ticketResult");
  clear("converterSummary");
  if ($("convertedTicket")) $("convertedTicket").textContent = "No filtered selections yet.";
  if ($("converterMessage")) { $("converterMessage").textContent = ""; $("converterMessage").className = ""; }
  $("lookupBtn").disabled = true;
  $("lookupMessage").textContent = "Filtering selections…";
  $("lookupMessage").className = "";
  try {
    const source = await api("/api/lookup", "POST", {code, bookmaker:"auto"});
    if (!Array.isArray(source.legs) || !source.legs.length) throw new Error("No ticket selections were returned.");
    // Automatically run the model before showing anything. The source ticket is kept only in memory
    // during this request and is never stored in sessionStorage or displayed.
    const report = await api("/api/analyze-ticket", "POST", {
      legs: source.legs.map(leg => ({event:leg.event, market:leg.market, selection:leg.selection, odds:leg.odds_verified ? leg.odds : null}))
    });
    const all = Array.isArray(report.ranked_legs) ? report.ranked_legs : [];
    const threshold = 0.70;
    let replacementReport = {results:[], note:"Replacement scan not completed."};
    try {
      replacementReport = await api("/api/find-replacements", "POST", {
        legs: source.legs.map(leg => ({event:leg.event, market:leg.market, selection:leg.selection, odds:leg.odds_verified ? leg.odds : null}))
      });
    } catch (replacementError) {
      replacementReport = {results:[], note:"Replacement scan unavailable: " + replacementError.message};
    }
    const norm = value => String(value || "").trim().toLowerCase().replace(/[^a-z0-9]+/g, " ").trim();
    const remaining = all.slice();
    const allocation = source.legs.map((sourceLeg, index) => {
      const matchIndex = remaining.findIndex(leg => norm(leg.event) === norm(sourceLeg.event) && norm(leg.market) === norm(sourceLeg.market) && norm(leg.selection) === norm(sourceLeg.selection));
      const analysis = matchIndex >= 0 ? remaining.splice(matchIndex, 1)[0] : null;
      // Number(null) and Number("") both equal 0; guard null/empty first so
      // unavailable estimates are never displayed as a fabricated 0.0%.
      const rawProbability = analysis && analysis.estimated_probability;
      const probability = rawProbability !== null && rawProbability !== undefined && rawProbability !== "" && Number.isFinite(Number(rawProbability)) ? Number(rawProbability) : null;
      const replacementRecord = (replacementReport.results || []).find(item => norm(item.event) === norm(sourceLeg.event));
      // Only use the engine's explicit recommendation, not merely the first raw candidate.
      // This prevents a candidate from bypassing the original-vs-best comparison.
      const candidate = replacementRecord && replacementRecord.decision === "REPLACE"
        ? (replacementRecord.recommended_replacement || null) : null;
      let decision, reason, chosenLeg = sourceLeg, chosenProbability = probability, chosenAnalysis = analysis;
      const originalEdge = analysis && analysis.estimated_edge_percent !== null && analysis.estimated_edge_percent !== undefined && Number.isFinite(Number(analysis.estimated_edge_percent)) ? Number(analysis.estimated_edge_percent) : null;
      const originalAssessment = replacementRecord && replacementRecord.original || {};
      const originalTwoModelPass = originalAssessment.conservative_probability !== null && originalAssessment.conservative_probability !== undefined
        && Number.isFinite(Number(originalAssessment.conservative_probability)) && Number(originalAssessment.conservative_probability) >= threshold
        && originalAssessment.estimated_value_percent !== null && originalAssessment.estimated_value_percent !== undefined
        && Number.isFinite(Number(originalAssessment.estimated_value_percent)) && Number(originalAssessment.estimated_value_percent) > 0
        && originalAssessment.model_probability !== null && originalAssessment.model_probability !== undefined
        && originalAssessment.alternate_model_probability !== null && originalAssessment.alternate_model_probability !== undefined
        && Math.abs(Number(originalAssessment.model_probability) - Number(originalAssessment.alternate_model_probability)) <= 0.08;
      if (!analysis) {
        decision = "REVIEW";
        reason = "Could not safely match this source selection to its analysis result.";
      } else if (candidate) {
        decision = "REPLACED";
        chosenLeg = {...sourceLeg, market:candidate.market, selection:candidate.selection, odds:candidate.odds, odds_verified:true,
          replacement_bookmaker:candidate.bookmaker, replacement_odds_updated_at:candidate.updated_at,
          replacement_freshness_basis:candidate.freshness_basis,
          replacement_type:candidate.replacement_type, replacement_settlement_warning:candidate.settlement_warning,
          replacement_estimated_probability:candidate.conservative_probability, replacement_estimated_value_percent:candidate.estimated_value_percent};
        chosenProbability = candidate.conservative_probability;
        chosenAnalysis = {...(analysis || {}), estimated_probability:candidate.conservative_probability,
          model_fair_odds:1/candidate.conservative_probability, estimated_edge_percent:candidate.estimated_value_percent,
          stats:analysis && analysis.stats || {}};
        reason = `Replacement from ${candidate.bookmaker} at ${candidate.odds.toFixed(2)} passed the two-model, fresh-observation, positive-value and 70% gates. Price freshness basis: ${candidate.freshness_basis || "not supplied"}. ${candidate.replacement_type === "alternative_market_same_fixture" ? "This is a different market; settlement rules must be checked manually. " : "Equivalent market/selection. "}Verify the price before use.`;
      } else if (replacementRecord && replacementRecord.decision === "KEEP" && originalTwoModelPass
          && analysis.status === "estimated" && probability !== null && probability >= threshold
          && originalEdge !== null && originalEdge > 0 && sourceLeg.odds_verified) {
        decision = "KEEP";
        reason = "Original passed the two-model 70% and positive-value gates; the replacement engine found no materially stronger qualifying candidate.";
      } else {
        decision = "REVIEW";
        reason = analysis.status === "estimated" && probability !== null
          ? (probability < threshold ? "Original estimate is below 70%; " : "Original has not passed all combined evidence/value gates; ") + (replacementRecord?.reason || "No qualifying fresh, supported replacement was found.")
          : String((analysis && (analysis.reason || analysis.stats?.reason || analysis.method)) || "Insufficient data or unsupported market; no probability was invented.") + " " + (replacementRecord?.reason || "No qualifying replacement found.");
      }
      return {sourceLeg, chosenLeg, analysis, chosenAnalysis, probability:chosenProbability, decision, reason, index:index+1, candidate};
    });
    const retainedSourceLegs = allocation.filter(item => item.decision === "KEEP" || item.decision === "REPLACED").map(item => ({
      ...item.chosenLeg,
      estimated_probability:item.probability,
      model_fair_odds:item.chosenAnalysis && item.chosenAnalysis.model_fair_odds,
      estimated_edge_percent:item.chosenAnalysis && item.chosenAnalysis.estimated_edge_percent,
      stats:item.chosenAnalysis && item.chosenAnalysis.stats || {},
      allocation_decision:item.decision
    }));
    const keptCount = allocation.filter(item => item.decision === "KEEP").length;
    const replacedCount = allocation.filter(item => item.decision === "REPLACED").length;
    const reviewCount = allocation.filter(item => item.decision === "REVIEW").length;
    // The converter receives only selections that passed the current rules. The complete
    // allocation is rendered for this response but is not persisted with the booking code.
    lastLookupData = {
      ok:true, filtered_ticket:true, original_booking_code_discarded:true,
      bookmaker:source.bookmaker, bookmaker_key:source.bookmaker_key,
      retrieved_at:source.retrieved_at, legs:retainedSourceLegs,
      displayed_selections_count:retainedSourceLegs.length,
      source_selection_count:retainedSourceLegs.length,
      original_selection_count:source.legs.length,
      allocation_summary:{total:allocation.length,keep:keptCount,replaced:replacedCount,review:reviewCount,replacements_found:replacedCount,replacement_scan:"configured_feeds_with_fresh_observations_required"},
      note:"Only KEEP and REPLACED selections are passed to reconstruction. Replacements require a configured feed, fresh observed odds (provider timestamp preferred; retrieval time explicitly labelled when absent), same-fixture supported market comparison, two supported model estimates agreeing within 8 percentage points, at least 70% conservative probability, positive estimated value, and an explicit KEEP/REPLACE decision from the comparison engine. REVIEW selections remain excluded; no substitute is invented."
    };
    try {
      if (retainedSourceLegs.length) sessionStorage.setItem("solveease:lastLookup", JSON.stringify(lastLookupData));
      else sessionStorage.removeItem("solveease:lastLookup");
    } catch (_) {}
    const output = $("ticketResult");
    const summary = el("div", "", output, "filter-summary");
    summary.textContent = `Full-ticket allocation: ${allocation.length} reviewed · ${keptCount} KEEP · ${replacedCount} REPLACED · ${reviewCount} REVIEW. Replacement search uses configured feeds with fresh timestamped prices; estimates are uncalibrated. Only KEEP/REPLACED selections go to the converter.`;
    const list = el("div", "", output, "filtered-output");
    allocation.forEach(item => {
      const row = el("article", "", list, "filtered-leg");
      const info = el("div", "", row);
      el("strong", `${item.index}. ${item.sourceLeg.event || "Event unavailable"}`, info);
      el("p", item.decision === "REPLACED" && item.candidate ? `${item.sourceLeg.market} · ${item.sourceLeg.selection} → ${item.candidate.market} · ${item.candidate.selection} @ ${item.candidate.odds.toFixed(2)} (${item.candidate.bookmaker})` : `${item.sourceLeg.market || "Market unavailable"} · ${item.sourceLeg.selection || "Selection unavailable"}`, info);
      const status = el("span", item.decision, row, "leg-metric");
      status.style.background = item.decision === "KEEP" ? "#164e3b" : item.decision === "REPLACED" ? "#16436b" : "#5b3a13";
      status.style.color = "#ffffff";
      el("p", item.probability === null ? "Model probability: unavailable" : `Model estimate: ${(item.probability * 100).toFixed(1)}%`, info, "small");
      el("p", item.reason, info, "small");
    });
    $("lookupMessage").textContent = keptCount
      ? `Allocation complete: ${keptCount} kept, ${replacedCount} replaced, ${reviewCount} require review. Confirm replacement prices on the bookmaker.`
      : `Allocation complete: ${keptCount} kept, ${replacedCount} replaced, ${reviewCount} require review. No unverified substitute was invented.`;
    $("lookupMessage").className = reviewCount ? "warning" : "success";
    if ($("converterMessage")) {
      $("converterMessage").textContent = "";
      $("converterMessage").className = "";
    }
    if ($("convertedTicket")) $("convertedTicket").textContent = retainedSourceLegs.length
      ? "Only KEEP and verified REPLACED selections are ready for reconstruction. REVIEW selections remain excluded."
      : "No selections are ready for reconstruction until review finds qualifying selections or verified replacements.";
  } catch (err) {
    lastLookupData = null;
    try { sessionStorage.removeItem("solveease:lastLookup"); } catch (_) {}
    $("lookupMessage").textContent = "Filtering unavailable: " + err.message;
    $("lookupMessage").className = "error";
  } finally {
    $("lookupBtn").disabled = false;
  }
}

function clearChildren(node) { node.replaceChildren(); }

function prepareConversion() {
  let data = lastLookupData;
  const manualText = $("manualTicketJson") ? $("manualTicketJson").value.trim() : "";
  if (manualText) {
    try {
      const manual = JSON.parse(manualText);
      if (manual && Array.isArray(manual.legs)) data = {...manual, ok:true, bookmaker:manual.bookmaker || "User-supplied ticket", retrieved_at:"Manual input", legs:manual.legs.map(leg => ({...leg, odds_verified:Number.isFinite(Number(leg.odds)) && Number(leg.odds)>1}))};
      else { $("converterMessage").textContent = "Manual JSON must contain a legs array."; $("converterMessage").className="error"; return; }
    } catch (_) { $("converterMessage").textContent = "Manual ticket JSON is invalid."; $("converterMessage").className="error"; return; }
  }
  const destination = $("destinationBookmaker").value;
  const names = {sportybet:"SportyBet",bet9ja:"Bet9ja",betking:"BetKing"};
  const message = $("converterMessage");
  const output = $("convertedTicket");
  clear("converterSummary");
  preparedTicketText = "";

  if (!data || data.ok !== true || !Array.isArray(data.legs) || !data.legs.length) {
    message.textContent = "Enter manual ticket JSON or look up a supported booking code first.";
    message.className = "warning";
    output.textContent = "No ticket available to reconstruct.";
    return;
  }

  const destinationName = names[destination] || "Selected bookmaker";
  const sameSource = String(data.bookmaker_key || "").toLowerCase() === destination;
  let needsVerification = 0;
  const lines = [
    "SOLVEEASE — MANUAL TICKET RECONSTRUCTION",
    "Source bookmaker: " + (data.bookmaker || "Unknown"),
    "Destination bookmaker: " + destinationName,
    "Source ticket retrieved: " + (data.retrieved_at || "Time unavailable"),
    "Displayed selections: " + data.legs.length,
    ...(data.filtered_ticket ? ["Ticket basis: FILTERED SELECTIONS ONLY", "Original booking code: DISCARDED; this filtered set has no new bookmaker-issued code"] : []),
    "",
    "STATUS: NOT A DESTINATION BOOKING CODE",
    "This checklist has not been matched against the destination's live markets.",
    ""
  ];

  data.legs.forEach((leg, index) => {
    const hasOdds = leg.odds_verified === true && Number.isFinite(Number(leg.odds));
    const event = String(leg.event || "Event unavailable");
    const market = String(leg.market || "Market needs verification");
    const selection = String(leg.selection || "Selection needs verification");
    const ambiguous = !hasOdds ||
      /needs verification|unavailable|not identifiable/i.test(market + " " + selection + " " + event);
    if (ambiguous) needsVerification++;

    lines.push(
      (index + 1) + ". " + event,
      "   Market: " + market,
      "   Selection: " + selection,
      "   Source odds: " + (hasOdds ? Number(leg.odds).toFixed(3) : "Unverified"),
      "   Destination match: " + (ambiguous ? "REQUIRES MANUAL IDENTIFICATION" : "NOT YET VERIFIED"),
      "   Current destination odds: Check directly with bookmaker",
      ""
    );
  });

  lines.push(
    "Before recreating: confirm event identity, kickoff, market name, handicap/total line,",
    "selection wording, settlement rules, current odds, and ticket acceptance.",
    "No destination code has been requested or generated."
  );
  preparedTicketText = lines.join("\n");
  output.textContent = preparedTicketText;

  const cards = [
    ["Selections to review", data.legs.length],
    ["Legs with ambiguous source details", needsVerification],
    ["Destination matching", "Not verified"]
  ];
  cards.forEach(([label,value]) => {
    const card = el("div", "", $("converterSummary"), "stat");
    el("span", label, card);
    el("strong", value, card);
  });

  message.textContent = sameSource
    ? "Prepared source-ticket checklist. Recreating at the same bookmaker still requires checking current markets and odds."
    : "Manual reconstruction checklist prepared for " + destinationName + ". Destination equivalence is not verified.";
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
    if (!navigator.clipboard || !navigator.clipboard.writeText) throw new Error("Clipboard unavailable");
    await navigator.clipboard.writeText(preparedTicketText);
    message.textContent = "Prepared ticket copied to clipboard.";
    message.className = "success";
  } catch (_) {
    message.textContent = "Automatic copy is unavailable in this browser. Long-press the text, select it, and copy manually.";
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

function renderHealth(items, targetId="sourceHealth") {
  renderList(targetId, items, (item,parent) => {
    const card = el("div", "", parent, "item");
    el("strong", item.bookmaker + " — " + item.status, card);
    el("p", item.detail || "No diagnostic detail available.", card, "small");
    const parts = [
      "HTTP: " + (item.http_status ?? "not reached"),
      "Content-Type: " + (item.content_type || "unknown"),
      "Response: " + (item.response_bytes ?? 0) + " bytes",
      "Fetch: " + (item.fetch_ms ?? 0) + " ms",
      "JSON: " + (item.json_valid ? "valid" : "no/invalid"),
      "Schema: " + (item.schema_valid ? "recognized" : "not confirmed"),
      "Events: " + (item.raw_events ?? 0) + " received / " + (item.events_loaded ?? 0) + " usable / " + (item.events_rejected ?? 0) + " rejected"
    ];
    el("p", parts.join(" · "), card, "small");
    if (item.last_successful_fetch) el("p", "Last successful JSON fetch: " + item.last_successful_fetch, card, "small");
    if (item.data_updated_at) el("p", "Newest event timestamp: " + item.data_updated_at + " · age " + item.freshness_seconds + " seconds" + (item.stale ? " · STALE" : ""), card, item.stale ? "warning" : "small");
    else el("p", "Data freshness: unknown (no usable updated_at timestamp)", card, "small");
    const reasons = item.rejection_reasons || {};
    const reasonText = Object.entries(reasons).map(([reason,count]) => reason + ": " + count).join("; ");
    if (reasonText) el("p", "Rejected-event reasons: " + reasonText, card, "small");
  }, "No source health data.");
}

async function scan() {
  $("scanBtn").disabled = true;
  $("scanMessage").textContent = "Fetching configured odds feeds…";
  $("scanMessage").className = "";
  ["sourceHealth","scanSummary","arbitrage","probabilities","alternatives","marketCatalog"].forEach(clear);
  $("marketCatalogNote").textContent = "Fetching market catalog…";
  try {
    const data = await api("/api/scan", "POST", {});
    $("scanMessage").textContent = "Scan completed at " + data.scanned_at;
    renderHealth(data.bookmakers);
    const summary = $("scanSummary");
    [
      ["Usable two-outcome markets for arbitrage",data.usable_two_outcome_markets],
      ["Arbitrage opportunities",data.arbitrage_count],
      ["Selections at/above threshold",data.qualified_selections_count],
      ["Alternative candidates",data.alternative_markets_count]
    ].forEach(([label,value]) => {
      const stat = el("div", "", summary, "stat");
      el("span", label, stat);
      el("strong", value, stat);
    });

    renderMarketCatalog(data.available_markets || [], data.available_markets_count || 0, !!data.available_markets_truncated);

    renderList("arbitrage", data.arbitrage, (item,parent) => {
      const card = el("div", "", parent, "item");
      el("h3", item.event + " · " + item.sport, card);
      el("p", "Market: " + item.market, card);
      el("p", "Theoretical margin: " + money(item.theoretical_margin_percent) + "%", card, "success");
      el("p", "Implied probability sum: " + money(item.implied_probability_sum) + "%", card);
      item.legs.forEach(leg => el("p",
        leg.selection + ": " + leg.bookmaker + " @ " + money(leg.odds) +
        " · suggested stake share " + money(leg.stake_fraction * 100) + "%",
        card));
      el("p", item.warning, card, "small");
    }, "No qualifying arbitrage opportunity found in currently available feeds. Unavailable feeds are not proof that no arbitrage exists.");

    renderList("probabilities", data.qualified_selections, (item,parent) => {
      const card = el("div", "", parent, "item");
      el("strong", item.event, card);
      el("p", item.market + " · " + item.selection, card);
      el("p", item.bookmaker + " @ " + money(item.odds), card);
      el("p", "Feed-estimated probability: " + money(item.estimated_probability * 100) + "%", card);
      el("p", item.evidence, card, "small");
    }, "No feed-provided estimates reached the configured probability threshold. Odds alone are not treated as a model prediction.");

    renderList("alternatives", data.alternative_markets, (item,parent) => {
      const card = el("div", "", parent, "item");
      el("strong", item.event, card);
      el("p", item.market + " · " + item.selection, card);
      el("p", item.bookmaker + " @ " + money(item.odds), card);
      el("p", "Feed-estimated probability: " + money(item.estimated_probability * 100) + "%", card);
      el("p", item.note, card, "small");
    }, "No qualifying supported market alternatives are available from configured feeds.");
  } catch (err) {
    $("scanMessage").textContent = err.message;
    $("scanMessage").className = "error";
  } finally {
    $("scanBtn").disabled = false;
  }
}

function renderMarketCatalog(markets, totalCount, truncated) {
  const target = $("marketCatalog");
  clear("marketCatalog");
  const note = $("marketCatalogNote");
  note.textContent = totalCount
    ? ("Showing " + markets.length + " of " + totalCount + " usable market rows from this scan." + (truncated ? " Catalog limited to first 250 markets." : ""))
    : "No usable markets are available from the configured feeds. A failed feed is not evidence that no opportunities exist.";
  markets.forEach(market => {
    const card = el("div", "", target, "item");
    el("strong", market.event, card);
    el("p", market.market + " · " + market.bookmaker, card, "small");
    market.outcomes.forEach(outcome => {
      const button = el("button", "", card);
      button.type = "button";
      button.textContent = "Add " + outcome.name + " @ " + outcome.odds;
      button.addEventListener("click", () => addCatalogLeg(market, outcome));
    });
    el("p", "Goal rates will be fetched automatically when supported historical results can be matched.", card, "small notice");
  });
}

function addCatalogLeg(market, outcome) {
  const message = $("betslipAnalysisMessage");
  let payload;
  try { payload = JSON.parse($("betslipJson").value || '{"legs":[]}'); }
  catch (_) { payload = {legs: []}; }
  if (!payload || !Array.isArray(payload.legs)) payload = {legs: []};
  if (payload.legs.length >= 50) {
    message.textContent = "Maximum 50 legs per analysis."; message.className = "warning"; return;
  }
  payload.legs.push({event: market.event, market: market.market, selection: outcome.name, odds: outcome.odds, bookmaker: market.bookmaker, updated_at: market.updated_at || null});
  $("betslipJson").value = JSON.stringify(payload, null, 2);
  message.textContent = "Added " + market.event + " — " + market.market + " — " + outcome.name + ". Goal rates will be fetched automatically where data coverage allows.";
  message.className = "success";
}

async function analyzeBetslip() {
  const message = $("betslipAnalysisMessage");
  const report = $("betslipAnalysisReport");
  clear("betslipAnalysisReport");
  message.className = "";
  let payload;
  try { payload = JSON.parse($("betslipJson").value); }
  catch (_) { message.textContent = "The betslip must be valid JSON."; message.className = "error"; return; }
  const button = $("analyzeBetslipBtn"); button.disabled = true;
  message.textContent = "Calculating supported market probabilities…";
  try {
    const data = await api("/api/analyze-betslip", "POST", payload);
    const grid = el("div", "", report, "grid");
    [["Legs received",data.legs_received],["Supported by model",data.supported_legs],["Unsupported legs",data.unsupported_legs]].forEach(([label,value]) => {
      const card = el("div", "", grid, "stat"); el("span", label, card); el("strong", String(value), card);
    });
    data.ranked_legs.forEach((leg, index) => {
      const card = el("div", "", report, "item");
      el("h3", (index + 1) + ". " + leg.event, card);
      el("p", leg.market + " · " + leg.selection + " · odds " + money(leg.odds, 3), card);
      if (leg.model_supported) {
        el("p", "Model probability: " + money(leg.estimated_probability * 100) + "% · model fair odds: " + money(leg.model_fair_odds, 3), card);
        el("p", "Odds-implied probability (raw): " + money(leg.odds_implied_probability * 100) + "% · estimated edge: " + money(leg.estimated_edge_percent) + "%", card, leg.estimated_edge_percent > 0 ? "success" : "small");
      } else el("p", "No probability estimate produced.", card, "warning");
      el("p", leg.method, card, "small");
      el("p", leg.home_goal_rate != null && leg.away_goal_rate != null ? ("Estimated goal rates: home " + leg.home_goal_rate + " / away " + leg.away_goal_rate) : "Goal rates unavailable; selection not assigned a model probability.", card, "small");
    });
    data.warnings.forEach(warning => el("p", warning, report, "small notice"));
    message.textContent = "Analysis complete. Supported markets are ranked by estimated edge, then probability; no bet was placed.";
    message.className = "success";
  } catch (err) { message.textContent = err.message; message.className = "error"; }
  finally { button.disabled = false; }
}
function clearBetslipAnalysis() {
  $("betslipJson").value = "";
  $("betslipAnalysisMessage").textContent = "";
  $("betslipAnalysisMessage").className = "";
  clear("betslipAnalysisReport");
}

async function runDailyBuilder() {
  const message=$("dailyMessage"), report=$("dailyReport"); message.textContent="Scanning fixture data…";
  let body={date:$('dailyDate').value.trim()||null}; const raw=$('dailyFixtures').value.trim();
  if(raw){try{const parsed=JSON.parse(raw); body.fixtures=Array.isArray(parsed)?parsed:(parsed.fixtures||parsed.events||[]);}catch(e){message.textContent="Fixture JSON is invalid.";message.className="error";return;}}
  try{const data=await api('/api/daily-accumulator','POST',body); report.textContent=JSON.stringify(data,null,2);message.textContent=data.status||'Daily scan complete.';message.className='success';}
  catch(e){message.textContent=e.message;message.className='error';}
}
async function sendDailyTelegram(){const message=$("dailyMessage"),raw=$("dailyReport").textContent;try{const report=JSON.parse(raw);const data=await api('/api/telegram-alert','POST',{report});message.textContent=data.delivered?'Telegram report delivered.':(data.error||'Telegram delivery not confirmed.');message.className=data.delivered?'success':'warning';}catch(e){message.textContent='Run a daily scan first, then send its report.';message.className='warning';}}
async function runOptimizer(){const message=$("optimizerMessage");try{const payload=JSON.parse($('optimizerJson').value);const data=await api('/api/optimize-ticket','POST',payload);$('optimizerReport').textContent=JSON.stringify(data,null,2);message.textContent='Optimization complete; review replacements and exclusions.';message.className='success';}catch(e){message.textContent=e.message;message.className='error';}}
async function runPsychologyCheck(){const message=$("optimizerMessage");try{const payload=JSON.parse($('optimizerJson').value);const legs=payload.legs||[];const data=await api('/api/psychology-check','POST',{selections:legs.flatMap(x=>[x.original||x,...(x.candidates||[])])});$('optimizerReport').textContent=JSON.stringify(data,null,2);message.textContent='Diagnostic flags generated; probabilities were not changed.';message.className='success';}catch(e){message.textContent=e.message;message.className='error';}}
async function testTelegram(){const message=$("telegramMessage");message.textContent='Sending test alert…';try{const data=await api('/api/telegram-test','POST',{});$('telegramReport').textContent=JSON.stringify(data,null,2);message.textContent=data.delivered?'Test alert delivered; check the configured Telegram chat.':(data.error||'Delivery not confirmed.');message.className=data.delivered?'success':'warning';}catch(e){message.textContent=e.message;message.className='error';}}
async function checkTelegramStatus(){try{const data=await api('/api/telegram-status');$('telegramReport').textContent=JSON.stringify(data,null,2);$('telegramMessage').textContent=data.configured?'Credentials are present; delivery still needs a test.':'Telegram is not configured.';$('telegramMessage').className=data.configured?'warning':'error';}catch(e){$('telegramMessage').textContent=e.message;}}

async function loadHealth() {
  try {
    const data = await api("/health");
    const summary = "Service online · " + data.configured_feeds +
      " configured odds feeds · SportyBet lookup adapter available";
    if ($("health")) $("health").textContent = summary;
    if ($("homeHealth")) $("homeHealth").textContent = summary;
    if (data.feed_status && $("sourceHealth")) renderHealth(data.feed_status, $("statusFeedHealth") && !$("statusPanel").classList.contains("page-hidden") ? "statusFeedHealth" : "sourceHealth");
  } catch (_) {
    if ($("health")) $("health").textContent = "Could not check service status.";
    if ($("homeHealth")) $("homeHealth").textContent = "Could not check service status.";
  }
}

$("code").addEventListener("keydown", event => {
  if (event.key === "Enter") lookup();
});
["betslipJson", "accumulatorLegs", "accumulatorStake"].forEach(id => {
  const node = $(id);
  if (node) node.addEventListener("input", persistWorkspace);
});
window.addEventListener("pagehide", persistWorkspace);
activatePage();
</script>
</body>
</html>"""


# ---------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "SolveEase/3.16.0"

    def log_message(self, fmt, *args):
        # Log method, path and HTTP status for debugging, but never query strings,
        # POST bodies, booking codes, or other user-supplied values.
        request_line = str(args[0]) if args else ""
        parts = request_line.split()
        method = parts[0] if len(parts) >= 1 else "UNKNOWN"
        target = parts[1] if len(parts) >= 2 else "/"
        try:
            safe_path = urlsplit(target).path or "/"
        except Exception:
            safe_path = "/<unparsed>"
        # Guard against control characters and cap the path length for clean logs.
        safe_path = re.sub(r"[^A-Za-z0-9_./-]", "?", safe_path)[:160]
        status = str(args[1]) if len(args) >= 2 else "unknown"
        logging.info("http method=%s path=%s status=%s", method[:12], safe_path, status[:12])

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
        # Never let NaN/Infinity or an unexpected value abort serialization and
        # leave the browser with an empty/truncated response. Non-finite numbers
        # are represented as JSON null and remain visibly unavailable to clients.
        def clean(value):
            if isinstance(value, float) and not math.isfinite(value):
                return None
            if isinstance(value, dict):
                return {str(key): clean(item) for key, item in value.items()}
            if isinstance(value, (list, tuple)):
                return [clean(item) for item in value]
            if isinstance(value, (str, int, float, bool)) or value is None:
                return value
            return str(value)
        try:
            body = json.dumps(clean(data), ensure_ascii=False, allow_nan=False).encode("utf-8")
        except Exception:
            logging.exception("JSON response serialization failed")
            status = 500
            body = json.dumps({"ok": False, "error": "Response serialization failed safely. Please retry; no selections were changed."}).encode("utf-8")
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
        if path.rstrip("/") in ("", "/lookup", "/converter", "/scanner", "/recommendations", "/accumulator", "/status", "/daily", "/optimize", "/alerts"):
            self.send_bytes(200, HTML.encode("utf-8"), "text/html; charset=utf-8")
        else:
            self.send_json(200, {"status": "online"})

    def do_GET(self):
        path = urlsplit(self.path).path.rstrip("/") or "/"
        if path in ("/", "/lookup", "/converter", "/scanner", "/recommendations", "/accumulator", "/status", "/daily", "/optimize", "/alerts"):
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
                    "detail": "URL is configured but no scan has tested it in this process" if config["feed_url"] else "No feed URL configured",
                    "http_status": None, "content_type": None, "response_bytes": 0,
                    "fetch_ms": 0, "json_valid": False, "schema_valid": False,
                    "raw_events": 0, "events_loaded": 0, "events_rejected": 0,
                    "last_successful_fetch": None, "data_updated_at": None,
                    "freshness_seconds": None, "stale": None,
                }
                for config in BOOKMAKERS.values()
            ]
            self.send_json(200, {
                "status": "online",
                "service": "SolveEase Booking & Arbitrage Dashboard",
                "version": "3.16.0",
                "configured_feeds": configured,
                "feed_status": feed_status,
                "feed_freshness_threshold_seconds": STALE_AFTER_SECONDS,
                "football_stats_enabled": FOOTBALL_STATS_ENABLED,
                "football_stats_providers": {"results_form": "Football-Data.co.uk CSV; configured major and additional European divisions", "xg": "Understat public JSON endpoint; selected leagues only; best-effort"},
                "football_stats_configured_divisions": list(FOOTBALL_DIVISIONS.items()),
                "football_stats_coverage_warning": "Configured divisions are attempted across up to three seasons; source files can be absent or incomplete. No live coverage claim is made until a lookup downloads usable rows.",
                "football_stats_cache_ttl_seconds": FOOTBALL_STATS_CACHE_TTL,
                "feed_status_note": "Configured does not mean tested; HTTP success does not prove official source authenticity or accurate live odds.",
                "ticket_lookup_adapters": {"sportybet": "available", "bet9ja": "not implemented", "betking": "not implemented"},
                "last_scan": last_scan,
                "last_lookup": last_lookup,
                "uptime_seconds": int(time.time() - STARTED_AT),
                "daily_accumulator": {"threshold": 0.70, "target": 20, "maximum": 40, "timezone": os.environ.get("APP_TIMEZONE", "Africa/Lagos")},
                "telegram_configured": bool(os.environ.get("TELEGRAM_BOT_TOKEN", "").strip() and os.environ.get("TELEGRAM_CHAT_ID", "").strip()),
            })
            return
        if path == "/api/telegram-status":
            self.send_json(200, {"ok": True, "configured": bool(os.environ.get("TELEGRAM_BOT_TOKEN", "").strip() and os.environ.get("TELEGRAM_CHAT_ID", "").strip()), "delivery_tested": False, "note": "Configuration presence is not proof of message delivery."})
            return
        self.send_json(404, {"ok": False, "error": "Route not found"})

    def do_POST(self):
        path = urlsplit(self.path).path
        if path not in ("/api/lookup", "/api/scan", "/api/find-replacements", "/api/analyze-betslip", "/api/audit-ticket-math", "/api/analyze-ticket", "/api/daily-accumulator", "/api/optimize-ticket", "/api/psychology-check", "/api/telegram-test", "/api/telegram-alert", "/api/model-probability", "/api/calibration-metrics"):
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

        if path == "/api/daily-accumulator":
            try:
                fixtures = body.get("fixtures")
                source = "request payload"
                if fixtures is None:
                    fetched = advanced.fetch_fixture_feed()
                    if not fetched.get("ok"):
                        self.send_json(503, fetched); return
                    fixtures = fetched["fixtures"]; source = fetched.get("source", "configured feed")
                result = advanced.build_daily_accumulator(fixtures, local_date=body.get("date"), timezone_name=body.get("timezone") or os.environ.get("APP_TIMEZONE", "Africa/Lagos"))
                result["fixture_source"] = source
                with STATE_LOCK:
                    global LAST_DAILY_REPORT
                    LAST_DAILY_REPORT = result
                self.send_json(200, result)
            except ValueError as exc: self.send_json(400, {"ok":False,"error":str(exc)})
            except Exception:
                logging.exception("Daily accumulator build failed")
                self.send_json(500, {"ok":False,"error":"Daily scan failed safely; no fixtures or selections were invented."})
            return
        if path == "/api/optimize-ticket":
            try: self.send_json(200, advanced.optimize_ticket(body.get("legs"), threshold=0.70))
            except ValueError as exc: self.send_json(400, {"ok":False,"error":str(exc)})
            return
        if path == "/api/psychology-check":
            selections = body.get("selections", [])
            if not isinstance(selections, list): self.send_json(400,{"ok":False,"error":"selections must be a list"}); return
            self.send_json(200,{"ok":True,"diagnostic_only":True,"results":[{"event":s.get("event","Unknown fixture"),"flags":advanced.psychology_flags(s)} for s in selections if isinstance(s,dict)],"note":"Flags do not alter probabilities and are not claims about a person's mental state."})
            return
        if path == "/api/model-probability":
            try:
                probability, method = advanced.market_probability(body.get("market", ""), body.get("selection", ""), body.get("home_goal_rate"), body.get("away_goal_rate"), body.get("rho", -0.08))
                self.send_json(200, {"ok": probability is not None, "estimated_probability": probability, "method": method, "calibrated": False, "note": "Dixon-Coles rho must be fitted on training data and probabilities calibrated out-of-sample before production confidence claims."})
            except ValueError as exc: self.send_json(400,{"ok":False,"error":str(exc)})
            return
        if path == "/api/calibration-metrics":
            try: self.send_json(200, {"ok":True, **advanced.calibration_metrics(body.get("predictions"), body.get("outcomes"))})
            except ValueError as exc: self.send_json(400,{"ok":False,"error":str(exc)})
            return
        if path == "/api/telegram-test":
            result = advanced.telegram_send("SolveEase · Telegram test alert\nDelivery is confirmed only if this message appears in the intended chat.")
            self.send_json(200,result); return
        if path == "/api/telegram-alert":
            report = body.get("report")
            if not isinstance(report,dict): self.send_json(400,{"ok":False,"error":"report object required"}); return
            result=advanced.telegram_send(advanced.format_daily_alert(report)); self.send_json(200,result); return

        if path == "/api/find-replacements":
            try:
                result = find_market_replacements(body.get("legs"), threshold=MIN_PROBABILITY, max_disagreement=REPLACEMENT_MAX_MODEL_DISAGREEMENT)
                self.send_json(200, result)
            except ValueError as exc:
                self.send_json(400, {"ok": False, "error": str(exc)})
            except Exception:
                logging.exception("Replacement market scan failed")
                self.send_json(500, {"ok": False, "error": "Replacement scan failed safely; no alternatives were invented."})
            return

        if path == "/api/analyze-ticket":
            analysis_started = time.monotonic()
            incoming_legs = body.get("legs") if isinstance(body.get("legs"), list) else []
            # Log only count and duration; never log selections, odds, or booking codes.
            logging.info("ticket_analysis_started legs=%s", len(incoming_legs))
            try:
                result = analyze_ticket_automatically(body)
                elapsed_ms = int((time.monotonic() - analysis_started) * 1000)
                logging.info("ticket_analysis_completed legs=%s estimated=%s elapsed_ms=%s", len(incoming_legs), result.get("estimated_legs", 0), elapsed_ms)
                self.send_json(200, result)
            except ValueError as exc:
                logging.info("ticket_analysis_rejected legs=%s elapsed_ms=%s", len(incoming_legs), int((time.monotonic() - analysis_started) * 1000))
                self.send_json(400, {"ok": False, "error": str(exc)})
            except Exception:
                logging.exception("Automatic ticket analysis failed")
                self.send_json(500, {"ok": False, "error": "Automatic analysis failed safely; no selections were changed."})
            return

        if path == "/api/audit-ticket-math":
            try:
                self.send_json(200, audit_ticket_math(body))
            except ValueError as exc:
                self.send_json(400, {"ok": False, "error": str(exc)})
            except Exception:
                logging.exception("Ticket maths audit failed")
                self.send_json(500, {"ok": False, "error": "Ticket maths audit failed safely; no odds or payouts were inferred."})
            return

        if path == "/api/analyze-betslip":
            try:
                result = analyze_betslip(body)
                self.send_json(200, result)
            except ValueError as exc:
                self.send_json(400, {"ok": False, "error": str(exc)})
            except Exception:
                logging.exception("Betslip analysis failed")
                self.send_json(500, {"ok": False, "error": "Betslip analysis failed safely; check server logs."})
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

        # Only SportyBet lookup is implemented. In auto mode, a successful
        # lookup confirms the source; code format alone never does.
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
                    "bookmaker_identification": "Not confirmed. Code format alone is insufficient.",
                })
            return

        if bookmaker in ("bet9ja", "betking"):
            self.send_json(501, {
                "ok": False,
                "error": (
                    "A verified lookup adapter is not configured for this bookmaker. "
                    "The dashboard will not guess its booking-code format."
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
    logging.info("SolveEase Booking & Arbitrage Dashboard v3.16.0 listening on port %s", PORT)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    if os.environ.get("SCAN_ONCE", "").strip().lower() in ("1", "true", "yes"):
        try:
            print(json.dumps(run_scan(), ensure_ascii=False, indent=2, allow_nan=False))
        except Exception:
            logging.exception("One-shot scan failed")
            raise SystemExit(1)
    else:
        main()
