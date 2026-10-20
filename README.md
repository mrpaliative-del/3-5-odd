# SolveEase Booking & Arbitrage Dashboard — v3.15.0

## v3.15.0 — Scheduled + live discovery and broad-market first pass

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

By default, SolveEase now attempts SportyBet public market discovery even when `SPORTYBET_ODDS_FEED_URL` is blank. Set `SPORTYBET_AUTO_DISCOVERY=0` to disable this behavior. This avoids the prior failure mode where every provider was marked “not configured” and the replacement engine had no odds rows to compare. The request is reported in feed health, including errors and schema failures. The public endpoint remains unofficial; availability is not guaranteed.

The built-in discovery now attempts scheduled (`factsCenter/pcUpcomingEvents`) and in-play (`factsCenter/pcLiveEvents`) football feeds by default, controlled by `SPORTYBET_MARKET_MODE=both|scheduled|live`. It first requests a broad/default market payload without `marketId`; if no usable markets are returned, it retries with the known-ID fallback. `SPORTYBET_MARKET_IDS` can override the fallback list. Responses are bounded to 12 MB, page size is configurable up to 30, and diagnostics record each mode, HTTP result, response size, and fallback use. These are unofficial website endpoints; the live endpoint/schema is not guaranteed. Broad-market discovery must still be verified in deployed diagnostics, and the number of returned rows is not the same as the number of distinct market types.

For SportyBet rows without a provider quote-update timestamp, the engine may use the fresh retrieval time for its observation-age check and labels that basis explicitly as `retrieved_at; provider timestamp unavailable`. It never rewrites that value as a provider timestamp.

### Configuration

- `PORT=10000`
- `MIN_PROBABILITY=0.70`
- `FEED_STALE_AFTER_SECONDS=300` (default)
- `SPORTYBET_AUTO_DISCOVERY=1` — default on; set to `0` to disable built-in SportyBet discovery
- `SPORTYBET_MARKET_MODE=both` — `both`, `scheduled`, or `live`
- `SPORTYBET_DISCOVERY_PAGE_SIZE=20` — fixtures requested per mode (1–30)
- `SPORTYBET_ODDS_FEED_URL` — optional authorized JSON feed URL; a configured feed replaces automatic discovery
- `SPORTYBET_MARKET_IDS` — optional comma-separated IDs used only for fallback; blank means broad request first
- `GLOBAL_ODDS_FEED_URLS` — optional semicolon/comma-separated list of authorized global JSON feed URLs

Keep secrets out of GitHub and set them in Render environment settings. The tool is read-only and does not place bets. No live SportyBet connectivity or out-of-sample probability calibration is claimed by automated tests.
