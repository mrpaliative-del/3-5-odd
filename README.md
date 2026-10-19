# SolveEase Booking & Arbitrage Dashboard v3.6.0

A mobile-friendly, read-only football odds dashboard using **Python standard library only**. No third-party Python packages are required. It does not log in to bookmakers or place bets.

## Features

- Responsive dashboard, SportyBet booking lookup, and manual ticket reconstruction checklist.
- Configurable odds-feed adapters and source-health diagnostics.
- Theoretical two-outcome arbitrage comparison across matching normalized markets.
- Feed-supplied probability filter (default 60%); supplied estimates are not independently verified.
- Accumulator odds arithmetic.
- **Selected betslip market analysis:** submits selected event/market/selection, decimal odds, and user-entered expected-goal rates to an independent-Poisson baseline model. Supported markets are 1X2, common double-chance selections, totals with a numeric line, and BTTS.
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
- `POST /api/analyze-betslip` — JSON body shown below

### Selected betslip analysis API

```json
{
  "legs": [
    {
      "event": "Alpha FC vs Beta FC",
      "market": "1X2",
      "selection": "Home",
      "odds": 2.1,
      "home_goal_rate": 1.55,
      "away_goal_rate": 1.05
    },
    {
      "event": "Alpha FC vs Beta FC",
      "market": "Total Goals 2.5",
      "selection": "Over 2.5",
      "odds": 1.9,
      "home_goal_rate": 1.55,
      "away_goal_rate": 1.05
    }
  ]
}
```

The `home_goal_rate` and `away_goal_rate` values are **user-supplied expected goals**, not fetched team statistics. The Poisson calculation is a baseline mathematical model, not a trained or historically calibrated prediction. Unsupported markets return no probability instead of a guessed one. Estimates and “edge” values are only meaningful if the input rates and odds are credible and current. The model assumes independent team goal counts and truncates the goal distribution at 12 goals before normalization. It does not calculate accumulator win probability because selections may be correlated.

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
- The scanner currently normalizes two-outcome markets only. Three-way 1X2 feeds are excluded from the existing arbitrage scanner; the separate betslip model can estimate 1X2 when you provide goal rates.
- Missing, stale, or unusable feed data is not evidence that no opportunity exists.

## Validation status

Run the tests locally with `python -m unittest -v`. Tests use synthetic fixtures and mathematical examples; they verify code behavior, not live bookmaker access or betting profitability. Live feeds have not been confirmed by this package. After deployment, inspect `/health` and the scan diagnostics to establish which configured sources actually respond and provide usable data.
