# v3.8.1 verification record

Verified locally in the build environment on 2026-10-19:

- `python -m py_compile main.py advanced_features.py test_main.py` — passed.
- `python -m unittest -q` — 35 tests passed.
- Dashboard updated to show three primary tools; retained-selection handoff added to the converter; default selection filters set to 70%.
- Local HTTP smoke test — 200 responses for `/`, `/lookup`, `/converter`, `/scanner`, `/recommendations`, `/accumulator`, `/status`, `/daily`, `/optimize`, `/alerts`, `/health`, and `/api/telegram-status`.
- Local JSON POST smoke tests — 200 responses for `/api/optimize-ticket`, `/api/model-probability`, `/api/calibration-metrics`, and `/api/daily-accumulator` with an empty fixture list.
- Front-end JavaScript syntax check — see build command output; performed when Node.js was available.

Not verified by this record:

- Live bookmaker feed availability, data licensing/authorization, or current odds.
- External fixture-feed compatibility/coverage.
- Telegram delivery (requires private token/chat ID and a received test message).
- Render deployment/redeployment or any real betting outcome.
- Out-of-sample probability calibration. Model outputs are estimates and not guarantees.
