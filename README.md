# SolveEase Booking & Arbitrage Dashboard — v3.18.0

## v3.18.0 — Scheduled fixtures and 100-ID market sweep

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

Scheduled fixtures are the default (`factsCenter/pcUpcomingEvents`); live fixtures are not scanned unless `SPORTYBET_MARKET_MODE=live` is explicitly set. `SPORTYBET_MARKET_MODE=scheduled|live|both` remains configurable, but scheduled is the default. Discovery tries a broad request, then a market-ID batch, then (if fewer than 100 market rows are returned or requests fail) probes up to 100 configured IDs individually using bounded concurrency. Successful responses are merged by fixture and duplicate markets are removed. The default ID sweep is numeric IDs 1–100 and is a discovery experiment, not a verified catalogue: SportyBet may use sparse/nonsequential IDs, reject requests, rate-limit, or return fewer than 100 distinct open markets. Diagnostics expose per-ID status and actual rows. `SPORTYBET_MARKET_IDS` can override the ID list; responses are bounded to 12 MB and page size is configurable up to 100 fixtures. These unofficial endpoints do not guarantee access to all markets or prove calibrated probabilities; verify live diagnostics.

For SportyBet rows without a provider quote-update timestamp, the engine may use the fresh retrieval time for its observation-age check and labels that basis explicitly as `retrieved_at; provider timestamp unavailable`. It never rewrites that value as a provider timestamp.

### Configuration

- `PORT=10000`
- `MIN_PROBABILITY=0.70`
- `FEED_STALE_AFTER_SECONDS=300` (default)
- `SPORTYBET_AUTO_DISCOVERY=1` — default on; set to `0` to disable built-in SportyBet discovery
- `SPORTYBET_MARKET_MODE=scheduled` — default; `scheduled`, `live`, or `both`
- `SPORTYBET_DISCOVERY_PAGE_SIZE=100` — scheduled fixtures requested (1–100)
- `SPORTYBET_ODDS_FEED_URL` — optional authorized JSON feed URL; a configured feed replaces automatic discovery
- `SPORTYBET_MARKET_IDS` — comma-separated market IDs used by the combined and individual-ID recovery attempts; blank uses numeric IDs 1–100 as an unverified discovery sweep
- `GLOBAL_ODDS_FEED_URLS` — optional semicolon/comma-separated list of authorized global JSON feed URLs

Keep secrets out of GitHub and set them in Render environment settings. The tool is read-only and does not place bets. Automated tests do not prove live SportyBet connectivity, complete market coverage, or out-of-sample probability calibration.


### Market-count interpretation

The requested target is 100 distinct market rows per fixture, not 100 fixtures. Actual counts are measured from returned rows after duplicate removal. A count below 100 is reported as incomplete coverage; the engine does not fabricate markets to fill the target. Probability thresholding still applies only to supported full-time markets with adequate historical evidence, fresh odds, two provisional estimates, model agreement and positive estimated value.
