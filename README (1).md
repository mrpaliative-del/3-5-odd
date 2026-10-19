# SolveEase Booking & Arbitrage Dashboard v3.5.3

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
- SportyBet's nested factsCenter response has a public, unofficial schema description; it is not an official developer API and can change.
- Bet9ja's public MarketWS XML operation is documented, but the exact live quote-field payload was not available for verification. The adapter intentionally rejects unclear quotes instead of guessing.
- The 1xBet `BestGamesExtZip` endpoint is an unofficial candidate. The compact live payload has not been confirmed against this app's parser; HTML/anti-bot responses are rejected.
- A flat-array Betway SportsAPI shape was found for South Africa, not Nigeria. The app no longer redirects a Nigeria URL to a South African endpoint; configure only a verified feed for the intended region.
- A feed with no usable markets does not prove there are no opportunities.
- The scanner only checks two-outcome markets and requires normalized event/market/outcome labels to match. It does not verify that different bookmakers' settlement rules are equivalent.
- Probability candidates use estimates provided by a configured feed; SolveEase does not independently validate them.
- The dashboard does not place bets or generate destination booking codes.

## Public evidence reviewed (19 October 2026)

- SportyBet factsCenter endpoint and nested `tournaments → events → markets → outcomes` response: [Sportybet-mcp community documentation](https://github.com/UbongErnest/Sportybet-mcp/blob/main/README.md). This describes an unofficial web endpoint, not a supported public developer API.
- Bet9ja XML operation and documented `SubEvents` / `Quote` / `clsQuoteList` container shape: [GetListOdds_OddLessThan service page](https://web.bet9ja.com/Controls/MarketWS.asmx?op=GetListOdds_OddLessThan). Its sample response does not establish real quote-field names or live values, so the adapter cannot safely claim that those odds are parsed.
- 1xBet `BestGamesExtZip` and `Value` array are described in [community API code](https://github.com/LanceSpezzate/1xbet-api-php/blob/master/_1xbet.php). That does not prove the Nigerian endpoint is currently reachable or that its compact markets match this adapter.
- The flat-array Betway `events` / `markets` / `outcomes` / `prices` shape is documented for South Africa in [scanner-schemas reference](https://github.com/MadunaCapital/scanner-schemas/blob/main/docs/as-built-architecture.md), not for Betway Nigeria. The code deliberately does not redirect Nigerian requests to the South African endpoint.

## Validation performed

This delivery was checked for Python syntax, inline JavaScript syntax, local HTTP route responses, unconfigured-feed behavior, documented-schema fixture cases, and synthetic positive/negative arbitrage cases. Public documentation and community code were reviewed, but external bookmaker services were not reachable from this test environment. Therefore this is a tested integration baseline, not a claim that every live bookmaker feed is currently operational. `/status` is the evidence source after deployment.
