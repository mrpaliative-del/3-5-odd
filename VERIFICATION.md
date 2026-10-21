# v3.28.0 verification record

- Python syntax compilation: run locally before packaging.
- Automated regression tests: 103 tests passed, including fresh observation storage, stale-source quarantine, invalid odds rejection, and mocked OddsRelay parsing/key-gating tests.
- Odds Pool tests use synthetic observations; they do not prove that any live bookmaker or aggregator is reachable.
- OddsRelay adapter follows the published `/v2/odds/standard` response concept, uses server-side Bearer authentication, reads per-venue `meta.last_seen`, and refuses incomplete 1X2 rows. Actual account coverage and price retrieval require deployment testing.
- SQLite persistence must be verified after a Render restart to prove the persistent disk is mounted at the configured path.
- Full verification requires checking `/health`, running `/api/scan`, checking `/api/odds-pool`, and inspecting real source diagnostics.
- The 70% gate is not proof of calibration or profitability. Historical backtesting and out-of-sample calibration are still required.
