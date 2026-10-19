# SolveEase Booking & Arbitrage Dashboard — v3.8.8

This release improves automatic football-statistics coverage by checking up to three seasons of configured Football-Data.co.uk CSV results for each supported division, instead of relying on the current season and a conditional previous-season fallback. It can improve early-season history availability; it does not guarantee coverage for every competition or every team-name variant.

The model still refuses to invent probabilities when statistics cannot be matched. A displayed probability is a model estimate, not a guarantee or proof of betting value. The public Football-Data.co.uk files have usage restrictions; review the publisher's terms before commercial reuse.

## Deploy on Render
- Build command: `pip install -r requirements.txt` (or leave blank; standard-library only).
- Start command: `python main.py`
- The service listens on `PORT` (Render supplies this automatically; defaults to 10000 locally).
- Keep your existing environment variables and secrets in Render; do not commit `.env`.

## Validation
Run `python -m unittest -q`. Unit tests use synthetic fixtures and do not prove live CSV availability, team matching, bookmaker-feed availability, or Telegram delivery.
