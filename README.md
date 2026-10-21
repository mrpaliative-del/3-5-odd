# SolveEase Booking & Arbitrage Dashboard — v3.25.0

This is the complete six-file project package. It retains the existing dashboard, ticket converter, booking-code lookup, arbitrage scanner, feed adapters, supporting advanced features, and test suite.

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

## v3.25.0 change
The OddsHarvester command was adjusted to use the documented `-s`, `-d`, `-m`, `-f`, and `-o` CLI options, without the previous `--concurrency` and `--request-delay` arguments.

## Feed and probability caveats
This code change does not prove that OddsPortal can be reached or that usable market rows will be returned. After deployment, run a replacement search and inspect the new feed diagnostic logs. An online status or HTTP 200 only proves the web service responds, not that markets were ingested.

Odds are prices, not calibrated probabilities. The application must not invent probabilities or replacement odds. Strict replacements must preserve fixture, market, line, outcome, and settlement rules; different-settlement markets are alternatives for manual review. No automatic bet placement is performed.

Use only data sources you are authorized to access.
