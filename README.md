# SolveEase Booking & Arbitrage Dashboard — v3.8.1

A single, integrated, standard-library Python application for read-only booking analysis, ticket conversion checklists, odds scanning/arbitrage calculations, daily fixture screening, same-fixture ticket optimization, model diagnostics and optional Telegram alerts. It does **not** place bets or create valid bookmaker booking codes.

## Included in this one repository/ZIP

- **Booking lookup & intelligence:** existing read-only SportyBet lookup adapter; analysis of supplied ticket legs; clear unsupported-source messaging. Bet9ja/BetKing lookup adapters are not implemented and are not guessed.
- **Same-fixture optimization:** compare an original selection with supplied alternatives for the same fixture; retain, replace or exclude; qualifying candidates must have estimated probability at least 70% and adequate/high/verified evidence quality or `calibrated: true`.
- **Daily accumulator builder:** local date defaults to `Africa/Lagos`; filters past/started fixtures and other dates; target 20, maximum 40, one selection per fixture; if fewer qualify it reports the shortfall and never lowers the 70% gate.
- **Probability/model tools:** existing goal-rate/Poisson analysis plus an explicit Dixon–Coles score matrix helper, Bayesian-style shrinkage helper, implied probability/overround removal, and Brier/log-loss calculator. Dixon–Coles rho and all model outputs remain uncalibrated until fitted and validated on suitable out-of-sample data.
- **Psychological decision checks:** diagnostic flags for recency, reputation, narrative, chasing/gambler's fallacy, timestamped odds movement availability, and consensus availability. These flags do not change probability estimates and do not claim to read minds.
- **Simplified dashboard:** exactly three primary tools on the home screen — Booking Code & Filter, Ticket Converter, and Arbitrage Scanner. Advanced pages remain available from their relevant workspaces/routes.
- **Filtered-ticket handoff:** the probability filter defaults to 70%; applying retained results transfers only the matched retained selections into the converter and discards the original code from active ticket state. The app does not generate a new bookmaker-issued code.
- **Telegram:** test message and daily report endpoints with readable summaries, bounded message length, safe handling of tokens, and explicit distinction between configuration and confirmed delivery.
- **Diagnostics:** health route, feed health/freshness reporting, standard-library tests, and explicit source/data limitations.

## Run locally

```bash
python main.py
```

The app listens on `0.0.0.0:$PORT` (default `10000`). Run tests with:

```bash
python -m py_compile main.py advanced_features.py test_main.py
python -m unittest -v
```

## Render deployment

- Build command: leave blank (no third-party dependencies).
- Start command: `python main.py`
- Health-check path: `/health`
- Set the variables in `.env.example` in Render's private Environment page as needed. Never commit live tokens.

## Optional variables

- `FIXTURES_FEED_URL`: HTTPS JSON feed you are permitted to access. JSON may be a list of fixtures or an object containing `fixtures`/`events`. Each fixture should contain `event`, ISO-8601 `start_time`, optional `event_id`, and `selections`.
- `APP_TIMEZONE`: IANA timezone, default `Africa/Lagos`.
- `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`: private Telegram bot credentials. Obtain the token from Telegram's official BotFather, and use the target private chat/group ID. Do not paste tokens into chat or source control.
- `FOOTBALL_STATS_ENABLED`, `FOOTBALL_STATS_TIMEOUT_SECONDS`, `FOOTBALL_STATS_CACHE_TTL_SECONDS`, `FOOTBALL_STATS_MAX_AGE_DAYS`: existing best-effort historic data configuration.
- `SPORTYBET_ODDS_FEED_URL`, `BET9JA_ODDS_FEED_URL`, `ONEXBET_ODDS_FEED_URL`, `BETWAY_ODDS_FEED_URL`: authorized structured odds feeds. A configured URL is not proof the source is official, accessible, current or complete.

## API quick reference

- `GET /health` — service and feed status.
- `POST /api/lookup` — `{ "code": "...", "bookmaker": "auto" }`.
- `POST /api/scan` — scan configured odds feeds.
- `POST /api/analyze-ticket` / `POST /api/analyze-betslip` — existing ticket model analysis.
- `POST /api/daily-accumulator` — optional `{ "date": "YYYY-MM-DD", "timezone": "Africa/Lagos", "fixtures": [...] }`; if fixtures are omitted, uses `FIXTURES_FEED_URL`.
- `POST /api/optimize-ticket` — `{ "legs": [{ "original": {...}, "candidates": [...] }] }`.
- `POST /api/psychology-check` — `{ "selections": [...] }`.
- `POST /api/model-probability` — `{ "market": "1X2", "selection": "Home", "home_goal_rate": 1.6, "away_goal_rate": 1.0 }`.
- `POST /api/calibration-metrics` — `{ "predictions": [0.7, 0.3], "outcomes": [1, 0] }`.
- `GET /api/telegram-status`, `POST /api/telegram-test`, `POST /api/telegram-alert`.

All POST endpoints require `Content-Type: application/json`.

## Fixture payload example

```json
{
  "fixtures": [
    {
      "event_id": "fixture-123",
      "event": "Home FC vs Away FC",
      "start_time": "2026-10-20T19:00:00+01:00",
      "selections": [
        {
          "market": "Double Chance",
          "selection": "Home/Draw",
          "odds": 1.35,
          "estimated_probability": 0.74,
          "evidence_quality": "adequate",
          "reasoning": "Model and current verified statistics agree"
        }
      ]
    }
  ]
}
```

A probability value in a payload is not automatically verified simply because it is present. Integrators should only supply model estimates with transparent provenance and an evidence-quality label backed by actual evidence. This project does not fabricate missing odds, fixtures, probabilities or source coverage.

## Important limitations and responsible use

- A 70% estimate is not a guarantee and does not mean the accumulator itself has a 70% chance. Multi-leg outcomes are dependent in some cases; the displayed independence product is explicitly naive.
- A 20-leg accumulator has low combined probability even when each leg's individual estimate is high. This app does not encourage staking or automatically execute bets.
- Data-source terms/licensing must be checked before commercial use. Football-Data and Understat coverage is best-effort and may be subject to terms that restrict commercial use.
- Arbitrage is only identified among accessible, equivalent, timely markets. Empty scans are not proof that no arbitrage exists. Settlement rules, limits, price movement and account restrictions can invalidate apparent opportunities.
- Telegram is **not activated by this ZIP alone**. Add private credentials, deploy, press “Send test alert”, and verify the message arrives.
- The ZIP cannot verify external feeds, live market availability, bookmaker coverage, or deployment credentials from the build environment. Those require post-deployment checks.
