
import os
import json
import html
import logging
import threading
import secrets
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

# ============================================================
# SolveEase Institutional Quant Dashboard
# Single booking-code dashboard; no JSON entry by the user.
# ============================================================

PORT = int(os.getenv("PORT", "10000"))
BANKROLL = max(0.0, float(os.getenv("BANKROLL", "100000")))
MIN_VALUE_RATIO = max(0.0, float(os.getenv("MIN_VALUE_RATIO", "1.00")))
MAX_LEGS = max(1, min(100, int(os.getenv("MAX_LEGS", "30"))))
MAX_BODY_BYTES = 100_000

INGEST_API_TOKEN = os.getenv("INGEST_API_TOKEN", "").strip()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("solveease")

state_lock = threading.Lock()
STATE = {
    "started_at": datetime.now(timezone.utc).isoformat(),
    "last_analysis_at": None,
    "last_booking_code": None,
    "last_result": None,
    "analyses": 0,
    "telegram_last_status": "not_attempted",
}


def load_presets():
    """Load named booking-code records from a Render environment variable."""
    raw = os.getenv("BOOKING_PRESETS_JSON", "").strip()
    if not raw:
        return {}

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            "BOOKING_PRESETS_JSON contains invalid JSON."
        ) from exc

    if not isinstance(data, dict):
        raise RuntimeError(
            "BOOKING_PRESETS_JSON must be an object keyed by booking code."
        )

    presets = {}
    for code, record in data.items():
        if not isinstance(code, str) or not code.strip():
            continue

        # Accept either {"legs": [...]} or a direct list of legs.
        if isinstance(record, list):
            legs = record
            source = "configured_preset"
        elif isinstance(record, dict):
            legs = record.get("legs")
            source = str(record.get("source", "configured_preset"))
        else:
            continue

        if isinstance(legs, list) and legs:
            presets[code.strip().upper()] = {
                "source": source,
                "legs": legs,
            }

    return presets


try:
    PRESETS = load_presets()
except RuntimeError:
    log.exception("Preset configuration error")
    raise


def number(value, name, minimum=None, maximum=None):
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a number.")

    try:
        result = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a number.")

    if result != result or result in (float("inf"), float("-inf")):
        raise ValueError(f"{name} must be finite.")

    if minimum is not None and result < minimum:
        raise ValueError(f"{name} must be at least {minimum}.")

    if maximum is not None and result > maximum:
        raise ValueError(f"{name} must not exceed {maximum}.")

    return result


def analyse_ticket(code, record):
    legs = record.get("legs")
    if not isinstance(legs, list) or not legs:
        raise ValueError("This booking code has no configured ticket legs.")

    if len(legs) > MAX_LEGS:
        raise ValueError(f"Maximum allowed legs: {MAX_LEGS}.")

    analysed_legs = []
    combined_odds = 1.0
    combined_probability = 1.0
    all_probabilities_supplied = True
    all_benchmarks_supplied = True
    combined_benchmark_odds = 1.0

    for index, leg in enumerate(legs, start=1):
        if not isinstance(leg, dict):
            raise ValueError(f"Leg {index} must be an object.")

        match = str(leg.get("match", "")).strip()
        market = str(leg.get("market", "")).strip()

        if not match or not market:
            raise ValueError(
                f"Leg {index} is missing its match or market."
            )

        odds = number(leg.get("odds"), f"Leg {index} odds",
                      minimum=1.000001, maximum=10000)

        probability = leg.get("fair_probability")
        fair_odds = leg.get("fair_odds")

        if probability is not None:
            probability = number(
                probability, f"Leg {index} fair_probability",
                minimum=0.000001, maximum=1.0
            )
        elif fair_odds is not None:
            fair_odds = number(
                fair_odds, f"Leg {index} fair_odds",
                minimum=1.000001, maximum=10000
            )
            # Raw implied probability, not a no-vig probability.
            probability = 1.0 / fair_odds
        else:
            all_probabilities_supplied = False
            probability = None

        if fair_odds is None and probability is not None:
            fair_odds = 1.0 / probability

        combined_odds *= odds

        if probability is not None:
            combined_probability *= probability

        if fair_odds is not None:
            combined_benchmark_odds *= fair_odds
        else:
            all_benchmarks_supplied = False

        analysed_legs.append({
            "match": match,
            "market": market,
            "odds": odds,
            "fair_probability": probability,
            "fair_odds": fair_odds,
            "kickoff": str(leg.get("kickoff", "Not supplied")),
        })

    if all_probabilities_supplied:
        expected_return = combined_probability * combined_odds
        expected_value = expected_return - 1.0
        value_ratio = expected_return
        estimated_probability = combined_probability
    else:
        expected_value = None
        value_ratio = None
        estimated_probability = None

    if all_benchmarks_supplied:
        benchmark_ratio = combined_odds / combined_benchmark_odds
    else:
        benchmark_ratio = None

    # Quarter-Kelly reference. This is an estimate, not a betting instruction.
    if estimated_probability is not None:
        b = combined_odds - 1.0
        q = 1.0 - estimated_probability
        full_kelly = (
            (b * estimated_probability - q) / b if b > 0 else 0.0
        )
        quarter_kelly_fraction = max(0.0, full_kelly / 4.0)
        reference_stake = BANKROLL * quarter_kelly_fraction
    else:
        quarter_kelly_fraction = None
        reference_stake = None

    qualifies = (
        expected_value is not None
        and expected_value > 0
        and value_ratio >= MIN_VALUE_RATIO
    )

    return {
        "booking_code": code,
        "source": record.get("source", "configured_preset"),
        "legs_count": len(analysed_legs),
        "legs": analysed_legs,
        "combined_odds": combined_odds,
        "estimated_probability": estimated_probability,
        "expected_value_percent": (
            expected_value * 100 if expected_value is not None else None
        ),
        "value_ratio": value_ratio,
        "benchmark_ratio": benchmark_ratio,
        "quarter_kelly_fraction": quarter_kelly_fraction,
        "reference_stake": reference_stake,
        "qualifies": qualifies,
        "qualification_note": (
            "Positive estimated EV using supplied probabilities."
            if qualifies else
            "Does not meet the configured value test, or probability "
            "data is missing."
        ),
        "warning": (
            "Estimates depend on supplied odds and probabilities. "
            "Independence between legs is assumed. No profit is guaranteed."
        ),
        "analysed_at": datetime.now(timezone.utc).isoformat(),
    }


def telegram_send(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return False, "not_configured"

    url = (
        "https://api.telegram.org/bot"
        + TELEGRAM_BOT_TOKEN
        + "/sendMessage"
    )
    body = json.dumps({
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message[:4000],
    }).encode("utf-8")

    request = Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urlopen(request, timeout=8) as response:
            result = json.loads(response.read().decode("utf-8"))
            if result.get("ok") is True:
                return True, "sent"
            return False, "telegram_rejected"
    except (HTTPError, URLError, TimeoutError, OSError, ValueError):
        log.exception("Telegram notification failed")
        return False, "send_failed"


def format_alert(result):
    ev = result["expected_value_percent"]
    ev_text = f"{ev:.2f}%" if ev is not None else "Unavailable"
    probability = result["estimated_probability"]
    probability_text = (
        f"{probability * 100:.2f}%"
        if probability is not None else "Unavailable"
    )

    return (
        "SolveEase Ticket Analysis\n"
        f"Code: {result['booking_code']}\n"
        f"Legs: {result['legs_count']}\n"
        f"Combined odds: {result['combined_odds']:.3f}\n"
        f"Estimated probability: {probability_text}\n"
        f"Estimated EV: {ev_text}\n"
        f"Result: {'QUALIFIES' if result['qualifies'] else 'NOT QUALIFIED'}\n"
        "Estimates only; not a guarantee."
    )


def json_bytes(data):
    return json.dumps(data, allow_nan=False).encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    server_version = "SolveEaseDashboard/1.0"

    def log_message(self, fmt, *args):
        log.info("%s - %s", self.address_string(), fmt % args)

    def send_json(self, status, data):
        body = json_bytes(data)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_html(self, body):
        encoded = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(encoded)

    def read_json_body(self):
        length_text = self.headers.get("Content-Length", "0")
        try:
            length = int(length_text)
        except ValueError:
            raise ValueError("Invalid Content-Length.")

        if length < 1 or length > MAX_BODY_BYTES:
            raise ValueError("Request body is empty or too large.")

        content_type = self.headers.get("Content-Type", "")
        if "application/json" not in content_type.lower():
            raise ValueError("Content-Type must be application/json.")

        try:
            data = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ValueError("Request body must contain valid JSON.")

        if not isinstance(data, dict):
            raise ValueError("Request body must be a JSON object.")

        return data

    def do_HEAD(self):
        path = urlparse(self.path).path
        if path in ("/", "/health"):
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()
        else:
            self.send_error(404)

    def do_GET(self):
        path = urlparse(self.path).path

        if path == "/":
            return self.send_html(DASHBOARD)

        if path == "/health":
            return self.send_json(200, {
                "status": "online",
                "service": "SolveEase Institutional Quant Dashboard",
                "preset_count": len(PRESETS),
                "telegram_configured": bool(
                    TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID
                ),
                "ingest_auth_configured": bool(INGEST_API_TOKEN),
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })

        if path == "/state":
            with state_lock:
                snapshot = dict(STATE)
            snapshot["preset_count"] = len(PRESETS)
            snapshot["telegram_configured"] = bool(
                TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID
            )
            return self.send_json(200, snapshot)

        return self.send_json(404, {"error": "Not found."})

    def do_POST(self):
        path = urlparse(self.path).path

        if path not in ("/api/analyse", "/api/ingest"):
            return self.send_json(404, {"error": "Not found."})

        # Only server-to-server ingestion requires the secret token.
        if path == "/api/ingest":
            if not INGEST_API_TOKEN:
                return self.send_json(503, {
                    "error": "INGEST_API_TOKEN is not configured."
                })

            supplied = self.headers.get("X-API-Token", "")
            if not secrets.compare_digest(supplied, INGEST_API_TOKEN):
                return self.send_json(401, {"error": "Unauthorised."})

        try:
            data = self.read_json_body()

            code = str(data.get("booking_code", "")).strip().upper()
            if not code or len(code) > 100:
                raise ValueError("Enter a valid booking code.")

            if path == "/api/analyse":
                # Dashboard lookup only: no arbitrary ticket JSON needed.
                record = PRESETS.get(code)
                if record is None:
                    return self.send_json(404, {
                        "error": (
                            "Booking code not found in configured records. "
                            "This server has no connected bookmaker resolver "
                            "for this code."
                        ),
                        "booking_code": code,
                    })
            else:
                # Authenticated ingestion can supply legs from a trusted
                # upstream system. It is not a bookmaker-code resolver.
                legs = data.get("legs")
                if not isinstance(legs, list) or not legs:
                    raise ValueError(
                        "Authenticated ingestion requires a non-empty legs list."
                    )
                record = {
                    "source": str(data.get("source", "authenticated_ingest")),
                    "legs": legs,
                }

            result = analyse_ticket(code, record)

            with state_lock:
                STATE["last_analysis_at"] = result["analysed_at"]
                STATE["last_booking_code"] = code
                STATE["last_result"] = result
                STATE["analyses"] += 1

            # Notify only qualifying analyses; never claim delivery unless
            # Telegram confirms the message was accepted.
            if result["qualifies"]:
                sent, status = telegram_send(format_alert(result))
                with state_lock:
                    STATE["telegram_last_status"] = status

            return self.send_json(200, result)

        except ValueError as exc:
            return self.send_json(400, {"error": str(exc)})
        except Exception:
            log.exception("Analysis failed")
            return self.send_json(500, {"error": "Internal analysis error."})


DASHBOARD = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="description" content="Analyse configured booking codes using transparent odds and probability estimates.">
<title>SolveEase Quant Dashboard</title>
<style>
:root{color-scheme:dark;--bg:#0b1020;--panel:#151d32;--line:#293653;
--text:#f2f5ff;--muted:#aebbd3;--accent:#77a9ff;--good:#75e0ad}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);
font:16px system-ui,-apple-system,Segoe UI,sans-serif}
main{max-width:850px;margin:0 auto;padding:24px 16px 50px}
header{margin-bottom:24px}
h1{font-size:clamp(25px,5vw,36px);margin:0 0 8px}
p{line-height:1.5;color:var(--muted)}
.panel{background:var(--panel);border:1px solid var(--line);
border-radius:16px;padding:18px;margin:16px 0}
label{display:block;font-weight:650;margin-bottom:8px}
input{width:100%;padding:15px;border:1px solid var(--line);
border-radius:10px;background:#0b1020;color:var(--text);font-size:18px}
button{width:100%;margin-top:12px;padding:14px;border:0;border-radius:10px;
background:var(--accent);color:#071126;font-weight:750;font-size:16px}
button:disabled{opacity:.6}
.grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}
.metric{padding:13px;border-radius:10px;background:#0b1020;overflow-wrap:anywhere}
.metric small{display:block;color:var(--muted);margin-bottom:7px}
.metric strong{font-size:19px}
#message{white-space:pre-wrap;overflow-wrap:anywhere}
.error{color:#ffb1b1}.good{color:var(--good)}
.leg{padding:12px 0;border-bottom:1px solid var(--line)}
.leg:last-child{border-bottom:0}
footer{font-size:13px;color:var(--muted)}
@media(max-width:460px){.grid{grid-template-columns:1fr}}
</style>
</head>
<body>
<main>
<header>
<h1>SolveEase Quant Dashboard</h1>
<p>Enter a booking code to analyse a ticket stored in the system.</p>
</header>

<section class="panel">
<form id="scan">
<label for="code">Booking code</label>
<input id="code" name="code" placeholder="Type your booking code"
autocomplete="off" autocapitalize="characters" maxlength="100" required>
<button id="submit" type="submit">Analyse Ticket</button>
</form>
<p id="message" role="status" aria-live="polite"></p>
</section>

<section id="results" class="panel" hidden>
<h2>Analysis results</h2>
<div class="grid" id="metrics"></div>
<h3>Ticket legs</h3>
<div id="legs"></div>
<p id="warning"></p>
</section>

<section class="panel">
<h2>What this tool can do</h2>
<p>It analyses booking codes that have been configured on the server.
It cannot retrieve a ticket from an unrelated bookmaker without a supported
integration.</p>
<p>Probability, expected value and stake figures are estimates, not guarantees.</p>
</section>
<footer>Private ticket data is not saved by your browser. Analysis state is
kept in server memory and resets when the service restarts.</footer>
</main>

<script>
const form = document.getElementById("scan");
const codeInput = document.getElementById("code");
const button = document.getElementById("submit");
const message = document.getElementById("message");
const results = document.getElementById("results");
const metrics = document.getElementById("metrics");
const legsBox = document.getElementById("legs");
const warning = document.getElementById("warning");

function metric(label, value) {
  const box = document.createElement("div");
  box.className = "metric";
  const small = document.createElement("small");
  small.textContent = label;
  const strong = document.createElement("strong");
  strong.textContent = value;
  box.append(small, strong);
  metrics.append(box);
}

function fmt(value, suffix = "") {
  return value === null || value === undefined
    ? "Unavailable"
    : Number(value).toLocaleString(undefined, {maximumFractionDigits: 3}) + suffix;
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const bookingCode = codeInput.value.trim().toUpperCase();
  if (!bookingCode) return;

  button.disabled = true;
  message.className = "";
  message.textContent = "Checking configured records…";
  results.hidden = true;

  try {
    const response = await fetch("/api/analyse", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({booking_code: bookingCode})
    });
    const data = await response.json();

    if (!response.ok) {
      throw new Error(data.error || "Analysis failed.");
    }

    metrics.replaceChildren();
    legsBox.replaceChildren();

    metric("Booking code", data.booking_code);
    metric("Number of legs", data.legs_count);
    metric("Combined odds", fmt(data.combined_odds));
    metric("Estimated probability", fmt(
      data.estimated_probability === null
        ? null : data.estimated_probability * 100, "%"
    ));
    metric("Estimated expected value", fmt(data.expected_value_percent, "%"));
    metric("Value ratio", fmt(data.value_ratio));
    metric("Benchmark ratio", fmt(data.benchmark_ratio));
    metric("Quarter-Kelly reference stake", fmt(data.reference_stake));

    data.legs.forEach((leg, index) => {
      const item = document.createElement("div");
      item.className = "leg";
      const title = document.createElement("strong");
      title.textContent = (index + 1) + ". " + leg.match;
      const details = document.createElement("p");
      details.textContent = leg.market + " · Odds " + leg.odds +
        " · Kickoff " + leg.kickoff;
      item.append(title, details);
      legsBox.append(item);
    });

    warning.textContent = data.warning + " " + data.qualification_note;
    results.hidden = false;
    message.className = data.qualifies ? "good" : "";
    message.textContent = data.qualifies
      ? "Analysis complete. The configured estimated-value test qualifies."
      : "Analysis complete. The configured estimated-value test did not qualify.";
  } catch (error) {
    message.className = "error";
    message.textContent = error.message;
  } finally {
    button.disabled = false;
  }
});
</script>
</body>
</html>"""


if __name__ == "__main__":
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    log.info("SolveEase dashboard listening on port %s", PORT)
    log.info("Configured booking codes: %s", len(PRESETS))
    server.serve_forever()
