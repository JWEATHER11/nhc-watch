#!/usr/bin/env python3
"""
radar_watch_pipeline.py -- Real NEXRAD Level II radar watch across the
SETX/SWLA corridor, using KHGX (Houston) and KLCH (Lake Charles).
This is ground truth, not a forecast: an actual radar beam finding an
actual echo right now, not a model's guess at what might happen.
Per instruction: "i dont want to use model data to tell me if real
storms are happening or not, model data can be wrong" -- this pairs
with metar_storm_pipeline.py (point station observations) as the two
real-observation watchers, both sent to the NWS chat, kept separate
from the model-based WXMODEL chat.

Validated against two real cases before being promoted out of
radar_prototype/ -- a quiet night (0% coverage, correctly no hits)
and Hurricane Beryl's actual Houston landfall (69% coverage, max
46.5 dBZ, correctly picks up a known severe event). Details in
radar_prototype/NOTES.md. Reuses the same dense 9x9 corridor grid as
the HRRR-based system (setx_swla_extra._hrrr_grid_points) so radar and
model checks line up on the same points.

Data source: unidata-nexrad-level2 -- the CURRENT bucket. The older
noaa-nexrad-level2 bucket referenced by most tutorials/docs was
deprecated Sept 1 2025 and no longer works.

Alerts only on genuine change (new area showing storm-intensity
echo, intensity crossing into the severe tier, or a full all-clear
after an active storm), same "don't repeat yourself" dedup pattern as
the rest of this system -- not a running commentary on light rain.
"""

import json
import os
import re
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pyart

import setx_swla_extra as sx

STATE_FILE = Path(__file__).parent / "radar_watch_state.json"
BEAUMONT_TZ = ZoneInfo("America/Chicago")
MAX_ATTEMPTS = 2
RETRY_DELAY_SEC = 2

RADAR_SITES = {
    "KHGX": {"lat": 29.4719, "lon": -95.0787, "label": "Houston"},
    "KLCH": {"lat": 30.1253, "lon": -93.2160, "label": "Lake Charles"},
}
BUCKET = "unidata-nexrad-level2"

MAX_GATE_DISTANCE_MI = 5  # beyond this, no real gate exists near the grid point
STORM_DBZ = 35  # real thunderstorm-intensity echo, not just light rain
SEVERE_DBZ = 50  # heavy rain / small hail possible
RAIN_DBZ = 20  # light-rain floor, used only for the coverage context line
MIN_STORM_GATES = 3  # a real cluster, not one noisy/anomalous gate


def _http_get_bytes(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": "radar-watch-pipeline/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _fetch_with_retries_bytes(url, label, timeout=20):
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            data = _http_get_bytes(url, timeout=timeout)
            if data:
                return data
            print(f"[{label}] Attempt {attempt}: empty response from {url}")
        except Exception as e:
            print(f"[{label}] Attempt {attempt} failed ({url}): {e}")
        if attempt < MAX_ATTEMPTS:
            time.sleep(RETRY_DELAY_SEC)
    return None


def latest_volume_key(site):
    now = datetime.now(timezone.utc)
    prefix = f"{now.year}/{now.month:02d}/{now.day:02d}/{site}/"
    url = f"https://{BUCKET}.s3.amazonaws.com/?list-type=2&prefix={prefix}&max-keys=1000"
    data = _fetch_with_retries_bytes(url, f"RadarWatch:{site}:list", timeout=20)
    if not data:
        return None
    keys = re.findall(r"<Key>([^<]+)</Key>", data.decode("utf-8", errors="ignore"))
    keys = [k for k in keys if not k.endswith("_MDM")]
    return keys[-1] if keys else None


def fetch_volume_bytes(key, site):
    url = f"https://{BUCKET}.s3.amazonaws.com/{key}"
    return _fetch_with_retries_bytes(url, f"RadarWatch:{site}:volume", timeout=60)


def lowest_sweep_reflectivity_field(radar):
    """Flattened (lats, lons, reflectivity) for the lowest elevation
    sweep -- flat arrays make nearest-neighbor a simple vectorized
    distance calc instead of a polar-coordinate lookup."""
    sweep0 = radar.get_slice(0)
    refl = radar.fields["reflectivity"]["data"][sweep0]
    lats, lons, _ = radar.get_gate_lat_lon_alt(0)
    return lats.flatten(), lons.flatten(), np.ma.filled(refl, np.nan).flatten()


def nearest_reflectivity(grid_lat, grid_lon, site_data):
    """Checks every radar site's lowest sweep and returns the value
    from whichever has the closest actual gate to this grid point.
    A NaN gate is a real 'no significant echo' reading (below the
    detectable-signal threshold), treated as 0.0 dBZ -- not skipped.
    Only a grid point with no real gate within MAX_GATE_DISTANCE_MI
    from any site is skipped entirely (see radar_prototype/NOTES.md
    for why this matters)."""
    best_val, best_dist_mi, best_site = None, None, None
    for site, (lats, lons, refl) in site_data.items():
        dist_deg = np.sqrt((lats - grid_lat) ** 2 + (lons - grid_lon) ** 2)
        idx = np.nanargmin(dist_deg)
        dist_mi = dist_deg[idx] * 69
        if dist_mi > MAX_GATE_DISTANCE_MI:
            continue
        val = refl[idx]
        if np.isnan(val):
            val = 0.0
        if best_dist_mi is None or dist_mi < best_dist_mi:
            best_val, best_dist_mi, best_site = val, dist_mi, site
    return best_val, best_dist_mi, best_site


def fetch_radar_signal():
    """Downloads the latest volume from each corridor radar, decodes
    the lowest sweep, and checks every corridor grid point. Returns
    None only if BOTH radars are unavailable this cycle -- one site
    down is handled gracefully with whichever site is up."""
    site_data = {}
    scan_times = {}
    for site in RADAR_SITES:
        key = latest_volume_key(site)
        if not key:
            print(f"[{site}] No volume found this cycle (non-fatal) -- skipping site.")
            continue
        data = fetch_volume_bytes(key, site)
        if not data:
            print(f"[{site}] Volume download failed this cycle (non-fatal) -- skipping site.")
            continue
        tmp_path = f"/tmp/{site}_radar_watch.ar2v"
        with open(tmp_path, "wb") as f:
            f.write(data)
        try:
            radar = pyart.io.read_nexrad_archive(tmp_path)
            site_data[site] = lowest_sweep_reflectivity_field(radar)
            scan_times[site] = radar.time.get("units", "")
        except Exception as e:
            print(f"[{site}] Decode failed this cycle (non-fatal): {e}")

    if not site_data:
        return None

    grid_points = sx._hrrr_grid_points()
    checked, rain_hits, storm_gate_count = 0, 0, 0
    storm_cities = set()
    max_val, max_loc = None, None
    for glat, glon in grid_points:
        val, dist_mi, site = nearest_reflectivity(glat, glon, site_data)
        if val is None:
            continue
        checked += 1
        if val >= RAIN_DBZ:
            rain_hits += 1
        if val >= STORM_DBZ:
            storm_gate_count += 1
            storm_cities.add(sx._nearest_city_label(glat, glon))
        if max_val is None or val > max_val:
            max_val, max_loc = val, (glat, glon)

    if storm_gate_count < MIN_STORM_GATES:
        storm_cities = set()

    return {
        "checked": checked,
        "total_points": len(grid_points),
        "rain_coverage_pct": round(100 * rain_hits / checked) if checked else 0,
        "storm_cities": sorted(storm_cities),
        "storm_gate_count": storm_gate_count,
        "max_dbz": round(float(max_val), 1) if max_val is not None else None,
        "max_near": sx._nearest_city_label(*max_loc) if max_loc else None,
        "sites_used": sorted(site_data.keys()),
    }


def check_radar_trigger(current, state):
    """Alerts on: a new city newly showing storm-intensity echo,
    intensity newly crossing into the severe tier, or a full all-clear
    after storms were active. Does not re-alert every cycle just
    because storms are still ongoing with no material change --
    same 'don't repeat yourself' dedup as metar_storm_pipeline.py."""
    reasons = []
    prev_cities = set(state.get("active_storm_cities", []))
    curr_cities = set(current["storm_cities"])

    newly_appeared = curr_cities - prev_cities
    if newly_appeared:
        reasons.append(
            f"Storm-intensity radar echo (>= {STORM_DBZ} dBZ) newly showing up near "
            + ", ".join(sorted(newly_appeared))
        )

    prev_severe = state.get("was_severe", False)
    is_severe = current["max_dbz"] is not None and current["max_dbz"] >= SEVERE_DBZ
    if is_severe and not prev_severe:
        near = f" near {current['max_near']}" if current.get("max_near") else ""
        reasons.append(f"Intensity climbing into heavy/hail-possible range -- {current['max_dbz']} dBZ{near}")

    all_clear = prev_cities and not curr_cities
    if all_clear:
        reasons.append("Storm-intensity echo has cleared the corridor")

    return (len(reasons) > 0), reasons


def build_radar_message(current, reasons):
    now_local = datetime.now(BEAUMONT_TZ)
    lines = [
        "<b>\U0001F4E1 Radar Watch</b> -- real observation, not model",
        now_local.strftime("%b %-d %I:%M %p").replace(" 0", " "),
        "",
    ]
    for r in reasons:
        lines.append(f"- {r}")
    lines.append("")
    if current["storm_cities"]:
        lines.append(f"Currently storm-intensity near: {', '.join(current['storm_cities'])}")
    if current.get("max_dbz") is not None:
        near = f" near {current['max_near']}" if current.get("max_near") else ""
        lines.append(f"Max reflectivity: {current['max_dbz']} dBZ{near}")
    lines.append(f"Corridor coverage (>= {RAIN_DBZ} dBZ, any rain): {current['rain_coverage_pct']}%")
    lines.append(f"Radars used: {', '.join(current['sites_used'])}")
    return "\n".join(lines)


def telegram_configured():
    return bool(os.environ.get("NWS_TELEGRAM_BOT_TOKEN")) and bool(os.environ.get("NWS_TELEGRAM_CHAT_ID"))


def send_telegram(text):
    bot_token = os.environ["NWS_TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["NWS_TELEGRAM_CHAT_ID"]
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = json.dumps({"chat_id": chat_id, "text": text, "parse_mode": "HTML"}).encode("utf-8")
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
        if attempt < MAX_ATTEMPTS:
            time.sleep(RETRY_DELAY_SEC)
    raise RuntimeError(f"Telegram send failed after {MAX_ATTEMPTS} attempts: {last_err}")


def deliver(text):
    if not telegram_configured():
        print("Telegram not configured -- skipping.")
        raise RuntimeError("NWS Telegram not configured")
    send_telegram(text)


def send_failure_alert(context, error):
    try:
        deliver(f"[radar-watch-pipeline error] {context}: {error}")
    except Exception as e:
        print(f"Could not even send the failure alert: {e}", file=sys.stderr)


def load_state():
    return json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2))


def process_radar_watch(state):
    current = fetch_radar_signal()
    if not current:
        print("Both radars unavailable this cycle (non-fatal) -- skipping.")
        return

    should_alert, reasons = check_radar_trigger(current, state)
    state["active_storm_cities"] = current["storm_cities"]
    state["was_severe"] = current["max_dbz"] is not None and current["max_dbz"] >= SEVERE_DBZ
    save_state(state)

    if not should_alert:
        print(f"No meaningful change -- not sending. Current: {current}")
        return

    print(f"Meaningful change detected -- sending. Reasons: {reasons}")
    message = build_radar_message(current, reasons)
    print(f"Message:\n{message}")
    try:
        deliver(message)
    except Exception as e:
        send_failure_alert("Radar watch delivery", str(e))
        return
    print("Sent successfully.")


def main():
    state = load_state()
    try:
        process_radar_watch(state)
    except Exception as e:
        print(f"Unexpected error (non-fatal): {e}")


if __name__ == "__main__":
    main()
