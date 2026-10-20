# Deploy v3.10.0 from Android

1. Download and extract `SolveEase_Booking_Arbitrage_Dashboard_v3.10.0_Full_Market_Intelligence_Upgrade.zip`.
2. Open the `3-5-odd-main` folder and upload its contents to the root of your existing GitHub repository `3-5-odd`, replacing the previous source files. Keep `.github/workflows/run_scanner.yml` in the `.github/workflows/` path.
3. Commit the update in GitHub.
4. In Render, open the existing service and confirm the start command is `python main.py`; keep the service's port/environment settings unless you intentionally need to change them.
5. Wait for deployment, then open `/health` on the Render URL. Confirm `version` is `3.10.0` and inspect `feed_status`.
6. If the SportyBet feed is restricted or unusable, the health page will report that. Do not treat an empty scan as proof that no markets exist. Set `SPORTYBET_MARKET_FEED_ENABLED=0` only if you want to disable the best-effort feed.

No bookmaker login credentials or betting automation are added. Do not upload a real `.env` file or private tokens to GitHub.
