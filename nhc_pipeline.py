#!/usr/bin/env python3
"""
nhc_pipeline.py -- Two-message NHC advisory pipeline:

  MESSAGE 1 (Fast Headline) -- sent IMMEDIATELY the moment a new advisory
  is detected. Contains: NHC's own issued time, category, wind, pressure,
  movement, track change since last advisory, wind/pressure trend, peak
  forecast, next advisory time, and the cone graphic. Never waits on
  anything. Never contains discussion/forecast narrative text.

  MESSAGE 2 (Discussion) -- sent INDEPENDENTLY, whenever NHC's Discussion
  product has genuinely been updated. Checked every loop iteration
  (~25 sec via nhc_fast_loop.py), completely decoupled from the advisory
  timing. Only sends when BOTH of these are true:
    1. The Discussion's own "Discussion Number" has increased, AND
    2. The actual discussion text is different from the last one sent
  This double-check protects against ever resending old content even if
  one signal alone were misleading. Includes NHC's own issued time for
  the Discussion, plus Key Messages.

Both hard-coded facts (wind, pressure, movement, trend) are 100% Python
math -- no AI. There is no AI anywhere in this pipeline; all narrative
text is NHC's own words, relayed verbatim.

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
    "TCM": f"https://www.nhc.noaa.gov/text/MIATCM{STORM_PIL_SUFFIX}.shtml?text",
}

STATE_FILE = Path(__file__).parent / "pipeline_state.json"
CENTRAL_UTC_OFFSET = 5  # CDT (UTC-5). Change to 6 for CST (winter).

MAX_ATTEMPTS = 3
RETRY_DELAY_SEC = 5


# ===========================================================================
# FETCH -- IEM primary, NHC fallback, with retries + cache-busting on each
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
    """Cache-buster forces a fresh fetch every time -- IEM/NHC can serve a
    cached response for a plain, unchanging URL otherwise."""
    pil = f"{product_type}{STORM_PIL_SUFFIX}"
    cache_buster = int(time.time())
    iem_url = f"{IEM_BASE}?pil={pil}&_cb={cache_buster}"

    text = _fetch_with_retries(iem_url, f"IEM:{pil}")
    if text:
        return text, "IEM"

    print(f"[{pil}] IEM failed after {MAX_ATTEMPTS} attempts, falling back to NHC...")
    nhc_url = f"{NHC_URLS[product_type]}&_cb={cache_buster}" if "?" in NHC_URLS[product_type] else f"{NHC_URLS[product_type]}?_cb={cache_buster}"
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
    return f"{hour12}:{minute:02d} {period}, day {day}"


def nhc_issued_time(text):
    """Extracts NHC's own issued date/time line, e.g. '700 AM CDT Wed Jul
    22 2026' -> '7:00 AM Wed Jul 22'. This is NHC's own stated time, not
    our clock -- always shown at the top of every message so timing is
    never ambiguous."""
    m = re.search(r"(\d{3,4})\s+([AP]M)\s+[A-Z]{3,4}\s+(\w{3})\s+(\w{3})\s+(\d{1,2})\s+\d{4}", text)
    if not m:
        return None
    hhmm, ampm, weekday, month, day = m.group(1), m.group(2), m.group(3), m.group(4), m.group(5)
    if len(hhmm) == 3:
        hour, minute = hhmm[0], hhmm[1:]
    else:
        hour, minute = hhmm[:2], hhmm[2:]
    return f"{int(hour)}:{minute} {ampm} {weekday} {month} {day}"


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


def tcp_next_advisory(text):
    m = re.search(r"Next\s+(?:complete\s+)?advisory\s+at\s+[^\n.]+", text, re.I)
    if not m:
        return None
    return re.sub(r"\s+(CDT|CST|EDT|EST)\b", "", m.group(0).strip(), flags=re.I)


# ===========================================================================
# TCM -- Forecast/Advisory: gusts + 34kt wind radii by quadrant
# ===========================================================================
def tcm_gusts_and_radii(text):
    result = {}
    m = re.search(r"MAX SUSTAINED WINDS\s+(\d{1,3})\s*KT\s+WITH GUSTS TO\s+(\d{1,3})\s*KT", text, re.I)
    if m:
        result["gust_kt"] = int(m.group(2))
    m = re.search(r"^\s*34 KT[.\s]+(\d{1,3})NE\s+(\d{1,3})SE\s+(\d{1,3})SW\s+(\d{1,3})NW", text, re.I | re.M)
    if m:
        ne, se, sw, nw = (int(m.group(i)) for i in range(1, 5))
        result["radii_34kt"] = {"NE": ne, "SE": se, "SW": sw, "NW": nw}
    return result


# ===========================================================================
# TCD -- Discussion: number, issued time, key messages, forecast positions,
# and the discussion text itself
# ===========================================================================
def tcd_discussion_number(text):
    m = re.search(r"Discussion Number\s+(\d+)", text, re.I)
    return int(m.group(1)) if m else None


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
            "mph": mph,
            "category": wind_category(mph),
            "note": (m.group(7) or "").strip(),
        })
    return rows


SHORT_TERM_LABELS = {"INIT", "12H", "24H"}
MID_TERM_LABELS = {"36H", "48H", "60H", "72H"}
LONG_TERM_LABELS = {"96H", "120H"}


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

    return {
        "peak_mph": peak["mph"],
        "peak_category": peak["category"],
        "peak_when": peak["local"],
        "peak_note": peak["note"],
        "weakening_starts": weakening["local"] if weakening else None,
        "weakening_to_mph": weakening["mph"] if weakening else None,
    }


def tcd_key_messages(text):
    m = re.search(r"Key Messages:\s*\n+(.*?)\n\s*\n\s*(?:FORECAST POSITIONS|\$\$)", text, re.I | re.S)
    if not m:
        return []
    block = m.group(1)
    items = re.split(r"\n\s*\n(?=\d+\.)", block.strip())
    cleaned = []
    for item in items:
        item = re.sub(r"^\d+\.\s*", "", item.strip())
        item = re.sub(r"\s+", " ", item).strip()
        if item:
            cleaned.append(item)
    return cleaned


def tcd_discussion_text(text):
    """NHC's own discussion paragraphs verbatim -- US government work,
    public domain, relayed as-is (not AI-rewritten)."""
    m = re.search(
        r"\d{3,4}\s+[AP]M\s+\w+\s+\w+\s+\w+\s+\d+\s+\d{4}\s*\n+(.*?)\n\s*\n\s*(?:Key Messages:|FORECAST POSITIONS)",
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


# ===========================================================================
# Distance / bearing between two fixes -- for "track change since last
# advisory"
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
# NHC cone / storm surge graphics -- official static URL pattern, cache-
# busted every call so Telegram can never serve a stale cached image.
# ===========================================================================
def build_cone_url():
    basin = STORM_PIL_SUFFIX[:2].upper()
    num = STORM_PIL_SUFFIX[2:].zfill(2)
    basin_file_code = "AL" if basin == "AT" else basin
    year = datetime.utcnow().year
    cache_buster = int(time.time())
    return f"https://www.nhc.noaa.gov/storm_graphics/{basin}{num}/{basin_file_code}{num}{year}_5day_cone.png?_cb={cache_buster}"


def build_surge_url():
    basin = STORM_PIL_SUFFIX[:2].upper()
    num = STORM_PIL_SUFFIX[2:].zfill(2)
    basin_file_code = "AL" if basin == "AT" else basin
    year = datetime.utcnow().year
    cache_buster = int(time.time())
    return f"https://www.nhc.noaa.gov/storm_graphics/{basin}{num}/{basin_file_code}{num}{year}_peak_surge.png?_cb={cache_buster}"


# ===========================================================================
# Message builders (bot-written, deterministic, zero AI)
# ===========================================================================
def build_fast_headline(facts):
    lines = []
    if facts.get("issued_time"):
        lines.append(f"Issued: {facts['issued_time']}")
        lines.append("")
    lines.append(f"{facts['name']} -- Advisory #{facts['advisory_num']}")
    if facts.get("current_category"):
        lines.append(f"Category: {facts['current_category']}")
    if facts.get("wind_mph") is not None:
        gust_str = f" (gusts {facts['gust_mph']} mph)" if facts.get("gust_mph") else ""
        lines.append(f"Sustained wind: {facts['wind_mph']} mph{gust_str}")
    if facts.get("pressure_mb") is not None:
        lines.append(f"Min pressure: {facts['pressure_mb']} mb")
    if facts.get("movement"):
        lines.append(f"Movement: {facts['movement']}")
    if facts.get("moved_desc"):
        lines.append(f"Track change: {facts['moved_desc']}")
    if facts.get("radii_34kt"):
        r = facts["radii_34kt"]
        lines.append(f"Tropical storm-force winds extend: NE {r['NE']} mi, SE {r['SE']} mi, SW {r['SW']} mi, NW {r['NW']} mi")
    if facts.get("wind_change_mph") is not None:
        wc = facts["wind_change_mph"]
        trend = "strengthened" if wc > 0 else ("weakened" if wc < 0 else "held steady")
        lines.append(f"Wind vs previous advisory: {wc:+d} mph ({trend})")
    if facts.get("pressure_change_mb") is not None:
        pc = facts["pressure_change_mb"]
        trend = "strengthening signal" if pc < 0 else ("weakening signal" if pc > 0 else "steady")
        lines.append(f"Pressure vs previous advisory: {pc:+d} mb ({trend})")
    fb = facts.get("forecast_bands")
    if fb and fb.get("peak_mph"):
        peak_when_clean = fb["peak_when"]
        lines.append(f"Peak forecast: {fb['peak_mph']} mph ({fb['peak_category']}) at {peak_when_clean}")
    if facts.get("next_advisory"):
        lines.append("")
        lines.append(f"({facts['next_advisory']})")
    return "\n".join(lines)


def build_discussion_message(issued_time, advisory_num, discussion_paragraphs, key_messages):
    parts = []
    if issued_time:
        parts.append(f"Issued: {issued_time}")
        parts.append("")
    parts.append(f"NHC Discussion (Advisory #{advisory_num})")
    parts.append("")
    parts.append("\n\n".join(discussion_paragraphs))
    if key_messages:
        parts.append("")
        parts.append("Key Messages:")
        for i, msg in enumerate(key_messages, 1):
            if i > 1:
                parts.append("")
            parts.append(f"{i}. {msg}")
    return "\n".join(parts)


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
    print(f"Cone/surge graphic send failed after {MAX_ATTEMPTS} attempts (non-fatal): {last_err}")


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
        print("Telegram not configured -- falling back to email-to-SMS.")
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
    state = load_state()

    # ---- PART A: discussion catch-up check, runs every single cycle,
    # completely independent of whether there's a new advisory this pass.
    # Only sends when BOTH the Discussion Number AND the actual text have
    # genuinely changed since the last one we sent -- never on just one
    # signal alone.
    pending = state.get("pending_discussion")
    if pending:
        try:
            tcd_text, tcd_source = fetch_product("TCD")
            if tcd_text:
                new_number = tcd_discussion_number(tcd_text)
                new_paragraphs = tcd_discussion_text(tcd_text)
                new_text_joined = "\n\n".join(new_paragraphs)
                last_number = state.get("last_sent_discussion_number")
                last_text = state.get("last_sent_discussion_text", "")

                number_is_new = new_number is not None and (last_number is None or new_number > last_number)
                text_is_new = bool(new_text_joined) and new_text_joined != last_text

                print(f"Discussion check: fetched #{new_number} (last sent #{last_number}), number_is_new={number_is_new}, text_is_new={text_is_new}")

                if number_is_new and text_is_new:
                    issued = nhc_issued_time(tcd_text)
                    key_messages = tcd_key_messages(tcd_text)
                    discussion_message = build_discussion_message(issued, pending["advisory_num"], new_paragraphs, key_messages)
                    deliver(discussion_message, subject=f"NHC Discussion -- Advisory #{pending['advisory_num']}")
                    print(f"Sent Discussion #{new_number} for advisory #{pending['advisory_num']} (confirmed new by both number and text).")
                    state["last_sent_discussion_number"] = new_number
                    state["last_sent_discussion_text"] = new_text_joined
                    state.pop("pending_discussion", None)
                    save_state(state)
                else:
                    print("Discussion not yet confirmed new -- will keep checking next cycle.")
        except Exception as e:
            print(f"Discussion catch-up check failed this cycle (non-fatal, will retry): {e}")

    # ---- PART B: cone verification resend (15 min after a send)
    cone_pending = state.get("pending_cone_verification")
    if cone_pending and (time.time() - cone_pending["queued_at"]) >= 900:
        try:
            if telegram_configured():
                cone_url = build_cone_url()
                send_telegram_photo(cone_url, caption=f"Cone Graphic -- 15-Min Backup Confirmation (Advisory #{cone_pending['advisory_num']})")
                print(f"Sent 15-minute verification cone graphic for advisory #{cone_pending['advisory_num']}.")
        except Exception as e:
            print(f"Cone verification resend failed (non-fatal): {e}")
        try:
            if cone_pending.get("full_message"):
                deliver(cone_pending["full_message"], subject=f"15-Min Backup -- Advisory #{cone_pending['advisory_num']}")
                print(f"Resent the fast headline for advisory #{cone_pending['advisory_num']} as the 15-minute backup.")
        except Exception as e:
            print(f"Full-update verification resend failed (non-fatal): {e}")
        try:
            if telegram_configured():
                surge_url = build_surge_url()
                send_telegram_photo(surge_url, caption=f"Peak Storm Surge Forecast (Advisory #{cone_pending['advisory_num']})")
        except Exception as e:
            print(f"Storm surge graphic send failed (non-fatal -- may not be active right now): {e}")
        state.pop("pending_cone_verification", None)
        save_state(state)

    # ---- PART C: check for a new advisory -- this is the FAST path
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

    if state.get("last_advisory_number") == advisory_num:
        print(f"No change -- still advisory #{advisory_num}. Not sending an update.")
        return

    # Sending every distinct advisory NHC issues -- full or intermediate --
    # per explicit instruction: never miss a real change, speed matters
    # more than cutting down message frequency.
    location = tcp_location(tcp_text)
    wind_mph = tcp_wind_mph(tcp_text)
    movement = tcp_movement(tcp_text)
    pressure_mb = tcp_pressure_mb(tcp_text)
    next_advisory = tcp_next_advisory(tcp_text)
    issued_time = nhc_issued_time(tcp_text)

    movement_str = None
    if movement:
        movement_str = "stationary" if movement.get("stationary") else f"{movement['compass']} at {movement['mph']} mph"

    facts = {
        "name": header["status_and_name"],
        "advisory_num": advisory_num,
        "issued_time": issued_time,
        "movement": movement_str,
        "wind_mph": wind_mph,
        "current_category": wind_category(wind_mph),
        "pressure_mb": pressure_mb,
        "next_advisory": next_advisory,
    }

    prev = state.get("last")
    if prev and location:
        dist_nm = haversine_nm(prev["lat"], prev["lon"], location["lat"], location["lon"])
        dist_mi = round(dist_nm * 1.15078)
        bearing = initial_bearing(prev["lat"], prev["lon"], location["lat"], location["lon"])
        compass = bearing_to_compass(bearing)
        facts["moved_desc"] = f"moved {dist_mi} mi {compass} since advisory #{prev.get('number', '?')}"

        if prev.get("wind_mph") is not None and wind_mph is not None:
            facts["wind_change_mph"] = wind_mph - prev["wind_mph"]
        if prev.get("pressure_mb") is not None and pressure_mb is not None:
            facts["pressure_change_mb"] = pressure_mb - prev["pressure_mb"]

    try:
        tcm_text, tcm_source = fetch_product("TCM")
        if tcm_text:
            print(f"TCM fetched from {tcm_source}")
            tcm_data = tcm_gusts_and_radii(tcm_text)
            if tcm_data.get("gust_kt"):
                facts["gust_mph"] = kt_to_mph(tcm_data["gust_kt"])
            if tcm_data.get("radii_34kt"):
                facts["radii_34kt"] = tcm_data["radii_34kt"]
    except Exception as e:
        print(f"Forecast/Advisory (TCM) unavailable (non-fatal): {e}")

    try:
        tcd_text_for_bands, _ = fetch_product("TCD")
        if tcd_text_for_bands:
            positions = tcd_forecast_positions(tcd_text_for_bands)
            bands = summarize_forecast_bands(positions, wind_mph)
            if bands:
                facts["forecast_bands"] = bands
    except Exception as e:
        print(f"Forecast bands unavailable (non-fatal): {e}")

    fast_headline = build_fast_headline(facts)
    print(f"Fast headline:\n{fast_headline}")

    try:
        deliver(fast_headline, subject=f"{header['status_and_name']} Adv #{advisory_num}")
    except Exception as e:
        send_failure_alert("Fast headline delivery", str(e))
        sys.exit(1)

    print("Fast headline sent successfully.")

    if telegram_configured():
        cone_url = build_cone_url()
        send_telegram_photo(cone_url, caption=f"{facts['name']} -- 5-Day Cone (Advisory #{advisory_num})")
        state["pending_cone_verification"] = {
            "advisory_num": advisory_num,
            "queued_at": time.time(),
            "full_message": fast_headline,
        }

    # Queue the discussion catch-up check -- Part A will pick this up on
    # this same cycle or any future cycle, whenever NHC actually publishes
    # a genuinely new Discussion (verified by number + text both).
    state["pending_discussion"] = {"advisory_num": advisory_num}

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
