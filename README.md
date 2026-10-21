# SolveEase Booking & Arbitrage Dashboard — v3.30.0

Complete six-file update preserving the existing booking lookup, ticket reconstruction/converter, market scanner, arbitrage checks, recommendations, accumulator maths, Telegram utilities, and regression tests.

## What's new
- **Feed Lab:** a dashboard control runs a live diagnostic scan and returns structured per-source results plus current Odds Pool counts.
- **Better response diagnosis:** HTML pages are identified separately from malformed JSON; explicitly declared character sets are honored, and opaque encodings are not guessed.
- **OddsHarvester diagnostics:** preserves complete command arguments, CLI help excerpt, exit code, and bounded stdout/stderr tails; subprocess decoding errors no longer hide the real failure.
- **Bet9ja XML mapping evidence:** no-market diagnostics include observed quote field names to guide safe mapping without inventing odds.
- **Historical name matching:** adds explicit aliases for recurring booking-slip names including Como 1907, Parma Calcio, Atletico Madrid and Real Sociedad.
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
1. Open `/health` and confirm version `3.30.0`.
2. Open `/status` and tap **Run Feed Lab tests**. This runs a real connector scan; it may take a while if a source times out.
3. Review each source's HTTP status, content type, JSON/schema validity, event counts and error details in the Feed Lab report.
4. Tap **Refresh Odds Pool** and inspect counts/recent observations. If the pool is empty, no connector returned normalized rows in that scan.
5. You can also inspect `/api/feed-lab` (latest diagnostics) and `/api/odds-pool` (pool counts).

## Limitations and safe behavior
- Live provider connectivity has **not** been proven by local unit tests; test actual source results on the deployed service.
- An HTTP 200 or valid JSON does not establish price correctness or source authenticity.
- A retrieval timestamp proves when SolveEase observed a response, not when the bookmaker last changed the price. The UI marks the freshness basis.
- 1X2 odds from the optional aggregator are not used unless all three outcomes are present for the same venue and event.
- Unsupported special markets (e.g. 2UP, win either half, corners/cards/player markets) need their own market-specific settlement and probability models; they must not inherit full-time probabilities.
- A 70% estimate is meaningful only when its model is calibrated out of sample. No forced picks, no guaranteed-win claims, no auto-betting.


## v3.30.0 operational fixes

- SportyBet discovery now has a bounded default fallback list of commonly documented market IDs when `SPORTYBET_MARKET_IDS` is blank. The endpoint is unofficial and these IDs may change; set the environment variable to a verified region-specific list if needed.
- HTTP error logs include a short sanitized upstream diagnostic to help distinguish invalid query parameters from access failures.
- OddsHarvester exit failures write bounded stderr/stdout tails to Render logs as well as the Feed Lab diagnostic payload.
- `/sw.js` now responds with a no-cache self-unregistering worker to clear stale registrations from previous builds instead of returning 404.
- None of these changes guarantees live odds availability; Feed Lab must show usable rows and the Odds Pool must store observations before any downstream use.
