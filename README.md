# SolveEase Booking & Arbitrage Dashboard — v3.26.0

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

## v3.26.0 change
The OddsHarvester integration now defaults to documented market identifiers `1x2,btts,over_under`. When the CLI exits without producing JSON, diagnostics include the requested market list and bounded stderr/stdout tails. This narrows a likely source of CLI usage errors and improves evidence for further troubleshooting, but does not guarantee live retrieval.

## Feed and probability caveats
This code change does not prove that OddsPortal can be reached or that usable market rows will be returned. After deployment, run a replacement search and inspect the new feed diagnostic logs. An online status or HTTP 200 only proves the web service responds, not that markets were ingested.

Odds are prices, not calibrated probabilities. The application must not invent probabilities or replacement odds. Strict replacements must preserve fixture, market, line, outcome, and settlement rules; different-settlement markets are alternatives for manual review. No automatic bet placement is performed.

Use only data sources you are authorized to access.


### v3.26.0 feed diagnostic change

OddsHarvester now defaults to the documented `1x2,btts,over_under` market identifiers and records the requested market list plus bounded stderr/stdout tails when the CLI exits without JSON. This improves diagnosis; it does not guarantee the upstream source will return odds.
