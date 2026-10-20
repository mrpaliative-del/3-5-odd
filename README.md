# SolveEase Booking & Arbitrage Dashboard — v3.12.0

## v3.12.0 — Broader market discovery and aligned 70% replacement filter

This release expands the replacement engine beyond a narrow 1X2 / match-total / BTTS set. It retains and evaluates up to 100 outcomes per market row, and the full-time scoreline models now cover:

- 1X2 and Double Chance
- Full-time match totals with explicit lines
- Both Teams To Score
- Home/away team-goal totals with explicit lines
- Team-to-score and clean-sheet yes/no markets
- Win-to-nil yes/no markets
- Total-goal odd/even markets
- Correct/exact score outcomes (evaluated, but naturally unlikely to meet 70%)

Markets for corners, cards, offsides, shots, player props, half-time/period markets, early-goal windows, 1UP/2UP and compound settlements remain excluded until dedicated data and settlement models exist. The engine records fixture-market coverage and outcome counts so an empty candidate list can be diagnosed rather than mistaken for a complete scan.

### Selection policy

- Minimum conservative model probability: **70%**.
- The candidate must have two available score-model estimates, both at/above 70%.
- Model disagreement must be no more than **8 percentage points**.
- Odds must be observed within the configured freshness window and the conservative probability × decimal odds must imply positive estimated value.
- Equivalent markets are preferred. Cross-market alternatives are separately labelled and their settlement difference is disclosed.
- No candidate is forced when evidence is missing.

The models remain **uncalibrated**. The 70% threshold is a filter on provisional estimates, not a verified 70% real-world win rate. Correct score and other long-shot market types will usually fail the threshold, as they should.

### SportyBet market discovery

When the configured SportyBet URL is the site homepage, the adapter now requests the endpoint's broader default market set by omitting a hard-coded `marketId` list. If the endpoint requires or works better with a specific set, configure `SPORTYBET_MARKET_IDS` with known IDs obtained legitimately from the source. The endpoint is unofficial and can change; broad retrieval is an attempt, not a guarantee that every market is returned.

For SportyBet rows without a provider quote-update timestamp, the engine may use the fresh retrieval time for its observation-age check and labels that basis explicitly as `retrieved_at; provider timestamp unavailable`. It never rewrites that value as a provider timestamp.

### Configuration

- `PORT=10000`
- `MIN_PROBABILITY=0.70`
- `FEED_STALE_AFTER_SECONDS=300` (default)
- `SPORTYBET_ODDS_FEED_URL` — use an authorized JSON feed URL or SportyBet homepage for the built-in public endpoint adapter
- `SPORTYBET_MARKET_IDS` — optional known market IDs; leave blank to ask for the endpoint's default market set
- `GLOBAL_ODDS_FEED_URLS` — optional semicolon/comma-separated list of authorized global JSON feed URLs

Keep secrets out of GitHub and set them in Render environment settings. The tool is read-only and does not place bets. No live SportyBet connectivity or out-of-sample probability calibration is claimed by automated tests.
