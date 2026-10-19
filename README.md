# SolveEase Booking & Arbitrage Dashboard v3.5.1

A mobile-friendly, read-only Python dashboard. No third-party Python packages are required.

## Run locally

```bash
python main.py
```

Open `http://127.0.0.1:10000` unless `PORT` is set to another value.

## Render deployment

- Runtime: Python 3
- Build command: `pip install -r requirements.txt`
- Start command: `python main.py`

Render supplies `PORT`; do not hard-code it in the deployment dashboard.

## Optional environment variables

- `REQUEST_TIMEOUT_SECONDS` — outbound request timeout (default `12`)
- `MIN_PROBABILITY` — minimum feed-supplied probability for display (default `0.60`)
- `BANKROLL` — non-negative bankroll setting (default `100000`; not a promise of returns)
- `FEED_STALE_AFTER_SECONDS` — timestamp age threshold (default `300`)
- `SPORTYBET_ODDS_FEED_URL`
- `BET9JA_ODDS_FEED_URL`
- `ONEXBET_ODDS_FEED_URL`
- `BETWAY_ODDS_FEED_URL`

Only configure feeds you own or are authorized to access. Feeds must return the expected response format; a configured URL does not prove that the feed is live or valid. Do not add bookmaker passwords or account credentials.

## Routes

- `/` — home
- `/lookup` — read-only SportyBet ticket lookup
- `/converter` — manual ticket reconstruction checklist
- `/scanner` — feed scan, theoretical two-outcome arbitrage, feed-provided probability candidates, and accumulator arithmetic report
- `/status` — source/service diagnostics
- `/health` — JSON health status
- `POST /api/lookup` — JSON body: `{"code":"...","bookmaker":"auto"}`
- `POST /api/scan` — JSON body: `{}`

## Accumulator calculator

Enter one leg per line in this format:

`Selection label | decimal odds`

The calculator reports combined odds, raw odds-implied probability, gross return and net profit for the entered stake. These are arithmetic only—not predictions, guaranteed returns, or a recommendation to bet. Correlated legs, bookmaker margin, price movement, void rules, limits and settlement terms can change outcomes.

## Important limitations

- SportyBet booking lookup relies on an unofficial endpoint that can change.
- Bet9ja, 1xBet and Betway feed adapters are conservative and only normalize recognized structures. Real feed schemas vary; use `/status` to verify actual results.
- A feed with no usable markets does not prove there are no opportunities.
- The scanner only checks two-outcome markets and requires normalized event/market/outcome labels to match. It does not verify that different bookmakers' settlement rules are equivalent.
- Probability candidates use estimates provided by a configured feed; SolveEase does not independently validate them.
- The dashboard does not place bets or generate destination booking codes.

## Validation performed

This delivery was checked for Python syntax, inline JavaScript syntax, local HTTP route responses, and synthetic positive/negative arbitrage cases. External bookmaker services were not verified from this test environment, so live-feed operation must be confirmed after deployment.
