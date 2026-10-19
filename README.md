# SolveEase Booking & Arbitrage Dashboard v3.7.0

A mobile-friendly, read-only football odds dashboard using **Python standard library only**. No third-party Python packages are required. It does not log in to bookmakers or place bets.

## Features

- Responsive dashboard, SportyBet booking lookup, and manual ticket reconstruction checklist.
- Configurable odds-feed adapters and source-health diagnostics.
- Theoretical two-outcome arbitrage comparison across matching normalized markets.
- Feed-supplied probability filter (default 60%); supplied estimates are not independently verified.
- Accumulator odds arithmetic.
- **Automatic booking-ticket analysis:** after a successful ticket lookup, the dashboard independently ranks the ticket legs, fetches historical results where available, estimates home/away goal rates, calculates supported market probabilities, and lets the user filter on a model-probability threshold. The original ticket is preserved.
- **Automatic team data:** fetches Football-Data.co.uk CSV results for configured major divisions and attempts Understat xG/xGA for supported leagues. It reports source, last result date, sample sizes, xG availability and reasons for unavailable estimates. Cache is in-memory; no third-party Python packages are required.
- **Manual JSON analysis remains supported:** goal-rate fields are optional; when omitted, the app attempts automatic stats retrieval. Supported markets are 1X2, common double-chance selections, totals with a numeric line, and BTTS.
- JSON one-shot scan mode for scheduled automation (`SCAN_ONCE=1`).
- Standard-library `unittest` suite.

## Run locally

```bash
python -m unittest -v
python -m py_compile main.py test_main.py
python main.py
```

Open `http://127.0.0.1:10000` unless `PORT` is set to another value.

## Render deployment

- Runtime: Python 3.11 or newer
- Build command: `pip install -r requirements.txt` (no third-party dependencies are installed)
- Start command: `python main.py`
- Set `PORT` only if your hosting platform does not supply it.

## Environment variables

Copy `.env.example` into your local private environment if needed. Do not commit real feed credentials or private URLs.

- `PORT` — HTTP port (default `10000`)
- `REQUEST_TIMEOUT_SECONDS` — outbound request timeout (default `12`)
- `MIN_PROBABILITY` — minimum feed-supplied probability (default `0.60`)
- `BANKROLL` — displayed configuration only; does not promise returns (default `100000`)
- `FEED_STALE_AFTER_SECONDS` — stale timestamp threshold (default `300`)
- `SPORTYBET_ODDS_FEED_URL`
- `BET9JA_ODDS_FEED_URL`
- `ONEXBET_ODDS_FEED_URL` (legacy alias `X1BET_ODDS_FEED_URL` also accepted)
- `BETWAY_ODDS_FEED_URL`
- `FOOTBALL_STATS_ENABLED` — `1` by default; set `0` to disable automatic team-stat lookup
- `FOOTBALL_STATS_TIMEOUT_SECONDS` — per-source HTTP timeout, default `5`
- `FOOTBALL_STATS_CACHE_TTL_SECONDS` — process-local result cache, default `21600` (6 hours)
- `FOOTBALL_STATS_MAX_AGE_DAYS` — reject statistics older than this, default `540`

Only configure feeds you own or are authorized to access. A configured URL does not prove a feed is live, current, or accurate. Never place bookmaker passwords or account credentials in feed settings.

## Routes

- `/` — home
- `/lookup` — read-only SportyBet ticket lookup
- `/converter` — manual ticket reconstruction checklist
- `/scanner` — odds scan, theoretical arbitrage, probability candidates, betslip analysis and accumulator maths
- `/status` — source/service diagnostics
- `/health` — JSON health status
- `POST /api/lookup` — JSON body: `{"code":"...","bookmaker":"auto"}`
- `POST /api/scan` — JSON body: `{}`
- `POST /api/analyze-betslip` — JSON body shown below; goal-rate fields are optional
- `POST /api/analyze-ticket` — analyzes the legs from a retrieved ticket using automatic stats lookup where possible

### Selected betslip analysis API

```json
{
  "legs": [
    {
      "event": "Alpha FC vs Beta FC",
      "market": "1X2",
      "selection": "Home",
      "odds": 2.1
    },
    {
      "event": "Alpha FC vs Beta FC",
      "market": "Total Goals 2.5",
      "selection": "Over 2.5",
      "odds": 1.9
    }
  ]
}
```

When `home_goal_rate` and `away_goal_rate` are omitted, the API attempts to fetch historical results and team form from Football-Data.co.uk for supported major divisions. For supported top leagues it also attempts Understat team xG/xGA via its public JSON endpoint. If xG cannot be matched or retrieved, the model falls back to goal-derived rates using recent form, home/away scoring and conceding, league averages, and shrinkage for small venue samples. It never fills a missing match with invented stats.

The independent-Poisson calculation is still a baseline, not a trained or historically calibrated prediction. The result-derived goal rates are **not official xG**. Understat coverage is limited, and its endpoint may change or block access. Team matching can fail for reserve, youth, women's, renamed or neutral-site fixtures. Unsupported markets return no probability. The model assumes independent team goal counts and truncates the goal distribution at 12 goals before normalization. It does not calculate accumulator win probability because selections may be correlated.

**Data-use note:** Football-Data.co.uk states its free downloadable data is intended for private individual use, not commercial automated products. Check its current terms before using this adapter in a public/commercial service. Understat access is best-effort and its current terms and usage restrictions should also be checked before public or commercial reuse. Do not treat these sources as licensed commercial feeds without confirming permission.

## Arbitrage and accumulator limitations

Arbitrage results are theoretical. Verify exact event, line, market settlement, void rules, odds freshness, limits, fees, and mutually exclusive/exhaustive outcomes. A displayed theoretical margin is not a guarantee that all legs can be placed at the displayed prices. Accumulator arithmetic is not a forecast and does not account for correlation, bookmaker margin, price movement or settlement terms.

## Scheduled scan

`.github/workflows/run_scanner.yml` runs `python main.py` with `SCAN_ONCE=1`, which prints one scan as JSON and exits. Configure repository Actions secrets named `SPORTYBET_ODDS_FEED_URL`, `BET9JA_ODDS_FEED_URL`, `ONEXBET_ODDS_FEED_URL`, and/or `BETWAY_ODDS_FEED_URL` for the feeds you are authorized to use. If none are configured, the job correctly reports all sources as not configured; it cannot discover odds by itself. The workflow does not send Telegram alerts.

## Source adapters and evidence limits

- SportyBet booking lookup uses an unofficial endpoint that may change.
- SportyBet nested factsCenter schema is unofficial and can change.
- Bet9ja's public MarketWS XML operation has not been verified against a complete current live quote payload. The adapter rejects unclear fields rather than guessing odds.
- 1xBet's endpoint and schema are unofficial; anti-bot or HTML responses are rejected.
- The Betway adapter supports a documented flat-array fixture, but that fixture does not prove a Nigeria feed works.
- The latest scan exposes up to 250 normalized two-outcome markets for user selection. The scanner currently normalizes two-outcome markets only. Three-way 1X2 feeds are excluded from the existing arbitrage scanner; the separate betslip model can estimate 1X2 when automatic team statistics match, or when goal rates are entered manually.
- Missing, stale, or unusable feed data is not evidence that no opportunity exists.

## Validation status

Run the tests locally with `python -m unittest -v`. Tests use synthetic fixtures and mathematical examples; they verify code behavior, not live bookmaker access or betting profitability. Live feeds have not been confirmed by this package. After deployment, inspect `/health` and the scan diagnostics to establish which configured sources actually respond and provide usable data.



## v3.7.0 automatic team-stat analysis

After a successful booking-code lookup, the UI automatically calls `POST /api/analyze-ticket`. It attempts to match each fixture against supported league CSV results, adds previous-season results where available, summarizes recent form and home/away goal rates, and tries Understat xG/xGA for selected leagues. Supported market probabilities are computed from the goal-rate model and ranked by estimated edge, then probability. The user can filter by minimum model probability; filtered and unavailable legs remain visible in separate sections. The retrieved booking is never rewritten.

No live provider response was verified during packaging because outbound DNS/network access was unavailable in the build environment. Automated tests use synthetic fixtures and mocked provider results. The first deployed lookup is the real integration check: `/health` reports whether stats are enabled, and each analysis card reports the source, latest result date and whether xG was available.
