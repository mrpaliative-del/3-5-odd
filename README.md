# SolveEase Booking & Arbitrage Dashboard — v3.10.7

Mobile-friendly, read-only football booking lookup and analysis dashboard. It does not place bets or log into bookmaker accounts.

## v3.10.7 — Global feed configuration and exact market alignment

- Added `GLOBAL_ODDS_FEED_URLS`, an optional semicolon- or comma-separated list of up to eight authorized JSON market-feed endpoints. Each must expose the documented generic `events` schema. The system never guesses providers or contacts an unconfigured URL.
- Global feeds are included in the same feed health, market scan and arbitrage pipeline as configured bookmaker feeds. Each configured URL is treated as one independent price source; do not configure duplicate URLs or mirrors of the same bookmaker as independent sources, as that can distort arbitrage comparisons. Configure only sources you are permitted to use; source availability and correctness are not guaranteed by the integration.
- Fixture comparison now uses the same explicit team aliases as historical-data matching, while preserving home/away order and avoiding fuzzy substring matches.
- Replacement search now requires the same supported market proposition: Over 2.5 can only be compared with Over 2.5, not Over 1.5 or BTTS. The same selection at another bookmaker can qualify as odds-shopping when all freshness and model gates pass.
- Unsupported original markets (including early goals, 1UP/2UP and compound markets) cannot be silently replaced with a different market. They return `unsupported_market` for manual review.
- This expands feed configuration and selection alignment; it does not create global historical results, prove any feed is live, or calibrate the model. Global competition coverage depends on the data source and configured endpoint.

## v3.10.6 — Real all-season data routing and coverage diagnostics

This release corrects the historical-data adapter for additional Football-Data.co.uk leagues. The site's extra leagues are published as all-seasons files at `/new/{CODE}.csv`, not as season-specific `/mmz4281/{season}/{CODE}.csv` files. The adapter now uses the correct endpoint, infers football-season codes from each row date, limits rows to the same recent rolling seasons used by the main leagues, and handles UTF-8/Windows-1252 decoding more safely. Added extra divisions include Austria, Denmark, Finland, Ireland, Norway, Poland, Romania, Russia, Sweden and Switzerland.

When a team still cannot be estimated, the API now returns loaded division counts, loaded match counts and per-team diagnostics distinguishing `TEAM_NOT_FOUND_IN_LOADED_DATA`, `INSUFFICIENT_MATCH_SAMPLE`, and `INSUFFICIENT_VENUE_SAMPLE`. The health endpoint lists configured divisions and explicitly warns that configured sources are not the same as successfully loaded data.

The source publisher lists these additional leagues separately from the season-by-season major divisions; the adapter is based on that distinction. See [Football-Data.co.uk available data](https://football-data.co.uk/all_new_data.php). Its free files have use restrictions; check permissions before any commercial reuse. Live download coverage was not established by unit tests.

## v3.10.5 — Special-market probability guard

This patch closes a market-interpretation defect: early-goal/time-window selections, 1UP/2UP selections, and compound markets (for example, “Home Team or Over 2.5”) are explicitly rejected by the standard full-match goal model. A numeric goal line no longer causes these markets to receive a standard full-match probability. Explicit team aliases were also extended for recurring European and Saudi/Egyptian ticket names. Aliases only help when the corresponding team appears in an available source; they do not add missing league results or establish fixture identity.

## v3.10.4 — Team-name normalization and honest coverage diagnostics

This patch adds explicit, conservative aliases for common club-name variants (for example PSG/Paris Saint-Germain, Man Utd/Manchester United, Wolves/Wolverhampton Wanderers, and accented/transliterated spellings). It does not fuzzy-match similar names or merge youth/reserve teams. When history is missing, the dashboard now explains that this can be caused by unsupported league coverage, unavailable source files, name mismatch, or a sample that is too small; missing coverage is not interpreted as a weak team. The HTTP service and health endpoint report the same release version.

Important: aliases only fix name matching. They do not create historical data for leagues absent from the configured sources, and special 1UP/2UP and early-goals markets remain unsupported for probability estimation until settlement-specific models are validated.

## v3.10.3 — Ticket arithmetic audit and robust market replacement validation

This release adds a separate booking-slip arithmetic audit (`POST /api/audit-ticket-math`) and a mobile dashboard panel. It multiplies every supplied leg price, compares the product with reported total odds, calculates simple stake-times-odds returns, and flags discrepancies without guessing bonus/settlement rules. Invalid leg prices prevent a combined-odds result. The audit does not require historical match data or live odds feeds.

This release also retains the same-fixture replacement search endpoint and connects it to the booking-code filtering workflow. It searches only the market rows returned by configured odds feeds and only recommends a replacement when all gates pass:

- Exact normalized fixture identity, preserving home/away order.
- Common supported market and settlement interpretation: double chance, goal totals, BTTS/GG-NG, and supported match-result labels. Special markets such as 1UP/2UP and early-goals are not guessed.
- Odds are numeric, attributed to a configured bookmaker feed, and carry a parseable source timestamp within `FEED_STALE_AFTER_SECONDS` (default 300 seconds). Fetch time or kickoff time is not substituted for an odds timestamp.
- Both the independent-Poisson and Dixon-Coles baseline estimates are available, each at least 70%, with disagreement no greater than 12 percentage points.
- Conservative estimated expected value is positive at the feed price.
- Lower odds are not rejected automatically; price alone is not the decision rule.

Candidates remain provisional because both models are uncalibrated. A fresh timestamp only confirms the feed's timestamp, not bookmaker authenticity or that a price remains available at bet placement. Always recheck the exact market and odds on the bookmaker site.

### Data coverage and limits

- Booking-code lookup is read-only and currently supports SportyBet's unofficial public endpoint. It may change without notice; there is no official public SportyBet developer API.
- Replacement search requires one or more configured odds feeds. No configured feed or no usable market rows means no replacement is returned.
- Untimestamped/stale prices, unsupported markets, insufficient historical team data, mismatched fixtures, model disagreement, estimates below 70%, or non-positive estimated value all lead to `REVIEW`.
- Current feed normalizers are conservative and can omit market types they cannot parse safely, including some three-outcome markets. Generic global feeds currently use the documented two-outcome market schema. Missing data is not interpreted as a bad team or a losing selection.
- Football-Data.co.uk history and optional Understat xG are best-effort; coverage varies. Check source usage terms before commercial use.
- Odds implied probability, model probability and estimated value are different measures. Expected value is only meaningful if model estimates are credible and prices are current.
- No automatic betting, no invented odds, no invented probabilities, and no guarantee of profit.

## Deploy on Render

- Build command: `pip install -r requirements.txt` (dependencies are standard-library only).
- Start command: `python main.py`.
- Render supplies `PORT`; local default is `10000`.
- Keep existing secrets in Render environment variables. Never upload a real `.env` file to GitHub.
- Optional odds feed variables: `SPORTYBET_ODDS_FEED_URL`, `BET9JA_ODDS_FEED_URL`, `ONEXBET_ODDS_FEED_URL`, `BETWAY_ODDS_FEED_URL`, and `GLOBAL_ODDS_FEED_URLS`.
- `FEED_STALE_AFTER_SECONDS` controls the maximum accepted odds timestamp age (default 300 seconds).

## Validation

Run:

```bash
python -m py_compile main.py advanced_features.py
python -m unittest -q
```

Tests use synthetic inputs and do not prove external feeds are available, complete, authorized, accurate or live. The replacement engine is only as useful as the configured feeds and the historical team data it can match.

### Ticket arithmetic audit input

Send `reported_total_odds`, `stake`, optional `reported_bonus`, optional `reported_payout`, and `legs` (each with a valid decimal `odds`) to `POST /api/audit-ticket-math`. A reported payout can differ from simple stake × odds due to bookmaker-specific rules; the endpoint flags rather than explains such differences.
