# SolveEase Booking Tools v3.22.0 verification

## Changes in this release
- Preserves the OddsHarvester/ OddsPortal feed identity separately from the attributed bookmaker, so replacement evaluation can resolve the correct feed-health record.
- Marks parsed OddsHarvester rows as `usable markets` when actual rows were normalized, rather than using the nonstandard `ok` status alone.
- Records actual JSON response bytes and separate source-event, raw-market-row, normalized-market-row, unique-market-row, and unique-selection-option counters.
- Keeps provider quote timestamps distinct from retrieval timestamps; retrieval time is not presented as a provider quote timestamp.

## Verification
- Python syntax compilation: passed.
- Unit tests: see release test result; tests use synthetic fixtures and do not prove live bookmaker access.
- Live Render feed retrieval: not verified by this package. The deployed service must be checked for v3.22.0 startup and feed diagnostics.

## Known limits
- Public source availability, browser requirements, source anti-bot behavior, and exact bookmaker market coverage are external dependencies.
- Missing historical team rates can still prevent a candidate from being probability-qualified. The app must not label implied odds as a calibrated probability.
- No automatic betting; user must verify odds and settlement rules.
