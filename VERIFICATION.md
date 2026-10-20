# SolveEase v3.15.0 Verification

## Automated checks

- `python -m py_compile main.py advanced_features.py test_main.py`: PASS
- `python -m unittest -q`: PASS — 88 tests
- Scheduled/live discovery: synthetic tests verify both modes are requested in `both` mode.
- Broad-first request: synthetic tests verify that `marketId` is omitted unless explicitly configured; if no usable rows are found, a known-market-ID fallback is attempted.
- Market breadth: synthetic fixture with 125 market rows is retained by the SportyBet adapter.
- Market identifiers: market ID, specifier, event ID, start time, market group and outcome IDs are preserved when available.
- Event IDs are deduplicated when live and scheduled feeds overlap.
- JSON response limit is 12 MB; discovery page size is configurable from 1 to 30 fixtures.
- Default selection threshold remains 70%; model disagreement cap remains 8 percentage points.

## Verification boundary

These are automated tests against synthetic payloads. They do not prove the unofficial SportyBet endpoints are currently reachable, that `pcLiveEvents` uses the assumed schema, that a broad request returns all available markets, or that probabilities are calibrated. The deployed feed diagnostics must confirm live and scheduled HTTP status, usable market rows, and per-fixture market counts. SportyBet does not publish an official public developer API contract; its site endpoints may change. Unsupported market families must remain unmodelled until their settlement logic and probability model are implemented and validated. The 70% threshold is a qualification rule, not a guarantee of a 70% real-world hit rate.
