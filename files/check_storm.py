#!/usr/bin/env python3
"""
check_storm.py — Fetches TWO NHC products for the storm:

  1. The Public/Intermediate Advisory (TCP) — used to detect "something new
     posted" and to compare against the last advisory (distance/direction/
     speed moved, NHC's own "CHANGES WITH THIS ADVISORY" section, status/
     wind/pressure deltas).
  2. The Forecast/Advisory (TCM) — used for the full field-by-field technical
     breakdown, including the multi-day forecast track table.

...then builds ONE text message with three parts, in this order:
  A) A short templated "in your voice" blurb — what changed, where it's
     headed, kept tight
  B) The distance/direction/speed-since-last-advisory comparison
  C) The full technical breakdown (every field, plus the forecast track)

This message will typically run 1,000+ characters. SMS gateways often
truncate or split long messages — that's a known, accepted tradeoff here
(the alternative was splitting this across text + email, which was decided
against in favor of one message).

State (last TCP advisory number + position/status/wind/pressure/time) lives
in state.json and is committed back by the GitHub Actions workflow.
"""

import json
import math
import os
import re
import smtplib
import sys
import urllib.request
from datetime import datetime
from email.mime.text import MIMEText
from pathlib import Path

# ---------------------------------------------------------------------------
# CONFIG — change these if this storm dissipates and a new one forms with a
# different Atlantic storm number (AT2 -> AT3, etc.)
# ---------------------------------------------------------------------------
PUBLIC_ADVISORY_URL = "https://www.nhc.noaa.gov/text/MIATCPAT2.shtml?text"
FORECAST_ADVISORY_URL = "https://www.nhc.noaa.gov/text/MIATCMAT2.shtml?text"
DISCUSSION_URL = "https://www.nhc.noaa.gov/text/MIATCDAT2.shtml?text"

STATE_FILE = Path(__file__).parent / "state.json"
CENTRAL_UTC_OFFSET = 5  # CDT (UTC-5). Change to 6 for CST (winter).

MONTHS = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------
def fetch(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "nhc-watch/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# Shared unit / time helpers
# ---------------------------------------------------------------------------
def kt_to_mph(kt: float) -> int:
    return round(kt * 1.15078)


def with_mph(kt: float) -> str:
    return f"{kt:g} kt ({kt_to_mph(kt)} mph)"


def zulu_to_central(zstr: str):
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
# TCP — Public/Intermediate Advisory (drives dedupe + comparison)
# ===========================================================================
def tcp_header(text: str):
    m = re.search(r"^(.+?)\s+(Intermediate\s+)?Advisory Number\s+(\S+)", text, re.I | re.M)
    if not m:
        return None
    return {
        "status_and_name": m.group(1).strip(),
        "intermediate": bool(m.group(2)),
        "number": m.group(3).rstrip("."),
    }


def tcp_issue_datetime(text: str):
    m = re.search(
        r"(\d{3,4})\s*(AM|PM)\s+[A-Z]{2,5}\s+\w{3}\s+(\w{3})\s+(\d{1,2})\s+(\d{4})",
        text, re.I,
    )
    if not m:
        return None
    digits, ampm, mon, day, year = m.groups()
    minute = int(digits[-2:])
    hour = int(digits[:-2]) if len(digits) > 2 else int(digits)
    if ampm.upper() == "PM" and hour != 12:
        hour += 12
    if ampm.upper() == "AM" and hour == 12:
        hour = 0
    month = MONTHS.get(mon.upper()[:3])
    if not month:
        return None
    try:
        return datetime(int(year), month, int(day), hour, minute)
    except ValueError:
        return None


def tcp_location(text: str):
    m = re.search(r"LOCATION\.\.\.(\d{1,3}\.?\d*)N\s+(\d{1,3}\.?\d*)W", text, re.I)
    return {"lat": float(m.group(1)), "lon": -float(m.group(2))} if m else None


def tcp_wind_mph(text: str):
    m = re.search(r"MAXIMUM SUSTAINED WINDS\.\.\.(\d{1,3})\s*MPH", text, re.I)
    return int(m.group(1)) if m else None


def tcp_movement(text: str):
    m = re.search(
        r"PRESENT MOVEMENT\.\.\.([A-Z]+)\s+OR\s+(\d{1,3})\s*DEGREES AT\s+(\d{1,3})\s*MPH",
        text, re.I,
    )
    if m:
        return {"compass": m.group(1).upper(), "degrees": int(m.group(2)), "mph": int(m.group(3))}
    if re.search(r"PRESENT MOVEMENT\.\.\.STATIONARY", text, re.I):
        return {"compass": None, "degrees": None, "mph": 0, "stationary": True}
    return None


def tcp_pressure_mb(text: str):
    m = re.search(r"MINIMUM CENTRAL PRESSURE\.\.\.(\d{3,4})\s*MB", text, re.I)
    return int(m.group(1)) if m else None


def tcp_changes(text: str):
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


def tcp_next_advisory(text: str):
    m = re.search(r"Next\s+(?:complete\s+)?advisory\s+at\s+[^\n.]+", text, re.I)
    return m.group(0).strip() if m else None


# ===========================================================================
# TCM — Forecast/Advisory (drives the full field breakdown)
# ===========================================================================
def tcm_header(text: str):
    m = re.search(r"^\s*(.+?)\s+FORECAST/ADVISORY NUMBER\s+(\S+)", text, re.I | re.M)
    if not m:
        return None
    name, num = m.group(1).strip(), m.group(2).rstrip(".")
    basin = re.search(r"\b(A[LEP]\d{6}|C[PE]\d{6})\b", text)
    return {"name": name, "number": num, "basin": basin.group(1) if basin else None}


def tcm_location(text: str):
    # TCM format: "TROPICAL DEPRESSION CENTER LOCATED NEAR 27.5N 85.0W AT 19/2100Z"
    # (no lettered fields in this product — that's the VDM format, different product)
    m = re.search(
        r"LOCATED\s+NEAR\s+(\d{1,3}\.?\d*)N\s+(\d{1,3}\.?\d*)W\s+AT\s+(\d{1,2}/\d{4}Z)",
        text, re.I,
    )
    if not m:
        return None
    return f"{m.group(1)}degN, {m.group(2)}degW"


def tcm_fix_time(text: str):
    m = re.search(r"^\s*A\.\s*(\S+Z)", text, re.I | re.M)
    if not m:
        return None
    return {"zulu": m.group(1), "local": zulu_to_central(m.group(1)) or m.group(1)}


def tcm_center_time(text: str):
    m = re.search(
        r"LOCATED\s+NEAR\s+\d{1,3}\.?\d*N\s+\d{1,3}\.?\d*W\s+AT\s+(\d{1,2}/\d{4}Z)",
        text, re.I,
    )
    if m:
        return {"zulu": m.group(1), "local": zulu_to_central(m.group(1)) or m.group(1)}
    return None


def tcm_movement(text: str):
    m = re.search(
        r"PRESENT MOVEMENT TOWARD THE\s+([A-Z-]+)\s+OR\s+(\d{1,3})\s*DEGREES AT\s+(\d{1,3})\s*KT",
        text, re.I,
    )
    if m:
        direction = m.group(1).capitalize()
        return f"{direction} ({m.group(2)}deg) at {with_mph(int(m.group(3)))}"
    if re.search(r"PRESENT MOVEMENT[^\n]*STATIONARY", text, re.I):
        return "Stationary"
    return None


def tcm_pressure(text: str):
    m = re.search(r"(?:ESTIMATED\s+)?MINIMUM CENTRAL PRESSURE\s+(\d{3,4})\s*MB", text, re.I)
    return f"{m.group(1)} mb" if m else None


def tcm_sustained_wind(text: str):
    m = re.search(
        r"MAX(?:IMUM)?\s*SUSTAINED\s*WINDS?\s+(\d{1,3})\s*KT\s*WITH\s*GUSTS\s*TO\s+(\d{1,3})\s*KT",
        text, re.I,
    )
    if m:
        return f"{with_mph(int(m.group(1)))} sustained, gusts {with_mph(int(m.group(2)))}"
    return None


def tcm_position_accuracy(text: str):
    m = re.search(r"POSITION ACCURATE WITHIN\s+(\d{1,3})\s*NM", text, re.I)
    return f"+/-{m.group(1)} nm" if m else None


def tcm_next_advisory(text: str):
    m = re.search(r"NEXT (?:COMPLETE )?ADVISORY AT\s+(\d{1,2}/\d{4}Z)", text, re.I)
    return (zulu_to_central(m.group(1)) or m.group(1)) if m else None


def tcm_forecast_track(text: str):
    pattern = re.compile(
        r"(FORECAST|OUTLOOK)\s+VALID\s+(\d{1,2}/\d{4}Z)\s+(\d{1,3}\.?\d*)N\s+(\d{1,3}\.?\d*)W([^\n]*)\n\s*MAX WIND\s+(\d{1,3})\s*KT\.\.\.GUSTS\s+(\d{1,3})\s*KT",
        re.I,
    )
    points = []
    for m in pattern.finditer(text):
        points.append({
            "kind": "Forecast" if m.group(1).upper() == "FORECAST" else "Outlook",
            "local": zulu_to_central(m.group(2)) or m.group(2),
            "lat": m.group(3), "lon": m.group(4),
            "note": (m.group(5) or "").replace("...", "").strip(),
            "max_wind": int(m.group(6)), "gusts": int(m.group(7)),
        })
    return points


def tcm_peak_wind(points):
    if not points:
        return None
    peak = max(points, key=lambda p: p["max_wind"])
    return f"{with_mph(peak['max_wind'])} sustained, gusts {with_mph(peak['gusts'])} (at {peak['local']})"


# ===========================================================================
# TCD — Discussion product's "FORECAST POSITIONS AND MAX WINDS" table
# Cleaner than the TCM track: already has both kt and mph, and uses simple
# H-offset labels (INIT, 12H, 24H...) instead of full Zulu valid times.
# ===========================================================================
def tcd_forecast_positions(text: str):
    pattern = re.compile(
        r"^\s*(INIT|\d{1,3}H)\s+(\d{1,2}/\d{4}Z)\s+(\d{1,3}\.?\d*)N\s+(\d{1,3}\.?\d*)W\s+"
        r"(\d{1,3})\s*KT\s+(\d{1,3})\s*MPH(?:\.\.\.([A-Z /-]+))?\s*$",
        re.I | re.M,
    )
    rows = []
    for m in pattern.finditer(text):
        zulu = m.group(2)
        rows.append({
            "label": m.group(1).upper(),
            "zulu": zulu,
            "local": zulu_to_central(zulu) or zulu,
            "lat": m.group(3), "lon": m.group(4),
            "kt": int(m.group(5)), "mph": int(m.group(6)),
            "note": (m.group(7) or "").strip(),
        })
    return rows


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
# "In your voice" blurb — templated, short & sweet, house style rules
# ===========================================================================
def build_voice_blurb(name, movement, dist_mi, compass, wind_mph, pressure_mb,
                       status_change, changes):
    parts = []

    if movement and not movement.get("stationary"):
        move_bit = f"continues moving {movement['compass']} at {movement['mph']} mph"
    else:
        move_bit = "remains nearly stationary"

    opener = f"{name} {move_bit}"
    if dist_mi is not None and compass:
        opener += f" & has shifted about {dist_mi:.0f} miles {compass} since the last update"
    opener += "."
    parts.append(opener)

    stat_bits = []
    if wind_mph is not None:
        stat_bits.append(f"winds at {wind_mph} mph")
    if pressure_mb is not None:
        stat_bits.append(f"pressure at {pressure_mb} mb")
    if stat_bits:
        parts.append("Current " + " & ".join(stat_bits) + ".")

    if status_change:
        parts.append(status_change)

    if changes:
        parts.append(f"NHC update: {changes}")
    else:
        parts.append("No new watches or warnings issued with this update.")

    parts.append("We'll keep tracking closely! \U0001F300")
    return " ".join(parts)


# ===========================================================================
# State
# ===========================================================================
def load_state():
    return json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}


def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ===========================================================================
# Email-to-SMS
# ===========================================================================
def send_text(body: str, subject: str = "NHC Update"):
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


# ===========================================================================
# Main
# ===========================================================================
def main():
    tcp_text = fetch(PUBLIC_ADVISORY_URL)

    header = tcp_header(tcp_text)
    if not header:
        print("Could not find a Public Advisory header — storm may be gone. Exiting quietly.")
        return

    advisory_num = header["number"]
    state = load_state()

    if state.get("last_advisory_number") == advisory_num:
        print(f"No change — still advisory #{advisory_num}. Not sending an alert.")
        return

    # --- Parse TCP (comparison data) ---
    location = tcp_location(tcp_text)
    wind_mph = tcp_wind_mph(tcp_text)
    movement = tcp_movement(tcp_text)
    pressure_mb = tcp_pressure_mb(tcp_text)
    changes = tcp_changes(tcp_text)
    next_adv_tcp = tcp_next_advisory(tcp_text)
    issue_dt = tcp_issue_datetime(tcp_text)

    dist_mi = None
    compass = None
    status_change_text = None
    comparison_lines = []

    prev = state.get("last")
    if prev and location:
        dist_nm = haversine_nm(prev["lat"], prev["lon"], location["lat"], location["lon"])
        dist_mi = dist_nm * 1.15078
        bearing = initial_bearing(prev["lat"], prev["lon"], location["lat"], location["lon"])
        compass = bearing_to_compass(bearing)
        move_line = f"Moved: {dist_mi:.0f} mi {compass} since advisory #{prev.get('number', '?')}"

        if prev.get("issue_dt") and issue_dt:
            try:
                prev_dt = datetime.fromisoformat(prev["issue_dt"])
                elapsed_hr = (issue_dt - prev_dt).total_seconds() / 3600
                if elapsed_hr > 0:
                    move_line += f" ({elapsed_hr:.1f} hr, ~{dist_mi/elapsed_hr:.0f} mph implied)"
            except ValueError:
                pass
        comparison_lines.append(move_line)

        if prev.get("status_and_name") and prev["status_and_name"] != header["status_and_name"]:
            status_change_text = f"{prev['status_and_name']} has been upgraded/updated to {header['status_and_name']}."
            comparison_lines.append(f"STATUS CHANGE: {prev['status_and_name']} -> {header['status_and_name']}")

        if prev.get("wind_mph") is not None and wind_mph is not None and prev["wind_mph"] != wind_mph:
            d = wind_mph - prev["wind_mph"]
            comparison_lines.append(f"Wind change: {'+' if d > 0 else ''}{d} mph vs last advisory")

        if prev.get("pressure_mb") is not None and pressure_mb is not None and prev["pressure_mb"] != pressure_mb:
            d = pressure_mb - prev["pressure_mb"]
            comparison_lines.append(f"Pressure change: {'+' if d > 0 else ''}{d} mb vs last advisory")

    if changes:
        comparison_lines.append(f"NHC CHANGES: {changes}")
    else:
        comparison_lines.append("NHC changes: None noted this advisory")

    # --- Voice blurb ---
    voice = build_voice_blurb(
        header["status_and_name"], movement, dist_mi, compass,
        wind_mph, pressure_mb, status_change_text, changes,
    )

    # --- Parse TCM (full technical breakdown) ---
    tcm_lines = []
    try:
        tcm_text = fetch(FORECAST_ADVISORY_URL)
        h = tcm_header(tcm_text)
        if h:
            tcm_lines.append(f"Storm/Adv#: {h['name']} #{h['number']} - {h['basin'] or ''}")
        loc = tcm_location(tcm_text)
        ctime = tcm_center_time(tcm_text)
        if loc:
            tcm_lines.append(f"Loc: {loc}" + (f" @ {ctime['local']}" if ctime else ""))
        mv = tcm_movement(tcm_text)
        if mv:
            tcm_lines.append(f"Movement: {mv}")
        pr = tcm_pressure(tcm_text)
        if pr:
            tcm_lines.append(f"Min pressure: {pr}")
        sw = tcm_sustained_wind(tcm_text)
        if sw:
            tcm_lines.append(f"Sustained wind: {sw}")
        track = tcm_forecast_track(tcm_text)
        peak = tcm_peak_wind(track)
        if peak:
            tcm_lines.append(f"Peak forecast wind: {peak}")
        acc = tcm_position_accuracy(tcm_text)
        if acc:
            tcm_lines.append(f"Position accuracy: {acc}")
        nxt = tcm_next_advisory(tcm_text)
        if nxt:
            tcm_lines.append(f"Next advisory: {nxt}")
        if track:
            tcm_lines.append(f"Forecast track ({len(track)} pts):")
            for p in track:
                note = f" -{p['note']}" if p["note"] else ""
                tcm_lines.append(
                    f"  {p['local']}: {p['lat']}N {p['lon']}W{note} "
                    f"{with_mph(p['max_wind'])}/gust {with_mph(p['gusts'])}"
                )
    except Exception as e:
        tcm_lines.append(f"(Forecast/Advisory detail unavailable: {e})")

    # --- Parse TCD (Discussion product's forecast positions table) ---
    tcd_lines = []
    try:
        tcd_text = fetch(DISCUSSION_URL)
        positions = tcd_forecast_positions(tcd_text)
        if positions:
            for p in positions:
                note = f" -{p['note']}" if p["note"] else ""
                tcd_lines.append(
                    f"  {p['label']:>4} {p['local']}: {p['lat']}N {p['lon']}W{note} "
                    f"{p['kt']} kt/{p['mph']} mph"
                )
    except Exception as e:
        tcd_lines.append(f"(Forecast positions unavailable: {e})")

    # --- Assemble the one big message ---
    body_parts = [
        voice,
        "",
        "--- Comparison ---",
        *comparison_lines,
        "",
        "--- Full Breakdown ---",
        *tcm_lines,
    ]
    if tcd_lines:
        body_parts += ["", "--- Forecast Positions & Max Winds ---", *tcd_lines]
    if next_adv_tcp:
        body_parts.append(next_adv_tcp)

    body = "\n".join(body_parts)
    print(f"Sending alert ({len(body)} chars):\n" + body)

    try:
        send_text(body, subject=f"{header['status_and_name']} Adv #{advisory_num}")
    except Exception as e:
        print(f"Failed to send text: {e}", file=sys.stderr)
        sys.exit(1)

    new_last = {"number": advisory_num, "status_and_name": header["status_and_name"]}
    if location:
        new_last["lat"], new_last["lon"] = location["lat"], location["lon"]
    if wind_mph is not None:
        new_last["wind_mph"] = wind_mph
    if pressure_mb is not None:
        new_last["pressure_mb"] = pressure_mb
    if issue_dt:
        new_last["issue_dt"] = issue_dt.isoformat()

    state["last_advisory_number"] = advisory_num
    state["last"] = new_last
    save_state(state)


if __name__ == "__main__":
    main()
