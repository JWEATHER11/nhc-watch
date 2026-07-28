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
