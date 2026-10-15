# ⚡ 5G Automated SportyBet Accumulator Streaming Daemon

An automated high-frequency sports analytics pipeline that sweeps elite European football leagues, calculates positive Single-Game Builder correlations, and pairs matching outcomes into same-day accumulators hitting a strict `3.00 - 5.20` odds target.

## 📡 System Architecture Logic
* **1G/2G Layer:** Ingests raw public decimal odds matrices.
* **3G Layer:** Filters for dominant home favorites (`1.15 - 1.35`) and applies a low-risk **Draw No Bet (Asian Handicap 0) + Over 1.5 Goals + Over 3.5 Team Corners** correlation script.
* **4G Layer:** Pairs qualified fixtures matching the exact same calendar kickoff day to accelerate bankroll recycling.
* **5G Layer:** Manages an internal signature cache database to prevent duplicate notifications and dispatches instant Markdown alerts to a Telegram Channel Webhook.

## 🚀 Deployment Instructions
1. Clone the repository framework.
2. Install system requirements: `pip install -r requirements.txt`
3. Configure your secret keys inside a hidden local `.env` file.
4. Execute the continuous streaming background service daemon:
   ```bash
   nohup python main.py > system_activity.log 2>&1 &
   ```
