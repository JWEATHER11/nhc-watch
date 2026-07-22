#!/usr/bin/env python3
"""
nhc_recon_pipeline.py -- Separate, independent pipeline for recon VDM
(Vortex Data Message) fixes. Runs on its own schedule (every 10-20 min,
set in the workflow file), fetches from IEM first with NHC fallback, and
delivers to the SAME Telegram chat as nhc_pipeline.py -- but as a fully
separate script/workflow/state file, so a problem in one never affects the
other.

VDMs are single-fix snapshots (pressure, eye, flight-level & surface wind
at one moment), not full advisories -- there's no "advisory number" to
dedupe on, so this dedupes on the fix's own timestamp instead. Recon only
flies when a plane is actually in the storm, so this naturally does
nothing between missions and picks back up when flights resume.

The AI step here writes a SHORT 1-2 sentence read on what this specific
fix tells us (e.g. "still weak & disorganized" vs "steadily deepening"),
not a full forecast -- that's what nhc_pipeline.py already covers.
"""

import json
import os
import re
import smtplib
import sys
import time
import urllib.error
import urllib.request
from email.mime.text import MIMEText
from pathlib import Path

STORM_PIL_SUFFIX = "NT2"  # Atlantic recon PIL suffix -- change if a new storm forms (NT2 -> NT3)

IEM_BASE = "https://mesonet.agron.iastate.edu/cgi-bin/afos/retrieve.py"
NHC_RECON_URL = "https://www.nhc.noaa.gov/text/MIAREPNT2.shtml?text"

STATE_FILE = Path(__file__).parent / "recon_pipeline_state.json"
CENTRAL_UTC_OFFSET = 5

MAX_ATTEMPTS = 3
RETRY_DELAY_SEC = 5


def _http_get(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": "nhc-recon-pipeline/1.0"})
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


def fetch_recon():
    pil = f"REP{STORM_PIL_SUFFIX}"
    iem_url = f"{IEM_BASE}?pil={pil}"
    text = _fetch_with_retries(iem_url, f"IEM:{pil}")
    if text:
        return text, "IEM"
    print(f"[{pil}] IEM failed, falling back to NHC...")
    text = _fetch_with_retries(NHC_RECON_URL, f"NHC:{pil}")
    if text:
        return text, "NHC"
    return None, "FAILED"


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


def extract_fix_time(text):
    m = re.search(r"^\s*A\.\s*(\S+Z)", text, re.I | re.M)
    if not m:
        return None
    zulu = m.group(1)
    return {"zulu": zulu, "local": zulu_to_central(zulu) or zulu}


def extract_central_pressure(text):
    m = re.search(r"^\s*D\.\s*(?:[A-Z]+\s+)*?(\d{3,4})\s*MB", text, re.I | re.M)
    return f"{m.group(1)} mb" if m else None


def extract_eye(text):
    m = re.search(r"^\s*H\.\s*(.+)$", text, re.I | re.M)
    if not m:
        return None
    line = m.group(1).strip()
    if re.match(r"^NA$", line, re.I):
        return "No eye reported"
    status = "Unclear"
    if re.search(r"OPEN", line, re.I):
        status = "Open"
    elif re.search(r"CLOSED|CIRCULAR|CONCENTRIC|RAGGED|ELLIPTICAL", line, re.I):
        status = "Closed"
    dia = re.search(r"(\d+)\s*NM", line, re.I)
    return f"{status} - {dia.group(1)} nm" if dia else status


def extract_center_location(text):
    m = re.search(r"^\s*B\.", text, re.I | re.M)
    if not m:
        return None
    chunk = text[m.start():m.start() + 200]
    m2 = re.search(
        r"(\d{1,3})\s*DEG\s*(\d{1,2})\s*MIN\s*([NS])[^\d]{0,20}(\d{1,3})\s*DEG\s*(\d{1,2})\s*MIN\s*([EW])",
        chunk, re.I | re.S,
    )
    if m2:
        return f"{m2.group(1)}deg{m2.group(2)}'{m2.group(3)}, {m2.group(4)}deg{m2.group(5)}'{m2.group(6)}"
    m3 = re.search(r"(\d{1,3}\.\d+)\s*DEG\s*([NS])[^\d]{0,15}(\d{1,3}\.\d+)\s*DEG\s*([EW])", chunk, re.I | re.S)
    if m3:
        return f"{m3.group(1)}deg{m3.group(2)}, {m3.group(3)}deg{m3.group(4)}"
    return None


def extract_flight_level_wind(text):
    m = re.search(r"MAX\s*FL(?:IGHT)?[- ]?(?:LEVEL)?\s*WIND\s*(\d{2,3})\s*KT", text, re.I)
    if m:
        return int(m.group(1))
    m = re.search(r"^\s*F\.\s*(.+)$", text, re.I | re.M)
    if m and not re.match(r"^NA$", m.group(1).strip(), re.I):
        wm = re.search(r"(\d{2,3})\s*KT", m.group(1), re.I)
        if wm:
            return int(wm.group(1))
    return None


def extract_surface_wind(text):
    m = re.search(r"MAX\s*(?:SFC|SURFACE)\s*WIND[^\n]{0,60}?(\d{2,3})\s*KT", text, re.I)
    if m:
        return int(m.group(1))
    m = re.search(r"SFMR[^\n]{0,60}?(\d{2,3})\s*KT", text, re.I)
    if m:
        return int(m.group(1))
    return None


def extract_aircraft(text):
    m = re.search(r"^\s*U\.\s*(.+)$", text, re.I | re.M)
    return m.group(1).strip() if m else None


def load_state():
    return json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2))


RECON_VOICE_PROMPT = """You write a ONE-sentence, sometimes two-sentence, read on a single hurricane recon aircraft fix for a Gulf Coast / Southeast Texas audience, in the voice of a local broadcast meteorologist.

House style: use "&" not "and", no comma before "&", no Oxford comma, capitalize "Tropical" always, confident & conversational, collective "we".

This is ONE data point from ONE recon pass, not a full forecast -- just give a quick read on what it tells us (e.g. still weak & disorganized, steadily deepening, holding steady, eye trying to form, etc.) based on the pressure/wind/eye status given. Do not invent numbers or claims not in the facts. No hashtags, at most one emoji. Output ONLY the sentence(s), nothing else."""


def call_claude_api(facts_summary):
    api_key = os.environ["ANTHROPIC_API_KEY"]
    payload = json.dumps({
        "model": "claude-sonnet-5",
        "max_tokens": 200,
        "system": RECON_VOICE_PROMPT,
        "messages": [{"role": "user", "content": f"Facts from this recon fix:\n\n{facts_summary}"}],
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


def send_email_sms_fallback(text, subject="Recon Update"):
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


def deliver(text, subject="Recon Update"):
    if telegram_configured():
        send_telegram(text)
        print("Delivered via Telegram.")
    else:
        print("Telegram not configured -- falling back to email-to-SMS.")
        send_email_sms_fallback(text, subject=subject)
        print("Delivered via email-to-SMS fallback.")


def send_failure_alert(context, error):
    try:
        deliver(f"[nhc-recon-pipeline error] {context}: {error}", subject="nhc-recon-pipeline error")
    except Exception as e:
        print(f"Could not even send the failure alert: {e}", file=sys.stderr)


def main():
    text, source = fetch_recon()
    if not text:
        send_failure_alert("Fetching recon VDM", "Both IEM and NHC failed")
        sys.exit(1)
    print(f"Recon fetched from {source}")

    fix_time = extract_fix_time(text)
    if not fix_time:
        print("No VDM fix time found -- likely no plane currently in the storm. Exiting quietly.")
        return

    state = load_state()
    if state.get("last_fix_zulu") == fix_time["zulu"]:
        print(f"No new fix -- still {fix_time['zulu']}. Not sending an update.")
        return

    pressure_mb = extract_central_pressure(text)
    pressure_val = int(re.match(r"(\d+)", pressure_mb).group(1)) if pressure_mb else None
    eye = extract_eye(text)
    location = extract_center_location(text)
    fl_wind_kt = extract_flight_level_wind(text)
    sfc_wind_kt = extract_surface_wind(text)
    aircraft = extract_aircraft(text)

    # --- Running peak/lowest across this mission (resets automatically
    # when a new mission starts, since state clears once VDMs stop coming
    # in and a fresh mission begins with an empty "mission" block) ---
    mission = state.get("mission", {})
    if fl_wind_kt and (mission.get("peak_fl_wind_kt") is None or fl_wind_kt > mission["peak_fl_wind_kt"]):
        mission["peak_fl_wind_kt"] = fl_wind_kt
        mission["peak_fl_wind_when"] = fix_time["local"]
    if sfc_wind_kt and (mission.get("peak_sfc_wind_kt") is None or sfc_wind_kt > mission["peak_sfc_wind_kt"]):
        mission["peak_sfc_wind_kt"] = sfc_wind_kt
        mission["peak_sfc_wind_when"] = fix_time["local"]
    if pressure_val and (mission.get("lowest_pressure_mb") is None or pressure_val < mission["lowest_pressure_mb"]):
        mission["lowest_pressure_mb"] = pressure_val
        mission["lowest_pressure_when"] = fix_time["local"]
    state["mission"] = mission

    facts_lines = [f"Fix time: {fix_time['local']}"]
    if aircraft:
        facts_lines.append(f"Aircraft: {aircraft}")
    if location:
        facts_lines.append(f"Location: {location}")
    if pressure_mb:
        facts_lines.append(f"Central pressure this fix: {pressure_mb}")
    if eye:
        facts_lines.append(f"Eye: {eye}")
    if fl_wind_kt:
        facts_lines.append(f"Flight-level wind this fix: {with_mph(fl_wind_kt)}")
    if sfc_wind_kt:
        facts_lines.append(f"Surface wind (SFMR) this fix: {with_mph(sfc_wind_kt)}")
    if mission.get("peak_fl_wind_kt"):
        facts_lines.append(f"Peak flight-level wind THIS MISSION so far: {with_mph(mission['peak_fl_wind_kt'])} at {mission['peak_fl_wind_when']}")
    if mission.get("peak_sfc_wind_kt"):
        facts_lines.append(f"Peak surface wind THIS MISSION so far: {with_mph(mission['peak_sfc_wind_kt'])} at {mission['peak_sfc_wind_when']}")
    if mission.get("lowest_pressure_mb"):
        facts_lines.append(f"Lowest pressure THIS MISSION so far: {mission['lowest_pressure_mb']} mb at {mission['lowest_pressure_when']}")
    facts_summary = "\n".join(facts_lines)
    print(f"Facts for Claude:\n{facts_summary}")

    try:
        narrative = call_claude_api(facts_summary)
    except Exception as e:
        send_failure_alert("Claude API rewrite step", str(e))
        sys.exit(1)

    header_lines = [f"Recon Fix -- {fix_time['local']}"]
    if aircraft:
        header_lines.append(f"Aircraft: {aircraft}")
    if location:
        header_lines.append(f"Location: {location}")
    if pressure_mb:
        header_lines.append(f"Pressure: {pressure_mb}")
    if eye:
        header_lines.append(f"Eye: {eye}")
    if fl_wind_kt:
        header_lines.append(f"Flight-level wind: {with_mph(fl_wind_kt)}")
    if sfc_wind_kt:
        header_lines.append(f"Surface wind: {with_mph(sfc_wind_kt)}")
    header_lines.append("")
    header_lines.append("-- This mission so far --")
    if mission.get("peak_fl_wind_kt"):
        header_lines.append(f"Peak flight-level wind: {with_mph(mission['peak_fl_wind_kt'])} ({mission['peak_fl_wind_when']})")
    if mission.get("peak_sfc_wind_kt"):
        header_lines.append(f"Peak surface wind: {with_mph(mission['peak_sfc_wind_kt'])} ({mission['peak_sfc_wind_when']})")
    if mission.get("lowest_pressure_mb"):
        header_lines.append(f"Lowest pressure: {mission['lowest_pressure_mb']} mb ({mission['lowest_pressure_when']})")

    full_message = "\n".join(header_lines) + "\n\n" + narrative
    print(f"Full message:\n{full_message}")

    try:
        deliver(full_message, subject=f"Recon Fix {fix_time['local']}")
    except Exception as e:
        send_failure_alert("Delivery", str(e))
        sys.exit(1)

    print("Sent successfully.")
    state["last_fix_zulu"] = fix_time["zulu"]
    save_state(state)


if __name__ == "__main__":
    main()
