import os
import time
import requests
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv

# Ingest variables safely from the local .env layer
load_dotenv()

ODDS_API_KEY = os.getenv("ODDS_API_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
REGION = os.getenv("ODDS_REGION", "uk")

MIN_ACCUMULATOR_ODDS = float(os.getenv("MIN_ACCUMULATOR_ODDS", 3.00))
MAX_ACCUMULATOR_ODDS = float(os.getenv("MAX_ACCUMULATOR_ODDS", 5.20))
POLL_DELAY_SECONDS = int(os.getenv("POLL_DELAY_SECONDS", 14400))

EUROPEAN_LEAGUES = [
    "soccer_epl", "soccer_spain_la_liga", "soccer_italy_serie_a", 
    "soccer_germany_bundesliga", "soccer_france_ligue_one", "soccer_uefa_champs_league"
]

def log_event(message):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] {message}")

def send_free_telegram_alert(match1, match2, total_odds, target_date):
    url = f"https://telegram.org{TELEGRAM_BOT_TOKEN}/sendMessage"
    formatted_date = datetime.strptime(target_date, "%Y-%m-%d").strftime("%B %d, %Y")
    
    message = (
        f"📊 *SPORTYBET SLIP UPDATE* 📊\n"
        f"📅 *MATCHDAY TARGET: {formatted_date.upper()}*\n\n"
        f"📈 *COMBINED TICKET TOTAL ODDS: {total_odds}* 🔥\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🏟️ *MATCH 1:* {match1['name']} ({match1['league'].upper()})\n"
        f"🔹 Draw No Bet (Asian Handicap 0) - Home Win\n"
        f"🔹 Total Match Goals - Over 1.5\n"
        f"🔹 Home Team Corners - Over 3.5\n\n"
        f"🏟️ *MATCH 2:* {match2['name']} ({match2['league'].upper()})\n"
        f"🔹 Draw No Bet (Asian Handicap 0) - Home Win\n"
        f"🔹 Total Match Goals - Over 1.5\n"
        f"🔹 Home Team Corners - Over 3.5\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"👉 _Open SportyBet, build these 2 matches on your slip, and lock in your target!_"
    )
    
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}
    try:
        res = requests.post(url, json=payload, timeout=10)
        if res.status_code == 200:
            log_event(f"📡 Value notification dispatched for {match1['name']}.")
    except Exception as e:
        log_event(f"❌ Telegram delivery failure: {e}")

def run_free_pipeline(sent_combinations_cache, last_cleanup_time):
    now_utc = datetime.now(timezone.utc)
    cutoff_utc = now_utc + timedelta(hours=48)
    pool_of_qualified_games = []
    
    log_event("🛰️ Querying live public odds data layers...")
    
    if now_utc - last_cleanup_time > timedelta(hours=24):
        sent_combinations_cache.clear()
        last_cleanup_time = now_utc

    for league in EUROPEAN_LEAGUES:
        odds_url = f"https://the-odds-api.com{league}/odds/"
        params = {"apiKey": ODDS_API_KEY, "regions": REGION, "markets": "h2h", "oddsFormat": "decimal"}
        
        try:
            response = requests.get(odds_url, params=params, timeout=15)
            if response.status_code != 200: continue
            matches = response.json()
            
            for match in matches:
                commence_time_str = match.get("commence_time")
                if not commence_time_str: continue
                match_time_utc = datetime.fromisoformat(commence_time_str.replace("Z", "+00:00"))
                
                if match_time_utc < now_utc or match_time_utc > cutoff_utc: continue
                kickoff_date = match_time_utc.strftime("%Y-%m-%d")
                
                home_team = match.get("home_team")
                bookmakers = match.get("bookmakers", [])
                if not bookmakers: continue
                outcomes = bookmakers[0].get("markets", [{}])[0].get("outcomes", [])
                
                home_odds = next((o.get("price") for o in outcomes if o.get("name") == home_team), None)
                
                if home_odds and (1.15 <= home_odds <= 1.35):
                    estimated_game_odds = round(home_odds * 1.15 * 1.22, 2)
                    
                    pool_of_qualified_games.append({
                        "id": match.get("id"),
                        "name": f"{home_team} vs {match.get('away_team')}",
                        "league": league.replace("soccer_", ""),
                        "estimated_odds": estimated_game_odds,
                        "kickoff_date": kickoff_date
                    })
        except Exception:
            continue
        time.sleep(1)

    # Same-Day pairing engine matrix
    for i in range(len(pool_of_qualified_games)):
        for j in range(i + 1, len(pool_of_qualified_games)):
            match1 = pool_of_qualified_games[i]
            match2 = pool_of_qualified_games[j]
            
            if match1['kickoff_date'] == match2['kickoff_date']:
                combined_total_odds = round(match1['estimated_odds'] * match2['estimated_odds'], 2)
                
                if MIN_ACCUMULATOR_ODDS <= combined_total_odds <= MAX_ACCUMULATOR_ODDS:
                    combination_signature = f"{min(match1['id'], match2['id'])}_{max(match1['id'], match2['id'])}"
                    
                    if combination_signature not in sent_combinations_cache:
                        send_free_telegram_alert(match1, match2, combined_total_odds, match1['kickoff_date'])
                        sent_combinations_cache.add(combination_signature)
                        time.sleep(1)
                        
    return last_cleanup_time

def main():
    sent_slips_memory_cache = set()
    last_cleanup_time = datetime.now(timezone.utc)
    log_event("🚀 BACKGROUND SCANNER INITIALIZED SUCCESSFULLY 24/7.")
    
    while True:
        try:
            last_cleanup_time = run_free_pipeline(sent_slips_memory_cache, last_cleanup_time)
        except Exception as error:
            log_event(f"❌ System Runtime Exception: {error}")
            
        log_event(f"💤 Entering Standby for {POLL_DELAY_SECONDS // 3600} hours to protect free API limits...")
        time.sleep(POLL_DELAY_SECONDS)

if __name__ == "__main__":
    if not ODDS_API_KEY or "your_real" in TELEGRAM_BOT_TOKEN:
        print("❌ CONFIG ERROR: Update your Telegram credentials inside the '.env' file first.")
    else:
        main()
