# Verification record for SolveEase v3.7.0

The package was tested locally with synthetic fixtures and mocked statistical-provider responses. These checks verify implementation behavior, not live source reachability, model calibration, or betting profitability. Outbound DNS/network access was unavailable in the packaging environment, so live Football-Data.co.uk and Understat requests could not be confirmed.

## Passed checks

- `python -m py_compile main.py test_main.py`
- `python -m unittest -v` — 25 tests pass
- Dashboard JavaScript syntax checked with `node --check`
- CSV result parsing, team-name/event parsing, venue splits, recent-form summaries, automatic-rate fallback, unsupported-data handling, and total-goals line parsing tested with fixtures
- Automatic ticket-analysis UI is triggered after successful booking lookup and provides a probability threshold filter
- Existing scanner, arbitrage, SportyBet adapter fixtures, ticket odds ranking/filtering and Poisson tests remain passing
- Local HTTP smoke tests are run separately after build; no live provider requests are claimed as verified

## Not verified

- Live Football-Data.co.uk CSV download availability in the deployment region
- Live Understat JSON response shape, coverage, and anti-automation availability
- Team matching for every bookmaker's event labels
- Model calibration or out-of-sample accuracy
- Positive expected return, profitability, or any guarantee that a selection will win
- Render deployment after repository replacement

## Model boundaries and data-use limits

- Where Understat xG/xGA is available and matched, its venue-specific match data is blended with result-based rates. Otherwise rates are estimated from historical goals, recent form, venue splits and league averages. Result-derived expected goal rates are not official xG.
- The Poisson model is not trained/calibrated. Estimated edge is not proof of value. No combined accumulator win probability is claimed.
- If team matching or data coverage fails, the system returns no model probability rather than fabricating one. The original ticket is preserved and no bet is placed.
- Football-Data.co.uk states that its free downloads are for private individual use; check current permissions before commercial reuse.
