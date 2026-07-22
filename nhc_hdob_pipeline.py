#!/usr/bin/env python3
"""
nhc_hdob_pipeline.py -- Tracks High-Density Observations (HDOB) from recon
aircraft. Unlike Vortex Messages (periodic, only when a center fix is made),
HDOB lines come every 30-120 seconds throughout a flight, giving continuous
flight-level wind, SFMR surface wind, and extrapolated surface pressure
readings.

This does NOT alert on every HDOB line (that would be constant spam).
It only alerts when a NEW MISSION RECORD is found -- a stronger flight-level
wind, a stronger SFMR surface wind, or a lower pressure than anything seen
so far this mission. That's the actual signal: "recon just found something
stronger than before."

Field format is the official NHC HD/HA data line spec:
  hhmmss LLLLH NNNNNW PPPP GGGGG XXXX sTTT sddd wwwSSS MMM KKK ppp FF
See: https://www.nhc.noaa.gov/abouthdobs_2007.shtml
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

IEM_BASE = "https://mesonet.agron.iastate.edu/cgi-bin/afos/retrieve.py"
HDOB_PIL = "AHONT1"  # Atlantic HDOB from NHC
NHC_HDOB_URL = "https://www.nhc.noaa.gov/text/URNT15-USAF.shtml?text"  # fallback

STATE_FILE = Path(__file__).parent / "hdob_state.json"
CENTRAL_UTC_OFFSET = 5

MAX_ATTEMPTS = 3
RETRY_DELAY_SEC = 5


def _http_get(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": "nhc-hdob-pipeline/1.0"})
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


def fetch_hdob():
    iem_url = f"{IEM_BASE}?pil={HDOB_PIL}"
    text = _fetch_with_retries(iem_url, f"IEM:{HDOB_PIL}")
    if text:
        return text, "IEM"
    print(f"[{HDOB_PIL}] IEM failed, falling back to NHC...")
    text = _fetch_with_retries(NHC_HDOB_URL, f"NHC:{HDOB_PIL}")
    if text:
        return text, "NHC"
    return None, "FAILED"


def kt_to_mph(kt):
    return round(kt * 1.15078)


def zulu_to_central(day, hhmmss):
    hour, minute = int(hhmmss[:2]), int(hhmmss[2:4])
    hour -= CENTRAL_UTC_OFFSET
    if hour < 0:
        hour += 24
        day -= 1
    period = "PM" if hour >= 12 else "AM"
    hour12 = hour % 12 or 12
    return f"{hour12}:{minute:02d} {period} CDT, day {day}"


# ---------------------------------------------------------------------------
# Parser -- exact fixed-field format from NHC's HDOB spec (see docstring)
# ---------------------------------------------------------------------------
HDOB_LINE_RE = re.compile(
    r"^(\d{6})\s+(\d{2})(\d{2})([NS])\s+(\d{3})(\d{2})([EW])\s+"
    r"(\d{4})\s+(\d{5})\s+(\d{4})\s+"
    r"([+-]\d{3})\s+([+-]\d{3}|///)\s+"
    r"(\d{3})(\d{3})\s+(\d{3})\s+(\d{3})\s+(\d{3}|///)\s+(\d{2})",
    re.M,
)

MISSION_ID_RE = re.compile(r"^([A-Z0-9]+)\s+(\S+)\s+(.+?)\s+HDOB\s+(\d+)\s+(\d{8})", re.M)


def parse_hdob_bulletin(text):
    mission_match = MISSION_ID_RE.search(text)
    aircraft = mission_match.group(1) if mission_match else None
    storm_name = mission_match.group(3).strip() if mission_match else None
    yyyymmdd = mission_match.group(5) if mission_match else None
    day = int(yyyymmdd[6:8]) if yyyymmdd else None

    obs = []
    for m in HDOB_LINE_RE.finditer(text):
        hhmmss = m.group(1)
        static_press_raw = m.group(8)
        extrap_or_dvalue_raw = m.group(10)

        static_press_mb = int(static_press_raw) / 10
        # If aircraft static pressure >= 550.0 mb, XXXX is extrapolated
        # surface pressure (same tenths-mb, leading-1-dropped format).
        # Otherwise it's a D-value in meters, not a pressure -- skip it.
        sfc_pressure_mb = None
        if static_press_mb >= 550.0:
            raw = int(extrap_or_dvalue_raw)
            # Leading "1" gets dropped for pressures >= 1000mb. In active
            # hurricane recon the true value is almost always well under
            # 1000, so this only matters in edge cases.
            sfc_pressure_mb = raw / 10 if raw >= 7000 else (1000 + raw / 10)

        fl_wind_dir = int(m.group(12))
        fl_wind_kt = int(m.group(13))
        peak_fl_wind_kt = int(m.group(14))
        peak_sfmr_kt_raw = m.group(15)
        peak_sfmr_kt = int(peak_sfmr_kt_raw) if peak_sfmr_kt_raw != "///" else None

        obs.append({
            "hhmmss": hhmmss,
            "day": day,
            "local_time": zulu_to_central(day, hhmmss) if day else hhmmss,
            "sfc_pressure_mb": sfc_pressure_mb,
            "peak_fl_wind_kt": peak_fl_wind_kt,
            "peak_sfmr_kt": peak_sfmr_kt,
        })
    return aircraft, storm_name, obs


def load_state():
    return json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2))


HDOB_VOICE_PROMPT = """You write a ONE-sentence alert for a Gulf Coast / Southeast Texas audience when hurricane recon finds a NEW record-strength reading during an active mission -- either the strongest flight-level or surface wind found so far, or the lowest pressure found so far.

House style: use "&" not "and", no Oxford comma, capitalize "Tropical" always, confident & conversational, collective "we".

This is specifically a NEW RECORD for this mission -- lead with that. Do not invent numbers not given. No hashtags, at most one emoji. Output ONLY the sentence, nothing else."""


def call_claude_api(facts_summary):
    api_key = os.environ["ANTHROPIC_API_KEY"]
    payload = json.dumps({
        "model": "claude-sonnet-5",
        "max_tokens": 150,
        "system": HDOB_VOICE_PROMPT,
        "messages": [{"role": "user", "content": f"New record found:\n\n{facts_summary}"}],
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


def send_email_sms_fallback(text, subject="HDOB Update"):
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


def deliver(text, subject="HDOB Update"):
    if telegram_configured():
        send_telegram(text)
        print("Delivered via Telegram.")
    else:
        send_email_sms_fallback(text, subject=subject)
        print("Delivered via email-to-SMS fallback.")


def send_failure_alert(context, error):
    try:
        deliver(f"[nhc-hdob-pipeline error] {context}: {error}", subject="nhc-hdob-pipeline error")
    except Exception as e:
        print(f"Could not even send the failure alert: {e}", file=sys.stderr)


def main():
    text, source = fetch_hdob()
    if not text:
        # Not fatal -- no plane flying is the normal state most of the time.
        print("No HDOB data available (likely no active mission right now). Exiting quietly.")
        return
    print(f"HDOB fetched from {source}")

    aircraft, storm_name, obs = parse_hdob_bulletin(text)
    if not obs:
        print("HDOB product found but no parseable observation lines. Exiting quietly.")
        return

    state = load_state()
    last_seen_time = state.get("last_seen_hhmmss")
    mission = state.get("mission", {})

    new_records = []
    latest_time_seen = last_seen_time

    for ob in obs:
        # Skip observations we've already processed in a prior run.
        if last_seen_time and ob["hhmmss"] <= last_seen_time:
            continue
        latest_time_seen = ob["hhmmss"] if not latest_time_seen or ob["hhmmss"] > latest_time_seen else latest_time_seen

        if ob["peak_fl_wind_kt"] and (mission.get("peak_fl_wind_kt") is None or ob["peak_fl_wind_kt"] > mission["peak_fl_wind_kt"]):
            mission["peak_fl_wind_kt"] = ob["peak_fl_wind_kt"]
            mission["peak_fl_wind_when"] = ob["local_time"]
            new_records.append(f"New peak flight-level wind: {ob['peak_fl_wind_kt']} kt ({kt_to_mph(ob['peak_fl_wind_kt'])} mph) at {ob['local_time']}")

        if ob["peak_sfmr_kt"] and (mission.get("peak_sfmr_kt") is None or ob["peak_sfmr_kt"] > mission["peak_sfmr_kt"]):
            mission["peak_sfmr_kt"] = ob["peak_sfmr_kt"]
            mission["peak_sfmr_when"] = ob["local_time"]
            new_records.append(f"New peak surface (SFMR) wind: {ob['peak_sfmr_kt']} kt ({kt_to_mph(ob['peak_sfmr_kt'])} mph) at {ob['local_time']}")

        if ob["sfc_pressure_mb"] and (mission.get("lowest_pressure_mb") is None or ob["sfc_pressure_mb"] < mission["lowest_pressure_mb"]):
            mission["lowest_pressure_mb"] = ob["sfc_pressure_mb"]
            mission["lowest_pressure_when"] = ob["local_time"]
            new_records.append(f"New lowest pressure: {ob['sfc_pressure_mb']:.1f} mb at {ob['local_time']}")

    state["mission"] = mission
    if latest_time_seen:
        state["last_seen_hhmmss"] = latest_time_seen

    if not new_records:
        print(f"No new mission records in this bulletin ({len(obs)} obs checked). Not sending an update.")
        save_state(state)
        return

    facts_lines = []
    if aircraft:
        facts_lines.append(f"Aircraft: {aircraft}")
    if storm_name:
        facts_lines.append(f"Storm: {storm_name}")
    facts_lines.extend(new_records)
    facts_summary = "\n".join(facts_lines)
    print(f"New records found:\n{facts_summary}")

    try:
        narrative = call_claude_api(facts_summary)
    except Exception as e:
        send_failure_alert("Claude API rewrite step", str(e))
        sys.exit(1)

    header_lines = ["Recon New Record"]
    if aircraft:
        header_lines.append(f"Aircraft: {aircraft}")
    header_lines.extend(new_records)
    full_message = "\n".join(header_lines) + "\n\n" + narrative
    print(f"Full message:\n{full_message}")

    try:
        deliver(full_message, subject="Recon New Record")
    except Exception as e:
        send_failure_alert("Delivery", str(e))
        sys.exit(1)

    print("Sent successfully.")
    save_state(state)


if __name__ == "__main__":
    main()
