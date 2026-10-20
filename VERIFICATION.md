# SolveEase Booking Tools v3.23.0 verification

## Changes
- Supports the documented OddsHarvester flat JSON record shape (`home_team`, `away_team`, `market`, `odds` mapping), in addition to nested market payloads.
- Logs real feeder counters to Render logs: source events, raw market rows, normalized rows, unique market rows, unique selection options, rejected rows, response bytes, and fetch duration.
- Keeps source quote timestamps distinct from retrieval timestamps.

## Verification
- Python compilation and unit tests are run against the included project.
- Synthetic tests do not prove live OddsPortal retrieval.
- Live Render feed retrieval and 100+ unique market ingestion remain unverified until the deployment logs show positive real counts.
