# SolveEase Booking & Arbitrage Dashboard — v3.28.0

Complete six-file update preserving the existing booking lookup, ticket reconstruction/converter, market scanner, arbitrage checks, recommendations, accumulator maths, Telegram utilities, and regression tests.

## What's new
- **SolveEase Odds Pool:** normalized odds observations are stored before they are exposed to downstream analysis.
- **SQLite persistence:** append-only observation history and per-source polling diagnostics; retention and row caps are configurable.
- **Verification gate:** validates fixture/market presence, outcome shape, decimal prices, duplicates and freshness. Rows with stale or unknown timestamps are quarantined; malformed rows are rejected.
- **Transparent status labels:** `verified_feed_integrity` means only that the observed row passed structural and freshness checks. It does **not** prove bookmaker authenticity, settlement equivalence, model calibration or profitability.
- **OddsRelay optional adapter:** uses the documented server-side Bearer key and the standard `h2h` product for the selected region. It requires an API key for prices; keyless health/coverage endpoints are not mistaken for odds. Only complete 1X2 outcome sets are accepted.
- **70% qualification principle retained:** a probability threshold is not itself a guarantee; replacements still require supported models, disagreement/value/freshness checks, and no forced selection.
- No automatic bet placement and no bookmaker credentials requested from users.

## Render deployment
Build command:
```bash
pip install -r requirements.txt && playwright install chromium
```
Start command:
```bash
python main.py
```

Attach a Render persistent disk mounted at `/var/data` for durable pool history. Set `ODDS_POOL_DB_PATH=/var/data/solveease_odds_pool.sqlite3`. Without a persistent disk, the database may be lost when the instance is replaced.

## Optional OddsRelay configuration
In Render → service → Environment, add:
- `ODDSRELAY_KEY` = your private server-side OddsRelay API key
- `ODDSRELAY_REGION` = a region supported by your plan (default `uk`)
- `ODDSRELAY_ENABLED` = `1`

Do not put the key in browser code, public files, GitHub, or chat. The adapter is optional; other sources continue to run if it is not configured. Check the API provider's current pricing, coverage and quota before enabling polling.

Optional pool controls:
- `ODDS_POOL_DB_PATH` — database file path
- `ODDS_POOL_MAX_ROWS` — maximum retained observations (default 50,000)
- `ODDS_POOL_RETENTION_DAYS` — age retention (default 30 days)

## Verify after deployment
1. Open `/status`.
2. Run a market scan from `/scanner`.
3. Use **Refresh Odds Pool** in the status page.
4. Check the counts and recent rows. If the pool is empty, the source layer has not returned normalized observations.
5. Confirm `/health` reports version `3.28.0` and `/api/odds-pool` returns a JSON summary.

## Limitations and safe behavior
- Live provider connectivity has **not** been proven by local unit tests; test actual source results on the deployed service.
- An HTTP 200 or valid JSON does not establish price correctness or source authenticity.
- A retrieval timestamp proves when SolveEase observed a response, not when the bookmaker last changed the price. The UI marks the freshness basis.
- 1X2 odds from the optional aggregator are not used unless all three outcomes are present for the same venue and event.
- Unsupported special markets (e.g. 2UP, win either half, corners/cards/player markets) need their own market-specific settlement and probability models; they must not inherit full-time probabilities.
- A 70% estimate is meaningful only when its model is calibrated out of sample. No forced picks, no guaranteed-win claims, no auto-betting.
