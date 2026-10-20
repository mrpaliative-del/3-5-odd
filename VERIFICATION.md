# SolveEase Booking Tools v3.20.0 — Verification

## Checks performed for this package

- Source version and server/API version updated to 3.20.0.
- Added a UI selector for choosing among multiple candidates returned by the replacement engine.
- Candidate selection updates the retained reconstruction ticket in session storage; no booking code is stored.
- Strict 70% conservative-probability gate, <=8 percentage-point model disagreement, positive estimated value, market support and freshness gates remain unchanged.
- Scheduled fixtures only; no auto-betting.

## Verification boundary

The multi-candidate UI can only show candidates returned by the backend. It does not repair live provider errors or prove that 100+ distinct open markets are available for any fixture. Synthetic unit tests cannot prove live SportyBet connectivity, bookmaker schema stability, fixture coverage, probability calibration, or profitability. The estimates remain uncalibrated and require independent backtesting against settled results.
