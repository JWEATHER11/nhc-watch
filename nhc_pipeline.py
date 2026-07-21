#!/usr/bin/env python3
"""
nhc_pipeline.py -- Fully automated NHC advisory pipeline:

  1. FETCH   -- Public Advisory (TCP) + Forecast/Advisory (TCM), from IEM's
                text-product API first, falling back to NHC's own website
                if IEM is unreachable.
  2. COMPARE -- against the last-seen advisory (position, wind, pressure,
                status), using hard-coded math (no AI involved in this
                step, for 100% accuracy).
  3. CONVERT -- kt -> mph, Zulu -> Central time. Hard-coded, not AI, so
                there's zero risk of a model inventing a wrong number.
  4. REWRITE -- sends the structured facts (not raw NHC text) to the Claude
                API, which writes a ready-to-post update in your voice for
                a Gulf Coast / Beaumont TX audience.
  5. DELIVER -- sends the finished post to Telegram if TELEGRAM_BOT_TOKEN
                and TELEGRAM_CHAT_ID secrets are present; otherwise falls
                back to the existing email-to-SMS delivery so the pipeline
                is still fully testable before Telegram is set up. If
                every attempt fails, sends a short plain-text failure
                alert via whichever channel is available.

State (last-seen advisory + position/wind/pressure) lives in state.json and
is committed back by the GitHub Actions workflow after each run.
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
    "TCM": f"https://www.nhc.noaa.gov/text/MIATCM{STORM_PIL_SUFFIX}.shtml?text",
}

STATE_FILE = Path(__file__).parent / "pipeline_state.json"
CENTRAL_UTC_OFFSET = 5  # CDT (UTC-5). Change to 6 for CST (winter).

MONTHS = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}

MAX_ATTEMPTS = 3
RETRY_DELAY_SEC = 5


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
# Shared unit / time helpers (hard-coded conversions -- no AI, no error risk)
# ===========================================================================
def kt_to_mph(kt):
    return round(kt * 1.15078)


def with_mph(kt):
    return f"{kt:g} kt ({kt_to_mph(kt)} mph)"


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
# TCP -- Public/Intermediate Advisory (drives dedupe + comparison)
# ===========================================================================
def tcp_header(text):
    m = re.search(r"^(.+?)\s+(Intermediate\s+)?Advisory Number\s+(\S+)", text, re.I | re.M)
    if not m:
        return None
    return {
        "status_and_name": m.group(1).strip(),
        "number": m.group(3).rstrip("."),
    }


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
# TCM -- Forecast/Advisory (peak forecast wind)
# ===========================================================================
def tcm_forecast_track(text):
    pattern = re.compile(
        r"(FORECAST|OUTLOOK)\s+VALID\s+(\d{1,2}/\d{4}Z)\s+(\d{1,3}\.?\d*)N\s+(\d{1,3}\.?\d*)W([^\n]*)\n\s*MAX WIND\s+(\d{1,3})\s*KT\.\.\.GUSTS\s+(\d{1,3})\s*KT",
        re.I,
    )
    points = []
    for m in pattern.finditer(text):
        points.append({
            "local": zulu_to_central(m.group(2)) or m.group(2),
            "max_wind": int(m.group(6)), "gusts": int(m.group(7)),
        })
    return points


def tcm_peak_wind(points):
    if not points:
        return None
    peak = max(points, key=lambda p: p["max_wind"])
    return {
        "mph": kt_to_mph(peak["max_wind"]),
        "gust_mph": kt_to_mph(peak["gusts"]),
        "when": peak["local"],
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
# Claude API -- rewrites structured facts into a ready-to-post update. This
# is the ONLY step touching an LLM; every number came from hard-coded
# parsing/math above, so the AI can only get wording wrong, never a fact.
# ===========================================================================
def build_facts_summary(facts):
    lines = [f"Storm: {facts['name']}", f"Advisory #: {facts['advisory_num']}"]
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
        lines.append(f"Wind change vs last advisory: {facts['wind_change_mph']:+d} mph")
    if facts.get("pressure_change_mb") is not None:
        lines.append(f"Pressure change vs last advisory: {facts['pressure_change_mb']:+d} mb")
    if facts.get("nhc_changes"):
        lines.append(f"NHC-stated changes this advisory: {facts['nhc_changes']}")
    if facts.get("peak_forecast"):
        pf = facts["peak_forecast"]
        lines.append(f"Peak forecast intensity: {pf['mph']} mph sustained, gusts {pf['gust_mph']} mph, expected {pf['when']}")
    if facts.get("next_advisory"):
        lines.append(f"Next advisory: {facts['next_advisory']}")
    return "\n".join(lines)


VOICE_SYSTEM_PROMPT = """You write short weather-update posts for a Gulf Coast / Southeast Texas (Beaumont, SETX/SWLA) audience, in the voice of a local broadcast meteorologist. House style rules, follow ALL of them:

- Use "&" instead of "and" -- no comma before "&"
- No comma before "but" or "or" either
- No Oxford comma anywhere
- Capitalize "Tropical" / "Tropics" always
- Confident, calm, conversational tone -- collective "we"
- No greeting -- lead straight into the update
- 2-4 short sentences, forward-looking
- Use only the facts given below -- do not invent numbers, locations, or impacts not present in the facts
- No hashtags. At most one relevant emoji if it fits naturally
- Output ONLY the post text, nothing else"""


def call_claude_api(facts_summary):
    api_key = os.environ["ANTHROPIC_API_KEY"]
    payload = json.dumps({
        "model": "claude-sonnet-5",
        "max_tokens": 400,
        "system": VOICE_SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": f"Write the update post from these facts:\n\n{facts_summary}"}],
    }).encode("utf-8")

    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        },
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
# Delivery -- Telegram if configured, else fall back to email-to-SMS so the
# whole pipeline is testable before Telegram is set up.
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
        if movement.get("stationary"):
            movement_str = "stationary"
        else:
            movement_str = f"{movement['compass']} ({movement['degrees']} deg) at {movement['mph']} mph"

    facts = {
        "name": header["status_and_name"],
        "advisory_num": advisory_num,
        "movement": movement_str,
        "wind_mph": wind_mph,
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

    try:
        tcm_text, tcm_source = fetch_product("TCM")
        if tcm_text:
            print(f"TCM fetched from {tcm_source}")
            track = tcm_forecast_track(tcm_text)
            peak = tcm_peak_wind(track)
            if peak:
                facts["peak_forecast"] = peak
    except Exception as e:
        print(f"Peak forecast wind unavailable (non-fatal): {e}")

    facts_summary = build_facts_summary(facts)
    print(f"Facts for Claude:\n{facts_summary}")

    try:
        post_text = call_claude_api(facts_summary)
    except Exception as e:
        send_failure_alert("Claude API rewrite step", str(e))
        sys.exit(1)

    print(f"Generated post:\n{post_text}")

    try:
        deliver(post_text, subject=f"{header['status_and_name']} Adv #{advisory_num}")
    except Exception as e:
        send_failure_alert("Delivery", str(e))
        sys.exit(1)

    print("Sent successfully.")

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
