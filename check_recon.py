#!/usr/bin/env python3
"""
check_recon.py -- Fetches the latest NHC recon Vortex Data Message (VDM),
decodes it (pressure, eye, winds in kt + mph, fix time in Central), compares
it to the last-seen fix, and texts a summary via email-to-SMS if it's new.

VDMs don't come on a fixed schedule like advisories do -- recon only flies
when a plane is in the storm, so a fix might arrive every 45 min during an
active mission and then nothing for hours between flights. This script dedupes
on the VDM's own fix time (the "A." line) rather than a sequence number, so it
naturally does nothing between missions and picks back up when flights resume.

State is stored separately from the advisory watcher's state.json, in
recon_state.json, so the two scheduled workflows never write to the same file.
"""

import json
import os
import re
import smtplib
import sys
import urllib.request
from email.mime.text import MIMEText
from pathlib import Path

# ---------------------------------------------------------------------------
# CONFIG -- change RECON_URL if this storm dissipates and a new one forms
# with a different Atlantic storm number (NT2 -> NT3, etc.)
# ---------------------------------------------------------------------------
RECON_URL = "https://www.nhc.noaa.gov/text/MIAREPNT2.shtml?text"
STATE_FILE = Path(__file__).parent / "recon_state.json"
CENTRAL_UTC_OFFSET = 5  # CDT (UTC-5). Change to 6 for CST (winter).


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------
def fetch_recon_text(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "nhc-watch/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    return raw


# ---------------------------------------------------------------------------
# Unit / time helpers (same logic as the HTML decoder and check_storm.py)
# ---------------------------------------------------------------------------
def kt_to_mph(kt: float) -> int:
    return round(kt * 1.15078)


def with_mph(kt: float) -> str:
    return f"{kt:g} kt ({kt_to_mph(kt)} mph)"


def parse_zulu(zstr: str):
    m = re.match(r"^(\d{1,2})/(\d{2}):?(\d{2})(?::(\d{2}))?Z$", zstr, re.I)
    if not m:
        return None
    return {"day": int(m.group(1)), "hour": int(m.group(2)), "minute": int(m.group(3))}


def zulu_to_central(zstr: str):
    t = parse_zulu(zstr)
    if not t:
        return None
    day, hour = t["day"], t["hour"] - CENTRAL_UTC_OFFSET
    if hour < 0:
        hour += 24
        day -= 1
    period = "PM" if hour >= 12 else "AM"
    hour12 = hour % 12 or 12
    return f"{hour12}:{t['minute']:02d} {period} CDT, day {day}"


# ---------------------------------------------------------------------------
# Extraction (ported from vdm-decoder.html's VDM tab)
# ---------------------------------------------------------------------------
def extract_fix_time(text: str):
    m = re.search(r"^\s*A\.\s*(\S+Z)", text, re.I | re.M)
    if not m:
        return None
    zulu = m.group(1)
    return {"zulu": zulu, "local": zulu_to_central(zulu) or zulu}


def extract_central_pressure(text: str):
    m = re.search(r"^\s*D\.\s*(?:[A-Z]+\s+)*?(\d{3,4})\s*MB", text, re.I | re.M)
    if m:
        return f"{m.group(1)} mb"
    m = re.search(r"(?:SFC|SURFACE|SLP)[^\n]{0,25}?(\d{3,4})\s*MB", text, re.I)
    return f"{m.group(1)} mb" if m else None


def extract_eye(text: str):
    m = re.search(r"^\s*H\.\s*(.+)$", text, re.I | re.M)
    if not m:
        return None
    line = m.group(1).strip()
    if re.match(r"^NA$", line, re.I):
        return "No eye reported (NA)"
    status = "Unclear"
    if re.search(r"OPEN", line, re.I):
        status = "Open"
    elif re.search(r"CLOSED|CIRCULAR|CONCENTRIC|RAGGED|ELLIPTICAL", line, re.I):
        status = "Closed"
    dia = re.search(r"(\d+)\s*NM", line, re.I)
    return f"{status} - {dia.group(1)} nm" if dia else status


def extract_center_location(text: str):
    b_idx = None
    m = re.search(r"^\s*B\.", text, re.I | re.M)
    if not m:
        return None
    chunk = text[m.start():m.start() + 200]

    m2 = re.search(
        r"(\d{1,3})\s*DEG\s*(\d{1,2})\s*MIN\s*([NS])[^\d]{0,20}(\d{1,3})\s*DEG\s*(\d{1,2})\s*MIN\s*([EW])",
        chunk, re.I | re.S,
    )
    if m2:
        lat = f"{m2.group(1)}deg{m2.group(2)}'{m2.group(3)}"
        lon = f"{m2.group(4)}deg{m2.group(5)}'{m2.group(6)}"
        return f"{lat}, {lon}"

    m3 = re.search(
        r"(\d{1,3}\.\d+)\s*DEG\s*([NS])[^\d]{0,15}(\d{1,3}\.\d+)\s*DEG\s*([EW])",
        chunk, re.I | re.S,
    )
    if m3:
        return f"{m3.group(1)}deg{m3.group(2)}, {m3.group(3)}deg{m3.group(4)}"

    return None


def extract_flight_level_wind(text: str):
    m = re.search(r"MAX\s*FL(?:IGHT)?[- ]?(?:LEVEL)?\s*WIND\s*(\d{2,3})\s*KT", text, re.I)
    if m:
        return with_mph(int(m.group(1)))
    m = re.search(r"^\s*F\.\s*(.+)$", text, re.I | re.M)
    if m and not re.match(r"^NA$", m.group(1).strip(), re.I):
        wm = re.search(r"(\d{2,3})\s*KT", m.group(1), re.I)
        if wm:
            return with_mph(int(wm.group(1)))
    return None


def extract_surface_wind(text: str):
    m = re.search(r"MAX\s*(?:SFC|SURFACE)\s*WIND[^\n]{0,60}?(\d{2,3})\s*KT", text, re.I)
    if m:
        return with_mph(int(m.group(1)))
    m = re.search(r"SFMR[^\n]{0,60}?(\d{2,3})\s*KT", text, re.I)
    if m:
        return with_mph(int(m.group(1)))
    return None


def extract_aircraft(text: str):
    # U. line usually has callsign + mission + storm name, e.g. "AF306 0302A CYCLONE    OB 11"
    m = re.search(r"^\s*U\.\s*(.+)$", text, re.I | re.M)
    return m.group(1).strip() if m else None


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------
def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}


def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ---------------------------------------------------------------------------
# Email-to-SMS
# ---------------------------------------------------------------------------
def send_text(body: str, subject: str = "Recon Update"):
    smtp_server = os.environ["SMTP_SERVER"]
    smtp_port = int(os.environ.get("SMTP_PORT", "587"))
    smtp_user = os.environ["SMTP_USER"]
    smtp_pass = os.environ["SMTP_PASS"]
    to_addr = os.environ["ALERT_TO"]

    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = smtp_user
    msg["To"] = to_addr

    with smtplib.SMTP(smtp_server, smtp_port) as server:
        server.starttls()
        server.login(smtp_user, smtp_pass)
        server.sendmail(smtp_user, [to_addr], msg.as_string())


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    text = fetch_recon_text(RECON_URL)

    fix_time = extract_fix_time(text)
    if not fix_time:
        print("No VDM fix time found on the page - likely no plane currently in the storm. Exiting quietly.")
        return

    state = load_state()

    if state.get("last_fix_zulu") == fix_time["zulu"]:
        print(f"No new fix - still {fix_time['zulu']}. Not sending an alert.")
        return

    pressure = extract_central_pressure(text)
    eye = extract_eye(text)
    location = extract_center_location(text)
    fl_wind = extract_flight_level_wind(text)
    sfc_wind = extract_surface_wind(text)
    aircraft = extract_aircraft(text)

    lines = [f"Recon fix @ {fix_time['local']}"]
    if aircraft:
        lines.append(f"Aircraft: {aircraft}")
    if location:
        lines.append(f"Loc: {location}")
    if pressure:
        lines.append(f"Pressure: {pressure}")
    if eye:
        lines.append(f"Eye: {eye}")
    if fl_wind:
        lines.append(f"FL wind: {fl_wind}")
    if sfc_wind:
        lines.append(f"Sfc wind: {sfc_wind}")

    body = "\n".join(lines)
    print("Sending alert:\n" + body)

    try:
        send_text(body, subject="Recon Fix Update")
    except Exception as e:
        print(f"Failed to send text: {e}", file=sys.stderr)
        sys.exit(1)

    state["last_fix_zulu"] = fix_time["zulu"]
    save_state(state)


if __name__ == "__main__":
    main()
