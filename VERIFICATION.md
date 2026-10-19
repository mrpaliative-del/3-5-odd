# v3.9.0 verification record

- Python compilation: passed (`main.py`, `advanced_features.py`, `test_main.py`).
- Automated unit tests: 41 passed.
- Statistics coverage: each configured division now checks up to three season CSVs and merges/deduplicates available rows before matching teams.
- The 70% display threshold and no-fabricated-probability behavior are unchanged.
- Live provider access, CSV availability, fixture-name matching, and Telegram delivery are not verified by local tests; verify these after deployment.
- Football-Data.co.uk public CSVs have usage restrictions; check the publisher's terms before commercial reuse.


## v3.9.0 changes

- Improved privacy-safe HTTP request logging so endpoint and status can be diagnosed in Render logs.
- Improved browser network error reporting to identify the failing endpoint without exposing booking codes.
- The 70% filter threshold is unchanged.
