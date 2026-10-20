# SolveEase Booking & Arbitrage Dashboard — v3.22.0

## v3.22.0 — Multi-bookmaker discovery fallback

This patch keeps the existing dashboard and adds an optional OddsHarvester adapter for scheduled football markets from OddsPortal. SportyBet remains the reference ticket; it is no longer the only source allowed to provide candidate prices.

### What changed

- Adds OddsHarvester CLI integration for scheduled fixtures and these market families: `1x2`, `btts`, `double_chance`, `draw_no_bet`, `over_under`, and `asian_handicap`.
- Parses multiple common JSON output shapes and retains each bookmaker's name, fixture, market/line, decimal odds, and available timestamps.
- Deduplicates exact source rows and caches results for 15 minutes by default.
- Adds explicit feed diagnostics for missing CLI, timeouts, invalid output, zero rows, and successful parsed rows.
- Leaves existing SportyBet lookup, ticket converter, arbitrage scanner, manual confirmation, and probability/settlement filters intact.

### Render deployment

Set the Render **Build Command** to:

```bash
pip install -r requirements.txt && playwright install chromium --with-deps
```

Keep the Start Command as:

```bash
python main.py
```

Browser installation is required for OddsHarvester. It is resource-heavy compared with the original standard-library-only app; if the free instance cannot install or run Chromium, set `ODDSHARVESTER_ENABLED=0` and provide an authorized external JSON feed using `GLOBAL_ODDS_FEED_URLS` instead.

### Environment variables

- `ODDSHARVESTER_ENABLED=1` (default; set `0` to disable)
- `ODDSHARVESTER_TIMEOUT_SECONDS=75` (15–150 seconds)
- `ODDSHARVESTER_CACHE_TTL_SECONDS=900` (15-minute default cache)
- `ODDSHARVESTER_MARKETS=1x2,btts,double_chance,draw_no_bet,over_under,asian_handicap`
- Existing source URLs remain optional; the scraper does not require bookmaker login credentials.

### Market counts and probabilities

OddsHarvester can return many real market lines across fixtures and bookmakers, but neither 100+ options per fixture nor one or two qualifying replacements is guaranteed. The dashboard must report actual unique retrieved options. OddsHarvester odds are prices, not probabilities. The existing 70% threshold still depends on the app's own model/evidence rules; the scraper never invents probabilities from odds or community votes.

Strictly equivalent replacements must match fixture, market, line, outcome, and settlement rules. Other markets are labelled as alternatives and require manual approval. No automatic bet placement is performed.

### Limitations

OddsHarvester relies on browser automation and OddsPortal availability, regional bookmaker coverage, and the site's current page structure. This patch is code-level integration; it has not been live-validated from the user's Render instance. Confirm the new OddsHarvester diagnostic shows `ok` and a positive `events_loaded` count before treating it as operational.
