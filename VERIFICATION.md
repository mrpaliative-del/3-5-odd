# v3.25.0 verification

## Code-level checks
- `main.py` compiled successfully with Python's `py_compile` in the packaging environment.
- The regression test suite is included as `test_main.py`; run it from this directory with `python -m unittest test_main`.

## Not verified by packaging
- Successful live OddsHarvester/OddsPortal retrieval
- Availability of 100+ unique market options
- Any qualifying replacement or any model probability threshold
- Live Render deployment behavior after this package is uploaded

After deployment, trigger one replacement search and inspect the newest `market_feeder_audit source=OddsHarvester` diagnostic. Verify the timestamp and actual row counts; deployment success alone is not evidence of feed success.
