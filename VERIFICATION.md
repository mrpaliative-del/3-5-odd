# v3.27.0 verification

## Code-level checks
- `main.py` compiled successfully in the packaging environment.
- The included `test_main.py` regression suite covers the CLI option-builder and the optional BetPawa configured-feed slot.
- OddsHarvester CLI arguments are now selected from the installed command's own `upcoming --help` output when available, with a documented short-option fallback if the help probe fails.

## Not verified by packaging
- Successful live OddsHarvester/OddsPortal retrieval on Render
- Availability of a specific number of unique market options
- Any qualifying replacement or probability threshold
- Direct scraping of BetPawa's dynamic public website
- A working authorized BetPawa JSON feed URL
- Live Render behavior after this package is uploaded

After deployment, trigger one replacement search and inspect the newest `market_feeder_audit source=OddsHarvester` diagnostic. Confirm nonzero normalized rows and actual quote data before treating the feed as operational. A successful deploy or HTTP 200 is not evidence of usable odds.

The app remains read-only: no automatic bets are placed, and unmatched selections must remain `REVIEW` rather than using guessed substitutes.
