#!/usr/bin/env python3
"""
wxmodel_pipeline.py -- Tropical cyclone model guidance monitor for the
Atlantic, Caribbean, and Gulf of Mexico. Uses only free public NHC
sources: CurrentStorms.json (to find active Atlantic systems) and the
ATCF a-deck files (raw model guidance) at ftp.nhc.noaa.gov/atcf/aid_public/.

For each active Atlantic system, pulls the latest cycle for GFS (AVNO),
ECMWF (EMX), HWRF, and HAFS (HAFA/HAFB), finds each model's peak wind
and the pressure at that peak, and reports which specific run produced
it. All times converted to Beaumont, TX (America/Chicago); all winds
shown in mph first with knots in parentheses, per instruction.

Sent to the WX Model Data Telegram chat. Same reliable pattern as
everything else: cache-busted fetch, dedup by (storm, model, cycle),
Telegram only, no AI (this is a straight relay/summary of NHC's own
numbers).
"""

import gzip
import io
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

CURRENT_STORMS_URL = "https://www.nhc.noaa.gov/CurrentStorms.json"
ATCF_BASE = "https://ftp.nhc.noaa.gov/atcf/aid_public"
STATE_FILE = "wxmodel_state.json"
MAX_ATTEMPTS = 2  # reduced from 3 -- per instruction, speed is critical
RETRY_DELAY_SEC = 2  # reduced from 5
BEAUMONT_TZ = ZoneInfo("America/Chicago")

# TECH codes we care about, with display names.
MODELS_OF_INTEREST = {
    "AVNO": "GFS",
    "AVNI": "GFS (interpolated)",
    "EMX": "ECMWF",
    "EMXI": "ECMWF (interpolated)",
    "HWRF": "HWRF",
    "HAFA": "HAFS-A",
    "HAFB": "HAFS-B",
}


def _http_get_bytes(url, timeout=10):  # reduced from 20 -- per instruction, speed is critical
    req = urllib.request.Request(url, headers={"User-Agent": "wxmodel-pipeline/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _fetch_with_retries_bytes(url, label):
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            data = _http_get_bytes(url)
            if data:
                return data
            print(f"[{label}] Attempt {attempt}: empty response from {url}")
        except Exception as e:
            print(f"[{label}] Attempt {attempt} failed ({url}): {e}")
        if attempt < MAX_ATTEMPTS:
            time.sleep(RETRY_DELAY_SEC)
    return None


def fetch_active_atlantic_storms():
    cache_buster = int(time.time())
    url = f"{CURRENT_STORMS_URL}?_cb={cache_buster}"
    data = _fetch_with_retries_bytes(url, "CurrentStorms.json")
    if not data:
        return []
    parsed = json.loads(data.decode("utf-8"))
    storms = parsed.get("activeStorms", [])
    # Atlantic basin only, per instruction -- ids look like "al022026".
    return [s for s in storms if s.get("id", "").lower().startswith("al")]


def fetch_adeck(storm_id):
    """Tries the gzipped file first (the normal case), falls back to
    plain .dat if the gzipped version isn't found."""
    cache_buster = int(time.time())
    gz_url = f"{ATCF_BASE}/a{storm_id.lower()}.dat.gz?_cb={cache_buster}"
    data = _fetch_with_retries_bytes(gz_url, f"ATCF:{storm_id}.gz")
    if data:
        try:
            return gzip.decompress(data).decode("utf-8", errors="replace")
        except OSError:
            pass  # not actually gzipped, fall through to treat as plain text
    plain_url = f"{ATCF_BASE}/a{storm_id.lower()}.dat?_cb={cache_buster}"
    data = _fetch_with_retries_bytes(plain_url, f"ATCF:{storm_id}")
    if data:
        return data.decode("utf-8", errors="replace")
    return None


def parse_adeck(text):
    """Parses ATCF a-deck comma-delimited rows into a list of dicts with
    the fields we care about: tech, cycle (YYYYMMDDHH), tau (forecast
    hour), vmax (knots), mslp (mb)."""
    rows = []
    for line in text.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 10:
            continue
        try:
            tech = parts[4]
            cycle = parts[2]
            tau = int(parts[5])
            vmax = int(parts[8]) if parts[8] else None
            mslp = int(parts[9]) if parts[9] and parts[9] != "0" else None
        except (ValueError, IndexError):
            continue
        if tech not in MODELS_OF_INTEREST:
            continue
        rows.append({"tech": tech, "cycle": cycle, "tau": tau, "vmax": vmax, "mslp": mslp})
    return rows


def to_beaumont_str(cycle_str):
    """'2026072612' -> Beaumont-local time string."""
    try:
        dt_utc = datetime.strptime(cycle_str, "%Y%m%d%H").replace(tzinfo=timezone.utc)
        dt_local = dt_utc.astimezone(BEAUMONT_TZ)
        return dt_local.strftime("%b %-d %I:%M%p %Z").replace(" 0", " ")
    except ValueError:
        return cycle_str


def knots_to_mph(knots):
    return round(knots * 1.15078)


def summarize_model_guidance(rows):
    """For each model of interest, finds the latest cycle, then within
    that cycle finds the peak VMAX and the MSLP/TAU at that peak."""
    by_tech = {}
    for r in rows:
        by_tech.setdefault(r["tech"], []).append(r)

    summaries = {}
    for tech, tech_rows in by_tech.items():
        latest_cycle = max(r["cycle"] for r in tech_rows)
        cycle_rows = [r for r in tech_rows if r["cycle"] == latest_cycle and r["vmax"] is not None]
        if not cycle_rows:
            continue
        peak = max(cycle_rows, key=lambda r: r["vmax"])
        summaries[tech] = {
            "cycle": latest_cycle,
            "vmax_kt": peak["vmax"],
            "mslp": peak["mslp"],
            "tau": peak["tau"],
        }
    return summaries


def classify_intensity_trend(prev_vmax_kt, current_vmax_kt):
    """Compares consecutive-cycle peak winds for the same model --
    NHC's own rapid intensification threshold is a 30 kt increase in 24
    hours, so that's used here as the trigger for calling it out
    explicitly, per instruction."""
    if prev_vmax_kt is None or current_vmax_kt is None:
        return None
    delta = current_vmax_kt - prev_vmax_kt
    if delta >= 30:
        return "RAPID INTENSIFICATION"
    if delta >= 10:
        return "strengthening"
    if delta <= -10:
        return "weakening"
    return "steady"


def build_storm_report(storm, summaries, previous_summaries=None):
    name = storm.get("name", "Unknown")
    classification = storm.get("classification", "")
    storm_id = storm.get("id", "").upper()
    lines = [f"{classification} {name} ({storm_id})", ""]

    if not summaries:
        lines.append("No model guidance available yet for this system.")
        return "\n".join(lines)

    previous_summaries = previous_summaries or {}

    # Prefer a stable, readable order.
    order = ["AVNO", "AVNI", "EMX", "EMXI", "HWRF", "HAFA", "HAFB"]
    for tech in order:
        if tech not in summaries:
            continue
        s = summaries[tech]
        mph = knots_to_mph(s["vmax_kt"])
        beaumont_time = to_beaumont_str(s["cycle"])
        pressure_str = f", {s['mslp']} mb" if s["mslp"] else ""
        model_name = MODELS_OF_INTEREST[tech]
        prev = previous_summaries.get(tech)
        trend = classify_intensity_trend(prev["vmax_kt"], s["vmax_kt"]) if prev else None
        trend_str = f" -- {trend}" if trend else ""
        lines.append(
            f"{model_name}: {mph} mph ({s['vmax_kt']} kt){pressure_str} "
            f"at hour {s['tau']} -- from {beaumont_time} run{trend_str}"
        )
    return "\n".join(lines)


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def telegram_configured():
    return bool(os.environ.get("WXMODEL_TELEGRAM_BOT_TOKEN")) and bool(os.environ.get("WXMODEL_TELEGRAM_CHAT_ID"))


def _telegram_chat_ids():
    """Every chat this bot delivers to -- the original chat, plus any
    additional destinations configured via WXMODEL_TELEGRAM_CHAT_ID_2,
    _3, etc. Lets the same feed reach more than one chat (e.g. a
    meteorologist team group) without touching the original one."""
    ids = [os.environ["WXMODEL_TELEGRAM_CHAT_ID"]]
    i = 2
    while True:
        extra = os.environ.get(f"WXMODEL_TELEGRAM_CHAT_ID_{i}")
        if not extra:
            break
        ids.append(extra)
        i += 1
    return ids


def send_telegram(text):
    bot_token = os.environ["WXMODEL_TELEGRAM_BOT_TOKEN"]
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    max_len = 4000
    # Split on line boundaries, never mid-character -- a raw character
    # split could land inside an HTML tag like "<b>" (splitting a long
    # active-storm message right in half of a tag), which Telegram
    # would then reject entirely with an HTML parse error, on exactly
    # the kind of long, critical message where that matters most.
    # Every <b>...</b> in this codebase stays within a single line, so
    # splitting only between lines is guaranteed safe.
    chunks = []
    current = ""
    for line in text.split("\n"):
        candidate = current + ("\n" if current else "") + line
        if len(candidate) > max_len and current:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)
    chunks = chunks or [text]
    chat_ids = _telegram_chat_ids()
    chat_errors = {}
    for chat_id in chat_ids:
        try:
            for idx, chunk in enumerate(chunks, 1):
                payload = json.dumps({"chat_id": chat_id, "text": chunk, "parse_mode": "HTML"}).encode("utf-8")
                req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"}, method="POST")
                last_err = None
                for attempt in range(1, MAX_ATTEMPTS + 1):
                    try:
                        with urllib.request.urlopen(req, timeout=20) as resp:
                            result = json.loads(resp.read().decode("utf-8"))
                            if result.get("ok"):
                                break
                            last_err = result.get("description", "Unknown Telegram error")
                    except Exception as e:
                        last_err = str(e)
                    if attempt < MAX_ATTEMPTS:
                        time.sleep(RETRY_DELAY_SEC)
                else:
                    raise RuntimeError(f"chunk {idx}: {last_err}")
        except Exception as e:
            chat_errors[chat_id] = str(e)
            print(f"Telegram send to {chat_id} failed: {e}")

    if len(chat_errors) == len(chat_ids):
        raise RuntimeError(f"Telegram send failed to ALL configured chats: {chat_errors}")


def deliver(text):
    if not telegram_configured():
        print("Telegram not configured -- skipping (no SMS fallback).")
        raise RuntimeError("Telegram not configured for WX Model chat")
    send_telegram(text)


def send_failure_alert(context, error):
    try:
        deliver(f"[wxmodel-pipeline error] {context}: {error}")
    except Exception as e:
        print(f"Could not even send the failure alert: {e}", file=sys.stderr)


def process_storm(storm, state):
    storm_id = storm.get("id", "")
    adeck_text = fetch_adeck(storm_id)
    if not adeck_text:
        print(f"[{storm_id}] Could not fetch a-deck (non-fatal, will retry next cycle).")
        return

    rows = parse_adeck(adeck_text)
    summaries = summarize_model_guidance(rows)

    # Dedup key: latest cycle seen per model, joined together -- any
    # change in any model's latest cycle counts as new information.
    fingerprint = "|".join(f"{tech}:{s['cycle']}" for tech, s in sorted(summaries.items()))
    last_fingerprint = state.get(storm_id)
    if fingerprint == last_fingerprint:
        print(f"[{storm_id}] No new model cycles -- not resending.")
        return

    print(f"[{storm_id}] New model guidance detected -- sending.")
    previous_summaries = state.get(f"{storm_id}_summaries")
    message = build_storm_report(storm, summaries, previous_summaries=previous_summaries)

    try:
        deliver(message)
    except Exception as e:
        send_failure_alert(f"{storm_id} delivery", str(e))
        return

    print(f"[{storm_id}] Sent successfully.")
    state[storm_id] = fingerprint
    state[f"{storm_id}_summaries"] = summaries
    save_state(state)


def process_quiet_basin(state):
    """Only announces 'quiet' once per day, so it doesn't spam -- keyed
    off the date so a genuinely new day gets a fresh quiet notice."""
    today_key = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if state.get("quiet_announced_date") == today_key:
        print("Basin quiet -- already announced today, not resending.")
        return

    message = (
        "Tropical Atlantic, Caribbean, and Gulf of Mexico -- quiet.\n\n"
        "No active systems at this time."
    )
    try:
        deliver(message)
    except Exception as e:
        send_failure_alert("quiet-basin delivery", str(e))
        return

    print("Quiet-basin notice sent.")
    state["quiet_announced_date"] = today_key
    save_state(state)


# Tropical Atlantic/Caribbean/Gulf grid for point-sampling deterministic
# model fields via Open-Meteo (free, no API key, confirmed working JSON
# API for both GFS and ECMWF -- no GRIB2 decoding needed). This trades
# perfect field-scanning precision for something reliable and simple:
# sample enough points to reasonably catch a developing low.
SCAN_LATS = [5, 10, 15, 20, 25, 30]
SCAN_LONS = [-90, -80, -70, -60, -50, -40, -30, -20]


# Checkpoints spanning short (0-5 day), medium (7-10 day), and long
# (12-14 day) range, per instruction to capture as much of each
# model's real forecast horizon as possible, not just the first few
# days. Easy to adjust.
SHORT_MEDIUM_LONG_HOURS = (0, 24, 48, 72, 96, 120, 168, 240, 336)


def fetch_model_grid(model_endpoint, forecast_hours=SHORT_MEDIUM_LONG_HOURS, models_param=None, label=None, forecast_days=15):
    """Queries an Open-Meteo forecast endpoint (GFS, ECMWF, or ECMWF
    with a specific models= override like AIFS) for pressure_msl and
    wind_speed_10m across the whole scan grid in a single batched
    request, then finds the lowest pressure (and nearby wind) at each
    requested forecast hour, out through medium/long range. Pass
    models_param to pin a specific model variant (e.g.
    'ecmwf_aifs025_single' for ECMWF's AI model) instead of the
    endpoint's default auto-selected model; forecast_days defaults to
    15 (GFS supports up to 16 -- pass 16 explicitly for GFS calls)."""
    lat_str = ",".join(str(lat) for lat in SCAN_LATS for _ in SCAN_LONS)
    lon_str = ",".join(str(lon) for _ in SCAN_LATS for lon in SCAN_LONS)
    cache_buster = int(time.time())
    models_bit = f"&models={models_param}" if models_param else ""
    url = (
        f"https://api.open-meteo.com/v1/{model_endpoint}"
        f"?latitude={lat_str}&longitude={lon_str}"
        f"&hourly=pressure_msl,wind_speed_10m&forecast_days={forecast_days}{models_bit}&_cb={cache_buster}"
    )
    data = _fetch_with_retries_bytes(url, label or f"OpenMeteo:{model_endpoint}")
    if not data:
        return None
    try:
        points = json.loads(data.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(points, list):
        print(f"[{model_endpoint}] Unexpected response shape -- skipping (non-fatal).")
        return None

    results = []
    for fh in forecast_hours:
        best = None
        for point in points:
            try:
                pressures = point["hourly"]["pressure_msl"]
                winds = point["hourly"]["wind_speed_10m"]
                if fh >= len(pressures):
                    continue
                p = pressures[fh]
                w = winds[fh]
                if p is None:
                    continue
            except (KeyError, IndexError, TypeError):
                continue
            if best is None or p < best["mslp_mb"]:
                best = {
                    "fh": fh,
                    "mslp_mb": round(p, 1),
                    "lat": round(point["latitude"], 1),
                    "lon": round(point["longitude"], 1),
                    "wind_mph": round(w * 0.621371) if w is not None else None,  # km/h -> mph
                }
        if best:
            results.append(best)

    if not results:
        return None

    run_time = points[0].get("hourly", {}).get("time", [None])[0]
    return {"run_time": run_time, "results": results}


def classify_region(lat, lon):
    """Rough geographic classification for the Tropical
    Atlantic/Caribbean/Gulf domain, per instruction -- so the report says
    'Gulf of Mexico' or 'Caribbean' instead of just a raw lat/lon."""
    if 18 <= lat <= 31 and -98 <= lon <= -81:
        return "Gulf of Mexico"
    if 9 <= lat <= 22 and -85 <= lon <= -60:
        return "Caribbean Sea"
    return "Open Tropical Atlantic"


def estimate_model_cycle(model_key):
    """Open-Meteo's forecast responses always start at '0:00 today'
    regardless of the actual model cycle used, so the exact init time
    isn't directly exposed in the standard forecast response. This
    estimates the most recent likely cycle (00/06/12/18Z) based on each
    model's known update schedule and typical publication delay, and is
    labeled clearly as an estimate."""
    now_utc = datetime.now(timezone.utc)
    delay_hours = 5 if model_key == "gfs_det" else 8  # GFS ~4-6h, ECMWF ~6-9h
    effective_time = now_utc - __import__("datetime").timedelta(hours=delay_hours)
    cycle_hour = (effective_time.hour // 6) * 6
    cycle_dt = effective_time.replace(hour=cycle_hour, minute=0, second=0, microsecond=0)
    return cycle_dt


def build_model_report(model_key, model_name, scan):
    cycle_dt_utc = estimate_model_cycle(model_key)
    cycle_local = cycle_dt_utc.astimezone(BEAUMONT_TZ)
    beaumont_str = cycle_local.strftime("%b %-d %I:%M%p %Z").replace(" 0", " ")
    cycle_z = f"{cycle_dt_utc.hour:02d}Z"

    lines = [f"{cycle_z} {model_name} -- Atlantic/Caribbean/Gulf scan (~{beaumont_str})", ""]
    for r in scan["results"]:
        wind_str = f", {r['wind_mph']} mph winds nearby" if r["wind_mph"] is not None else ""
        lat_dir = f"{abs(r['lat'])}N" if r["lat"] >= 0 else f"{abs(r['lat'])}S"
        lon_dir = f"{abs(r['lon'])}W" if r["lon"] <= 0 else f"{abs(r['lon'])}E"
        region = classify_region(r["lat"], r["lon"])
        lines.append(
            f"Hour {r['fh']}: lowest pressure {r['mslp_mb']} mb near "
            f"{lat_dir} {lon_dir} ({region}){wind_str}"
        )
    lines.append("")
    lines.append("Note: this is a grid-sampled scan of the model's pressure field, not an official NHC designation. Cycle time is estimated from the model's typical update schedule.")
    return "\n".join(lines)


def process_model_scan(model_key, model_endpoint, model_name, state):
    scan = fetch_model_grid(model_endpoint)
    if not scan:
        print(f"[{model_name}] Could not fetch grid data this cycle (non-fatal).")
        return

    fingerprint = scan["run_time"]
    state_key = f"{model_key}_cycle"
    if state.get(state_key) == fingerprint:
        print(f"[{model_name}] Already reported this cycle -- not resending.")
        return

    message = build_model_report(model_key, model_name, scan)
    try:
        deliver(message)
    except Exception as e:
        send_failure_alert(f"{model_name} delivery", str(e))
        return

    print(f"[{model_name}] Sent successfully.")
    state[state_key] = fingerprint
    save_state(state)


# Dense basin-wide grid for genuine local-minimum detection -- replaces
# the old fixed-checkpoint-list approach entirely (2026-07-30) after two
# real misses in one session: a fixed absolute pressure threshold can't
# work consistently across the whole basin (normal background pressure
# differs meaningfully by latitude), and any short list of "likely"
# points will always miss whatever forms somewhere else -- which is
# exactly what happened (Google Weather Lab, then separately GFS/ECMWF/
# Google AI all showing a real developing low with nothing in this
# system's checkpoint list anywhere near it). This checks for an actual
# local minimum -- a point meaningfully lower than its own immediate
# neighbors, the same thing your eye does looking at an MSLP map -- so
# it works the same way anywhere in the domain without a per-region
# magic number. Confirmed live: a 70-point batched request completes in
# ~1.3s, so this is cheap enough to run every cycle. Capped at 34N per
# instruction -- north of ~35-40N is baroclinic/extratropical, not a
# Gulf Coast threat.
BASIN_GRID_LATS = [10, 14, 18, 22, 26, 30, 34]
BASIN_GRID_LONS = [-95, -87, -79, -71, -63, -55, -47, -39, -31, -23]
BASIN_SCAN_HOURS = list(range(24, 337, 24))  # every 24h out through day 14
LOCAL_MIN_ANOMALY_MB = 3.0  # how much lower than the surrounding neighbors counts as real
BASIN_SCAN_MODELS = {
    "GFS": "gfs_seamless",
    "ECMWF": "ecmwf_ifs025",
    "ICON": "icon_seamless",
}
MAX_BASIN_CANDIDATES = 5  # cap how many distinct systems get the full (expensive) corroboration workup


def _basin_grid_points():
    return [(la, lo) for la in BASIN_GRID_LATS for lo in BASIN_GRID_LONS]


def fetch_basin_grid(model_param):
    """One batched request covering the whole basin grid -- fast and
    small because it's a single deterministic run, not an ensemble
    (the same density via the ensemble endpoint took 59s/11.7MB in
    testing, unusable for a routine check)."""
    points = _basin_grid_points()
    lat_str = ",".join(str(p[0]) for p in points)
    lon_str = ",".join(str(p[1]) for p in points)
    cache_buster = int(time.time())
    url = (
        f"https://api.open-meteo.com/v1/forecast"
        f"?latitude={lat_str}&longitude={lon_str}&hourly=pressure_msl"
        f"&models={model_param}&forecast_days=15&_cb={cache_buster}"
    )
    data = _fetch_with_retries_bytes(url, f"BasinGrid:{model_param}")
    if not data:
        return None
    try:
        parsed = json.loads(data.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(parsed, list) or len(parsed) != len(points):
        return None
    return parsed


def find_local_minima(grid_data):
    """Real local-minimum detection: each grid point is compared to its
    own immediate neighbors at the same forecast hour, not a fixed
    absolute threshold -- this is what actually distinguishes a genuine
    closed low from ordinary background pressure, and it works
    identically at 12N or 34N without a different number for each."""
    points = _basin_grid_points()
    n_lons = len(BASIN_GRID_LONS)
    findings = []
    for fh in BASIN_SCAN_HOURS:
        grid_vals = {}
        for idx in range(len(points)):
            hourly = grid_data[idx].get("hourly", {})
            press = hourly.get("pressure_msl", [])
            if fh < len(press) and press[fh] is not None:
                row, col = idx // n_lons, idx % n_lons
                grid_vals[(row, col)] = press[fh]
        for (row, col), val in grid_vals.items():
            neighbors = [
                grid_vals[(row + dr, col + dc)]
                for dr in (-1, 0, 1) for dc in (-1, 0, 1)
                if (dr, dc) != (0, 0) and (row + dr, col + dc) in grid_vals
            ]
            if len(neighbors) < 4:
                continue
            neighbor_avg = sum(neighbors) / len(neighbors)
            anomaly = val - neighbor_avg
            if anomaly <= -LOCAL_MIN_ANOMALY_MB:
                lat, lon = BASIN_GRID_LATS[row], BASIN_GRID_LONS[col]
                findings.append({
                    "lat": lat, "lon": lon, "fh": fh,
                    "pressure": round(val, 1), "neighbor_avg": round(neighbor_avg, 1),
                    "anomaly": round(anomaly, 1),
                })
    return findings


def _cluster_candidates(all_model_findings):
    """all_model_findings: {model_label: [findings...]}. Groups nearby
    findings (close in space and time) into single candidate systems,
    keeping the deepest anomaly as representative and noting which
    models agree -- multi-model agreement is much stronger evidence
    than any one model's raw output. Caps at MAX_BASIN_CANDIDATES since
    the expensive per-point corroboration checks below only make sense
    for genuine standouts, not every grid cell that dips slightly below
    its neighbors."""
    flat = []
    for model_label, findings in all_model_findings.items():
        for f in findings:
            flat.append({**f, "model": model_label})
    flat.sort(key=lambda f: f["anomaly"])  # most negative (deepest) first

    clusters = []
    for f in flat:
        placed = False
        for c in clusters:
            rep = c["representative"]
            if abs(f["lat"] - rep["lat"]) <= 4 and abs(f["lon"] - rep["lon"]) <= 6 and abs(f["fh"] - rep["fh"]) <= 48:
                c["models"].add(f["model"])
                placed = True
                break
        if not placed:
            clusters.append({"representative": f, "models": {f["model"]}})

    clusters.sort(key=lambda c: c["representative"]["anomaly"])
    return clusters[:MAX_BASIN_CANDIDATES]


def _basin_region_label(lat, lon):
    if 18 <= lat <= 31 and -98 <= lon <= -81:
        return "Gulf of Mexico"
    if 9 <= lat <= 22 and -85 <= lon <= -60:
        return "Caribbean Sea"
    if 26 <= lat <= 36 and -82 <= lon <= -65:
        return "Off SE US Coast"
    if 22 <= lat <= 36:
        return "Subtropical Atlantic"
    return "Open Tropical Atlantic"


def fetch_disturbance_environment(lat, lon, forecast_hour, model_param="gfs_seamless"):
    """For a candidate local-minimum, checks the environmental factors
    a real forecaster actually weighs for tropical development
    potential -- not just 'is pressure low here': 850mb vorticity (real
    spin vs. just a broad low), vertical wind shear between 850mb and
    200mb (high shear tears a developing system apart before it can
    organize), 700mb relative humidity (dry air entrainment suppresses
    development even with everything else favorable), and sea surface
    temperature (needs roughly 26C/80F+ to sustain deep convection).
    Only ever called for the handful of flagged candidates from
    find_local_minima, never swept across the whole grid -- these are
    meaningfully more expensive per-point checks than the basin scan."""
    vorticity = fetch_relative_vorticity(lat, lon, forecast_hour, model_param)

    shear_kt, rh_700 = None, None
    cache_buster = int(time.time())
    url = (
        f"https://api.open-meteo.com/v1/forecast"
        f"?latitude={lat}&longitude={lon}"
        f"&hourly=wind_speed_200hPa,wind_direction_200hPa,wind_speed_850hPa,wind_direction_850hPa,relative_humidity_700hPa"
        f"&models={model_param}&forecast_days=15&_cb={cache_buster}"
    )
    data = _fetch_with_retries_bytes(url, "DisturbanceEnv")
    if data:
        try:
            parsed = json.loads(data.decode("utf-8"))
            hourly = parsed.get("hourly", {})

            def _at(field):
                vals = hourly.get(field, [])
                return vals[forecast_hour] if forecast_hour < len(vals) else None

            spd200, dir200 = _at("wind_speed_200hPa"), _at("wind_direction_200hPa")
            spd850, dir850 = _at("wind_speed_850hPa"), _at("wind_direction_850hPa")
            if None not in (spd200, dir200, spd850, dir850):
                u200, v200 = _wind_to_uv(spd200, dir200)
                u850, v850 = _wind_to_uv(spd850, dir850)
                shear_kt = round(math.hypot(u200 - u850, v200 - v850) * 0.539957, 1)
            rh_700 = _at("relative_humidity_700hPa")
        except (json.JSONDecodeError, UnicodeDecodeError):
            pass

    sst_c = None
    cache_buster2 = int(time.time())
    sst_url = (
        f"https://marine-api.open-meteo.com/v1/marine"
        f"?latitude={lat}&longitude={lon}&hourly=sea_surface_temperature&forecast_days=15&_cb={cache_buster2}"
    )
    sst_data = _fetch_with_retries_bytes(sst_url, "DisturbanceSST")
    if sst_data:
        try:
            sst_parsed = json.loads(sst_data.decode("utf-8"))
            sst_vals = sst_parsed.get("hourly", {}).get("sea_surface_temperature", [])
            if forecast_hour < len(sst_vals) and sst_vals[forecast_hour] is not None:
                sst_c = round(sst_vals[forecast_hour], 1)
        except (json.JSONDecodeError, UnicodeDecodeError):
            pass

    return {"vorticity": vorticity, "shear_kt": shear_kt, "rh_700": rh_700, "sst_c": sst_c}


def _development_favorability(env):
    """Plain-language rollup of the environmental factors, matching how
    a forecaster talks through favorable/unfavorable conditions -- a
    count of how many factors line up, not a single manufactured
    verdict, since any one of these being missing shouldn't hide the
    others."""
    favorable, total = 0, 0
    notes = []
    if env.get("vorticity") is not None:
        total += 1
        if env["vorticity"] >= VORTICITY_NOTABLE_THRESHOLD:
            favorable += 1
            notes.append(f"real closed rotation (850mb vorticity {env['vorticity']})")
        else:
            notes.append(f"little/no real spin yet (850mb vorticity {env['vorticity']})")
    if env.get("shear_kt") is not None:
        total += 1
        if env["shear_kt"] <= 20:
            favorable += 1
            notes.append(f"low shear ({env['shear_kt']} kt) -- favorable")
        elif env["shear_kt"] <= 35:
            notes.append(f"moderate shear ({env['shear_kt']} kt) -- marginal")
        else:
            notes.append(f"high shear ({env['shear_kt']} kt) -- unfavorable")
    if env.get("rh_700") is not None:
        total += 1
        if env["rh_700"] >= 50:
            favorable += 1
            notes.append(f"adequate mid-level moisture ({env['rh_700']}% RH at 700mb)")
        else:
            notes.append(f"drier mid-levels ({env['rh_700']}% RH at 700mb) -- can suppress development")
    if env.get("sst_c") is not None:
        total += 1
        sst_f = round(env["sst_c"] * 9 / 5 + 32, 1)
        if env["sst_c"] >= 26:
            favorable += 1
            notes.append(f"warm enough water ({sst_f}F) to sustain convection")
        else:
            notes.append(f"water too cool ({sst_f}F) to sustain much development")
    return favorable, total, notes


_basin_scan_cache = []


def get_basin_candidates():
    """Runs the shared dense-grid local-minimum scan across GFS, ECMWF,
    and ICON once per process and caches it -- each of the three
    ensemble model callers below (google_ai/gefs/ecmwf_ens) reuses the
    same candidates instead of re-scanning the whole basin three times.
    Environmental corroboration (vorticity/shear/moisture/SST) is also
    computed here, once per candidate, for the same reason."""
    global _basin_scan_cache
    if _basin_scan_cache:
        return _basin_scan_cache

    all_findings = {}
    for label, model_param in BASIN_SCAN_MODELS.items():
        grid = fetch_basin_grid(model_param)
        all_findings[label] = find_local_minima(grid) if grid else []

    clusters = _cluster_candidates(all_findings)
    for c in clusters:
        rep = c["representative"]
        c["env"] = fetch_disturbance_environment(rep["lat"], rep["lon"], rep["fh"])
        c["favorable"], c["total_factors"], c["env_notes"] = _development_favorability(c["env"])

    _basin_scan_cache = clusters
    return clusters


ENSEMBLE_MODELS = {
    "google_ai": ("google_weathernext2_ensemble", "Google WeatherNext AI Ensemble"),
    "gefs": ("gfs_seamless", "GEFS (GFS Ensemble)"),
    "ecmwf_ens": ("ecmwf_ifs025_ensemble", "ECMWF Ensemble"),
}

DISTURBANCE_THRESHOLD_MB = 1008  # kept for INTERESTING_MSLP_THRESHOLD_MB below; no longer
# used for detection itself -- see LOCAL_MIN_ANOMALY_MB and find_local_minima.

GOOGLE_AI_FORECAST_DAYS = 15  # Google WeatherNext's actual max range
DEFAULT_ENSEMBLE_FORECAST_DAYS = 15  # GEFS/ECMWF Ensemble also support out to ~15-16 days


def fetch_ensemble_genesis_signal(model_key):
    """Checks this model's ensemble agreement specifically at the
    candidate locations found by the shared basin-wide local-minimum
    scan (get_basin_candidates) instead of a fixed checkpoint list --
    see get_basin_candidates for why that changed. Each candidate
    already carries its full environmental corroboration workup."""
    model_param, model_name = ENSEMBLE_MODELS[model_key]
    forecast_days = GOOGLE_AI_FORECAST_DAYS if model_key == "google_ai" else DEFAULT_ENSEMBLE_FORECAST_DAYS
    candidates = get_basin_candidates()
    if not candidates:
        return None

    findings = []
    for cluster in candidates:
        rep = cluster["representative"]
        lat, lon, fh = rep["lat"], rep["lon"], rep["fh"]

        cache_buster = int(time.time())
        url = (
            f"https://ensemble-api.open-meteo.com/v1/ensemble"
            f"?latitude={lat}&longitude={lon}&hourly=pressure_msl"
            f"&models={model_param}&forecast_days={forecast_days}&_cb={cache_buster}"
        )
        data = _fetch_with_retries_bytes(url, f"Ensemble:{model_key}:{lat},{lon}")
        pct, total = 0, 0
        if data:
            try:
                parsed = json.loads(data.decode("utf-8"))
                hourly = parsed.get("hourly", {})
                member_keys = [k for k in hourly if k.startswith("pressure_msl_member")]
                if member_keys:
                    below, cnt = 0, 0
                    for mk in member_keys:
                        series = hourly[mk]
                        if fh < len(series) and series[fh] is not None:
                            cnt += 1
                            if series[fh] <= rep["neighbor_avg"] - LOCAL_MIN_ANOMALY_MB:
                                below += 1
                    if cnt:
                        pct, total = round(100 * below / cnt), cnt
            except (json.JSONDecodeError, UnicodeDecodeError):
                pass

        findings.append({
            "region": _basin_region_label(lat, lon), "lat": lat, "lon": lon, "fh": fh,
            "pct": pct, "members": total,
            "pressure": rep["pressure"], "neighbor_avg": rep["neighbor_avg"], "anomaly": rep["anomaly"],
            "models_agreeing": sorted(cluster["models"]),
            "vorticity": cluster["env"].get("vorticity"),
            "env_notes": cluster["env_notes"],
            "favorable_factors": cluster["favorable"], "total_factors": cluster["total_factors"],
        })

    if not findings:
        return None
    return {"model_name": model_name, "findings": findings}


VORTICITY_GRID_OFFSET_DEG = 1.5  # spacing for the finite-difference neighbor points
VORTICITY_NOTABLE_THRESHOLD = 8.0  # x10^-5 s^-1 -- modest but real organized spin,
# well below a mature tropical cyclone core (which can show 30-85+) but clearly
# above typical background shear/noise


def _wind_to_uv(speed, direction_deg):
    """Meteorological wind_direction is where the wind blows FROM, so the
    velocity vector points the opposite way."""
    rad = math.radians(direction_deg)
    u = -speed * math.sin(rad)
    v = -speed * math.cos(rad)
    return u, v


def fetch_relative_vorticity(lat, lon, forecast_hour, model_param="gfs_seamless"):
    """Estimates 850mb relative vorticity at (lat, lon, forecast_hour) via
    centered finite differences on wind_speed_850hPa/wind_direction_850hPa
    at four neighboring points -- a real (if coarse) measure of whether
    there's genuine closed cyclonic rotation here, not just a broad area
    of lower pressure. Positive values in the Northern Hemisphere mean
    counterclockwise (cyclonic) spin; a mature tropical cyclone core can
    show values of 30-85+ (x10^-5 s^-1), ordinary background flow is
    usually under ~5. Always checked against deterministic GFS regardless
    of which ensemble flagged the pressure signal -- this is a corroboration
    check, not itself an ensemble-agreement metric."""
    offsets = {
        "north": (lat + VORTICITY_GRID_OFFSET_DEG, lon),
        "south": (lat - VORTICITY_GRID_OFFSET_DEG, lon),
        "east": (lat, lon + VORTICITY_GRID_OFFSET_DEG),
        "west": (lat, lon - VORTICITY_GRID_OFFSET_DEG),
    }
    lat_str = ",".join(str(p[0]) for p in offsets.values())
    lon_str = ",".join(str(p[1]) for p in offsets.values())
    cache_buster = int(time.time())
    url = (
        f"https://api.open-meteo.com/v1/forecast"
        f"?latitude={lat_str}&longitude={lon_str}"
        f"&hourly=wind_speed_850hPa,wind_direction_850hPa"
        f"&models={model_param}&forecast_days=15&_cb={cache_buster}"
    )
    data = _fetch_with_retries_bytes(url, "Vorticity")
    if not data:
        return None
    try:
        points = json.loads(data.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(points, list) or len(points) != 4:
        return None

    uv = {}
    for label, point in zip(offsets.keys(), points):
        hourly = point.get("hourly", {})
        speeds = hourly.get("wind_speed_850hPa", [])
        dirs = hourly.get("wind_direction_850hPa", [])
        if forecast_hour >= len(speeds) or speeds[forecast_hour] is None or dirs[forecast_hour] is None:
            return None
        uv[label] = _wind_to_uv(speeds[forecast_hour], dirs[forecast_hour])

    dx_m = 2 * VORTICITY_GRID_OFFSET_DEG * 111000 * math.cos(math.radians(lat))
    dy_m = 2 * VORTICITY_GRID_OFFSET_DEG * 111000
    dv_dx = (uv["east"][1] - uv["west"][1]) / dx_m
    du_dy = (uv["north"][0] - uv["south"][0]) / dy_m
    vorticity = (dv_dx - du_dy) * 1e5  # scale to x10^-5 s^-1, matching the standard display convention
    return round(vorticity, 1)


def _ensemble_bearing(lat1, lon1, lat2, lon2):
    """Same great-circle bearing math as check_storm.py's storm-movement
    calc, reused here to give a genuine calculated direction between
    where a genesis signal first shows up and where it later shows up --
    not a guess, an actual bearing between the two flagged points."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dlmb = math.radians(lon2 - lon1)
    y = math.sin(dlmb) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dlmb)
    deg = (math.degrees(math.atan2(y, x)) + 360) % 360
    dirs = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
            "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]
    return dirs[round(deg / 22.5) % 16]


def build_ensemble_report(model_name, signal):
    lines = [f"{model_name} -- genesis signal check", ""]
    findings = sorted(signal["findings"], key=lambda f: f["anomaly"])
    for f in findings:
        agreement = f", {f['pct']}% of {f['members']} {model_name} members agree" if f["members"] else ""
        lines.append(
            f"{f['region']} (hour {f['fh']}): {f['pressure']} mb vs. {f['neighbor_avg']} mb "
            f"surrounding average ({f['anomaly']:+.1f} mb anomaly){agreement}"
        )
        if f.get("models_agreeing"):
            lines.append(f"  Also flagged by: {', '.join(f['models_agreeing'])}")
        for note in f.get("env_notes", []):
            lines.append(f"  - {note}")
        fav, tot = f.get("favorable_factors", 0), f.get("total_factors", 0)
        if tot:
            lines.append(f"  Development factors favorable: {fav}/{tot}")
        lines.append("")

    if len(findings) >= 2:
        first, last = findings[0], findings[-1]
        if first["region"] != last["region"]:
            compass = _ensemble_bearing(first["lat"], first["lon"], last["lat"], last["lon"])
            lines.append(
                f"Deepest signal near {first['region']}, another near {last['region']} -- "
                f"roughly {compass} of the first, worth watching whether these are the same system."
            )
            lines.append("")

    lines.append("Note: this is a real local pressure minimum + environmental check across a basin-wide grid, not a precise genesis probability map or an official NHC designation.")
    return "\n".join(lines)


def process_ensemble_genesis(model_key, state):
    signal = fetch_ensemble_genesis_signal(model_key)
    state_key = f"{model_key}_genesis_fingerprint"
    if not signal:
        if state.get(state_key):
            del state[state_key]
            save_state(state)
        return

    _, model_name = ENSEMBLE_MODELS[model_key]
    fingerprint = json.dumps(signal["findings"], sort_keys=True)
    if state.get(state_key) == fingerprint:
        print(f"[{model_name}] Genesis signal unchanged -- not resending.")
        return

    message = build_ensemble_report(model_name, signal)
    try:
        deliver(message)
    except Exception as e:
        send_failure_alert(f"{model_name} genesis delivery", str(e))
        return

    print(f"[{model_name}] Genesis signal sent.")
    state[state_key] = fingerprint
    save_state(state)


INTERESTING_MSLP_THRESHOLD_MB = DISTURBANCE_THRESHOLD_MB


def tier_label(pct):
    """Turns a raw ensemble-agreement percentage into a plain-language
    chance tier, matching how NHC talks about formation odds (low/
    medium/high). Thresholds are easy to adjust."""
    if pct >= 50:
        return "High chance"
    if pct >= 25:
        return "Medium chance"
    return "Low chance"


def current_cycle_key():
    now_utc = datetime.now(timezone.utc)
    # GFS is typically ready ~5h after each cycle, but Euro (the
    # bottleneck) genuinely runs ~6.5-7h per multiple independent
    # real-world sources (wethr.net, WeatherBell-style model schedules)
    # -- confirmed directly against the user's own observation (12Z
    # cycle not fully ready until ~1-2 PM CDT = 18-19Z). Using 7h here
    # so both models are genuinely available before labeling a cycle
    # ready, not just guessing.
    effective_time = now_utc - timedelta(hours=7)
    cycle_hour = (effective_time.hour // 6) * 6
    cycle_date = effective_time.strftime("%Y-%m-%d")
    return f"{cycle_date}T{cycle_hour:02d}"


def fetch_nhc_outlook_summary():
    cache_buster = int(time.time())
    url = f"https://mesonet.agron.iastate.edu/cgi-bin/afos/retrieve.py?pil=TWOAT&_cb={cache_buster}"
    data = _fetch_with_retries_bytes(url, "NHC:TWOAT")
    if not data:
        return None
    text = data.decode("utf-8", errors="replace")
    text = text.split("\x01")[-1] if "\x01" in text else text
    text = text.replace("\x03", "").strip()
    lower = text.lower()
    if "tropical cyclone formation is not expected" in lower and "percent" not in lower:
        return "No areas of interest noted by NHC."
    mentions = re.findall(
        r"Formation chance through 48 hours\.\.\.\w+\.\.\.\d+ percent"
        r"|Formation chance through 7 days\.\.\.\w+\.\.\.\d+ percent",
        text,
    )
    if not mentions:
        return "No formation percentages currently listed by NHC."
    return "; ".join(mentions[:6])


def build_combined_cycle_report(cycle_hour_utc, gfs_scan, ecmwf_scan, aifs_scan, ensemble_signals, nhc_summary, rainfall_flags=None, setx_swla_outlook=None, ndfd_summary=None, front_signal=None, line_signal=None, temp_gradient=None, ndfd_changed=True):
    cycle_dt_utc = datetime.now(timezone.utc).replace(hour=cycle_hour_utc, minute=0, second=0, microsecond=0)
    cycle_local = cycle_dt_utc.astimezone(BEAUMONT_TZ)
    beaumont_str = cycle_local.strftime("%b %-d %I:%M %p").replace(" 0", " ")
    lines = ["<b>\U0001F30E Model Watch</b> -- Beaumont time", f"Cycle: {cycle_hour_utc:02d}Z (~{beaumont_str})", ""]

    is_interesting = False

    import setx_swla_extra as _sx2
    # Fetched once, up front, and reused both for the short-term prose
    # below AND the 7-Day Forecast section later -- this is the dense
    # 9x9-grid, HRRR/Euro-blended data, per instruction: day 1-2 must
    # use HRRR on the large grid so a small storm can't fall entirely
    # between points, and the short-term summary shouldn't recompute a
    # second, different answer from the old sparse 5-point grid.
    try:
        seven_day = _sx2.fetch_seven_day_forecast()
    except Exception as e:
        print(f"[Combined cycle] 7-day forecast unavailable (non-fatal): {e}")
        seven_day = None
    try:
        hrrr_grid_lines = _sx2.build_hrrr_grid_note(_sx2.fetch_hrrr_grid_detail())
    except Exception as e:
        print(f"[Combined cycle] HRRR grid detail unavailable (non-fatal): {e}")
        hrrr_grid_lines = None
    try:
        ndfd_today_tomorrow, ndfd_days_3_5 = _sx2.fetch_ndfd_qpf_by_range()
    except Exception as e:
        print(f"[Combined cycle] NDFD range data unavailable (non-fatal): {e}")
        ndfd_today_tomorrow, ndfd_days_3_5 = None, None
    try:
        wpc_setx_swla_note = _sx2.fetch_setx_swla_wpc_corroboration()
    except Exception as e:
        print(f"[Combined cycle] SETX/SWLA WPC check unavailable (non-fatal): {e}")
        wpc_setx_swla_note = None

    lines.append("<b>\U0001F327️ SETX/SWLA RAIN OUTLOOK</b>")
    lines.extend(_sx2.build_setx_swla_section(setx_swla_outlook, seven_day, hrrr_grid_lines, ndfd_today_tomorrow, ndfd_days_3_5, wpc_setx_swla_note))
    try:
        conditions_detail = _sx2.fetch_conditions_detail()
        temp_blend = _sx2.fetch_temperature_blend()
        conditions_lines = _sx2.build_conditions_section(conditions_detail, setx_swla_outlook, front_signal, gfs_scan, ecmwf_scan, temp_blend)
        if conditions_lines:
            lines.extend(conditions_lines)
    except Exception as e:
        print(f"[Combined cycle] Conditions detail unavailable (non-fatal): {e}")
    try:
        seven_day_lines = _sx2.build_seven_day_section(seven_day)
        if seven_day_lines:
            lines.extend(seven_day_lines)
    except Exception as e:
        print(f"[Combined cycle] 7-day forecast section build failed (non-fatal): {e}")
    if line_signal:
        note = _sx2.build_organized_line_note(line_signal)
        if note:
            lines.append(f"- {note}")
    if temp_gradient:
        note = _sx2.build_temperature_gradient_note(temp_gradient)
        if note:
            lines.append(f"- {note}")
    try:
        afd_front_mentions = _sx2.fetch_afd_front_mention()
    except Exception as e:
        print(f"[Combined cycle] AFD front check unavailable (non-fatal): {e}")
        afd_front_mentions = None
    front_lines = _sx2.build_front_signal_section(front_signal, afd_front_mentions)
    if front_lines:
        lines.append("")
        lines.extend(front_lines)

    if rainfall_flags:
        lines.append("")
        lines.append("<b>\U0001F30A GULF COAST RAINFALL WATCH</b> (next 10 days)")
        for model_name, r in rainfall_flags.items():
            lines.append(f"- {model_name}: heaviest near {r['place']}, {r['total_in']}\" possible")
            if r.get("wpc_note"):
                lines.append(f"  {r['wpc_note']}")

    lines.append("")
    lines.append("<b>\U0001F300 MAIN MODELS</b>")
    for label, scan in (("GFS", gfs_scan), ("Euro", ecmwf_scan)):
        if scan and scan.get("results"):
            best = min(scan["results"], key=lambda r: r["mslp_mb"])
            region = classify_region(best["lat"], best["lon"])
            wind_str = f", {best['wind_mph']} mph nearby" if best.get("wind_mph") is not None else ""
            lines.append(f"- {label}: lowest {best['mslp_mb']} mb near {region} by hour {best['fh']}{wind_str}")
            # NOTE: raw deterministic MSLP dipping below a threshold
            # somewhere across a wide multi-day grid is normal
            # background noise on its own -- per instruction, this must
            # NOT drive the tropical-development summary by itself.
            # Only genuine ensemble agreement or an explicit NHC
            # formation percentage does that (see below).
        else:
            lines.append(f"- {label}: data unavailable this cycle")

    lines.append("")
    lines.append("<b>\U0001F4CA SIDE NOTES</b>")
    if aifs_scan and aifs_scan.get("results"):
        best = min(aifs_scan["results"], key=lambda r: r["mslp_mb"])
        region = classify_region(best["lat"], best["lon"])
        wind_str = f", {best['wind_mph']} mph nearby" if best.get("wind_mph") is not None else ""
        lines.append(f"- ECMWF AIFS (AI): lowest {best['mslp_mb']} mb near {region} by hour {best['fh']}{wind_str}")
    else:
        lines.append("- ECMWF AIFS (AI): data unavailable this cycle")
    for model_key, signal in ensemble_signals.items():
        _, model_name = ENSEMBLE_MODELS[model_key]
        if signal and signal.get("findings"):
            if model_key == "google_ai":
                for f in signal["findings"]:
                    lines.append(f"- {model_name}: {tier_label(f['pct'])} ({f['pct']}%) of members show a developing low near {f['region']} by hour {f['fh']}")
            else:
                top = min(signal["findings"], key=lambda f: f["anomaly"])
                lines.append(f"- {model_name}: {tier_label(top['pct'])} ({top['pct']}%) of members show a developing low near {top['region']} by hour {top['fh']}")
            by_region = {}
            for f in signal["findings"]:
                by_region[f["region"]] = max(by_region.get(f["region"], 0), f["pct"])
            if len(by_region) > 1:
                split_str = ", ".join(f"{region} {pct}%" for region, pct in sorted(by_region.items(), key=lambda kv: -kv[1]))
                lines.append(f"  Track split: {split_str}")
            is_interesting = True
        else:
            lines.append(f"- {model_name}: no signal above threshold")
    nhc_line = nhc_summary or "unavailable this cycle"
    lines.append(f"- NHC: {nhc_line}")
    if nhc_summary and "percent" in nhc_summary:
        is_interesting = True

    if ndfd_summary:
        lines.append("")
        if ndfd_changed:
            lines.append("<b>\U0001F4CB NWS comparison</b> (brief): " + "; ".join(ndfd_summary))
        else:
            lines.append("<b>\U0001F4CB NWS comparison</b>: No significant change from NWS Houston / Lake Charles.")

    lines.append("")
    if is_interesting:
        lines.append("<b>\U0001F4DD Summary:</b> Signals of possible tropical development noted above -- worth watching closely.")
    else:
        lines.append("<b>\U0001F4DD Summary:</b> Quiet. No significant tropical signals detected this cycle.")

    lines.append("")
    lines.append("Expect updates roughly: 00Z ~2AM, 06Z ~8AM, 12Z ~2PM, 18Z ~8PM (Beaumont time)")

    return "\n".join(lines)

# Representative coastal/near-coastal points across the Gulf Coast
# states -- used to flag heavy rainfall potential specifically for TX,
# LA, MS, AL, and FL. (Restored after an accidental deletion during an
# earlier rewrite.)
GULF_COAST_RAIN_POINTS = [
    (29.76, -95.37, "Houston, TX"),
    (30.08, -94.10, "Beaumont, TX"),
    (27.80, -97.40, "Corpus Christi, TX"),
    (35.47, -97.52, "Oklahoma City, OK"),
    (29.95, -90.07, "New Orleans, LA"),
    (30.45, -91.19, "Baton Rouge, LA"),
    (34.75, -92.29, "Little Rock, AR"),
    (35.15, -90.05, "Memphis, TN"),
    (30.69, -88.04, "Mobile, AL"),
    (32.30, -90.18, "Jackson, MS"),
    (30.42, -87.22, "Pensacola, FL"),
    (27.95, -82.46, "Tampa, FL"),
    (30.33, -81.66, "Jacksonville, FL"),
    (25.76, -80.19, "Miami, FL"),
    (32.08, -81.10, "Savannah, GA"),
    (32.78, -79.93, "Charleston, SC"),
    (36.85, -76.29, "Norfolk, VA"),
]

HEAVY_RAIN_THRESHOLD_INCHES = 1.0

GULF_STATE_NAMES = {
    "TX": "TEXAS", "OK": "OKLAHOMA", "LA": "LOUISIANA", "AR": "ARKANSAS",
    "TN": "TENNESSEE", "AL": "ALABAMA", "MS": "MISSISSIPPI", "FL": "FLORIDA",
    "GA": "GEORGIA", "SC": "SOUTH CAROLINA", "VA": "VIRGINIA",
}

WPC_HEADLINE_RE = re.compile(
    r"THERE IS A (MARGINAL|SLIGHT|MODERATE|HIGH) RISK OF EXCESSIVE RAINFALL\s*(.*?)\.\.\.",
    re.S,
)


def _fetch_wpc_day_blocks():
    """Fetches and parses the WPC Excessive Rainfall Discussion once
    per cycle (reused across all models below) -- WPC is only ever
    shown as backup confirmation of what the models already flagged,
    never as an independent signal, per instruction."""
    import wpc_ero_pipeline as _wpc
    raw = _wpc.fetch_ero_discussion()
    if not raw:
        return None
    text = _wpc.clean_body(raw)
    return _wpc.split_into_day_blocks(text) or None


def _wpc_corroboration_for_place(blocks, place_name):
    """Only returns a note if a WPC day's OFFICIAL HEADLINE (not its
    narrative discussion text, which can say things like 'the Moderate
    Risk...was removed') carries a Moderate or High risk AND that
    headline's region text names the same city/state the model already
    flagged, per instruction -- Slight/Marginal never count here, and a
    non-matching area never gets attached."""
    if not blocks:
        return None
    city = place_name.split(",")[0].strip().upper()
    state_abbr = place_name.split(",")[-1].strip().upper()
    state_name = GULF_STATE_NAMES.get(state_abbr, "")
    keywords = [city] + ([state_name] if state_name else [])

    for days, block_text in blocks:
        m = WPC_HEADLINE_RE.search(block_text.upper())
        if not m:
            continue
        risk_word, region_text = m.group(1), m.group(2)
        if risk_word not in ("MODERATE", "HIGH"):
            continue
        if not any(kw in region_text for kw in keywords):
            continue
        day_str = "/".join(f"Day {d}" for d in days)
        return f"WPC {day_str}: {risk_word.title()} risk of excessive rainfall in the area -- matches model signal"
    return None


def _fetch_rainfall_totals(endpoint):
    lat_str = ",".join(str(p[0]) for p in GULF_COAST_RAIN_POINTS)
    lon_str = ",".join(str(p[1]) for p in GULF_COAST_RAIN_POINTS)
    cache_buster = int(time.time())
    url = (
        f"https://api.open-meteo.com/v1/{endpoint}"
        f"?latitude={lat_str}&longitude={lon_str}"
        f"&daily=precipitation_sum&forecast_days=10&_cb={cache_buster}"
    )
    data = _fetch_with_retries_bytes(url, f"GulfCoastRainfall:{endpoint}")
    if not data:
        return {}
    try:
        points = json.loads(data.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {}
    if not isinstance(points, list):
        return {}
    totals = {}
    for point, (_, _, place_name) in zip(points, GULF_COAST_RAIN_POINTS):
        try:
            totals_mm = point["daily"]["precipitation_sum"]
        except (KeyError, TypeError):
            continue
        total_in = sum(v for v in totals_mm if v is not None) / 25.4
        totals[place_name] = round(total_in, 1)
    return totals


def _fetch_rainfall_totals_google_ai():
    lat_str = ",".join(str(p[0]) for p in GULF_COAST_RAIN_POINTS)
    lon_str = ",".join(str(p[1]) for p in GULF_COAST_RAIN_POINTS)
    cache_buster = int(time.time())
    url = (
        f"https://ensemble-api.open-meteo.com/v1/ensemble"
        f"?latitude={lat_str}&longitude={lon_str}"
        f"&daily=precipitation_sum&models=google_weathernext2_ensemble"
        f"&forecast_days=10&_cb={cache_buster}"
    )
    data = _fetch_with_retries_bytes(url, "GulfCoastRainfall:google_ai")
    if not data:
        return {}
    try:
        points = json.loads(data.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {}
    if not isinstance(points, list):
        return {}
    totals = {}
    for point, (_, _, place_name) in zip(points, GULF_COAST_RAIN_POINTS):
        daily = point.get("daily", {})
        member_keys = [k for k in daily if k.startswith("precipitation_sum_member")]
        if not member_keys:
            continue
        member_totals = []
        for mk in member_keys:
            vals = daily[mk]
            member_totals.append(sum(v for v in vals if v is not None))
        if member_totals:
            avg_mm = sum(member_totals) / len(member_totals)
            totals[place_name] = round(avg_mm / 25.4, 1)
    return totals


def fetch_gulf_coast_rainfall():
    model_totals = {
        "GFS": _fetch_rainfall_totals("gfs"),
        "Euro": _fetch_rainfall_totals("ecmwf"),
        "Google AI": _fetch_rainfall_totals_google_ai(),
    }
    results = {}
    wpc_blocks = None
    wpc_fetch_attempted = False
    for model_name, totals in model_totals.items():
        if not totals:
            continue
        heaviest_place = max(totals, key=lambda p: totals[p])
        heaviest_val = totals[heaviest_place]
        if heaviest_val >= HEAVY_RAIN_THRESHOLD_INCHES:
            entry = {"place": heaviest_place, "total_in": heaviest_val}
            if not wpc_fetch_attempted:
                try:
                    wpc_blocks = _fetch_wpc_day_blocks()
                except Exception as e:
                    print(f"[Gulf Coast Watch] WPC corroboration unavailable (non-fatal): {e}")
                wpc_fetch_attempted = True
            wpc_note = _wpc_corroboration_for_place(wpc_blocks, heaviest_place)
            if wpc_note:
                entry["wpc_note"] = wpc_note
            results[model_name] = entry
    if not results:
        return None
    return results


def process_combined_cycle(state):
    cycle_key = current_cycle_key()
    if state.get("last_combined_cycle") == cycle_key:
        print(f"[Combined cycle] Already sent {cycle_key} -- not resending.")
        return
    gfs_scan = fetch_model_grid("gfs", forecast_days=16)
    ecmwf_scan = fetch_model_grid("ecmwf", forecast_days=15)
    aifs_scan = fetch_model_grid("ecmwf", models_param="ecmwf_aifs025_single", label="OpenMeteo:AIFS", forecast_days=15)
    ensemble_signals = {}
    for model_key in ENSEMBLE_MODELS:
        ensemble_signals[model_key] = fetch_ensemble_genesis_signal(model_key)
    nhc_summary = fetch_nhc_outlook_summary()
    rainfall_flags = fetch_gulf_coast_rainfall()
    import setx_swla_extra as _sx
    setx_swla_outlook = _sx.fetch_setx_swla_rainfall_outlook()
    front_signal = _sx.fetch_front_signal()
    line_signal = _sx.fetch_organized_line_signal()
    temp_gradient = _sx.fetch_temperature_gradient()

    # Strict NWS Houston/Lake Charles dedup, per instruction -- only
    # send when something meaningfully changed, capped per day.
    today_str = datetime.now(timezone.utc).astimezone(BEAUMONT_TZ).strftime("%Y-%m-%d")
    if state.get("ndfd_send_day") != today_str:
        state["ndfd_send_day"] = today_str
        state["ndfd_send_count"] = 0
    ndfd_totals = _sx.fetch_ndfd_qpf_totals()
    should_send_ndfd, ndfd_summary, updated_ndfd_totals = _sx.describe_ndfd_change(
        ndfd_totals, state.get("last_ndfd_totals"), state.get("ndfd_send_count", 0)
    )
    state["last_ndfd_totals"] = updated_ndfd_totals
    if should_send_ndfd:
        state["ndfd_send_count"] = state.get("ndfd_send_count", 0) + 1
    ndfd_changed = should_send_ndfd
    if not should_send_ndfd:
        ndfd_summary = None

    cycle_hour_utc = int(cycle_key.split("T")[1])
    message = build_combined_cycle_report(cycle_hour_utc, gfs_scan, ecmwf_scan, aifs_scan, ensemble_signals, nhc_summary, rainfall_flags, setx_swla_outlook, ndfd_summary, front_signal, line_signal, temp_gradient, ndfd_changed)
    try:
        deliver(message)
    except Exception as e:
        send_failure_alert("Combined cycle delivery", str(e))
        return
    print(f"[Combined cycle] Sent {cycle_key} successfully.")
    state["last_combined_cycle"] = cycle_key

    # Trending: store this cycle's snapshot, compare against the last
    # two, and send a short second message right after the main one --
    # per instruction, only flagging what's actually changed.
    try:
        temp_buckets = _sx.fetch_temperature_buckets()
        snapshot = _sx.build_trend_snapshot(setx_swla_outlook, front_signal, gfs_scan, ecmwf_scan, temp_buckets)
        history = state.get(_sx.TREND_HISTORY_KEY, [])
        history = [snapshot] + history[:3]  # up to 4 runs total, per instruction
        trend_message = _sx.build_trending_message(cycle_hour_utc, history)
        deliver(trend_message)
        state[_sx.TREND_HISTORY_KEY] = history
        print(f"[Combined cycle] Trending message sent for {cycle_key}.")
    except Exception as e:
        print(f"[Combined cycle] Trending message failed (non-fatal): {e}")

    save_state(state)


def main():
    state = load_state()
    storms = fetch_active_atlantic_storms()
    if not storms:
        try:
            process_quiet_basin(state)
        except Exception as e:
            print(f"Unexpected error in quiet-basin handling (non-fatal): {e}")
    else:
        for storm in storms:
            try:
                process_storm(storm, state)
            except Exception as e:
                print(f"[{storm.get('id')}] Unexpected error (non-fatal, continuing): {e}")
    try:
        process_combined_cycle(state)
    except Exception as e:
        print(f"[Combined cycle] Unexpected error (non-fatal): {e}")


if __name__ == "__main__":
    main()
