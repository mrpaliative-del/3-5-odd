# SolveEase Booking & Arbitrage Dashboard — v3.27.0

This is the complete six-file project package. It retains the dashboard, ticket converter, booking-code lookup, arbitrage scanner, feed adapters, supporting advanced features, and regression tests.

## Files
- `main.py` — application entry point and dashboard logic
- `advanced_features.py` — supporting advanced features
- `test_main.py` — automated regression tests
- `requirements.txt` — Python dependencies
- `README.md` — setup and operating notes
- `VERIFICATION.md` — validation scope and live-feed caveats

## Render deployment
Build command:
```bash
pip install -r requirements.txt && playwright install chromium
```
Start command:
```bash
python main.py
```

## v3.27.0 changes
- OddsHarvester now runs `upcoming --help` first and selects CLI options advertised by the installed version, reducing failures caused by changed short/long option aliases.
- Diagnostics include the constructed command arguments and help-probe result when no bounded JSON file is produced.
- Added an optional `BETPAWA_ODDS_FEED_URL` source slot using the same strict JSON event/market/outcomes schema as other configured feeds.

## Important BetPawa limitation
BetPawa's public website displays markets, but this release does **not** claim a verified direct public-site scraper or official BetPawa API. `BETPAWA_ODDS_FEED_URL` must point to an authorized JSON feed you can access. Leaving it unset causes no BetPawa request and no fabricated data. Direct public-page extraction needs its own live verification before it can be labelled an operational adapter.

## Feed and probability caveats
- An online status or HTTP 200 only proves the web service responds, not that odds were ingested.
- Odds are prices, not calibrated probabilities. The app must not invent probabilities or replacement odds.
- Strict replacements must preserve fixture, market, line, outcome and settlement rules. If the data do not support an exact match, keep `REVIEW`.
- No automatic bet placement is performed. Use only data sources you are authorized to access.
