# SolveEase Booking Tools v3.18.0 — Verification

## Automated checks

- `python -m py_compile main.py advanced_features.py test_main.py`: PASS
- `python -m unittest -q`: PASS — 90 tests
- Scheduled fixtures are the default mode.
- The discovery path attempts broad retrieval, a market-ID batch, then up to 100 individual market IDs when a fixture is below the 100-row target or retrieval fails.
- Successful payloads are merged by fixture and market keys; diagnostics report market counts per fixture and the number below target.
- The adapter preserves a synthetic fixture containing 125 market rows.
- The selection threshold remains 70%; maximum model disagreement remains 8 percentage points.
- No automatic bet placement is implemented.

## Verification boundary

Tests use synthetic responses. They do not prove live SportyBet connectivity, that numeric IDs 1–100 are valid or exhaustive, that any particular fixture has 100 distinct open markets, or that probabilities are calibrated. The numeric ID sweep is an unverified discovery attempt and may be rate-limited or rejected. Check deployed diagnostics before relying on market counts. Unsupported settlement families remain excluded unless a suitable model and settlement rules are implemented. The 70% threshold is a provisional qualification filter, not a guarantee of a 70% real-world hit rate.
