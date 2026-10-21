# SolveEase Booking & Arbitrage Dashboard v3.25.0

## Render configuration
- Build command: `pip install -r requirements.txt && playwright install chromium`
- Start command: `python main.py`
- Runtime: Python 3.11–3.14 may work depending on dependency wheel availability; use the Render default only if all dependencies install successfully.

## v3.25.0 change
Corrected the OddsHarvester CLI invocation to use documented `-s`, `-d`, `-m`, `-f`, and `-o` options, and removed unsupported `--concurrency` and `--request-delay` arguments that could cause CLI usage output and exit code 2.

## Important validation note
This is a code-level fix. It does not guarantee OddsPortal access or successful live market retrieval. After deploying, run one replacement search and check the feed diagnostic. A successful deployment/HTTP 200 does not prove that the feed returned usable markets.

Only use data sources you are authorized to access. No automatic bet placement is performed.
