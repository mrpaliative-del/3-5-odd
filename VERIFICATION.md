# SolveEase v3.13.0 Verification

## Automated checks

- `python -m py_compile main.py advanced_features.py test_main.py`: PASS
- `python -m unittest -q`: PASS — 83 tests
- Multi-outcome normalization: tested with 25 exact-score outcomes
- Added scoreline model families: team totals, total-goal parity, clean sheet and exact score checked against both provisional models
- Unsupported corners, half-time, early-goal and 1UP/2UP markets remain unmodelled
- Default threshold remains 70%; maximum two-model disagreement tightened from 12 to 8 percentage points
- SportyBet homepage request no longer hard-codes the previous short market-ID list; optional `SPORTYBET_MARKET_IDS` override documented
- Feed retrieval time is kept separate from provider-reported quote timestamp
- Market-coverage counters expose fixture market rows, outcomes, modelled outcomes and qualifying candidates

## Verification boundary

Tests use synthetic fixtures and do not prove live SportyBet availability, that its endpoint returns every available market, source authorization, odds correctness, real-world probability calibration, or profitability. The full-time score models are provisional and uncalibrated. The 70% threshold is a qualification rule, not a guarantee of a 70% hit rate. Unsupported market families fail closed rather than borrowing a goal model.


## Additional v3.13.0 coverage

- Added default SportyBet public-endpoint discovery when no explicit odds feed URL is configured; other bookmaker feeds remain opt-in.
- Added deterministic test confirming the adapter requests the endpoint without guessing a `marketId`.
- Added `SPORTYBET_AUTO_DISCOVERY=0` opt-out.
- Automated tests do not prove the live endpoint will respond or return every market; live connectivity must be checked in deployment feed diagnostics.
