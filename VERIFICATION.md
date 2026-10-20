# Verification — SolveEase Booking Tools v3.21.0

## Verified in the build workspace

- `python -m py_compile main.py` — passed.
- `python -m unittest -q` — 93 tests passed.
- Synthetic OddsHarvester adapter tests — passed for multiple bookmakers, market lines, and rejection of community vote percentages as odds.
- Runtime dependency check — correctly reports `dependency unavailable` when the `oddsharvester` CLI is not installed; no false claim of market retrieval is made.

## Not verified

- Live OddsHarvester scraping from Render or Nigeria.
- Successful SportyBet retrieval (the observed HTTP 422 remains an upstream/request issue).
- 100+ markets for a particular fixture or one/two suitable replacements per selection.
- Probability calibration or actual future betting outcomes.

OddsHarvester requires its CLI and Playwright Chromium browser installation. If the deployment cannot install/run Chromium, the feed diagnostic will state that and the user can disable the adapter or configure an authorized external JSON feed.

The app must continue to show `REVIEW` if a source is unavailable, a fixture is ambiguous, a market is unsupported, or probabilities are not sufficiently supported.
