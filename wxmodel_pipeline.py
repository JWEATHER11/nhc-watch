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


def find_recent_gfs_cycle():
    """GFS runs 00/06/12/18Z with roughly 4-5h publication delay -- tries
    the most recent plausible cycles, newest first."""
    import xarray as xr

    now_utc = datetime.now(timezone.utc)
    for day_offset in (0, 1):
        base_date = now_utc - __import__("datetime").timedelta(days=day_offset)
        for hour in (18, 12, 6, 0):
            date_str = base_date.strftime("%Y%m%d")
            url = f"https://nomads.ncep.noaa.gov/dods/gfs_0p25_1hr/gfs{date_str}/gfs_0p25_1hr_{hour:02d}z"
            try:
                ds = xr.open_dataset(url)
                return ds, date_str, hour
            except Exception as e:
                print(f"[GFS deterministic] Cycle {date_str} {hour:02d}Z not available yet ({e}), trying older...")
                continue
    return None, None, None


def scan_gfs_deterministic():
    """Scans the latest deterministic GFS run for developing lows in the
    Tropical Atlantic, Caribbean, and Gulf of Mexico, even before NHC
    designates anything as an invest. Uses NOMADS OPeNDAP (xarray +
    netCDF4) instead of raw GRIB decoding -- much more reliable in a CI
    environment. Reports the lowest pressure found, its location, and
    the associated wind speed, at several forecast hours."""
    import numpy as np

    ds, cycle_date, cycle_hour = find_recent_gfs_cycle()
    if ds is None:
        print("[GFS deterministic] Could not open any recent GFS cycle -- skipping this scan (non-fatal).")
        return None

    try:
        mslp = ds["prmslmsl"].sel(lat=slice(5, 35), lon=slice(260, 340))
        u10 = ds["ugrd10m"].sel(lat=slice(5, 35), lon=slice(260, 340))
        v10 = ds["vgrd10m"].sel(lat=slice(5, 35), lon=slice(260, 340))
    except KeyError as e:
        print(f"[GFS deterministic] Expected variable not found ({e}) -- skipping (non-fatal).")
        ds.close()
        return None

    results = []
    for fh in (0, 24, 48, 72, 96, 120):
        try:
            mslp_fh = mslp.isel(time=fh).values
        except (IndexError, KeyError):
            continue
        flat_idx = int(np.nanargmin(mslp_fh))
        row, col = np.unravel_index(flat_idx, mslp_fh.shape)
        min_pa = float(mslp_fh[row, col])
        min_lat = float(mslp.lat.values[row])
        min_lon_raw = float(mslp.lon.values[col])
        display_lon = min_lon_raw - 360 if min_lon_raw > 180 else min_lon_raw

        try:
            u_val = float(u10.isel(time=fh).values[row, col])
            v_val = float(v10.isel(time=fh).values[row, col])
            wind_mph = round(((u_val ** 2 + v_val ** 2) ** 0.5) * 2.23694)
        except Exception:
            wind_mph = None

        results.append({
            "fh": fh,
            "mslp_mb": round(min_pa / 100, 1),
            "lat": round(min_lat, 1),
            "lon": round(display_lon, 1),
            "wind_mph": wind_mph,
        })

    ds.close()
    if not results:
        return None
    return {"cycle_date": cycle_date, "cycle_hour": cycle_hour, "results": results}


def build_gfs_deterministic_report(scan):
    cycle_dt = datetime.strptime(f"{scan['cycle_date']}{scan['cycle_hour']:02d}", "%Y%m%d%H").replace(tzinfo=timezone.utc)
    cycle_local = cycle_dt.astimezone(BEAUMONT_TZ)
    cycle_str = cycle_local.strftime("%b %-d %I:%M%p %Z").replace(" 0", " ")

    lines = [f"GFS Deterministic -- Atlantic/Caribbean/Gulf scan ({cycle_str} run)", ""]
    for r in scan["results"]:
        wind_str = f", {r['wind_mph']} mph winds nearby" if r["wind_mph"] is not None else ""
        lines.append(
            f"Hour {r['fh']}: lowest pressure {r['mslp_mb']} mb near "
            f"{r['lat']}N {abs(r['lon'])}W{wind_str}"
        )
    lines.append("")
    lines.append("Note: this is a raw scan of the model's pressure field, not an official NHC designation.")
    return "\n".join(lines)


def process_gfs_deterministic(state):
    scan = scan_gfs_deterministic()
    if not scan:
        return

    fingerprint = f"{scan['cycle_date']}{scan['cycle_hour']:02d}"
    if state.get("gfs_deterministic_cycle") == fingerprint:
        print("[GFS deterministic] Already reported this cycle -- not resending.")
        return

    message = build_gfs_deterministic_report(scan)
    try:
        deliver(message)
    except Exception as e:
        send_failure_alert("GFS deterministic delivery", str(e))
        return

    print("[GFS deterministic] Sent successfully.")
    state["gfs_deterministic_cycle"] = fingerprint
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
        process_gfs_deterministic(state)
    except Exception as e:
        print(f"[GFS deterministic] Unexpected error (non-fatal): {e}")


if __name__ == "__main__":
    main()
