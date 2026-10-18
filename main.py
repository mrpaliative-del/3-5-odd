#!/usr/bin/env python3
"""
SolveEase SportyBet Booking-Code Dashboard
Python standard library only.

Render Start Command:
    python main.py

Optional environment variables:
    PORT=10000
    BANKROLL=100000
    SPORTYBET_TIMEOUT_SECONDS=15

IMPORTANT:
The SportyBet share endpoint used here is unofficial. A successful
HTTP request does not guarantee the booking code can be resolved.
"""

import json
import logging
import math
import os
import re
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen


# ---------------------------------------------------------
# Configuration
# ---------------------------------------------------------

HOST = "0.0.0.0"
PORT = int(os.environ.get("PORT", "10000"))
BANKROLL = float(os.environ.get("BANKROLL", "100000"))
REQUEST_TIMEOUT = float(
    os.environ.get("SPORTYBET_TIMEOUT_SECONDS", "15")
)

SPORTYBET_URL = "https://www.sportybet.com/api/ng/orders/share/"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)

APP_STARTED = time.time()
LAST_LOOKUP = None
LAST_LOOKUP_LOCK = threading.Lock()


# ---------------------------------------------------------
# SportyBet lookup
# ---------------------------------------------------------

def find_outcomes(payload):
    """Find a likely list of selection/outcome objects in a response."""

    candidate_keys = (
        "outcomes",
        "selections",
        "bets",
        "orderItems",
        "items",
        "details",
        "legs",
    )

    if isinstance(payload, dict):
        for key in candidate_keys:
            value = payload.get(key)
            if isinstance(value, list) and value:
                if any(isinstance(item, dict) for item in value):
                    return value

        for value in payload.values():
            result = find_outcomes(value)
            if result:
                return result

    elif isinstance(payload, list):
        for item in payload:
            result = find_outcomes(item)
            if result:
                return result

    return []


def first_value(obj, keys, default=""):
    """Return the first non-empty matching field."""

    if not isinstance(obj, dict):
        return default

    for key in keys:
        value = obj.get(key)
        if value is not None and value != "":
            if isinstance(value, (str, int, float, bool)):
                return value

    return default


def normalize_leg(item, index):
    """Normalize common selection fields without inventing missing data."""

    event = first_value(
        item,
        (
            "eventName", "event_name", "matchName", "match",
            "fixtureName", "event", "homeAwayName",
        ),
        "Event details unavailable",
    )

    market = first_value(
        item,
        (
            "marketName", "market_name", "marketDesc",
            "market", "betType", "playTypeName",
        ),
        "Market unavailable",
    )

    selection = first_value(
        item,
        (
            "outcomeName", "outcome_name", "selectionName",
            "optionName", "runnerName", "pick", "selection",
            "name",
        ),
        "Selection details unavailable",
    )

    odds_raw = first_value(
        item,
        (
            "odds", "odd", "decimalOdds", "price",
            "oddsValue", "currentOdds",
        ),
        None,
    )

    try:
        odds = float(odds_raw)
        if not math.isfinite(odds) or odds <= 1:
            odds = None
    except (TypeError, ValueError):
        odds = None

    kickoff = first_value(
        item,
        (
            "matchTime", "startTime", "eventTime",
            "kickoff", "beginTime",
        ),
        "",
    )

    return {
        "number": index + 1,
        "event": str(event),
        "market": str(market),
        "selection": str(selection),
        "odds": odds,
        "kickoff": kickoff,
    }


def find_text_fields(payload):
    """Search common ticket-level fields without assuming a schema."""

    result = {}

    wanted = {
        "status": (
            "status", "orderStatus", "betStatus", "state",
        ),
        "stake": (
            "stake", "betAmount", "orderAmount", "amount",
        ),
        "potential_return": (
            "potentialReturn", "maxReturn", "possibleWin",
            "returnAmount",
        ),
        "booking_code": (
            "shareCode", "bookingCode", "orderCode",
        ),
        "currency": ("currency", "currencyCode"),
    }

    def walk(node):
        if isinstance(node, dict):
            for output_key, aliases in wanted.items():
                if output_key in result:
                    continue

                for alias in aliases:
                    value = node.get(alias)
                    if value is not None and value != "":
                        if isinstance(value, (str, int, float)):
                            result[output_key] = value
                            break

            for value in node.values():
                walk(value)

        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(payload)
    return result


def lookup_sportybet(booking_code):
    """Fetch a shared booking code from SportyBet's unofficial endpoint."""

    url = SPORTYBET_URL + quote(booking_code, safe="")

    request = Request(
        url,
        headers={
            "Accept": "application/json, text/plain, */*",
            "User-Agent": (
                "Mozilla/5.0 (Linux; Android 15) "
                "AppleWebKit/537.36 Chrome/131.0 Mobile Safari/537.36"
            ),
            "Current-Country": "NG",
            "Referer": "https://www.sportybet.com/ng/",
        },
        method="GET",
    )

    try:
        with urlopen(request, timeout=REQUEST_TIMEOUT) as response:
            status_code = response.status
            raw_body = response.read(2_000_000).decode(
                "utf-8", errors="replace"
            )

    except HTTPError as exc:
        logging.warning("SportyBet returned HTTP %s", exc.code)

        if exc.code in (401, 403, 429):
            return {
                "ok": False,
                "error": (
                    "SportyBet rejected or rate-limited the request. "
                    "Try again later or load the code on SportyBet's "
                    "official website."
                ),
            }

        return {
            "ok": False,
            "error": f"SportyBet returned HTTP {exc.code}.",
        }

    except (URLError, TimeoutError, OSError) as exc:
        logging.warning("SportyBet request failed: %s", exc)

        return {
            "ok": False,
            "error": (
                "Could not reach SportyBet. Check the service logs "
                "and try again."
            ),
        }

    if status_code != 200:
        return {
            "ok": False,
            "error": f"SportyBet returned HTTP {status_code}.",
        }

    try:
        payload = json.loads(raw_body)
    except json.JSONDecodeError:
        logging.warning("SportyBet returned a non-JSON response")
        return {
            "ok": False,
            "error": (
                "SportyBet did not return JSON. Its endpoint may have "
                "changed or may require additional access."
            ),
        }

    if not isinstance(payload, (dict, list)):
        return {
            "ok": False,
            "error": "SportyBet returned an unexpected response.",
        }

    if isinstance(payload, dict):
        biz_code = payload.get("bizCode")

        if biz_code is not None and str(biz_code) != "10000":
            message = first_value(
                payload,
                ("message", "msg", "errorMsg", "error"),
                "SportyBet did not accept this booking code.",
            )
            return {
                "ok": False,
                "error": str(message),
            }

    raw_legs = find_outcomes(payload)

    if not raw_legs:
        logging.info(
            "SportyBet response contained no recognizable selections"
        )
        return {
            "ok": False,
            "error": (
                "No recognizable ticket selections were returned. "
                "The booking code may be invalid, expired, private, "
                "or the SportyBet response format may have changed."
            ),
            "source_status": status_code,
        }

    legs = [
        normalize_leg(item, index)
        for index, item in enumerate(raw_legs)
        if isinstance(item, dict)
    ]

    if not legs:
        return {
            "ok": False,
            "error": "The response did not contain usable selections.",
        }

    known_odds = [
        leg["odds"] for leg in legs
        if isinstance(leg["odds"], (int, float))
    ]

    combined_odds = None

    if len(known_odds) == len(legs):
        combined_odds = math.prod(known_odds)

    ticket_fields = find_text_fields(payload)

    result = {
        "ok": True,
        "booking_code": booking_code,
        "selections_count": len(legs),
        "legs": legs,
        "combined_odds": combined_odds,
        "status": ticket_fields.get("status", "Not provided"),
        "stake": ticket_fields.get("stake"),
        "potential_return": ticket_fields.get("potential_return"),
        "currency": ticket_fields.get("currency", "NGN"),
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
        "source": "SportyBet unofficial share endpoint",
        "note": (
            "Ticket information returned by the source is shown "
            "without inventing missing odds or probabilities."
        ),
    }

    global LAST_LOOKUP
    with LAST_LOOKUP_LOCK:
        LAST_LOOKUP = {
            "at": result["retrieved_at"],
            "selections_count": result["selections_count"],
            "ok": True,
        }

    logging.info(
        "Booking lookup returned %s selections",
        len(legs),
    )

    return result


# ---------------------------------------------------------
# Dashboard HTML
# ---------------------------------------------------------

HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="theme-color" content="#101827">
<meta name="description"
 content="SolveEase booking-code lookup dashboard.">
<title>SolveEase | Booking-Code Dashboard</title>
<style>
:root {
  color-scheme: dark;
  --bg:#0b1220;
  --panel:#121d30;
  --panel2:#18263c;
  --text:#eef4ff;
  --muted:#9eb0ca;
  --line:#2b3b54;
  --accent:#69d7ac;
  --danger:#ff8585;
}
* { box-sizing:border-box; }
body {
  margin:0;
  background:var(--bg);
  color:var(--text);
  font:16px/1.5 system-ui,-apple-system,sans-serif;
}
main {
  width:min(920px,100%);
  margin:auto;
  padding:22px 16px 44px;
}
header { margin:8px 0 24px; }
.brand { color:var(--accent); font-weight:800; letter-spacing:.4px; }
h1 { font-size:clamp(27px,5vw,39px); line-height:1.15; margin:10px 0; }
p { color:var(--muted); }
.panel {
  background:var(--panel);
  border:1px solid var(--line);
  border-radius:16px;
  padding:20px;
  margin:16px 0;
}
label { display:block; font-weight:650; margin-bottom:8px; }
input {
  width:100%;
  min-height:52px;
  padding:13px;
  border-radius:10px;
  border:1px solid var(--line);
  background:#09101d;
  color:var(--text);
  font-size:17px;
}
button {
  width:100%;
  min-height:50px;
  margin-top:12px;
  padding:12px 16px;
  border:0;
  border-radius:10px;
  background:var(--accent);
  color:#071b15;
  font-size:16px;
  font-weight:800;
  cursor:pointer;
}
button:disabled { opacity:.55; cursor:wait; }
.small { font-size:13px; color:var(--muted); }
#message { white-space:pre-wrap; overflow-wrap:anywhere; }
.error { color:var(--danger); }
.success { color:var(--accent); }
.grid {
  display:grid;
  grid-template-columns:repeat(auto-fit,minmax(145px,1fr));
  gap:10px;
}
.stat {
  padding:13px;
  background:var(--panel2);
  border-radius:10px;
  min-width:0;
}
.stat strong { display:block; font-size:21px; overflow-wrap:anywhere; }
.stat span { font-size:12px; color:var(--muted); }
.leg {
  background:var(--panel2);
  border:1px solid var(--line);
  border-radius:12px;
  padding:14px;
  margin-top:10px;
}
.leg h3 { margin:0 0 6px; font-size:16px; }
.leg p { margin:5px 0; overflow-wrap:anywhere; }
.odd { color:var(--accent); font-weight:800; }
.hidden { display:none; }
footer { font-size:12px; color:var(--muted); margin-top:24px; }
</style>
</head>
<body>
<main>
<header>
  <div class="brand">SOLVEEASE</div>
  <h1>Booking-Code Dashboard</h1>
  <p>Enter a SportyBet booking code to request the ticket details.</p>
</header>

<section class="panel">
  <form id="lookupForm">
    <label for="bookingCode">SportyBet booking code</label>
    <input id="bookingCode" name="bookingCode"
      autocomplete="off" autocapitalize="none" spellcheck="false"
      maxlength="120" placeholder="Enter booking code" required>
    <button id="submitButton" type="submit">Look up ticket</button>
  </form>
  <p class="small">
    Enter the code only. Do not paste JSON, passwords, or account details.
  </p>
  <div id="message" role="status" aria-live="polite"></div>
</section>

<section id="results" class="panel hidden">
  <h2>Ticket summary</h2>
  <div class="grid">
    <div class="stat"><span>Selections</span><strong id="count">—</strong></div>
    <div class="stat"><span>Combined odds</span><strong id="combined">—</strong></div>
    <div class="stat"><span>Ticket status</span><strong id="status">—</strong></div>
    <div class="stat"><span>Stake</span><strong id="stake">—</strong></div>
    <div class="stat"><span>Potential return</span><strong id="returns">—</strong></div>
  </div>
  <h2>Selections</h2>
  <div id="legs"></div>
  <p id="retrieved" class="small"></p>
  <p id="note" class="small"></p>
</section>

<section class="panel">
  <h2>Service status</h2>
  <p id="health">Checking service status…</p>
  <p class="small">
    Ticket retrieval relies on an unofficial endpoint and may fail
    if SportyBet changes or restricts it. Verify all details on
    SportyBet before making any decision.
  </p>
</section>

<footer>
  SolveEase · Read-only lookup · No SportyBet login required by this page.
  This dashboard does not place bets.
</footer>
</main>

<script>
const form = document.getElementById("lookupForm");
const codeInput = document.getElementById("bookingCode");
const button = document.getElementById("submitButton");
const message = document.getElementById("message");
const results = document.getElementById("results");

function setMessage(text, kind) {
  message.textContent = text || "";
  message.className = kind || "";
}

function displayValue(value) {
  if (value === null || value === undefined || value === "") return "Not provided";
  return String(value);
}

function addTextElement(parent, tag, text, className) {
  const element = document.createElement(tag);
  element.textContent = text;
  if (className) element.className = className;
  parent.appendChild(element);
  return element;
}

function renderTicket(data) {
  document.getElementById("count").textContent =
    displayValue(data.selections_count);

  document.getElementById("combined").textContent =
    data.combined_odds == null
      ? "Unavailable"
      : Number(data.combined_odds).toFixed(2);

  document.getElementById("status").textContent =
    displayValue(data.status);

  const currency = data.currency || "NGN";
  document.getElementById("stake").textContent =
    data.stake == null ? "Not provided" :
    currency + " " + data.stake;

  document.getElementById("returns").textContent =
    data.potential_return == null ? "Not provided" :
    currency + " " + data.potential_return;

  const legsBox = document.getElementById("legs");
  legsBox.replaceChildren();

  for (const leg of (data.legs || [])) {
    const card = document.createElement("article");
    card.className = "leg";

    addTextElement(
      card, "h3",
      String(leg.number) + ". " + displayValue(leg.event)
    );

    addTextElement(
      card, "p", "Market: " + displayValue(leg.market)
    );

    addTextElement(
      card, "p", "Selection: " + displayValue(leg.selection)
    );

    addTextElement(
      card, "p",
      "Odds: " + (leg.odds == null
        ? "Not provided"
        : Number(leg.odds).toFixed(2)),
      "odd"
    );

    if (leg.kickoff !== "") {
      addTextElement(card, "p", "Time: " + String(leg.kickoff));
    }

    legsBox.appendChild(card);
  }

  document.getElementById("retrieved").textContent =
    "Retrieved: " + displayValue(data.retrieved_at);

  document.getElementById("note").textContent =
    displayValue(data.note);

  results.classList.remove("hidden");
}

form.addEventListener("submit", async function(event) {
  event.preventDefault();

  const bookingCode = codeInput.value.trim();

  if (!bookingCode) {
    setMessage("Enter a booking code first.", "error");
    return;
  }

  if (bookingCode.length > 120) {
    setMessage("The booking code is too long.", "error");
    return;
  }

  button.disabled = true;
  button.textContent = "Looking up…";
  results.classList.add("hidden");
  setMessage("Requesting ticket details from SportyBet…", "");

  try {
    const response = await fetch("/api/analyse", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({booking_code: bookingCode})
    });

    const data = await response.json();

    if (!response.ok || !data.ok) {
      throw new Error(data.error || "The lookup could not be completed.");
    }

    renderTicket(data);
    setMessage("Ticket details retrieved.", "success");

  } catch (error) {
    setMessage(
      error.message || "Request failed. Please try again.",
      "error"
    );
  } finally {
    button.disabled = false;
    button.textContent = "Look up ticket";
  }
});

async function loadHealth() {
  const target = document.getElementById("health");

  try {
    const response = await fetch("/health");
    const data = await response.json();

    target.textContent = data.status === "online"
      ? "Online · API route available"
      : "Service response received";
  } catch (_) {
    target.textContent = "Could not check service status.";
  }
}

loadHealth();
</script>
</body>
</html>
"""


# ---------------------------------------------------------
# HTTP server and API routes
# ---------------------------------------------------------

class SolveEaseHandler(BaseHTTPRequestHandler):

    server_version = "SolveEase/1.0"

    def log_message(self, fmt, *args):
        logging.info(
            '%s - "%s"',
            self.address_string(),
            fmt % args,
        )

    def send_bytes(self, status, body, content_type):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()

        if self.command != "HEAD":
            self.wfile.write(body)

    def send_json(self, status, payload):
        body = json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")

        self.send_bytes(
            status,
            body,
            "application/json; charset=utf-8",
        )

    def read_json_body(self):
        content_length = self.headers.get("Content-Length", "0")

        try:
            length = int(content_length)
        except (ValueError, TypeError):
            raise ValueError("Invalid Content-Length.")

        if length < 1:
            raise ValueError("Request body is empty.")

        if length > 16_384:
            raise ValueError("Request body is too large.")

        body = self.rfile.read(length)

        try:
            return json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ValueError("Request body must be valid JSON.")

    def do_HEAD(self):
        path = urlsplit(self.path).path.rstrip("/") or "/"
        if path in ("/", "/health", "/state"):
            if path == "/":
                body = HTML.encode("utf-8")
                self.send_bytes(
                    200, body, "text/html; charset=utf-8"
                )
            else:
                self.send_json(200, {"status": "online"})
        else:
            self.send_json(404, {"ok": False, "error": "Not found."})

    def do_GET(self):
        path = urlsplit(self.path).path.rstrip("/") or "/"

        if path == "/":
            self.send_bytes(
                200,
                HTML.encode("utf-8"),
                "text/html; charset=utf-8",
            )
            return

        if path == "/favicon.ico":
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        if path == "/api/analyse":
            self.send_json(405, {
                "ok": False,
                "error": (
                    "This endpoint requires POST. Open the dashboard, "
                    "enter a booking code, and tap Look up ticket."
                ),
                "expected_method": "POST",
                "expected_path": "/api/analyse",
            })
            return

        if path == "/health":
            with LAST_LOOKUP_LOCK:
                last_lookup = LAST_LOOKUP

            self.send_json(200, {
                "status": "online",
                "service": "SolveEase Booking-Code Dashboard",
                "api_route": "/api/analyse",
                "sportybet_lookup": "unofficial endpoint",
                "bankroll_configured": BANKROLL >= 0,
                "last_lookup": last_lookup,
                "uptime_seconds": int(time.time() - APP_STARTED),
            })
            return

        if path == "/state":
            with LAST_LOOKUP_LOCK:
                last_lookup = LAST_LOOKUP

            self.send_json(200, {
                "status": "online",
                "last_lookup": last_lookup,
            })
            return

        logging.warning("Unmatched GET route: %s", self.path)

        self.send_json(404, {
            "ok": False,
            "error": "Route not found.",
            "requested_path": path,
            "hint": "Open the dashboard homepage.",
        })

    def do_POST(self):
        if self.path != "/api/analyse":
            self.send_json(404, {
                "ok": False,
                "error": "Route not found.",
            })
            return

        content_type = self.headers.get("Content-Type", "")
        if "application/json" not in content_type.lower():
            self.send_json(415, {
                "ok": False,
                "error": "Send the booking code through the dashboard form.",
            })
            return

        try:
            body = self.read_json_body()
        except ValueError as exc:
            self.send_json(400, {
                "ok": False,
                "error": str(exc),
            })
            return

        if not isinstance(body, dict):
            self.send_json(400, {
                "ok": False,
                "error": "Expected a booking-code object.",
            })
            return

        booking_code = body.get("booking_code", "")

        if not isinstance(booking_code, str):
            self.send_json(400, {
                "ok": False,
                "error": "Booking code must be text.",
            })
            return

        booking_code = booking_code.strip()

        if not booking_code:
            self.send_json(400, {
                "ok": False,
                "error": "Enter a booking code.",
            })
            return

        if len(booking_code) > 120:
            self.send_json(400, {
                "ok": False,
                "error": "Booking code must be 120 characters or fewer.",
            })
            return

        if re.search(r"[\x00-\x1f\x7f]", booking_code):
            self.send_json(400, {
                "ok": False,
                "error": "Booking code contains invalid characters.",
            })
            return

        logging.info("Received booking-code lookup request")

        try:
            result = lookup_sportybet(booking_code)
        except Exception:
            logging.exception("Unexpected booking lookup error")
            self.send_json(500, {
                "ok": False,
                "error": "Unexpected server error during lookup.",
            })
            return

        if not result.get("ok"):
            self.send_json(502, result)
            return

        self.send_json(200, result)


# ---------------------------------------------------------
# Start
# ---------------------------------------------------------

def main():
    if not math.isfinite(BANKROLL) or BANKROLL < 0:
        raise ValueError("BANKROLL must be a non-negative number.")

    if not math.isfinite(REQUEST_TIMEOUT) or REQUEST_TIMEOUT <= 0:
        raise ValueError(
            "SPORTYBET_TIMEOUT_SECONDS must be greater than zero."
        )

    server = ThreadingHTTPServer((HOST, PORT), SolveEaseHandler)
    server.daemon_threads = True

    logging.info("SolveEase dashboard listening on port %s", PORT)
    logging.info("Booking-code lookup route: POST /api/analyse")
    logging.info("SportyBet lookup endpoint configured")
    logging.info("No predefined booking codes are required")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logging.info("Shutting down SolveEase dashboard")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
