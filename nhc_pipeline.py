#!/usr/bin/env python3
"""
nhc_pipeline.py -- Fully automated NHC advisory pipeline, split into two
independent messages per advisory:

  MESSAGE 1 (fast, sent immediately when a new advisory is detected):
    - NHC's own issued time, at the top
    - Cone graphic
    - Category, sustained wind (+gusts), min pressure, movement
    - Track change since the previous advisory (distance & direction moved)
    - Wind/pressure trend vs. previous advisory
    - Peak forecast intensity & timing
    - Next advisory time
    - Tropical-storm-force wind radii by quadrant
    NEVER contains discussion/forecast narrative text.

  MESSAGE 2 (discussion, sent independently, only when genuinely new):
    - NHC's own issued time for the Discussion specifically
    - The Discussion text + Key Messages, verbatim from NHC (public domain,
      no AI paraphrase)
    - Checked every loop iteration (~25s), completely decoupled from
      Message 1's timing
    - Only sends when BOTH signals agree it's new: (a) NHC's own
      "Discussion Number" has increased, AND (b) the actual text differs
      from the last one delivered. Either signal alone isn't trusted --
      both must agree, so a repeat can never slip through.

All numbers are hard-coded parsing/math -- zero AI involved in any fact.
State lives in pipeline_state.json, committed back by the workflow, with
pull-before-push + retry to survive concurrent commits from other loops.
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

IEM_BASE = "https://mesonet.agron.iastate.edu/cgi-bin/afos/retrieve.py"
CURRENT_STORMS_URL = "https://www.nhc.noaa.gov/CurrentStorms.json"

STATE_FILE = Path(__file__).parent / "pipeline_state.json"
CENTRAL_UTC_OFFSET = 5  # CDT (UTC-5). Change to 6 for CST (winter).

MAX_ATTEMPTS = 2  # reduced from 3 -- speed, matches wxmodel_pipeline.py fix
RETRY_DELAY_SEC = 2  # reduced from 5 -- speed, matches wxmodel_pipeline.py fix


# ===========================================================================
# FETCH -- IEM primary, NHC fallback, with retries + cache-busting on each
# ===========================================================================
def _http_get(url, timeout=10):  # reduced from 20 -- speed, matches wxmodel_pipeline.py fix
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


def fetch_product(product_type, storm_suffix):
    """Cache-buster on every fetch -- confirmed necessary, IEM/NHC can
    serve a cached response for a plain unchanging URL."""
    pil = f"{product_type}{storm_suffix}"
    cache_buster = int(time.time() * 1000)
    iem_url = f"{IEM_BASE}?pil={pil}&_cb={cache_buster}"

    text = _fetch_with_retries(iem_url, f"IEM:{pil}")
    if text:
        return text, "IEM"

    print(f"[{pil}] IEM failed after {MAX_ATTEMPTS} attempts, falling back to NHC...")
    base_url = f"https://www.nhc.noaa.gov/text/MIA{pil}.shtml?text"
    nhc_url = f"{base_url}&_cb={cache_buster}"
    text = _fetch_with_retries(nhc_url, f"NHC:{pil}")
    if text:
        return text, "NHC"

    print(f"[{pil}] Both IEM and NHC failed after {MAX_ATTEMPTS} attempts each.")
    return None, "FAILED"


def fetch_active_storm_bin():
    """Auto-discovers the current active Atlantic-basin storm's PIL suffix
    (e.g. "AT3") from NHC's own machine-readable active-storms feed, instead
    of relying on a hardcoded suffix that has to be manually updated every
    time one storm ends and another forms. Confirmed live 2026-08-12: a
    hardcoded suffix left pointed at a dissipated storm (Bertha/AT2) meant
    the pipeline silently never noticed Tropical Storm Cristobal (AT3) form
    days later -- this makes that class of bug impossible going forward.
    Returns None if no Atlantic storm is currently active."""
    cache_buster = int(time.time())
    text = _fetch_with_retries(f"{CURRENT_STORMS_URL}?_cb={cache_buster}", "CurrentStorms")
    if not text:
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    for storm in data.get("activeStorms", []):
        storm_id = (storm.get("id") or "").lower()
        bin_number = storm.get("binNumber")
        if storm_id.startswith("al") and bin_number:
            return bin_number
    return None


# ===========================================================================
# Shared unit / time / category helpers
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


def issued_time_from_header(text):
    """Pulls NHC's own stated issue time/date line, e.g.
    '1000 AM CDT Wed Jul 22 2026' -- their words, not our clock."""
    m = re.search(r"^\s*\d{3,4}\s+[AP]M\s+[A-Z]{2,4}\s+\w{3}\s+\w{3}\s+\d{1,2}\s+\d{4}\s*$", text, re.M)
    return m.group(0).strip() if m else None


# ===========================================================================
# TCP -- Public/Intermediate Advisory
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
# TCD -- Discussion: forecast positions table, Key Messages, discussion
# text, issued time, and the Discussion Number itself (for dual-check)
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
    """NHC's own discussion paragraphs, verbatim (US government work,
    public domain) -- captures everything between the date/time header
    and 'Key Messages:' (or 'FORECAST POSITIONS' if no key messages)."""
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


def tcd_discussion_number(text):
    """NHC's own 'Discussion Number NN' -- the first of our two
    independent signals that a genuinely new discussion has posted."""
    m = re.search(r"Discussion Number\s+(\d+)", text, re.I)
    return int(m.group(1)) if m else None


# ===========================================================================
# TCM -- Forecast/Advisory: gusts + wind radii by quadrant
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
# Distance / bearing between two fixes -- for track-change display
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
# Graphics -- cone + storm surge, cache-busted every call
# ===========================================================================
def build_cone_url(storm_suffix):
    basin = storm_suffix[:2].upper()
    num = storm_suffix[2:].zfill(2)
    basin_file_code = "AL" if basin == "AT" else basin
    year = datetime.utcnow().year
    cache_buster = int(time.time())
    return f"https://www.nhc.noaa.gov/storm_graphics/{basin}{num}/{basin_file_code}{num}{year}_5day_cone.png?_cb={cache_buster}"


def build_surge_url(storm_suffix):
    basin = storm_suffix[:2].upper()
    num = storm_suffix[2:].zfill(2)
    basin_file_code = "AL" if basin == "AT" else basin
    year = datetime.utcnow().year
    cache_buster = int(time.time())
    return f"https://www.nhc.noaa.gov/storm_graphics/{basin}{num}/{basin_file_code}{num}{year}_peak_surge.png?_cb={cache_buster}"


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
    """Downloads the image ourselves and uploads the bytes directly to
    Telegram (multipart/form-data) -- more reliable than passing the URL
    for Telegram to fetch itself, which can fail with "wrong type of the
    web page content" if Telegram's fetcher doesn't like the response."""
    bot_token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    url = f"https://api.telegram.org/bot{bot_token}/sendPhoto"
    last_err = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            image_req = urllib.request.Request(photo_url, headers={"User-Agent": "nhc-pipeline/1.0"})
            with urllib.request.urlopen(image_req, timeout=20) as img_resp:
                image_bytes = img_resp.read()

            boundary = "----nhcPhotoBoundary"
            parts = [
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"chat_id\"\r\n\r\n{chat_id}\r\n".encode("utf-8"),
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"caption\"\r\n\r\n{caption[:1024]}\r\n".encode("utf-8"),
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"photo\"; filename=\"graphic.png\"\r\nContent-Type: image/png\r\n\r\n".encode("utf-8"),
                image_bytes,
                f"\r\n--{boundary}--\r\n".encode("utf-8"),
            ]
            body = b"".join(parts)
            req = urllib.request.Request(
                url, data=body,
                headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
                method="POST",
            )
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
    print(f"Photo send failed after {MAX_ATTEMPTS} attempts (non-fatal): {last_err}")


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
    """Telegram only -- no SMS/email fallback, per explicit instruction
    (GitHub Actions itself never texts anyone; this fallback was the
    actual source of unwanted texts, now removed everywhere). Raises if
    Telegram isn't configured, so callers correctly don't mark things as
    sent."""
    if not telegram_configured():
        print("Telegram not configured -- skipping (no SMS fallback, per instruction).")
        raise RuntimeError("Telegram not configured (SMS fallback disabled per instruction)")
    send_telegram(text)
    print("Delivered via Telegram.")


def send_failure_alert(context, error):
    try:
        deliver(f"[nhc-pipeline error] {context}: {error}", subject="nhc-pipeline error")
    except Exception as e:
        print(f"Could not even send the failure alert: {e}", file=sys.stderr)


# ===========================================================================
# Message 1 -- Fast Headline (bot-built, deterministic, no discussion)
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
        peak_when_clean = fb["peak_when"].replace(" CDT", "").replace(" CST", "")
        lines.append(f"Peak forecast: {fb['peak_mph']} mph ({fb['peak_category']}) at {peak_when_clean}")
    if facts.get("next_advisory"):
        next_advisory_clean = facts["next_advisory"].replace(" CDT", "").replace(" CST", "")
        lines.append("")
        lines.append(f"({next_advisory_clean})")
    return "\n".join(lines)


# ===========================================================================
# Message 2 -- Discussion (only sent when genuinely new, checked separately)
# ===========================================================================
def check_and_send_discussion(state, storm_suffix):
    """Runs every loop iteration, completely independent of whether a new
    TCP advisory was found this cycle. Only sends when BOTH signals agree
    a new discussion has posted: the Discussion Number increased, AND the
    text itself differs from the last one delivered."""
    pending = state.get("pending_discussion")
    if not pending:
        return

    try:
        tcd_text, tcd_source = fetch_product("TCD", storm_suffix)
    except Exception as e:
        print(f"[Discussion check] fetch failed (non-fatal, will retry next loop): {e}")
        return
    if not tcd_text:
        print("[Discussion check] No TCD available yet, will retry next loop.")
        return

    new_number = tcd_discussion_number(tcd_text)
    new_paragraphs = tcd_discussion_text(tcd_text)
    new_text = "\n\n".join(new_paragraphs) if new_paragraphs else None

    last_number = state.get("last_sent_discussion_number")
    last_text = state.get("last_sent_discussion_text")

    number_is_new = new_number is not None and (last_number is None or new_number > last_number)
    text_is_new = new_text is not None and new_text != last_text

    if not (number_is_new and text_is_new):
        print(f"[Discussion check] Not yet new (number_is_new={number_is_new}, text_is_new={text_is_new}) -- still waiting.")
        return

    print(f"[Discussion check] Genuinely new Discussion #{new_number} confirmed by BOTH signals -- sending now.")
    issued = issued_time_from_header(tcd_text)
    key_messages = tcd_key_messages(tcd_text)

    parts = []
    if issued:
        parts.append(f"Issued: {issued}")
        parts.append("")
    parts.append(f"NHC Discussion -- {pending.get('advisory_label', '')}".rstrip())
    parts.append("")
    parts.append(new_text)
    if key_messages:
        parts.append("")
        parts.append("Key Messages:")
        for i, msg in enumerate(key_messages, 1):
            if i > 1:
                parts.append("")
            parts.append(f"{i}. {msg}")

    full_message = "\n".join(parts)
    print(f"Discussion message:\n{full_message}")

    try:
        deliver(full_message, subject=f"NHC Discussion #{new_number}")
        print("Discussion delivered successfully.")
    except Exception as e:
        send_failure_alert("Discussion delivery", str(e))
        return

    state["last_sent_discussion_number"] = new_number
    state["last_sent_discussion_text"] = new_text
    state.pop("pending_discussion", None)
    save_state(state)


# ===========================================================================
# Main
# ===========================================================================
def main():
    state = load_state()

    # --- Auto-discover the active Atlantic storm instead of relying on a
    # hardcoded suffix that has to be manually updated (see
    # fetch_active_storm_bin docstring for why this replaced that). If the
    # tracked storm changed (including going from "some storm" to "none"),
    # reset all storm-specific tracking so nothing bleeds across storms. ---
    storm_bin = fetch_active_storm_bin()
    tracked_bin = state.get("current_storm_bin")
    if storm_bin != tracked_bin:
        if storm_bin:
            print(f"Active storm changed: {tracked_bin!r} -> {storm_bin!r}. Resetting advisory tracking for the new storm.")
        else:
            print(f"Previously tracked storm {tracked_bin!r} is no longer active. Clearing tracking until a new storm forms.")
        for key in ("last", "last_advisory_number", "last_sent_discussion_number",
                    "last_sent_discussion_text", "pending_cone_verification", "pending_discussion"):
            state.pop(key, None)
        state["current_storm_bin"] = storm_bin
        save_state(state)

    if not storm_bin:
        print("No active Atlantic storm currently -- nothing to check this cycle.")
        return

    # --- Backup verification: 15 min after a successful send, resend the
    # cone + full Message 1 + storm surge graphic as a confirmation ---
    pending_cone = state.get("pending_cone_verification")
    if pending_cone and (time.time() - pending_cone["queued_at"]) >= 900:
        try:
            if telegram_configured():
                send_telegram_photo(build_cone_url(storm_bin), caption=f"Cone Graphic -- 15-Min Backup (Advisory #{pending_cone['advisory_num']})")
        except Exception as e:
            print(f"Cone verification resend failed (non-fatal): {e}")
        try:
            if pending_cone.get("full_message"):
                deliver(pending_cone["full_message"], subject=f"15-Min Backup -- Advisory #{pending_cone['advisory_num']}")
        except Exception as e:
            print(f"Full-message verification resend failed (non-fatal): {e}")
        try:
            if telegram_configured():
                send_telegram_photo(build_surge_url(storm_bin), caption=f"Peak Storm Surge Forecast (Advisory #{pending_cone['advisory_num']})")
        except Exception as e:
            print(f"Storm surge resend failed (non-fatal -- may not be active): {e}")
        state.pop("pending_cone_verification", None)
        save_state(state)

    # --- Discussion check runs every iteration, independent of advisories ---
    check_and_send_discussion(state, storm_bin)

    # --- Fetch the Public Advisory and check for a new one ---
    tcp_text, tcp_source = fetch_product("TCP", storm_bin)
    if not tcp_text:
        # Confirmed live 2026-08-10: an IEM outage caused this to fire a
        # fresh Telegram alert every single 25s loop iteration with zero
        # throttling. Confirmed live 2026-08-12: a 30-min throttle still
        # re-alerted repeatedly over one multi-hour outage. Now alerts
        # ONCE per ongoing outage and stays silent until it recovers.
        if not state.get("fetch_failure_alerted"):
            send_failure_alert("Fetching Public Advisory", "Both IEM and NHC failed")
            state["fetch_failure_alerted"] = True
            save_state(state)
        else:
            print("Fetch failed again, but already alerted for this ongoing outage -- staying quiet.")
        sys.exit(1)
    if state.pop("fetch_failure_alerted", None) is not None:
        save_state(state)
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
    # never miss a real change, speed matters more than message frequency.
    issued_time = issued_time_from_header(tcp_text)
    location = tcp_location(tcp_text)
    wind_mph = tcp_wind_mph(tcp_text)
    movement = tcp_movement(tcp_text)
    pressure_mb = tcp_pressure_mb(tcp_text)
    next_advisory = tcp_next_advisory(tcp_text)

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

    # TCD forecast-position table for peak-forecast timing (no discussion
    # text pulled here -- that's handled entirely separately/independently)
    try:
        tcd_text_for_bands, _ = fetch_product("TCD", storm_bin)
        if tcd_text_for_bands:
            positions = tcd_forecast_positions(tcd_text_for_bands)
            bands = summarize_forecast_bands(positions, wind_mph)
            if bands:
                facts["forecast_bands"] = bands
    except Exception as e:
        print(f"Forecast positions unavailable (non-fatal): {e}")

    # TCM for gusts + wind radii
    try:
        tcm_text, tcm_source = fetch_product("TCM", storm_bin)
        if tcm_text:
            print(f"TCM fetched from {tcm_source}")
            tcm_data = tcm_gusts_and_radii(tcm_text)
            if tcm_data.get("gust_kt"):
                facts["gust_mph"] = kt_to_mph(tcm_data["gust_kt"])
            if tcm_data.get("radii_34kt"):
                facts["radii_34kt"] = tcm_data["radii_34kt"]
    except Exception as e:
        print(f"Forecast/Advisory (TCM) unavailable (non-fatal): {e}")

    # --- MESSAGE 1: Fast Headline -- sent immediately, no discussion ---
    fast_message = build_fast_headline(facts)
    print(f"Fast headline:\n{fast_message}")

    if telegram_configured():
        try:
            send_telegram_photo(build_cone_url(storm_bin), caption=f"{facts['name']} -- 5-Day Cone (Advisory #{advisory_num})")
        except Exception as e:
            print(f"Cone graphic send failed (non-fatal): {e}")

    try:
        deliver(fast_message, subject=f"{header['status_and_name']} Adv #{advisory_num}")
    except Exception as e:
        send_failure_alert("Fast headline delivery", str(e))
        sys.exit(1)

    print("Fast headline sent successfully.")

    if telegram_configured():
        state["pending_cone_verification"] = {
            "advisory_num": advisory_num,
            "queued_at": time.time(),
            "full_message": fast_message,
        }

    # Queue the discussion check -- it'll pick this up on this same
    # iteration or any future one, whenever NHC's Discussion actually
    # updates, completely independent of this advisory's own timing.
    state["pending_discussion"] = {"advisory_num": advisory_num, "advisory_label": f"Advisory #{advisory_num}"}

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
