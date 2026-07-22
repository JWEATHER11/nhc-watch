#!/usr/bin/env python3
"""
nhc_pipeline.py -- Fully automated NHC advisory pipeline:

  1. FETCH   -- Public Advisory (TCP) + Discussion (TCD), from IEM's
                text-product API first, falling back to NHC's own website
                if IEM is unreachable.
  2. COMPARE -- against the last-seen advisory (position, wind, pressure,
                status), using hard-coded math (no AI involved, 100%
                accuracy).
  3. CONVERT -- kt -> mph, Zulu -> Central time, mph -> Saffir-Simpson
                category. Hard-coded, not AI.
  4. REWRITE -- sends structured facts (not raw NHC text) to the Claude
                API, which writes ONLY the narrative paragraph -- current
                state, organization/trend, short/mid/long-term outlook,
                peak timing, weakening timing -- in your voice. The
                surrounding data header and NHC's own Key Messages are
                assembled by plain code, not AI, so those are always
                exactly what NHC said.
  5. DELIVER -- sends the finished message to Telegram if configured,
                otherwise falls back to email-to-SMS. Failure alerts go
                out the same way.

Message structure (bot header + AI narrative + bot footer):
  [Storm/Advisory#, current stats -- all hard-coded]
  [1 paragraph AI-written narrative]
  [NHC's own Key Messages, verbatim from the Discussion product]

To pause this entirely during quiet weeks: go to this repo's Actions tab,
click "NHC Pipeline (IEM + Claude Rewrite)" in the left sidebar, click the
"..." menu, click "Disable workflow." Click "Enable workflow" the same way
to turn it back on. No code or secrets need to change either way.

State lives in pipeline_state.json, committed back by the workflow.
"""

import json
import math
import os
import re
import smtplib
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from email.mime.text import MIMEText
from pathlib import Path

# ---------------------------------------------------------------------------
# CONFIG -- change this if this storm dissipates and a new one forms with a
# different Atlantic storm number (AT2 -> AT3, etc. / PIL suffix 2 -> 3)
# ---------------------------------------------------------------------------
STORM_PIL_SUFFIX = "AT2"

IEM_BASE = "https://mesonet.agron.iastate.edu/cgi-bin/afos/retrieve.py"
NHC_URLS = {
    "TCP": f"https://www.nhc.noaa.gov/text/MIATCP{STORM_PIL_SUFFIX}.shtml?text",
    "TCD": f"https://www.nhc.noaa.gov/text/MIATCD{STORM_PIL_SUFFIX}.shtml?text",
}

STATE_FILE = Path(__file__).parent / "pipeline_state.json"
CENTRAL_UTC_OFFSET = 5  # CDT (UTC-5). Change to 6 for CST (winter).

MONTHS = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}

MAX_ATTEMPTS = 3
RETRY_DELAY_SEC = 5

# Bands for short/mid/long-term forecast grouping, keyed by NHC's own
# H-offset labels from the Discussion product's forecast table.
SHORT_TERM_LABELS = {"INIT", "12H", "24H"}
MID_TERM_LABELS = {"36H", "48H", "60H", "72H"}
LONG_TERM_LABELS = {"96H", "120H"}


# ===========================================================================
# FETCH -- IEM primary, NHC fallback, with retries on each
# ===========================================================================
def _http_get(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": "nhc-pipeline/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _fetch_with_retries(url, label):
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            text = _http_get(url)
            if text and text.strip():
                return text
            print(f"[{label}] Attempt {attempt}: empty response from {url}")
        except Exception as e:
            print(f"[{label}] Attempt {attempt} failed ({url}): {e}")
        if attempt < MAX_ATTEMPTS:
            time.sleep(RETRY_DELAY_SEC)
    return None


def fetch_product(product_type):
    pil = f"{product_type}{STORM_PIL_SUFFIX}"
    iem_url = f"{IEM_BASE}?pil={pil}"

    text = _fetch_with_retries(iem_url, f"IEM:{pil}")
    if text:
        return text, "IEM"

    print(f"[{pil}] IEM failed after {MAX_ATTEMPTS} attempts, falling back to NHC...")
    nhc_url = NHC_URLS[product_type]
    text = _fetch_with_retries(nhc_url, f"NHC:{pil}")
    if text:
        return text, "NHC"

    print(f"[{pil}] Both IEM and NHC failed after {MAX_ATTEMPTS} attempts each.")
    return None, "FAILED"


# ===========================================================================
# Shared unit / time / category helpers (hard-coded -- no AI, no error risk)
# ===========================================================================
def kt_to_mph(kt):
    return round(kt * 1.15078)


def wind_category(mph):
    if mph is None:
        return None
    if mph < 39:
        return "Tropical Depression"
    if mph < 74:
        return "Tropical Storm"
    if mph < 96:
        return "Category 1 Hurricane"
    if mph < 111:
        return "Category 2 Hurricane"
    if mph < 130:
        return "Category 3 Hurricane"
    if mph < 157:
        return "Category 4 Hurricane"
    return "Category 5 Hurricane"


def zulu_to_central(zstr):
    m = re.match(r"^(\d{1,2})/(\d{2}):?(\d{2})(?::(\d{2}))?Z$", zstr, re.I)
    if not m:
        return None
    day, hour, minute = int(m.group(1)), int(m.group(2)), int(m.group(3))
    hour -= CENTRAL_UTC_OFFSET
    if hour < 0:
        hour += 24
        day -= 1
    period = "PM" if hour >= 12 else "AM"
    hour12 = hour % 12 or 12
    return f"{hour12}:{minute:02d} {period} CDT, day {day}"


# ===========================================================================
# TCP -- Public/Intermediate Advisory (drives dedupe + comparison + header)
# ===========================================================================
def tcp_header(text):
    m = re.search(r"^(.+?)\s+(Intermediate\s+)?Advisory Number\s+(\S+)", text, re.I | re.M)
    if not m:
        return None
    return {"status_and_name": m.group(1).strip(), "number": m.group(3).rstrip(".")}


def tcp_location(text):
    m = re.search(r"LOCATION\.\.\.(\d{1,3}\.?\d*)N\s+(\d{1,3}\.?\d*)W", text, re.I)
    return {"lat": float(m.group(1)), "lon": -float(m.group(2))} if m else None


def tcp_wind_mph(text):
    m = re.search(r"MAXIMUM SUSTAINED WINDS\.\.\.(\d{1,3})\s*MPH", text, re.I)
    return int(m.group(1)) if m else None


def tcp_movement(text):
    m = re.search(
        r"PRESENT MOVEMENT\.\.\.([A-Z]+)\s+OR\s+(\d{1,3})\s*DEGREES AT\s+(\d{1,3})\s*MPH",
        text, re.I,
    )
    if m:
        return {"compass": m.group(1).upper(), "degrees": int(m.group(2)), "mph": int(m.group(3))}
    if re.search(r"PRESENT MOVEMENT\.\.\.STATIONARY", text, re.I):
        return {"compass": None, "degrees": None, "mph": 0, "stationary": True}
    return None


def tcp_pressure_mb(text):
    m = re.search(r"MINIMUM CENTRAL PRESSURE\.\.\.(\d{3,4})\s*MB", text, re.I)
    return int(m.group(1)) if m else None


def tcp_changes(text):
    m = re.search(
        r"CHANGES WITH THIS ADVISORY:\s*\n+(.*?)\n\s*\n\s*SUMMARY OF WATCHES",
        text, re.I | re.S,
    )
    if not m:
        return None
    block = m.group(1).strip()
    if re.match(r"^none\.?$", block, re.I):
        return None
    return re.sub(r"\s+", " ", block).strip()


def tcp_next_advisory(text):
    m = re.search(r"Next\s+(?:complete\s+)?advisory\s+at\s+[^\n.]+", text, re.I)
    return m.group(0).strip() if m else None


# ===========================================================================
# TCD -- Discussion product: forecast positions table (with clean short/mid/
# long-term H-offset labels) + NHC's own Key Messages
# ===========================================================================
def tcd_forecast_positions(text):
    pattern = re.compile(
        r"^\s*(INIT|\d{1,3}H)\s+(\d{1,2}/\d{4}Z)\s+(\d{1,3}\.?\d*)N\s+(\d{1,3}\.?\d*)W\s+"
        r"(\d{1,3})\s*KT\s+(\d{1,3})\s*MPH(?:\.\.\.([A-Z /-]+))?\s*$",
        re.I | re.M,
    )
    rows = []
    for m in pattern.finditer(text):
        mph = int(m.group(6))
        rows.append({
            "label": m.group(1).upper(),
            "local": zulu_to_central(m.group(2)) or m.group(2),
            "lat": m.group(3), "lon": m.group(4),
            "kt": int(m.group(5)), "mph": mph,
            "category": wind_category(mph),
            "note": (m.group(7) or "").strip(),
        })
    return rows


def tcd_key_messages(text):
    m = re.search(r"Key Messages:\s*\n+(.*?)\n\s*\n\s*(?:FORECAST POSITIONS|\$\$)", text, re.I | re.S)
    if not m:
        return []
    block = m.group(1)
    # Each message is a numbered item; split on "N." at line starts
    items = re.split(r"\n\s*\n(?=\d+\.)", block.strip())
    cleaned = []
    for item in items:
        item = re.sub(r"^\d+\.\s*", "", item.strip())
        item = re.sub(r"\s+", " ", item).strip()
        if item:
            cleaned.append(item)
    return cleaned


def tcd_discussion_text(text):
    """Pulls NHC's own discussion paragraphs verbatim -- this is a US
    government work (public domain), so relaying it directly is both
    accurate and the actual meteorologist's own words, not an AI
    paraphrase. Captures everything between the date/time header line and
    "Key Messages:" (or "FORECAST POSITIONS" if there are no key messages
    this advisory)."""
    m = re.search(
        r"\d{3,4}\s+[AP]M\s+[A-Z]+\s+\w{3}\s+\w+\s+\d{1,2}\s+\d{4}\s*\n+(.*?)\n\s*\n\s*(?:Key Messages:|FORECAST POSITIONS)",
        text, re.I | re.S,
    )
    if not m:
        return []
    block = m.group(1).strip()
    paragraphs = re.split(r"\n\s*\n", block)
    cleaned = []
    for p in paragraphs:
        p = re.sub(r"\s+", " ", p).strip()
        if p:
            cleaned.append(p)
    return cleaned


def summarize_forecast_bands(positions, current_mph):
    if not positions:
        return None

    def band_for(label):
        if label in SHORT_TERM_LABELS:
            return "short"
        if label in MID_TERM_LABELS:
            return "mid"
        if label in LONG_TERM_LABELS:
            return "long"
        return None

    bands = {"short": [], "mid": [], "long": []}
    for p in positions:
        b = band_for(p["label"])
        if b:
            bands[b].append(p)

    peak = max(positions, key=lambda p: p["mph"])
    peak_idx = positions.index(peak)
    weakening = None
    for p in positions[peak_idx + 1:]:
        if p["mph"] < peak["mph"]:
            weakening = p
            break

    def band_desc(pts):
        if not pts:
            return None
        return ", ".join(f"{p['label']} ({p['local']}): {p['mph']} mph {p['category']}{' - ' + p['note'] if p['note'] else ''}" for p in pts)

    return {
        "current_category": wind_category(current_mph),
        "peak_mph": peak["mph"],
        "peak_category": peak["category"],
        "peak_when": peak["local"],
        "peak_note": peak["note"],
        "weakening_starts": weakening["local"] if weakening else None,
        "weakening_to_mph": weakening["mph"] if weakening else None,
        "weakening_to_category": weakening["category"] if weakening else None,
        "short_term": band_desc(bands["short"]),
        "mid_term": band_desc(bands["mid"]),
        "long_term": band_desc(bands["long"]),
    }


# ===========================================================================
# Distance / bearing between two fixes
# ===========================================================================
def haversine_nm(lat1, lon1, lat2, lon2):
    R = 3440.065
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi, dlmb = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def initial_bearing(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dlmb = math.radians(lon2 - lon1)
    y = math.sin(dlmb) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dlmb)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def bearing_to_compass(deg):
    dirs = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
            "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]
    return dirs[round(deg / 22.5) % 16]


# ===========================================================================
# State
# ===========================================================================
def load_state():
    return json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ===========================================================================
# Bot-written header (deterministic, no AI) -- exactly what you asked for:
# the structured facts up top, same style as the decoder tool.
# ===========================================================================
def build_bot_header(facts):
    lines = [f"{facts['name']} -- Advisory #{facts['advisory_num']}"]
    if facts.get("current_category"):
        lines.append(f"Category: {facts['current_category']}")
    if facts.get("movement"):
        lines.append(f"Movement: {facts['movement']}")
    if facts.get("wind_mph") is not None:
        lines.append(f"Sustained wind: {facts['wind_mph']} mph")
    if facts.get("pressure_mb") is not None:
        lines.append(f"Min pressure: {facts['pressure_mb']} mb")
    fb = facts.get("forecast_bands")
    if fb and fb.get("peak_mph"):
        peak_when_clean = fb['peak_when'].replace(" CDT", "").replace(" CST", "")
        lines.append(f"Peak forecast: {fb['peak_mph']} mph ({fb['peak_category']}) at {peak_when_clean}")
    if facts.get("next_advisory"):
        next_advisory_clean = facts['next_advisory'].replace(" CDT", "").replace(" CST", "")
        lines.append("")
        lines.append(f"({next_advisory_clean})")
    return "\n".join(lines)


# ===========================================================================
# Claude API -- writes ONLY the narrative paragraph from structured facts.
# Every number came from hard-coded parsing/math above, so the AI can only
# get wording wrong, never a fact.
# ===========================================================================
def build_facts_summary(facts):
    lines = [f"Storm: {facts['name']}"]
    if facts.get("current_category"):
        lines.append(f"Current category: {facts['current_category']}")
    if facts.get("movement"):
        lines.append(f"Movement: {facts['movement']}")
    if facts.get("wind_mph") is not None:
        lines.append(f"Sustained winds: {facts['wind_mph']} mph")
    if facts.get("pressure_mb") is not None:
        lines.append(f"Pressure: {facts['pressure_mb']} mb")
    if facts.get("moved_desc"):
        lines.append(f"Since last advisory: {facts['moved_desc']}")
    if facts.get("status_change"):
        lines.append(f"Status change: {facts['status_change']}")
    if facts.get("wind_change_mph") is not None:
        trend = "strengthening" if facts["wind_change_mph"] > 0 else ("weakening" if facts["wind_change_mph"] < 0 else "holding steady")
        lines.append(f"Wind trend vs last advisory: {facts['wind_change_mph']:+d} mph ({trend})")
    if facts.get("pressure_change_mb") is not None:
        lines.append(f"Pressure trend vs last advisory: {facts['pressure_change_mb']:+d} mb")
    if facts.get("nhc_changes"):
        lines.append(f"NHC-stated changes this advisory: {facts['nhc_changes']}")

    fb = facts.get("forecast_bands")
    if fb:
        if fb.get("short_term"):
            lines.append(f"Short-term forecast (next ~24h): {fb['short_term']}")
        if fb.get("mid_term"):
            lines.append(f"Mid-term forecast (1.5-3 days): {fb['mid_term']}")
        if fb.get("long_term"):
            lines.append(f"Long-term outlook (4-5 days, lower confidence): {fb['long_term']}")
        if fb.get("peak_mph"):
            lines.append(f"Peak forecast intensity: {fb['peak_mph']} mph ({fb['peak_category']}), expected {fb['peak_when']}" + (f" ({fb['peak_note']})" if fb.get("peak_note") else ""))
        if fb.get("weakening_starts"):
            lines.append(f"Weakening trend begins by: {fb['weakening_starts']}, dropping to {fb['weakening_to_mph']} mph ({fb['weakening_to_category']})")
        else:
            lines.append("Weakening trend: not yet showing in the forecast (still intensifying or holding through the available forecast)")

    return "\n".join(lines)


VOICE_SYSTEM_PROMPT = """You write the narrative portion of a weather update for a Gulf Coast / Southeast Texas (Beaumont, SETX/SWLA) audience, in the voice of a local broadcast meteorologist. This narrative gets inserted between a data header and NHC's official Key Messages, so do NOT repeat raw numbers that would already be in a header (advisory number) -- focus on telling the STORY of the storm using the facts given.

House style rules, follow ALL of them:
- Use "&" instead of "and" -- no comma before "&"
- No comma before "but" or "or" either
- No Oxford comma anywhere
- Capitalize "Tropical" / "Tropics" always
- Confident, calm, conversational tone -- collective "we"
- No greeting -- lead straight into the update
- 4-6 short sentences, forward-looking

Write EXACTLY two paragraphs, separated by one blank line:

PARAGRAPH 1 (current state & why): current intensity, whether it looks organized/strengthening or ragged/weakening (based on the wind & pressure trend given), and what's driving any watch/warning changes right now.

PARAGRAPH 2 (medium-to-long-term outlook): where it's headed, peak intensity/category and roughly when, and when it's expected to start weakening, if the facts show that.

Do not invent numbers, locations, categories, or impacts not present in the facts. No hashtags. At most one relevant emoji if it fits naturally, in the second paragraph. Output ONLY the two paragraphs, nothing else -- no headers, no bullet points, no preamble."""


def call_claude_api(facts_summary):
    api_key = os.environ["ANTHROPIC_API_KEY"]
    payload = json.dumps({
        "model": "claude-sonnet-5",
        "max_tokens": 500,
        "system": VOICE_SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": f"Write the narrative from these facts:\n\n{facts_summary}"}],
    }).encode("utf-8")

    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=payload,
        headers={"Content-Type": "application/json", "x-api-key": api_key, "anthropic-version": "2023-06-01"},
        method="POST",
    )

    last_err = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                text_blocks = [b["text"] for b in data.get("content", []) if b.get("type") == "text"]
                result = "\n".join(text_blocks).strip()
                if result:
                    return result
                last_err = "Empty response from Claude API"
        except urllib.error.HTTPError as e:
            last_err = f"HTTP {e.code}: {e.read().decode('utf-8', errors='replace')[:200]}"
        except Exception as e:
            last_err = str(e)
        print(f"[Claude API] Attempt {attempt} failed: {last_err}")
        if attempt < MAX_ATTEMPTS:
            time.sleep(RETRY_DELAY_SEC)

    raise RuntimeError(f"Claude API failed after {MAX_ATTEMPTS} attempts: {last_err}")


# ===========================================================================
# Delivery
# ===========================================================================
def telegram_configured():
    return bool(os.environ.get("TELEGRAM_BOT_TOKEN")) and bool(os.environ.get("TELEGRAM_CHAT_ID"))


def send_telegram(text):
    bot_token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = json.dumps({"chat_id": chat_id, "text": text}).encode("utf-8")
    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"}, method="POST")

    last_err = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                result = json.loads(resp.read().decode("utf-8"))
                if result.get("ok"):
                    return
                last_err = result.get("description", "Unknown Telegram error")
        except Exception as e:
            last_err = str(e)
        print(f"[Telegram] Attempt {attempt} failed: {last_err}")
        if attempt < MAX_ATTEMPTS:
            time.sleep(RETRY_DELAY_SEC)
    raise RuntimeError(f"Telegram send failed after {MAX_ATTEMPTS} attempts: {last_err}")


# ===========================================================================
# NHC cone graphic -- official static URL pattern confirmed from NWS Service
# Change Notice 26-27 (2026): storm_graphics/BBXX/CCXXYYYY_5day_cone.png
# ===========================================================================
def build_cone_url():
    """The cone image lives at the SAME url every advisory -- NHC just
    updates the file in place. That means Telegram's own servers can (and
    did) serve a cached copy of an OLDER cone instead of re-fetching the
    live file. Appending a changing query parameter (current epoch seconds)
    makes each request look like a brand new URL to Telegram, forcing a
    real fetch of whatever NHC currently has, every single time."""
    basin = STORM_PIL_SUFFIX[:2].upper()  # "AT", "EP", or "CP"
    num = STORM_PIL_SUFFIX[2:].zfill(2)   # e.g. "2" -> "02"
    basin_file_code = "AL" if basin == "AT" else basin  # Atlantic dir is AT, filename code is AL
    year = datetime.utcnow().year
    cache_buster = int(time.time())
    return f"https://www.nhc.noaa.gov/storm_graphics/{basin}{num}/{basin_file_code}{num}{year}_5day_cone.png?_cb={cache_buster}"


def build_surge_url():
    """Peak Storm Surge Forecast graphic -- same directory/naming pattern
    as the cone, confirmed live. Only meaningful when a storm surge
    watch/warning is active; NHC doesn't always have a current one, so
    callers should treat a fetch failure here as normal/non-fatal."""
    basin = STORM_PIL_SUFFIX[:2].upper()
    num = STORM_PIL_SUFFIX[2:].zfill(2)
    basin_file_code = "AL" if basin == "AT" else basin
    year = datetime.utcnow().year
    cache_buster = int(time.time())
    return f"https://www.nhc.noaa.gov/storm_graphics/{basin}{num}/{basin_file_code}{num}{year}_peak_surge.png?_cb={cache_buster}"


def send_telegram_photo(photo_url, caption=""):
    bot_token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    url = f"https://api.telegram.org/bot{bot_token}/sendPhoto"
    payload = json.dumps({"chat_id": chat_id, "photo": photo_url, "caption": caption[:1024]}).encode("utf-8")
    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"}, method="POST")

    last_err = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                result = json.loads(resp.read().decode("utf-8"))
                if result.get("ok"):
                    return
                last_err = result.get("description", "Unknown Telegram error")
        except Exception as e:
            last_err = str(e)
        print(f"[Telegram photo] Attempt {attempt} failed: {last_err}")
        if attempt < MAX_ATTEMPTS:
            time.sleep(RETRY_DELAY_SEC)
    # Non-fatal -- if the photo fails (e.g. graphic not published yet this
    # advisory), we still want the text update to go out.
    print(f"Cone graphic send failed after {MAX_ATTEMPTS} attempts (non-fatal): {last_err}")


def send_email_sms_fallback(text, subject="NHC Update"):
    smtp_server = os.environ["SMTP_SERVER"]
    smtp_port = int(os.environ.get("SMTP_PORT", "587"))
    smtp_user = os.environ["SMTP_USER"]
    smtp_pass = os.environ["SMTP_PASS"]
    to_addr = os.environ["ALERT_TO"]

    msg = MIMEText(text)
    msg["Subject"] = subject
    msg["From"] = smtp_user
    msg["To"] = to_addr

    with smtplib.SMTP(smtp_server, smtp_port) as server:
        server.starttls()
        server.login(smtp_user, smtp_pass)
        server.sendmail(smtp_user, [to_addr], msg.as_string())


def deliver(text, subject="NHC Update"):
    if telegram_configured():
        send_telegram(text)
        print("Delivered via Telegram.")
    else:
        print("Telegram not configured yet -- falling back to email-to-SMS for now.")
        send_email_sms_fallback(text, subject=subject)
        print("Delivered via email-to-SMS fallback.")


def send_failure_alert(context, error):
    try:
        deliver(f"[nhc-pipeline error] {context}: {error}", subject="nhc-pipeline error")
    except Exception as e:
        print(f"Could not even send the failure alert: {e}", file=sys.stderr)


# ===========================================================================
# Main
# ===========================================================================
def main():
    tcp_text, tcp_source = fetch_product("TCP")
    if not tcp_text:
        send_failure_alert("Fetching Public Advisory", "Both IEM and NHC failed -- check the source URLs / storm may be gone")
        sys.exit(1)
    print(f"TCP fetched from {tcp_source}")

    header = tcp_header(tcp_text)
    if not header:
        print("Could not find a Public Advisory header -- storm may be gone. Exiting quietly.")
        return

    advisory_num = header["number"]
    state = load_state()

    # Backup verification: 15 minutes after a successful send, resend the
    # cone graphic (confirms it actually came through) plus the storm
    # surge graphic (updates less often, so it's fine to only include it
    # here rather than slowing down the fast lead update).
    pending = state.get("pending_cone_verification")
    if pending and (time.time() - pending["queued_at"]) >= 900:
        try:
            if telegram_configured():
                cone_url = build_cone_url()
                send_telegram_photo(cone_url, caption=f"Cone Graphic -- Backup Confirmation (Advisory #{pending['advisory_num']})")
                print(f"Sent 15-minute verification resend of the cone graphic for advisory #{pending['advisory_num']}.")
        except Exception as e:
            print(f"Cone verification resend failed (non-fatal): {e}")
        try:
            if telegram_configured():
                surge_url = build_surge_url()
                send_telegram_photo(surge_url, caption=f"Peak Storm Surge Forecast (Advisory #{pending['advisory_num']})")
                print(f"Sent storm surge graphic for advisory #{pending['advisory_num']}.")
        except Exception as e:
            print(f"Storm surge graphic send failed (non-fatal -- may not be active right now): {e}")
        state.pop("pending_cone_verification", None)
        save_state(state)

    if state.get("last_advisory_number") == advisory_num:
        print(f"No change -- still advisory #{advisory_num}. Not sending an update.")
        return

    location = tcp_location(tcp_text)
    wind_mph = tcp_wind_mph(tcp_text)
    movement = tcp_movement(tcp_text)
    pressure_mb = tcp_pressure_mb(tcp_text)
    nhc_changes = tcp_changes(tcp_text)
    next_advisory = tcp_next_advisory(tcp_text)

    movement_str = None
    if movement:
        movement_str = "stationary" if movement.get("stationary") else f"{movement['compass']} at {movement['mph']} mph"

    facts = {
        "name": header["status_and_name"],
        "advisory_num": advisory_num,
        "movement": movement_str,
        "wind_mph": wind_mph,
        "current_category": wind_category(wind_mph),
        "pressure_mb": pressure_mb,
        "nhc_changes": nhc_changes,
        "next_advisory": next_advisory,
    }

    prev = state.get("last")
    if prev and location:
        dist_nm = haversine_nm(prev["lat"], prev["lon"], location["lat"], location["lon"])
        dist_mi = round(dist_nm * 1.15078)
        bearing = initial_bearing(prev["lat"], prev["lon"], location["lat"], location["lon"])
        compass = bearing_to_compass(bearing)
        facts["moved_desc"] = f"moved {dist_mi} mi {compass} since advisory #{prev.get('number', '?')}"

        if prev.get("status_and_name") and prev["status_and_name"] != header["status_and_name"]:
            facts["status_change"] = f"{prev['status_and_name']} -> {header['status_and_name']}"
        if prev.get("wind_mph") is not None and wind_mph is not None:
            facts["wind_change_mph"] = wind_mph - prev["wind_mph"]
        if prev.get("pressure_mb") is not None and pressure_mb is not None:
            facts["pressure_change_mb"] = pressure_mb - prev["pressure_mb"]

    key_messages = []
    try:
        tcd_text, tcd_source = fetch_product("TCD")
        if tcd_text:
            print(f"TCD fetched from {tcd_source}")
            positions = tcd_forecast_positions(tcd_text)
            bands = summarize_forecast_bands(positions, wind_mph)
            if bands:
                facts["forecast_bands"] = bands
            key_messages = tcd_key_messages(tcd_text)
            discussion_paragraphs = tcd_discussion_text(tcd_text)
    except Exception as e:
        print(f"Discussion product unavailable (non-fatal): {e}")
        discussion_paragraphs = []

    bot_header = build_bot_header(facts)

    if discussion_paragraphs:
        narrative = "\n\n".join(discussion_paragraphs)
    else:
        narrative = "(NHC's discussion text wasn't available for this advisory.)"

    parts = [bot_header, "", "NHC Discussion:", "", narrative]
    if key_messages:
        parts.append("")
        parts.append("Key Messages:")
        for i, msg in enumerate(key_messages, 1):
            if i > 1:
                parts.append("")
            parts.append(f"{i}. {msg}")
    full_message = "\n".join(parts)

    print(f"Full message:\n{full_message}")

    if telegram_configured():
        cone_url = build_cone_url()
        send_telegram_photo(cone_url, caption=f"{facts['name']} -- 5-Day Cone (Advisory #{advisory_num})")

    try:
        deliver(full_message, subject=f"{header['status_and_name']} Adv #{advisory_num}")
    except Exception as e:
        send_failure_alert("Delivery", str(e))
        sys.exit(1)

    print("Sent successfully.")

    if telegram_configured():
        state["pending_cone_verification"] = {"advisory_num": advisory_num, "queued_at": time.time()}

    new_last = {"number": advisory_num, "status_and_name": header["status_and_name"]}
    if location:
        new_last["lat"], new_last["lon"] = location["lat"], location["lon"]
    if wind_mph is not None:
        new_last["wind_mph"] = wind_mph
    if pressure_mb is not None:
        new_last["pressure_mb"] = pressure_mb

    state["last_advisory_number"] = advisory_num
    state["last"] = new_last
    save_state(state)


if __name__ == "__main__":
    main()
