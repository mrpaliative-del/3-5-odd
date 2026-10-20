# SolveEase Booking & Arbitrage Dashboard — v3.20.0

## v3.20.0 — Multi-candidate replacement selection

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

By default, SolveEase attempts SportyBet public market discovery even when `SPORTYBET_ODDS_FEED_URL` is blank. Set `SPORTYBET_AUTO_DISCOVERY=0` to disable it. Scheduled/upcoming fixtures only are used. The endpoint is unofficial and may fail, change schema, or return fewer markets than expected. Broad discovery is attempted first; explicitly configured `SPORTYBET_MARKET_IDS` may be used for fallback. Numeric IDs are not guessed. Actual market counts are reported from returned data; the app never fabricates markets to meet the 100-option target.

For rows without a provider quote-update timestamp, the engine may use the fresh retrieval time for its observation-age check and labels that basis explicitly as `retrieved_at; provider timestamp unavailable`. This is not proof that the bookmaker price remained unchanged during that interval.

### Multi-candidate replacement selection

When a ticket selection qualifies for replacement and the engine returns multiple candidates, the lookup view exposes a selector to choose among them. The selector only offers candidates returned by the backend after its current checks. Strict equivalent market/selection candidates are distinguished from alternative markets with different settlement propositions. The user's choice updates the in-memory/session reconstruction ticket; the user must still verify the price and settlement rules manually. This does not guarantee multiple candidates will exist: feed failures, limited coverage, or insufficient team history can leave a selection in `REVIEW`.

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


## v3.20.0 safety updates

- Adds a multi-candidate selector for selections the engine has decided to replace.
- Keeps the existing independent booking-code lookup, ticket converter and arbitrage scanner.
- Keeps the conservative 70% probability threshold, maximum 8-point model disagreement, positive estimated-value requirement, fresh-observation checks and supported-market restrictions.
- Does not place bets and does not invent missing odds or probabilities.
- Model estimates remain uncalibrated; automated tests cannot prove live provider connectivity, 100+ distinct open markets per fixture, or profitability.
