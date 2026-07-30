#!/usr/bin/env python3
"""
PROTOTYPE -- NOT wired into any live pipeline or Telegram send yet.

Real-time NEXRAD Level II reflectivity check across the SETX/SWLA
dense corridor grid (same 81 points used by the HRRR-based system),
using KHGX (Houston) and KLCH (Lake Charles) -- the two radars that
together cover the corridor.

Data source: unidata-nexrad-level2 (the CURRENT bucket -- the older
noaa-nexrad-level2 bucket referenced by most tutorials/Py-ART examples
is deprecated as of Sept 1 2025 and no longer works, confirmed live).

This is genuinely real, observed radar data -- not model output. Each
run downloads the two radars' latest volume scans (~5-7MB each),
decodes with Py-ART, and for every corridor grid point finds the
nearest actual radar gate from whichever site is closer, reporting
reflectivity there.

Status: validated against real live data (see radar_prototype/NOTES.md
for what's been checked so far). NOT yet promoted to a real pipeline --
that requires testing against more cases (including known past severe
weather) before it should be trusted to alert anyone about anything.
"""

import re
import urllib.request
from datetime import datetime, timezone

import numpy as np
import pyart

RADAR_SITES = {
    "KHGX": {"lat": 29.4719, "lon": -95.0787, "label": "Houston"},
    "KLCH": {"lat": 30.1253, "lon": -93.2160, "label": "Lake Charles"},
}

BUCKET = "unidata-nexrad-level2"

# Same dense 9x9 grid used by the HRRR-based system, per instruction --
# keeps radar and model checks directly comparable against the same
# points.
GRID_LAT_MIN, GRID_LAT_MAX = 29.55, 30.75
GRID_LON_MIN, GRID_LON_MAX = -95.55, -93.10
GRID_ROWS, GRID_COLS = 9, 9

REFLECTIVITY_HIT_DBZ = 20  # "real precip" threshold, standard light-rain floor
MAX_GATE_DISTANCE_MI = 5  # if the nearest actual gate is farther than this from
                           # a grid point, treat it as "no data" rather than a stale match


def _grid_points():
    lat_step = (GRID_LAT_MAX - GRID_LAT_MIN) / (GRID_ROWS - 1)
    lon_step = (GRID_LON_MAX - GRID_LON_MIN) / (GRID_COLS - 1)
    points = []
    for r in range(GRID_ROWS):
        for c in range(GRID_COLS):
            points.append((
                round(GRID_LAT_MIN + r * lat_step, 3),
                round(GRID_LON_MIN + c * lon_step, 3),
            ))
    return points


def latest_volume_key(site):
    now = datetime.now(timezone.utc)
    prefix = f"{now.year}/{now.month:02d}/{now.day:02d}/{site}/"
    url = f"https://{BUCKET}.s3.amazonaws.com/?list-type=2&prefix={prefix}&max-keys=1000"
    req = urllib.request.Request(url, headers={"User-Agent": "radar-prototype/1.0"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = resp.read().decode()
    keys = re.findall(r"<Key>([^<]+)</Key>", data)
    keys = [k for k in keys if not k.endswith("_MDM")]
    if not keys:
        return None
    return keys[-1]


def fetch_volume(key, dest_path):
    url = f"https://{BUCKET}.s3.amazonaws.com/{key}"
    req = urllib.request.Request(url, headers={"User-Agent": "radar-prototype/1.0"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = resp.read()
    with open(dest_path, "wb") as f:
        f.write(data)
    return dest_path


def lowest_sweep_reflectivity_field(radar):
    """Returns (flat_lats, flat_lons, flat_refl) for the lowest
    elevation sweep -- flattened so nearest-neighbor search is a
    simple vectorized distance calc, not a polar-coordinate lookup."""
    sweep0 = radar.get_slice(0)
    refl = radar.fields["reflectivity"]["data"][sweep0]
    lats, lons, _ = radar.get_gate_lat_lon_alt(0)
    return lats.flatten(), lons.flatten(), np.ma.filled(refl, np.nan).flatten()


def nearest_reflectivity(grid_lat, grid_lon, site_data):
    """For one grid point, checks every radar site's lowest-sweep
    data and returns the value from whichever site has the CLOSEST
    actual gate to this point -- not just whichever radar it's
    geometrically nearest to, since a gate might not exist there in
    one radar's scan geometry.

    A NaN gate value is NOT "no data" -- Py-ART masks a gate when the
    return is below the detectable-signal threshold, i.e. the radar
    checked that spot and found no significant echo. That's a real,
    valid "clear" reading (exactly what most of the sky looks like on
    a quiet day), so it's treated as 0.0 dBZ here rather than skipped.
    Only a gate that's too far away (no real measurement to use at
    all) is skipped."""
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


def main():
    site_data = {}
    for site in RADAR_SITES:
        print(f"[{site}] Finding latest volume...")
        key = latest_volume_key(site)
        if not key:
            print(f"[{site}] No volume found (non-fatal) -- skipping this site.")
            continue
        print(f"[{site}] Latest: {key}")
        path = f"/tmp/{site}_latest.ar2v"
        fetch_volume(key, path)
        print(f"[{site}] Decoding...")
        radar = pyart.io.read_nexrad_archive(path)
        site_data[site] = lowest_sweep_reflectivity_field(radar)
        print(f"[{site}] Decoded -- scan time {radar.time['units']}")

    if not site_data:
        print("No radar data available this run.")
        return

    grid_points = _grid_points()
    hits, checked = 0, 0
    max_val, max_loc, max_site = None, None, None
    for glat, glon in grid_points:
        val, dist_mi, site = nearest_reflectivity(glat, glon, site_data)
        if val is None:
            continue
        checked += 1
        if val >= REFLECTIVITY_HIT_DBZ:
            hits += 1
        if max_val is None or val > max_val:
            max_val, max_loc, max_site = val, (glat, glon), site

    print()
    print(f"=== Corridor radar summary ({datetime.now(timezone.utc).isoformat()}) ===")
    print(f"Grid points with valid radar data: {checked} / {len(grid_points)}")
    if checked:
        print(f"Coverage (>= {REFLECTIVITY_HIT_DBZ} dBZ): {round(100 * hits / checked)}%")
        print(f"Max reflectivity in corridor: {max_val:.1f} dBZ at {max_loc} (nearest radar: {max_site})")
    else:
        print("No grid points had a usable nearby gate from either radar this run.")


if __name__ == "__main__":
    main()
