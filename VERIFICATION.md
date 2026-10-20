# SolveEase v3.10.0 verification record

Checks performed in the build environment:
- Python compilation: `main.py`, `advanced_features.py`, and `test_main.py`.
- Unit tests using synthetic fixtures and mocked feeds.
- Same-fixture candidate selection, target-bookmaker restrictions, stale-feed rejection, missing-statistics rejection, 3-way market cataloguing, and lower-odds alternatives that pass the estimated-value gate.
- ZIP integrity check after packaging.

The test suite does not contact live bookmakers in its replacement tests. It does not establish live SportyBet endpoint availability, completeness/currentness of real odds, coverage of all leagues/fixtures, model calibration, profitability, or live Render deployment.

Model notes: the Poisson and Dixon-Coles variants share the same historical goal-rate inputs and are not independent evidence. The conservative minimum, 70% threshold, 12 percentage-point disagreement gate, and positive estimated-value screen are safety filters, not calibrated probability guarantees. Only full-time result, Double Chance, totals and BTTS are currently auto-screened. Other markets remain REVIEW until exact market/settlement models are supported.
