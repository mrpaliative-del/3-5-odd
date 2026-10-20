# SolveEase Booking & Arbitrage Dashboard — v3.10.3

Mobile-friendly, read-only football booking lookup and analysis dashboard. It does not place bets or log into bookmaker accounts.

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
- Current feed normalizers are conservative and can omit market types they cannot parse safely, including some three-outcome markets. Missing data is not interpreted as a bad team or a losing selection.
- Football-Data.co.uk history and optional Understat xG are best-effort; coverage varies. Check source usage terms before commercial use.
- Odds implied probability, model probability and estimated value are different measures. Expected value is only meaningful if model estimates are credible and prices are current.
- No automatic betting, no invented odds, no invented probabilities, and no guarantee of profit.

## Deploy on Render

- Build command: `pip install -r requirements.txt` (dependencies are standard-library only).
- Start command: `python main.py`.
- Render supplies `PORT`; local default is `10000`.
- Keep existing secrets in Render environment variables. Never upload a real `.env` file to GitHub.
- Optional odds feed variables: `SPORTYBET_ODDS_FEED_URL`, `BET9JA_ODDS_FEED_URL`, `ONEXBET_ODDS_FEED_URL`, `BETWAY_ODDS_FEED_URL`.
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
