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
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

CURRENT_STORMS_URL = "https://www.nhc.noaa.gov/CurrentStorms.json"
ATCF_BASE = "https://ftp.nhc.noaa.gov/atcf/aid_public"
STATE_FILE = "wxmodel_state.json"
MAX_ATTEMPTS = 3
RETRY_DELAY_SEC = 5
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


def _http_get_bytes(url, timeout=20):
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


def build_storm_report(storm, summaries):
    name = storm.get("name", "Unknown")
    classification = storm.get("classification", "")
    storm_id = storm.get("id", "").upper()
    lines = [f"{classification} {name} ({storm_id})", ""]

    if not summaries:
        lines.append("No model guidance available yet for this system.")
        return "\n".join(lines)

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
        lines.append(
            f"{model_name}: {mph} mph ({s['vmax_kt']} kt){pressure_str} "
            f"at hour {s['tau']} -- from {beaumont_time} run"
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


def send_telegram(text):
    bot_token = os.environ["WXMODEL_TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["WXMODEL_TELEGRAM_CHAT_ID"]
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    max_len = 4000
    chunks = [text[i:i + max_len] for i in range(0, len(text), max_len)] or [text]
    for idx, chunk in enumerate(chunks, 1):
        payload = json.dumps({"chat_id": chat_id, "text": chunk}).encode("utf-8")
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
            raise RuntimeError(f"Telegram send failed after {MAX_ATTEMPTS} attempts on chunk {idx}: {last_err}")


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
    message = build_storm_report(storm, summaries)

    try:
        deliver(message)
    except Exception as e:
        send_failure_alert(f"{storm_id} delivery", str(e))
        return

    print(f"[{storm_id}] Sent successfully.")
    state[storm_id] = fingerprint
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


def fetch_model_grid(model_endpoint, forecast_hours=(0, 24, 48, 72, 96, 120)):
    """Queries Open-Meteo's GFS or ECMWF endpoint for pressure_msl and
    wind_speed_10m across the whole scan grid in a single batched
    request, then finds the lowest pressure (and nearby wind) at each
    requested forecast hour."""
    lat_str = ",".join(str(lat) for lat in SCAN_LATS for _ in SCAN_LONS)
    lon_str = ",".join(str(lon) for _ in SCAN_LATS for lon in SCAN_LONS)
    cache_buster = int(time.time())
    url = (
        f"https://api.open-meteo.com/v1/{model_endpoint}"
        f"?latitude={lat_str}&longitude={lon_str}"
        f"&hourly=pressure_msl,wind_speed_10m&forecast_days=6&_cb={cache_buster}"
    )
    data = _fetch_with_retries_bytes(url, f"OpenMeteo:{model_endpoint}")
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


def build_model_report(model_name, scan):
    run_dt_str = scan["run_time"]
    beaumont_str = "unknown time"
    if run_dt_str:
        try:
            dt_utc = datetime.strptime(run_dt_str, "%Y-%m-%dT%H:%M").replace(tzinfo=timezone.utc)
            dt_local = dt_utc.astimezone(BEAUMONT_TZ)
            beaumont_str = dt_local.strftime("%b %-d %I:%M%p %Z").replace(" 0", " ")
        except ValueError:
            pass

    lines = [f"{model_name} -- Atlantic/Caribbean/Gulf scan ({beaumont_str} run)", ""]
    for r in scan["results"]:
        wind_str = f", {r['wind_mph']} mph winds nearby" if r["wind_mph"] is not None else ""
        lat_dir = f"{abs(r['lat'])}N" if r["lat"] >= 0 else f"{abs(r['lat'])}S"
        lon_dir = f"{abs(r['lon'])}W" if r["lon"] <= 0 else f"{abs(r['lon'])}E"
        lines.append(
            f"Hour {r['fh']}: lowest pressure {r['mslp_mb']} mb near "
            f"{lat_dir} {lon_dir}{wind_str}"
        )
    lines.append("")
    lines.append("Note: this is a grid-sampled scan of the model's pressure field, not an official NHC designation.")
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

    message = build_model_report(model_name, scan)
    try:
        deliver(message)
    except Exception as e:
        send_failure_alert(f"{model_name} delivery", str(e))
        return

    print(f"[{model_name}] Sent successfully.")
    state[state_key] = fingerprint
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
        process_model_scan("gfs_det", "gfs", "GFS Deterministic", state)
    except Exception as e:
        print(f"[GFS Deterministic] Unexpected error (non-fatal): {e}")

    try:
        process_model_scan("ecmwf_det", "ecmwf", "ECMWF Deterministic", state)
    except Exception as e:
        print(f"[ECMWF Deterministic] Unexpected error (non-fatal): {e}")


if __name__ == "__main__":
    main()
