# Verification record for SolveEase v3.6.1

Verification was performed in a local Python 3 environment with no bookmaker feed URLs configured. These checks establish code behavior for the tested cases; they do not establish live bookmaker access, model calibration, or betting profitability.

## Passed checks

- `python -m py_compile main.py test_main.py`
- `python -m unittest -v` — 18 tests pass
- Embedded dashboard JavaScript syntax checked with `node --check`
- Local HTTP smoke test: `/`, `/health`, `/scanner`, and `/status` returned HTTP 200
- Local API smoke tests: valid `POST /api/analyze-betslip` and `POST /api/scan` returned HTTP 200; the model returned a probability for a supported totals market
- Local API validation test: empty betslip returned HTTP 400 with a validation message
- `SCAN_ONCE=1 python main.py` emitted valid JSON and exited without starting the persistent HTTP server
- Unconfigured feeds were not contacted; the one-shot scan correctly returned zero usable markets

- Embedded dashboard JavaScript passed `node --check`

## Not verified

- Live SportyBet, Bet9ja, 1xBet or Betway feed reachability and current schema compatibility
- Freshness or accuracy of bookmaker odds
- Accuracy/calibration of the independent-Poisson estimates on historical match data
- Any positive expected return or profitability
- Render deployment after replacing the existing repository

## Important model boundary

The betslip model uses goal rates supplied by the user. It does not automatically retrieve recent form, league averages, head-to-head records or live team statistics. The displayed estimated edge is a mathematical comparison of a model estimate and the entered odds, not proof of real value. No bets are placed.


Version 3.6.1 adds a bounded catalog of real normalized markets from the latest scan and lets users add a selected outcome to the analysis JSON after entering expected-goal rates. The goal rates remain explicit user inputs; no team statistics are invented.
