# SolveEase v3.10.2 Verification

Local checks performed for this package:

- Python compilation: `main.py` and `advanced_features.py` passed.
- Unit tests: 55 tests passed, including replacement-gate tests plus v3.10.2 input validation for invalid probability thresholds and non-finite model disagreement limits.
- Front-end JavaScript extracted from the embedded dashboard passed `node --check`.
- ZIP integrity checked after packaging.
- Feed row and outcome structure checks added so malformed rows are skipped rather than crashing replacement evaluation.
- Invalid replacement thresholds and disagreement bounds now fail fast with clear errors.

Not verified by these checks: live Render deployment, real bookmaker prices, SportyBet endpoint stability, feed authenticity, complete market coverage, historical coverage for every team, model calibration, or profitability.

The replacement engine requires actual configured feed rows with parseable source timestamps. It intentionally refuses to recommend alternatives when those data are missing or stale. The 70% threshold is unchanged. No automatic bet placement is implemented.
