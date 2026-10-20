# SolveEase v3.10.1 Verification

Local checks performed for this package:

- Python compilation: `main.py` and `advanced_features.py` passed.
- Unit tests: 52 tests passed, including replacement-gate tests for fixture matching, fresh timestamps, supported common markets, two-model threshold, positive estimated value, and allowing a lower-odds candidate when the value gates pass.
- Front-end JavaScript extracted from the embedded dashboard passed `node --check`.
- ZIP integrity checked after packaging.

Not verified by these checks: live Render deployment, real bookmaker prices, SportyBet endpoint stability, feed authenticity, complete market coverage, historical coverage for every team, model calibration, or profitability.

The replacement engine requires actual configured feed rows with parseable source timestamps. It intentionally refuses to recommend alternatives when those data are missing or stale. The 70% threshold is unchanged. No automatic bet placement is implemented.
