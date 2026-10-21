# v3.31.0 verification record

- Python syntax compilation completed for `main.py` and `advanced_features.py`.
- Automated tests include all v3.28.0 regression cases plus Feed Lab route presence, HTML-vs-JSON diagnostics, declared-charset decoding, refusal to guess unknown encodings, expanded explicit team aliases, and structured OddsHarvester diagnostics.
- Tests use synthetic payloads and mocks. They do not prove any live bookmaker or aggregator is reachable or returning usable odds.
- Feed Lab runs the existing real connector scan and returns per-source HTTP/schema/row diagnostics plus current Odds Pool counts.
- OddsHarvester now exposes command arguments, CLI help excerpt, exit code, and bounded output tails to make exit-code failures diagnosable; a live scrape still needs a deployed test.
- Bet9ja no-market responses expose observed XML quote field names for safe adapter mapping. No uncertain odds values are inferred.
- The 70% gate is retained; these changes do not establish probability calibration or profitability. Market-specific settlement models for 1UP/2UP and Team to Win Either Half remain separate validation work.
- Render persistence still requires a real restart test with a disk mounted at `/var/data` and `ODDS_POOL_DB_PATH` configured.


## v3.31.0 additional checks

- Added regression coverage for the SportyBet market-ID fallback when the environment variable is blank.
- Confirmed bounded source-error logging and stale service-worker route in source review.
- Live provider access is not verified by local tests.


## v3.31.0 additional regression checks

- Removed guessed default SportyBet market-ID fallbacks. With no operator-configured IDs, only the broad request is attempted and the report explains why fallback was skipped.
- Changed the SportyBet market breadth trigger from an unrealistic hard-coded 100 rows to a configurable minimum of 3 market rows per fixture (bounded 1–20). This is only a scan/recovery threshold, not a completeness guarantee.
- Ticket REVIEW cards no longer repeat long source diagnostics; full per-source diagnostics remain in Feed Lab and the API response.
- REVIEW model estimates are explicitly labeled unqualified, and common team-name aliases were added.
- These changes do not prove that SportyBet, Bet9ja, Betway, 1xBet, or OddsHarvester is returning usable live data.
