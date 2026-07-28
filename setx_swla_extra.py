#!/usr/bin/env python3
"""setx_swla_extra.py -- Expanded local SETX/SWLA rainfall outlook."""

import json
import time

import wxmodel_pipeline as w

SETX_SWLA_POINTS = [
    (29.76, -95.37),
    (30.08, -94.10),
    (29.90, -93.94),
    (30.68, -93.99),
    (30.23, -93.22),
]

TRAINING_STORM_HOURLY_THRESHOLD_IN = 1.0

NDFD_OFFICES = {
    "NWS Houston": "https://api.weather.gov/gridpoints/HGX/63,95",
    "NWS Lake Charles": "https://api.weather.gov/gridpoints/LCH/60,95",
}


def _sum_precip_mm(values):
    return sum(v for v in values if v is not None)


def fetch_setx_swla_rainfall_outlook():
    lat_str = ",".join(str(p[0]) for p in SETX_SWLA_POINTS)
    lon_str = ",".join(str(p[1]) for p in SETX_SWLA_POINTS)
    cache_buster = int(time.time())

    def fetch(endpoint, models_param=None, forecast_days=10):
        models_bit = f"&models={models_param}" if models_param else ""
        url = (
            f"https://api.open-meteo.com/v1/{endpoint}"
            f"?latitude={lat_str}&longitude={lon_str}"
            f"&daily=precipitation_sum&hourly=precipitation"
            f"&forecast_days={forecast_days}{models_bit}&_cb={cache_buster}"
        )
        data = w._fetch_with_retries_bytes(url, f"SETX_SWLA:{endpoint}:{models_param or 'auto'}")
        if not data:
            return None
        try:
            points = json.loads(data.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None
        return points if isinstance(points, list) else None

    gfs_points = fetch("gfs", forecast_days=10)
    euro_points = fetch("ecmwf", forecast_days=10)
    hrrr_points = fetch("forecast", models_param="ncep_hrrr_conus", forecast_days=2)

    def bucket_totals(points, day_start, day_end):
        totals = []
        if not points:
            return None
        for point in points:
            daily_mm = point.get("daily", {}).get("precipitation_sum", [])
            days = daily_mm[day_start:day_end]
            if days:
                totals.append(_sum_precip_mm(days) / 25.4)
        return round(sum(totals) / len(totals), 1) if totals else None

    short_gfs = bucket_totals(gfs_points, 0, 3)
    medium_gfs = bucket_totals(gfs_points, 3, 5)
    short_euro = bucket_totals(euro_points, 0, 3)
    medium_euro = bucket_totals(euro_points, 3, 5)

    long_wet_points = 0
    long_total_points = 0
    if gfs_points:
        for point in gfs_points:
            daily_mm = point.get("daily", {}).get("precipitation_sum", [])
            days = [v for v in daily_mm[5:10] if v is not None]
            if days:
                long_total_points += 1
                if sum(days) / 25.4 >= 0.25:
                    long_wet_points += 1
    long_confidence_pct = round(100 * long_wet_points / long_total_points) if long_total_points else None

    coverage_points = 0
    coverage_total = 0
    if gfs_points:
        for point in gfs_points:
            daily_mm = point.get("daily", {}).get("precipitation_sum", [])
            days = [v for v in daily_mm[:3] if v is not None]
            if days:
                coverage_total += 1
                if sum(days) >= 2.5:
                    coverage_points += 1
    coverage_pct = round(100 * coverage_points / coverage_total) if coverage_total else None

    max_hourly_in = 0.0
    for points in (gfs_points, euro_points, hrrr_points):
        if not points:
            continue
        for point in points:
            hourly_mm = point.get("hourly", {}).get("precipitation", [])
            for v in hourly_mm:
                if v is not None:
                    max_hourly_in = max(max_hourly_in, v / 25.4)

    if short_gfs is None and short_euro is None and long_confidence_pct is None:
        return None

    return {
        "short_gfs_in": short_gfs,
        "medium_gfs_in": medium_gfs,
        "short_euro_in": short_euro,
        "medium_euro_in": medium_euro,
        "long_confidence_pct": long_confidence_pct,
        "coverage_pct": coverage_pct,
        "max_hourly_in": round(max_hourly_in, 1),
        "heavy_potential": max_hourly_in >= TRAINING_STORM_HOURLY_THRESHOLD_IN,
    }


def _coverage_label(pct):
    if pct is None:
        return "coverage estimate unavailable"
    if pct >= 60:
        return f"widespread, {pct}% of the corridor"
    if pct >= 40:
        return f"scattered to widespread, {pct}% of the corridor"
    if pct >= 10:
        return f"isolated, {pct}% of the corridor"
    return f"mostly dry, {pct}% of the corridor"


def fetch_ndfd_qpf_summary():
    summaries = []
    for office_name, url in NDFD_OFFICES.items():
        cache_buster = int(time.time())
        data = w._fetch_with_retries_bytes(f"{url}?_cb={cache_buster}", f"NDFD:{office_name}")
        if not data:
            continue
        try:
            parsed = json.loads(data.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        qpf = parsed.get("properties", {}).get("quantitativePrecipitation", {}).get("values", [])
        total_in = 0.0
        for entry in qpf[:28]:
            val = entry.get("value")
            if val is not None:
                total_in += val / 25.4
        summaries.append(f"{office_name} NDFD: ~{round(total_in, 1)}\" over 7 days")
    return summaries or None


def build_setx_swla_section(outlook):
    if not outlook:
        return ["- Local SETX/SWLA rainfall data unavailable this cycle."]
    lines = []
    lines.append("Short term (day of + 2-3 days):")
    parts = []
    if outlook["short_gfs_in"] is not None:
        parts.append(f"GFS {outlook['short_gfs_in']}\"")
    if outlook["short_euro_in"] is not None:
        parts.append(f"Euro {outlook['short_euro_in']}\"")
    lines.append("- " + (", ".join(parts) if parts else "data unavailable") + " across the Houston-Beaumont-Port Arthur-Jasper-Lake Charles corridor")
    lines.append(f"- Coverage: {_coverage_label(outlook['coverage_pct'])}")
    lines.append("Medium term (3-5 days):")
    parts = []
    if outlook["medium_gfs_in"] is not None:
        parts.append(f"GFS {outlook['medium_gfs_in']}\"")
    if outlook["medium_euro_in"] is not None:
        parts.append(f"Euro {outlook['medium_euro_in']}\"")
    lines.append("- " + (", ".join(parts) if parts else "data unavailable"))
    lines.append("Longer term (5+ days):")
    if outlook["long_confidence_pct"] is not None:
        wet_or_dry = "rain chances" if outlook["long_confidence_pct"] >= 40 else "mainly dry"
        lines.append(f"- {wet_or_dry}, confidence ~{outlook['long_confidence_pct']}% (model agreement across the corridor)")
    else:
        lines.append("- longer-range signal unavailable this cycle")
    if outlook["heavy_potential"]:
        lines.append(f"- Heavy rain potential: YES -- up to {outlook['max_hourly_in']}\"/hr somewhere in the corridor (GFS/Euro/HRRR), watch for training storms/localized flooding")
    else:
        lines.append("- Heavy rain potential: nothing pointing to 1\"+/hr training storms right now")
    return lines


# --- Front / dewpoint / wind-shift signal watch ---------------------
# Uses Euro for temperature trends (per instruction, same as Euro for
# precip), GFS to cross-check. Flags things plainly -- no forecast
# discussion, just what the models are showing.

DEWPOINT_DROP_THRESHOLD_F = 15.0
TEMP_DROP_THRESHOLD_F = 8.0
NORTHERLY_MIN_DEG = 315
NORTHERLY_MAX_DEG = 45


def _c_to_f(c):
    return c * 9 / 5 + 32


def _is_northerly(deg):
    return deg >= NORTHERLY_MIN_DEG or deg <= NORTHERLY_MAX_DEG


def fetch_front_signal():
    """Checks Euro (primary) and GFS (cross-check) hourly dewpoint,
    wind direction, and temperature across the SETX/SWLA corridor over
    the next 4 days for a sharp dewpoint drop, a shift to northerly
    winds, and/or a notable temperature drop -- plain signals that a
    front is pushing in, not a full forecast discussion."""
    lat_str = ",".join(str(p[0]) for p in SETX_SWLA_POINTS)
    lon_str = ",".join(str(p[1]) for p in SETX_SWLA_POINTS)
    cache_buster = int(time.time())

    def fetch(endpoint):
        url = (
            f"https://api.open-meteo.com/v1/{endpoint}"
            f"?latitude={lat_str}&longitude={lon_str}"
            f"&hourly=temperature_2m,dewpoint_2m,wind_direction_10m"
            f"&forecast_days=4&temperature_unit=fahrenheit&_cb={cache_buster}"
        )
        data = w._fetch_with_retries_bytes(url, f"FrontSignal:{endpoint}")
        if not data:
            return None
        try:
            points = json.loads(data.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None
        return points if isinstance(points, list) else None

    euro_points = fetch("ecmwf")
    gfs_points = fetch("gfs")

    dewpoint_drop = 0.0
    temp_drop = 0.0
    shift_to_north = False

    for points in (euro_points, gfs_points):
        if not points:
            continue
        for point in points:
            hourly = point.get("hourly", {})
            dp = [v for v in hourly.get("dewpoint_2m", []) if v is not None]
            temp = [v for v in hourly.get("temperature_2m", []) if v is not None]
            wdir = hourly.get("wind_direction_10m", [])

            if len(dp) >= 25:
                for i in range(len(dp) - 24):
                    drop = dp[i] - dp[i + 24]
                    dewpoint_drop = max(dewpoint_drop, drop)
            if len(temp) >= 25:
                for i in range(len(temp) - 24):
                    drop = temp[i] - temp[i + 24]
                    temp_drop = max(temp_drop, drop)
            if wdir:
                early = [d for d in wdir[:12] if d is not None]
                later = [d for d in wdir[-24:] if d is not None]
                if early and later:
                    early_north = sum(1 for d in early if _is_northerly(d)) / len(early)
                    later_north = sum(1 for d in later if _is_northerly(d)) / len(later)
                    if later_north >= 0.5 and early_north < 0.3:
                        shift_to_north = True

    if euro_points is None and gfs_points is None:
        return None

    return {
        "dewpoint_drop_f": round(dewpoint_drop, 1),
        "temp_drop_f": round(temp_drop, 1),
        "shift_to_north": shift_to_north,
        "front_signal": dewpoint_drop >= DEWPOINT_DROP_THRESHOLD_F or shift_to_north,
        "cooling_signal": temp_drop >= TEMP_DROP_THRESHOLD_F,
    }


def build_front_signal_section(signal):
    if not signal:
        return None
    lines = []
    if signal["front_signal"]:
        lines.append("FRONT WATCH:")
        if signal["dewpoint_drop_f"] >= DEWPOINT_DROP_THRESHOLD_F:
            lines.append(f"- Sharp dewpoint drop: models show up to {signal['dewpoint_drop_f']}F drop within 24h somewhere in the corridor")
        if signal["shift_to_north"]:
            lines.append("- Winds shifting more out of the north over the period -- sign of a front pushing through")
    if signal["cooling_signal"]:
        lines.append(f"- Temperature trend (Euro): up to {signal['temp_drop_f']}F cooler within 24h somewhere in the corridor")
    return lines or None


# --- Trending (compares last 3 cycles) -------------------------------
# Sent as a short, separate message right after the main cycle update,
# per instruction -- only calls out what's actually changed.

TREND_HISTORY_KEY = "trend_snapshots"
TREND_RAIN_CHANGE_THRESHOLD_IN = 0.3
TREND_COVERAGE_CHANGE_THRESHOLD_PCT = 15
TREND_TEMP_CHANGE_THRESHOLD_F = 5.0
TREND_PRESSURE_CHANGE_THRESHOLD_MB = 3.0


def build_trend_snapshot(setx_swla_outlook, front_signal, gfs_scan, ecmwf_scan):
    """A compact snapshot of the key numbers from this cycle, stored so
    the next 1-2 cycles can be compared against it."""
    snapshot = {}
    if setx_swla_outlook:
        snapshot["short_rain_in"] = setx_swla_outlook.get("short_gfs_in")
        snapshot["coverage_pct"] = setx_swla_outlook.get("coverage_pct")
    if front_signal:
        snapshot["temp_drop_f"] = front_signal.get("temp_drop_f")
    lowest_mb = None
    for scan in (gfs_scan, ecmwf_scan):
        if scan and scan.get("results"):
            best = min(scan["results"], key=lambda r: r["mslp_mb"])
            if lowest_mb is None or best["mslp_mb"] < lowest_mb:
                lowest_mb = best["mslp_mb"]
    if lowest_mb is not None:
        snapshot["tropical_lowest_mb"] = lowest_mb
    return snapshot


def build_trending_message(cycle_hour_utc, history):
    """history is a list of up to 3 snapshots, newest first (index 0 =
    this cycle, 1 = previous, 2 = the one before that). Only flags what
    actually changed, per instruction -- otherwise says so plainly."""
    if len(history) < 2:
        return f"Trending ({cycle_hour_utc:02d}Z): Not enough prior cycles yet to compare."

    current = history[0]
    previous = history[1]
    notes = []

    cur_rain = current.get("short_rain_in")
    prev_rain = previous.get("short_rain_in")
    if cur_rain is not None and prev_rain is not None:
        diff = cur_rain - prev_rain
        if diff >= TREND_RAIN_CHANGE_THRESHOLD_IN:
            notes.append(f"Trending: Rainfall totals increasing over recent runs (+{round(diff,1)}\").")
        elif diff <= -TREND_RAIN_CHANGE_THRESHOLD_IN:
            notes.append(f"Trending: Rainfall totals decreasing over recent runs ({round(diff,1)}\").")

    cur_cov = current.get("coverage_pct")
    prev_cov = previous.get("coverage_pct")
    if cur_cov is not None and prev_cov is not None:
        diff = cur_cov - prev_cov
        if diff >= TREND_COVERAGE_CHANGE_THRESHOLD_PCT:
            notes.append(f"Trending: Storm coverage expanding vs previous runs (+{diff}%).")
        elif diff <= -TREND_COVERAGE_CHANGE_THRESHOLD_PCT:
            notes.append(f"Trending: Storm coverage shrinking vs previous runs ({diff}%).")

    cur_temp_drop = current.get("temp_drop_f")
    prev_temp_drop = previous.get("temp_drop_f")
    if cur_temp_drop is not None and prev_temp_drop is not None:
        diff = cur_temp_drop - prev_temp_drop
        if diff >= TREND_TEMP_CHANGE_THRESHOLD_F:
            notes.append("Trending: Stronger cold front signal -- temperatures trending colder run over run.")

    cur_mb = current.get("tropical_lowest_mb")
    prev_mb = previous.get("tropical_lowest_mb")
    if cur_mb is not None and prev_mb is not None:
        diff = prev_mb - cur_mb  # positive = deepening
        if diff >= TREND_PRESSURE_CHANGE_THRESHOLD_MB:
            notes.append(f"Trending: Tropical signal deepening/more organized vs previous runs (-{round(diff,1)} mb).")
        elif diff <= -TREND_PRESSURE_CHANGE_THRESHOLD_MB:
            notes.append(f"Trending: Tropical signal weakening/less organized vs previous runs (+{round(-diff,1)} mb).")
    elif cur_mb is not None and prev_mb is None:
        notes.append("Trending: New tropical signal showing up that wasn't there last run.")

    if not notes:
        return f"Trending ({cycle_hour_utc:02d}Z): No significant change from previous runs."

    return f"Trending ({cycle_hour_utc:02d}Z):\n" + "\n".join(notes)
