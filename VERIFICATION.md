# SolveEase v3.10.7 Verification

- `python -m py_compile main.py advanced_features.py test_main.py`: PASS
- `python -m unittest -q`: PASS — 73 tests
- Additional all-season leagues route through `/new/{CODE}.csv`; the tests mock HTTP and do not contact the live publisher.
- All-season CSV rows receive a season code from the match date and are filtered to the recent rolling seasons.
- Team-coverage failure payload includes configured/loaded division counts, loaded match count and team-level missing/small-sample diagnostics.
- Health metadata and HTTP server version updated to 3.10.7.
- Existing 36-leg ticket arithmetic regression and special-market rejection tests retained.

Not tested: live source availability, live SportyBet lookup, odds feed freshness, prediction calibration, or exact settlement of early-goal/1UP/2UP markets. Historical data source use restrictions must be reviewed before commercial reuse.


## v3.10.7 validation scope

- Added optional `GLOBAL_ODDS_FEED_URLS` support for up to eight authorized generic JSON market feeds.
- Replacement comparisons require alias-aligned fixtures and identical supported market/selection signatures, including the exact goal line and side.
- The same market/outcome can be compared across distinct configured price sources; cross-market substitution is blocked.
- Added tests for explicit team-alias fixture matching, preserved home/away order, and strict same-market/line/selection alignment.
- Added tests confirming special-market legs cannot be cross-market substituted.
- Unit tests use synthetic feed data; they do not verify any external provider's availability, licensing, completeness, timestamp authenticity, or odds accuracy.
