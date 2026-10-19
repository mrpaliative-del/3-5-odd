# v3.8.4 verification record

- Python compilation: passed (`main.py`, `advanced_features.py`, `test_main.py`).
- Automated unit tests: 37 passed.
- Embedded browser JavaScript syntax: passed with `node --check`.
- Automatic lookup flow: booking code is cleared immediately; lookup and statistical analysis run in sequence; only model-estimated selections at or above 70% are rendered and persisted.
- Persistence safety: only the filtered selection set is saved; legacy unfiltered session data is discarded on converter restore.
- Failure behavior: if automatic statistics cannot return usable JSON, the app shows a concise error and does not present odds-implied ranking as a model forecast.
- Live provider access and Telegram delivery are not verified by local tests; test these after deployment.
