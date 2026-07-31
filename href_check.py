#!/usr/bin/env python3
"""
href_check.py -- Real HREF (NCEP's High-Resolution Ensemble Forecast,
10-member 3km convection-allowing ensemble) as a backup/corroboration
check for the HRRR alert. Tested live against GEFS first: GEFS (~25km
global ensemble) showed 0% everywhere on a day HRRR was flagging real
isolated storms -- too coarse to see small-scale summer convection, so
it would've been constant false "no backup" noise. HREF is actually
convection-allowing like HRRR itself, so it can genuinely corroborate
or contradict a specific HRRR hit.

Open-Meteo doesn't carry HREF, so this pulls straight from NOMADS
(NCEP's own free GRIB2 mirror), using the .idx sidecar files to
byte-range only the specific probability field needed instead of
downloading the full ~35MB grib file per forecast hour.

This is a BACKUP signal riding along with the core HRRR alert, not a
dependency of it -- every failure mode here (network, missing eccodes
package, unexpected file layout) must degrade to returning None, never
raise, so a NOMADS hiccup can never take down the primary HRRR watch.
"""

import tempfile
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

NOMADS_BASE = "https://nomads.ncep.noaa.gov/pub/data/nccf/com/href/prod"
HREF_THRESHOLD_MM = 12.7  # 0.5in -- same "any real rain" bar the HRRR alert itself uses
TIMEOUT_SEC = 20

# Same corridor cities the HRRR grid/alert already reports near, so this
# reads as a direct cross-check rather than a separate geography.
CORRIDOR_POINTS = {
    "Houston": (29.76, -95.37),
    "Beaumont": (30.08, -94.1),
    "Port Arthur": (29.9, -93.94),
    "Jasper": (30.92, -93.99),
    "Lake Charles": (30.22, -93.22),
}


def _fetch_url(url, headers=None, timeout=TIMEOUT_SEC):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _latest_href_cycle_with_data():
    """HREF posts 4x/day (00/06/12/18Z) with a ~2-3h publication lag
    that isn't fixed enough to just estimate -- this actually probes
    NOMADS for the f48 idx file and steps back a cycle at a time until
    it finds one that's really there, so a slow cycle never produces a
    silently-missing read."""
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    cycle = now.replace(hour=(now.hour // 6) * 6)
    for _ in range(6):  # up to 36h back -- generous safety margin
        date_str = cycle.strftime("%Y%m%d")
        hh = f"{cycle.hour:02d}"
        idx_url = f"{NOMADS_BASE}/href.{date_str}/ensprod/href.t{hh}z.conus.prob.f48.grib2.idx"
        try:
            _fetch_url(idx_url, timeout=10)
            return cycle
        except Exception:
            cycle -= timedelta(hours=6)
    return None


def _parse_idx(idx_bytes):
    entries = []
    for line in idx_bytes.decode("utf-8", errors="replace").strip().split("\n"):
        parts = line.split(":")
        if len(parts) < 3:
            continue
        try:
            entries.append((int(parts[0]), int(parts[1]), line))
        except ValueError:
            continue
    return entries


def _find_message_range(entries, match_substrings):
    for i, (_num, offset, desc) in enumerate(entries):
        if all(s in desc for s in match_substrings):
            end = entries[i + 1][1] - 1 if i + 1 < len(entries) else None
            return offset, end
    return None, None


def _fetch_grib_message(fhr_url, start, end):
    headers = {"Range": f"bytes={start}-{end}" if end is not None else f"bytes={start}-"}
    return _fetch_url(fhr_url, headers=headers)


def _sample_points(grib_bytes):
    import eccodes

    with tempfile.NamedTemporaryFile(suffix=".grib2") as tf:
        tf.write(grib_bytes)
        tf.flush()
        with open(tf.name, "rb") as f:
            gid = eccodes.codes_grib_new_from_file(f)
            if gid is None:
                return {}
            try:
                results = {}
                for name, (lat, lon) in CORRIDOR_POINTS.items():
                    nearest = eccodes.codes_grib_find_nearest(gid, lat, lon + 360)[0]
                    results[name] = round(nearest.value)
                return results
            finally:
                eccodes.codes_release(gid)


def fetch_href_corroboration():
    """Returns {"cycle": iso_str, "day1": {city: pct}, "day2": {city:
    pct}} or None on any failure. day1/day2 are HREF's own fixed
    0-24h / 24-48h windows relative to the cycle init time -- close
    enough to today/tomorrow for a corridor-wide corroboration check,
    the exact calendar-day mapping is done by the caller since it
    needs Beaumont-local 'today'."""
    try:
        import eccodes  # noqa -- fail fast & cheap if the package isn't installed
    except ImportError:
        return None

    try:
        cycle = _latest_href_cycle_with_data()
        if cycle is None:
            return None

        date_str = cycle.strftime("%Y%m%d")
        hh = f"{cycle.hour:02d}"
        base = f"{NOMADS_BASE}/href.{date_str}/ensprod/href.t{hh}z.conus.prob"

        out = {"cycle": cycle.isoformat()}
        for day_key, fhr, window_label in (
            ("day1", "f24", "0-1 day acc"),
            ("day2", "f48", "1-2 day acc"),
        ):
            idx_url = f"{base}.{fhr}.grib2.idx"
            entries = _parse_idx(_fetch_url(idx_url))
            match = ["APCP", window_label, f"prob >{HREF_THRESHOLD_MM}"]
            start, end = _find_message_range(entries, match)
            if start is None:
                continue
            grib_bytes = _fetch_grib_message(f"{base}.{fhr}.grib2", start, end)
            sampled = _sample_points(grib_bytes)
            if sampled:
                out[day_key] = sampled

        if "day1" not in out and "day2" not in out:
            return None
        return out
    except Exception:
        return None
