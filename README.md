# SolveEase Booking & Arbitrage Dashboard — v3.10.0

A mobile-first, read-only football ticket analysis dashboard. It can look up supported SportyBet booking codes, analyze selections using historical goal rates, scan configured bookmaker market feeds, screen same-fixture replacement markets, compare odds and estimated value, and prepare a filtered ticket summary for manual recreation. It never places bets and never creates destination booking codes.

## v3.10.0 — full-market intelligence and replacement engine

- **Automatic SportyBet market feed attempt:** when `SPORTYBET_MARKET_FEED_ENABLED` is enabled (default), the scanner uses the public SportyBet upcoming-events market endpoint through the existing adapter. This is an unofficial integration; it may change, rate-limit, restrict access, or return incomplete markets. Set `SPORTYBET_MARKET_FEED_ENABLED=0` to disable it, or set `SPORTYBET_ODDS_FEED_URL` to an authorized feed URL.
- **Two-way and three-way market catalog:** full-time result markets can be catalogued; arbitrage calculations remain restricted to complete, matching two-outcome markets.
- **Same-fixture replacement search:** ticket lookup now searches the configured target bookmaker's returned market rows. Team names are matched exactly after normalization/known aliases; the engine never substitutes a different fixture.
- **Common-market priorities:** Double Chance, Over/Under Goals, Match Result and BTTS are currently probability-screened. The feed catalog can expose additional markets, but handicap, corners, Draw No Bet and half-time selections remain REVIEW until exact settlement-aware models are implemented.
- **Two-model sensitivity screen:** independent-Poisson and Dixon-Coles score models are both evaluated using the available goal-rate inputs. The lower probability is used; candidates with more than 12 percentage points of disagreement are rejected. These are related model variants sharing inputs, not independent evidence and not calibrated predictions.
- **Value filter:** a selection must pass the fixed 70% conservative model-estimate threshold and have positive estimated value at the actual parsed decimal price. A lower-priced alternative is not automatically rejected if it has better estimated value; a worse price for the identical selection is dominated and skipped.
- **Price and feed checks:** stale feed data is rejected. When the source has no market-update timestamp, the output says freshness is unknown rather than calling it verified fresh. Cross-bookmaker availability counts only reflect the feeds configured and observed by this app.
- **Full-ticket allocation:** original selections remain KEEP only if they pass the probability, model-agreement, odds-availability and estimated-value checks. A provisional REPLACED selection must come from a configured target-bookmaker feed and pass the same-fixture, market-support, probability, model-agreement and value gates. Otherwise it remains REVIEW.
- **3-way result support:** the odds catalogue can now preserve 1X2 outcomes without treating them as a two-way arbitrage market.
- **Auditable limits:** missing data stays unavailable; no odds, team statistics, probabilities or alternative markets are invented. The original booking code is cleared and is not persisted in session storage.

### Important model limitation

The historical results provider and selected xG source do not cover every competition. Football-Data.co.uk publishes usage restrictions for its CSV files; review its terms before commercial reuse. The two probability models share the same underlying goal rates and are not out-of-sample calibrated. A 70% estimate is a screening threshold, not proof of a true 70% chance, a guarantee, or betting advice. Estimated value is only as trustworthy as the rates and price source. Verify the exact selection, current price, market rules, and settlement on the bookmaker before use.

## Deployment on Render

- Build command: `pip install -r requirements.txt` (the app uses Python's standard library only).
- Start command: `python main.py`.
- Port: Render's `PORT` is used automatically; defaults to `10000` locally.
- No new secret is required for the default SportyBet public-feed attempt. It can fail or be restricted; `/health` and the feed-status page show whether a usable schema was actually received.
- Keep private tokens in Render environment variables. Do not commit `.env` or secrets.

### Optional environment variables

- `SPORTYBET_MARKET_FEED_ENABLED=1` — enable the best-effort public SportyBet market feed; set `0` to disable.
- `SPORTYBET_ODDS_FEED_URL` — override the SportyBet source with an authorized feed URL.
- `BET9JA_ODDS_FEED_URL`, `ONEXBET_ODDS_FEED_URL`, `BETWAY_ODDS_FEED_URL` — optional additional bookmaker feeds. The app does not claim those feeds are configured or available unless they return usable data.
- `REQUEST_TIMEOUT_SECONDS=12`, `FEED_STALE_AFTER_SECONDS=300`.
- `FOOTBALL_STATS_ENABLED=1`, `FOOTBALL_STATS_TIMEOUT_SECONDS=5`, `FOOTBALL_STATS_CACHE_TTL_SECONDS=21600`, `FOOTBALL_STATS_MAX_AGE_DAYS=540`.
- `APP_TIMEZONE=Africa/Lagos` for daily reports.
- `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` for optional Telegram alerts; configuration presence is not proof of delivery.

## API additions

- `POST /api/replacement-search` with a `legs` array containing `event`, `market`, `selection`, `odds`, `home_goal_rate`, `away_goal_rate`, `stats_status`, and optional `source_bookmaker_key`. This route only considers market prices returned by the configured target-bookmaker feed. Historical rates must already be supported; the endpoint does not invent them.
- `GET /health` reports the version, configured/tested feeds, replacement threshold, disagreement limit and currently supported market families.

## Verification

Run:

```bash
python -m py_compile main.py advanced_features.py test_main.py
python -m unittest -v
```

The test suite uses synthetic fixtures and mocks. Passing tests verifies the tested code paths only; it does **not** prove the live SportyBet endpoint is accessible, that a particular fixture/market is offered, that all 47 ticket fixtures are covered by historical data, that the probabilities are calibrated, or that any bet will win.
