
#!/usr/bin/env python3
"""
SolveEase Institutional Quant Dashboard
---------------------------------------
Features:
- Render-compatible HTTP dashboard and health endpoints.
- Validated JSON ticket ingestion.
- Fair-probability calculations when explicitly supplied.
- Sharp decimal-odds comparison, clearly labeled as an estimate.
- Expected value and quarter-Kelly calculations.
- Conservative accumulator estimates with independence warnings.
- Telegram alerts with honest delivery status.
- Authentication for ticket ingestion.
- Safe browser rendering, bounded requests, and input validation.

IMPORTANT:
This is an analytical tool, not a source of verified live odds.
Do not treat sample fixtures or implied probabilities as verified forecasts.
"""

import os
import time
import html
import math
import json
import hmac
import logging
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import requests
from dotenv import load_dotenv

load_dotenv()

# ============================================================
# CONFIGURATION
# ============================================================

def env_int(name, default, minimum, maximum):
    raw = os.getenv(name, str(default))
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be an integer.")
    if not minimum <= value <= maximum:
        raise ValueError(
            f"{name} must be between {minimum} and {maximum}."
        )
    return value


def env_float(name, default, minimum, maximum):
    raw = os.getenv(name, str(default))
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be numeric.")
    if not math.isfinite(value) or not minimum <= value <= maximum:
        raise ValueError(
            f"{name} must be between {minimum} and {maximum}."
        )
    return value


PORT = env_int("PORT", 10000, 1, 65535)
TARGET_LEGS = env_int("TARGET_LEGS", 20, 1, 50)
MAX_INPUT_LEGS = env_int("MAX_INPUT_LEGS", 100, 1, 500)
MAX_BODY_BYTES = env_int(
    "MAX_BODY_BYTES", 250_000, 1_024, 2_000_000
)
MIN_VALUE_RATIO = env_float(
    "MIN_VALUE_RATIO", 1.00, 0.50, 5.00
)
BANKROLL = env_float("BANKROLL", 100_000, 0.01, 1e15)

TELEGRAM_BOT_TOKEN = os.getenv(
    "TELEGRAM_BOT_TOKEN", ""
).strip()
TELEGRAM_CHAT_ID = os.getenv(
    "TELEGRAM_CHAT_ID", ""
).strip()

# Set a long, random secret in Render environment variables.
# Ingestion is rejected when this secret is missing.
INGEST_API_TOKEN = os.getenv("INGEST_API_TOKEN", "").strip()

# Default off: do not send the built-in sample ticket at startup.
SEND_STARTUP_ALERT = (
    os.getenv("SEND_STARTUP_ALERT", "false").strip().lower()
    in ("1", "true", "yes")
)

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s UTC | %(levelname)s | %(message)s",
)
log = logging.getLogger("solveease-quant")

HTTP_SESSION = requests.Session()
HTTP_SESSION.headers.update(
    {"User-Agent": "SolveEaseQuantDashboard/2.0"}
)

STATE_LOCK = threading.Lock()
LATEST_TICKET_STATE = {
    "status": "initialized",
    "booking_code": "SOLVEEASE_QUANT",
    "updated_at": None,
    "data_quality": "No ticket submitted",
    "original_legs_count": 0,
    "refined_legs_count": 0,
    "combined_odds": 0.0,
    "joint_probability": None,
    "average_value_ratio": None,
    "estimated_ticket_ev": None,
    "independence_assumption": True,
    "telegram_sent": False,
    "legs": [],
}

# ============================================================
# VALIDATION HELPERS
# ============================================================

def finite_number(value, name, minimum, maximum):
    """Parse a finite numeric value inside an allowed range."""
    if isinstance(value, bool):
        raise ValueError(f"{name} must be numeric.")

    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be numeric.")

    if not math.isfinite(number):
        raise ValueError(f"{name} must be finite.")

    if not minimum <= number <= maximum:
        raise ValueError(
            f"{name} must be between {minimum} and {maximum}."
        )

    return number


def clean_text(value, name, maximum=200, default=""):
    if value is None:
        value = default

    if not isinstance(value, str):
        raise ValueError(f"{name} must be text.")

    value = value.strip()

    if len(value) > maximum:
        raise ValueError(f"{name} exceeds {maximum} characters.")

    return value


def validate_kickoff(value):
    value = clean_text(
        value, "kickoff", 50, "Not supplied"
    )

    if value == "Not supplied":
        return value

    # Accept ISO dates/timestamps, including a trailing Z.
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(
            "kickoff must be an ISO date/time or timestamp."
        )

    return value


# ============================================================
# PROBABILITY, VALUE AND STAKING ENGINE
# ============================================================

def institutional_sharp_filter(leg):
    """
    Supported probability inputs:

    fair_probability:
        Explicit probability between 0 and 1, supplied by the caller.
        It is not independently verified by this application.

    pinnacle_fair:
        Decimal odds from a claimed sharp benchmark.
        Its reciprocal is an implied probability, NOT a verified
        no-vig probability. It is treated as a rough estimate.

    A leg qualifies only if it has positive estimated EV and its
    odds ratio meets MIN_VALUE_RATIO. The qualification is not a
    guarantee of positive long-run returns or arbitrage.
    """

    if not isinstance(leg, dict):
        raise ValueError("Every leg must be a JSON object.")

    odds = finite_number(leg.get("odds"), "odds", 1.01, 1000)

    raw_fair_probability = leg.get("fair_probability")
    raw_sharp_odds = leg.get("pinnacle_fair")

    if raw_fair_probability is not None:
        true_prob = finite_number(
            raw_fair_probability,
            "fair_probability",
            0.001,
            0.999,
        )
        probability_source = "caller_supplied_probability"
        sharp_decimal_odds = None

        if raw_sharp_odds is not None:
            sharp_decimal_odds = finite_number(
                raw_sharp_odds,
                "pinnacle_fair",
                1.01,
                1000,
            )
    elif raw_sharp_odds is not None:
        sharp_decimal_odds = finite_number(
            raw_sharp_odds,
            "pinnacle_fair",
            1.01,
            1000,
        )
        true_prob = 1.0 / sharp_decimal_odds
        probability_source = (
            "sharp_odds_implied_probability_not_novig"
        )
    else:
        raise ValueError(
            "Each leg requires fair_probability or pinnacle_fair."
        )

    match = clean_text(
        leg.get("match", "Unknown match"), "match", 200
    )
    market = clean_text(
        leg.get("market", "Unspecified"), "market", 100
    )
    kickoff = validate_kickoff(leg.get("kickoff"))

    raw_id = leg.get("id", 0)
    if isinstance(raw_id, bool) or not isinstance(raw_id, (int, str)):
        raise ValueError("id must be an integer or short text.")
    leg_id = clean_text(str(raw_id), "id", 50)

    # EV per unit stake. Positive EV is only an estimate.
    ev = (true_prob * odds) - 1.0

    # Single-outcome price comparison, not an arbitrage guarantee.
    if sharp_decimal_odds is not None:
        value_ratio = odds / sharp_decimal_odds
    else:
        # Fair odds derived from an explicitly supplied probability.
        value_ratio = odds * true_prob

    # Kelly fraction for a single binary selection.
    b = odds - 1.0
    kelly = max(0.0, (true_prob * b - (1.0 - true_prob)) / b)
    quarter_kelly_fraction = kelly * 0.25

    # This is a mathematical reference only, not a betting instruction.
    quarter_kelly_stake = BANKROLL * quarter_kelly_fraction

    qualified = ev > 0 and value_ratio >= MIN_VALUE_RATIO

    return {
        "id": leg_id,
        "match": match,
        "market": market,
        "odds": round(odds, 6),
        "pinnacle_fair": (
            round(sharp_decimal_odds, 6)
            if sharp_decimal_odds is not None
            else None
        ),
        "kickoff": kickoff,
        "true_prob": round(true_prob, 6),
        "probability_source": probability_source,
        "ev": round(ev, 6),
        "value_ratio": round(value_ratio, 6),
        "quarter_kelly_percent": round(
            quarter_kelly_fraction * 100, 4
        ),
        "quarter_kelly_reference_amount": round(
            quarter_kelly_stake, 2
        ),
        "qualified": qualified,
    }


def refine_and_filter_ticket(raw_legs, max_legs=None):
    if not isinstance(raw_legs, list):
        raise ValueError("legs must be a JSON array.")

    if not raw_legs:
        raise ValueError("No legs were supplied.")

    if len(raw_legs) > MAX_INPUT_LEGS:
        raise ValueError(
            f"Maximum input size is {MAX_INPUT_LEGS} legs."
        )

    if max_legs is None:
        max_legs = TARGET_LEGS

    max_legs = min(
        int(finite_number(max_legs, "max_legs", 1, 50)),
        TARGET_LEGS,
    )

    evaluated = [institutional_sharp_filter(leg) for leg in raw_legs]
    qualified_legs = [leg for leg in evaluated if leg["qualified"]]

    # Stable ordering; supplied kickoff timestamps are not live-verified.
    qualified_legs.sort(key=lambda item: item["kickoff"])
    final_legs = qualified_legs[:max_legs]

    combined_odds = (
        math.prod(leg["odds"] for leg in final_legs)
        if final_legs else 0.0
    )

    # Numerical product is only an independence-based estimate.
    joint_probability = (
        math.prod(leg["true_prob"] for leg in final_legs)
        if final_legs else None
    )

    average_value_ratio = (
        sum(leg["value_ratio"] for leg in final_legs)
        / len(final_legs)
        if final_legs else None
    )

    # Expected return per unit stake under the same independence
    # assumption and supplied probability estimates.
    estimated_ticket_ev = (
        joint_probability * combined_odds - 1.0
        if final_legs else None
    )

    probability_sources = {
        leg["probability_source"] for leg in final_legs
    }
    data_quality = (
        "Estimated; probability inputs supplied by caller"
        if "caller_supplied_probability" in probability_sources
        else "Implied probabilities from decimal odds; not no-vig"
    ) if final_legs else "No selections qualified"

    return {
        "original_legs_count": len(raw_legs),
        "evaluated_legs_count": len(evaluated),
        "refined_legs_count": len(final_legs),
        "combined_odds": round(combined_odds, 6),
        "joint_probability": (
            round(joint_probability, 10)
            if joint_probability is not None else None
        ),
        "average_value_ratio": (
            round(average_value_ratio, 6)
            if average_value_ratio is not None else None
        ),
        "estimated_ticket_ev": (
            round(estimated_ticket_ev, 6)
            if estimated_ticket_ev is not None else None
        ),
        "independence_assumption": True,
        "data_quality": data_quality,
        "legs": final_legs,
        "evaluated_legs": evaluated,
    }


# ============================================================
# TELEGRAM
# ============================================================

def send_telegram_message(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("Telegram is not configured.")
        return False

    url = (
        "https://api.telegram.org/bot"
        + TELEGRAM_BOT_TOKEN
        + "/sendMessage"
    )

    try:
        response = HTTP_SESSION.post(
            url,
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message[:4000],
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=15,
        )

        if not response.ok:
            log.warning(
                "Telegram returned HTTP status %s.",
                response.status_code,
            )
            return False

        try:
            result = response.json()
        except ValueError:
            return False

        return result.get("ok") is True

    except requests.RequestException as exc:
        # Do not log the bot token or full request URL.
        log.warning("Telegram delivery failed: %s", type(exc).__name__)
        return False


def format_ticket_alert(analysis, booking_code):
    ratio = analysis["average_value_ratio"]
    probability = analysis["joint_probability"]
    ticket_ev = analysis["estimated_ticket_ev"]

    lines = [
        "SOLVEEASE QUANT DASHBOARD",
        f"Ticket: {html.escape(booking_code)}",
        (
            f"Qualified legs: {analysis['refined_legs_count']} / "
            f"{analysis['original_legs_count']}"
        ),
        (
            "Average value ratio: "
            + (f"{ratio:.3f}x" if ratio is not None else "N/A")
        ),
        f"Combined decimal odds: {analysis['combined_odds']:.3f}",
        (
            "Independence-based probability estimate: "
            + (
                f"{probability:.4%}"
                if probability is not None else "N/A"
            )
        ),
        (
            "Estimated ticket EV per unit stake: "
            + (f"{ticket_ev:+.4f}" if ticket_ev is not None else "N/A")
        ),
        "",
    ]

    for index, leg in enumerate(analysis["legs"], 1):
        lines.extend([
            f"{index}. {html.escape(leg['match'])}",
            f"Kickoff: {html.escape(leg['kickoff'])}",
            f"Market: {html.escape(leg['market'])}",
            (
                f"Odds: {leg['odds']:.3f} | "
                f"Estimated probability: {leg['true_prob']:.2%}"
            ),
            (
                f"EV: {leg['ev']:+.4f} | "
                f"Value ratio: {leg['value_ratio']:.3f}x"
            ),
            f"Probability source: {leg['probability_source']}",
            "",
        ])

    lines.extend([
        "Data quality: estimates, not verified live forecasts.",
        "Value ratio does not prove arbitrage.",
        "Accumulator probability assumes independent selections.",
        "No profit is guaranteed.",
    ])

    return "\n".join(lines)


# ============================================================
# DASHBOARD
# ============================================================

DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow">
<title>SolveEase Quant Dashboard</title>
<style>
:root{color-scheme:dark;--bg:#0f172a;--card:#1e293b;
--border:#334155;--accent:#38bdf8;--muted:#94a3b8}
*{box-sizing:border-box}
body{margin:0;padding:16px;font:15px system-ui,sans-serif;
background:var(--bg);color:#f8fafc}
main{max-width:1150px;margin:auto}
header,.card{background:var(--card);border:1px solid var(--border);
border-radius:12px;padding:18px;margin-bottom:16px}
h1{font-size:22px;color:var(--accent)}
h2{font-size:18px}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(145px,1fr));gap:10px}
.stat{background:#0b1220;padding:14px;border-radius:8px;overflow-wrap:anywhere}
.stat small{display:block;color:var(--muted)}
.stat strong{display:block;margin-top:7px;font-size:19px}
label{display:block;margin:12px 0 6px;color:var(--muted)}
input,textarea{width:100%;padding:12px;background:#0b1220;color:white;
border:1px solid var(--border);border-radius:7px}
textarea{min-height:210px;font-family:monospace}
button{padding:12px 16px;margin-top:12px;background:var(--accent);
color:#0f172a;border:0;border-radius:7px;font-weight:bold;cursor:pointer}
button:disabled{opacity:.6}
.notice{color:#fbbf24;line-height:1.5}
.status{white-space:pre-wrap;overflow-wrap:anywhere}
.table-wrap{overflow-x:auto}
table{border-collapse:collapse;width:100%;min-width:800px;font-size:13px}
th,td{text-align:left;padding:10px;border-bottom:1px solid var(--border)}
th{color:var(--muted)}
</style>
</head>
<body>
<main>
<header>
<h1>SolveEase Quant Dashboard</h1>
<p class="notice">Analytical estimates only. No verified live odds feed is connected.</p>
</header>

<section class="stats">
<div class="stat"><small>Ticket</small><strong id="code">—</strong></div>
<div class="stat"><small>Qualified legs</small><strong id="legs">0 / 0</strong></div>
<div class="stat"><small>Average value ratio</small><strong id="ratio">N/A</strong></div>
<div class="stat"><small>Combined odds</small><strong id="odds">0.000</strong></div>
<div class="stat"><small>Joint probability estimate</small><strong id="prob">N/A</strong></div>
<div class="stat"><small>Telegram</small><strong id="telegram">Not sent</strong></div>
</section>

<section class="card">
<h2>Ingest a ticket</h2>
<p class="notice">The ingestion token is sent only with this request. Do not share it.</p>
<form id="form">
<label for="token">Ingestion API token</label>
<input id="token" type="password" autocomplete="off" required>
<label for="booking">Ticket identifier</label>
<input id="booking" maxlength="100" value="SOLVEEASE_QUANT" required>
<label for="payload">Ticket JSON array</label>
<textarea id="payload" required spellcheck="false">[
  {
    "id": 1,
    "match": "Example Fixture — SAMPLE ONLY",
    "market": "Home or Draw",
    "odds": 1.50,
    "fair_probability": 0.70,
    "kickoff": "2026-10-18T18:00:00Z"
  }
]</textarea>
<button id="submit" type="submit">Validate and analyse</button>
<p id="message" class="status" role="status" aria-live="polite"></p>
</form>
</section>

<section class="card">
<h2>Latest ticket</h2>
<p id="quality" class="notice">Waiting for data.</p>
<p id="ev"></p>
<div class="table-wrap">
<table>
<thead><tr>
<th>#</th><th>Kickoff</th><th>Fixture</th><th>Market</th>
<th>Odds</th><th>Probability</th><th>EV</th><th>Ratio</th>
</tr></thead>
<tbody id="rows"><tr><td colspan="8">No selections.</td></tr></tbody>
</table>
</div>
</section>
</main>

<script>
"use strict";

const $ = id => document.getElementById(id);

function put(id, value) {
  $(id).textContent = value == null ? "N/A" : String(value);
}

function fmt(value, digits=3) {
  return Number.isFinite(Number(value)) ? Number(value).toFixed(digits) : "N/A";
}

function update(state) {
  put("code", state.booking_code || "—");
  put("legs", `${state.refined_legs_count || 0} / ${state.original_legs_count || 0}`);
  put("ratio", state.average_value_ratio == null ? "N/A" : fmt(state.average_value_ratio) + "x");
  put("odds", fmt(state.combined_odds));
  put("prob", state.joint_probability == null ? "N/A" : (Number(state.joint_probability) * 100).toFixed(4) + "%");
  put("telegram", state.telegram_sent ? "Sent" : "Not confirmed");
  put("quality", state.data_quality || "No data.");
  put("ev", state.estimated_ticket_ev == null
    ? "Estimated ticket EV: N/A"
    : "Estimated ticket EV per unit stake: " + fmt(state.estimated_ticket_ev, 4));

  const tbody = $("rows");
  tbody.replaceChildren();

  if (!Array.isArray(state.legs) || !state.legs.length) {
    const tr = document.createElement("tr");
    const td = document.createElement("td");
    td.colSpan = 8;
    td.textContent = "No selections qualified.";
    tr.appendChild(td);
    tbody.appendChild(tr);
    return;
  }

  state.legs.forEach((leg, index) => {
    const tr = document.createElement("tr");
    const values = [
      index + 1, leg.kickoff, leg.match, leg.market,
      fmt(leg.odds), (Number(leg.true_prob) * 100).toFixed(2) + "%",
      fmt(leg.ev, 4), fmt(leg.value_ratio) + "x"
    ];
    values.forEach(value => {
      const td = document.createElement("td");
      td.textContent = String(value ?? "N/A");
      tr.appendChild(td);
    });
    tbody.appendChild(tr);
  });
}

async function fetchState() {
  try {
    const response = await fetch("/state", {cache:"no-store"});
    if (response.ok) update(await response.json());
  } catch (_) {
    // A later refresh may recover.
  }
}

$("form").addEventListener("submit", async event => {
  event.preventDefault();
  const button = $("submit");
  button.disabled = true;
  put("message", "Validating ticket…");

  try {
    const parsed = JSON.parse($("payload").value);
    const legs = Array.isArray(parsed) ? parsed : parsed.legs;
    if (!Array.isArray(legs)) throw new Error("JSON must contain a legs array.");

    const response = await fetch("/ingest-ticket", {
      method:"POST",
      headers:{
        "Content-Type":"application/json",
        "X-API-Token":$("token").value
      },
      body:JSON.stringify({
        booking_code:$("booking").value,
        legs
      })
    });

    const result = await response.json();
    if (!response.ok) throw new Error(result.error || "Request failed.");

    update(result.state);
    put("message",
      "Analysis complete. Telegram delivery: " +
      (result.telegram_sent ? "confirmed." : "not confirmed/configured.")
    );
  } catch (error) {
    put("message", error.message || "Request failed.");
  } finally {
    button.disabled = false;
  }
});

fetchState();
setInterval(fetchState, 15000);
</script>
</body>
</html>
"""

# ============================================================
# HTTP SERVER
# ============================================================

class InstitutionalDashboardServer(BaseHTTPRequestHandler):
    server_version = "SolveEaseDashboard"
    sys_version = ""

    def _send_response(self, status, data, content_type="application/json; charset=utf-8"):
        if content_type.startswith("application/json"):
            body = json.dumps(
                data, ensure_ascii=False, allow_nan=False
            ).encode("utf-8")
        else:
            body = data.encode("utf-8")

        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'unsafe-inline'; "
            "style-src 'unsafe-inline'; connect-src 'self'; "
            "img-src 'self' data:; object-src 'none'; "
            "base-uri 'none'; frame-ancestors 'none'"
        )
        self.end_headers()

        if self.command != "HEAD":
            self.wfile.write(body)

    def do_HEAD(self):
        path = urlparse(self.path).path
        if path in ("/", "/dashboard", "/health", "/state"):
            body = (
                DASHBOARD_HTML
                if path in ("/", "/dashboard")
                else json.dumps({"status": "ok"})
            )
            content_type = (
                "text/html; charset=utf-8"
                if path in ("/", "/dashboard")
                else "application/json; charset=utf-8"
            )
            self._send_response(200, body, content_type)
        else:
            self._send_response(404, {"error": "Not found."})

    def do_GET(self):
        path = urlparse(self.path).path

        if path in ("/", "/dashboard"):
            self._send_response(200, DASHBOARD_HTML, "text/html; charset=utf-8")
            return

        if path == "/health":
            self._send_response(200, {
                "status": "ok",
                "service": "SolveEase Quant Dashboard",
                "telegram_configured": bool(
                    TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID
                ),
                "ingestion_configured": bool(INGEST_API_TOKEN),
                "odds_feed_configured": False,
                "note": "Health status does not verify odds or profitability.",
            })
            return

        if path == "/state":
            with STATE_LOCK:
                snapshot = json.loads(
                    json.dumps(LATEST_TICKET_STATE, allow_nan=False)
                )
            self._send_response(200, snapshot)
            return

        self._send_response(404, {"error": "Not found."})

    def do_POST(self):
        path = urlparse(self.path).path

        if path != "/ingest-ticket":
            self._send_response(404, {"error": "Not found."})
            return

        if not INGEST_API_TOKEN:
            self._send_response(503, {
                "error": "Ingestion is disabled: configure INGEST_API_TOKEN."
            })
            return

        supplied_token = self.headers.get("X-API-Token", "")
        if not hmac.compare_digest(supplied_token, INGEST_API_TOKEN):
            self._send_response(401, {"error": "Invalid API token."})
            return

        content_type = self.headers.get("Content-Type", "")
        if "application/json" not in content_type.lower():
            self._send_response(415, {
                "error": "Content-Type must be application/json."
            })
            return

        raw_length = self.headers.get("Content-Length", "")
        try:
            content_length = int(raw_length)
        except (TypeError, ValueError):
            self._send_response(411, {"error": "Valid Content-Length required."})
            return

        if content_length < 1 or content_length > MAX_BODY_BYTES:
            self._send_response(413, {"error": "Request body size not allowed."})
            return

        try:
            raw = self.rfile.read(content_length)
            payload = json.loads(raw.decode("utf-8"))

            if not isinstance(payload, dict):
                raise ValueError("Payload must be a JSON object.")

            raw_legs = payload.get("legs")
            booking_code = clean_text(
                payload.get("booking_code", "CUSTOM_QUANT"),
                "booking_code",
                100,
            )

            if not booking_code:
                raise ValueError("booking_code cannot be empty.")

            analysis = refine_and_filter_ticket(raw_legs)

            # Keep only public fields in the displayed state.
            public_analysis = {
                key: value for key, value in analysis.items()
                if key != "evaluated_legs"
            }

            # Send before committing final state so the response can report
            # actual delivery status. A failed Telegram send does not fail
            # the mathematical analysis.
            message = format_ticket_alert(analysis, booking_code)
            telegram_sent = send_telegram_message(message)

            new_state = {
                "status": "analysed",
                "booking_code": booking_code,
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "data_quality": analysis["data_quality"],
                "original_legs_count": analysis["original_legs_count"],
                "refined_legs_count": analysis["refined_legs_count"],
                "combined_odds": analysis["combined_odds"],
                "joint_probability": analysis["joint_probability"],
                "average_value_ratio": analysis["average_value_ratio"],
                "estimated_ticket_ev": analysis["estimated_ticket_ev"],
                "independence_assumption": True,
                "telegram_sent": telegram_sent,
                "legs": analysis["legs"],
            }

            with STATE_LOCK:
                LATEST_TICKET_STATE.clear()
                LATEST_TICKET_STATE.update(new_state)

            log.info(
                "Ticket analysed: code=%s input=%d qualified=%d telegram=%s",
                booking_code,
                analysis["original_legs_count"],
                analysis["refined_legs_count"],
                telegram_sent,
            )

            self._send_response(200, {
                "status": "success",
                "message": "Ticket validated and analysed.",
                "telegram_sent": telegram_sent,
                "state": new_state,
                "analysis": public_analysis,
            })

        except (json.JSONDecodeError, UnicodeDecodeError):
            self._send_response(400, {"error": "Invalid JSON or UTF-8 payload."})
        except ValueError as exc:
            self._send_response(400, {"error": str(exc)})
        except Exception:
            log.exception("Ticket ingestion failed.")
            self._send_response(500, {
                "error": "Internal processing error. Check server logs."
            })

    def log_message(self, fmt, *args):
        log.info("HTTP %s - %s", self.address_string(), fmt % args)


# ============================================================
# SAMPLE DATA — NOT LIVE-VERIFIED
# ============================================================

SAMPLE_LEGS = [
    {
        "id": 1,
        "match": "Example Fixture — SAMPLE ONLY",
        "market": "Home or Draw",
        "odds": 1.50,
        "fair_probability": 0.70,
        "kickoff": "2026-10-18T18:00:00Z",
    },
    {
        "id": 2,
        "match": "Another Example — SAMPLE ONLY",
        "market": "Over 1.5 Goals",
        "odds": 1.60,
        "fair_probability": 0.66,
        "kickoff": "2026-10-18T19:00:00Z",
    },
]


def start_server():
    server = ThreadingHTTPServer(
        ("0.0.0.0", PORT),
        InstitutionalDashboardServer,
    )
    server.daemon_threads = True
    log.info("Dashboard listening on port %s.", PORT)

    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == "__main__":
    if not INGEST_API_TOKEN:
        log.warning(
            "INGEST_API_TOKEN is missing. Ticket ingestion is disabled."
        )

    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("Telegram is not configured.")

    # Initialise a safe empty state. Do not present fixtures as live data.
    with STATE_LOCK:
        LATEST_TICKET_STATE.update({
            "status": "online",
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "data_quality": (
                "No verified live odds feed configured. "
                "Submit data for analysis."
            ),
        })

    server_thread = threading.Thread(
        target=start_server,
        name="dashboard-http",
        daemon=True,
    )
    server_thread.start()

    log.info("SolveEase Quant Dashboard started.")

    if SEND_STARTUP_ALERT:
        send_telegram_message(
            "SolveEase Quant Dashboard started. "
            "No live odds or profitable opportunities have been verified."
        )

    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        log.info("Shutdown requested.")
