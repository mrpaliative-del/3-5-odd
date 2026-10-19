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


## v3.9.2 diagnostics fix

- Render logs now include HTTP method, safe endpoint path, and response status; request bodies, booking codes, and query strings remain excluded.
- Browser network errors identify whether `/api/lookup` or `/api/analyze-ticket` failed before an HTTP response arrived.
- The filtering cutoff remains 70%; no probability or selection is fabricated.


## v3.9.2 cross-league team-history quick fix
- Team histories are now matched independently across supported domestic CSV divisions; the two clubs no longer have to appear in the same CSV.
- Cross-league rates are normalized against each team's own league scoring baselines and scaled to the home team's scoring environment.
- Staleness is checked against the older of the two teams' latest available match dates.
- Understat xG is blended only when both teams match the same supported league.
- Unsupported teams/competitions still return not-estimated; no probabilities are fabricated.
- This does not add leagues absent from the configured CSV provider and is not a guarantee that all 47 legs will be covered.


## v3.9.2 full-ticket allocation quick fix

- Reviews every source ticket leg in its original order instead of displaying only qualifying selections.
- Marks a selection KEEP only when the supported model estimate meets the 70% threshold.
- Marks below-threshold, unmatched, unsupported, or data-limited legs REVIEW.
- Does not fabricate a replacement. A replacement requires a separately verified candidate for the same fixture and intended market; this version reports when none is available.
- Only KEEP selections are passed into the ticket reconstruction workflow. The original booking code is not persisted.
- The model remains uncalibrated; estimates are not guaranteed probabilities or betting advice.
