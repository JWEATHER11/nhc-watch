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
    # HRRR only extends ~48h, so "today + tomorrow" is its full useful
    # range here, per instruction.
    short_hrrr = bucket_totals(hrrr_points, 0, 2)

    def coverage_word_for(points, day_start, day_end):
        hits = 0
        total = 0
        if not points:
            return None
        for point in points:
            daily_mm = point.get("daily", {}).get("precipitation_sum", [])
            days = [v for v in daily_mm[day_start:day_end] if v is not None]
            if days:
                total += 1
                if sum(days) >= 2.5:
                    hits += 1
        if total == 0:
            return None
        pct = round(100 * hits / total)
        return _coverage_word(pct)

    hrrr_coverage_word = coverage_word_for(hrrr_points, 0, 2)

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

    # Explicit 1-3" and 1-5" additional-rainfall check for days 5-10,
    # per instruction -- a plain yes/no rather than just a percentage.
    max_long_total_in = 0.0
    if gfs_points:
        for point in gfs_points:
            daily_mm = point.get("daily", {}).get("precipitation_sum", [])
            days = [v for v in daily_mm[5:10] if v is not None]
            if days:
                max_long_total_in = max(max_long_total_in, sum(days) / 25.4)
    sees_1in_plus = max_long_total_in >= 1.0
    sees_3in_plus = max_long_total_in >= 3.0
    sees_5in_plus = max_long_total_in >= 5.0

    def coverage_for_range(day_start, day_end):
        points_hit = 0
        points_total = 0
        if gfs_points:
            for point in gfs_points:
                daily_mm = point.get("daily", {}).get("precipitation_sum", [])
                days = [v for v in daily_mm[day_start:day_end] if v is not None]
                if days:
                    points_total += 1
                    if sum(days) >= 2.5:
                        points_hit += 1
        return round(100 * points_hit / points_total) if points_total else None

    coverage_pct = coverage_for_range(0, 3)
    medium_coverage_pct = coverage_for_range(3, 5)
    long_coverage_pct = coverage_for_range(5, 10)

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
        "medium_coverage_pct": medium_coverage_pct,
        "long_coverage_pct": long_coverage_pct,
        "max_hourly_in": round(max_hourly_in, 1),
        "heavy_potential": max_hourly_in >= TRAINING_STORM_HOURLY_THRESHOLD_IN,
        "short_hrrr_in": short_hrrr,
        "hrrr_coverage_word": hrrr_coverage_word,
        "max_long_total_in": round(max_long_total_in, 1),
        "sees_1in_plus": sees_1in_plus,
        "sees_3in_plus": sees_3in_plus,
        "sees_5in_plus": sees_5in_plus,
    }


def _coverage_word(pct):
    """Plain-language coverage word only -- isolated / scattered /
    numerous / widespread, per instruction (no percentages for medium
    and longer term)."""
    if pct is None:
        return None
    if pct >= 70:
        return "widespread"
    if pct >= 50:
        return "numerous"
    if pct >= 30:
        return "scattered"
    if pct >= 10:
        return "isolated"
    return "mostly dry"


def _coverage_label(pct):
    """Short-term coverage: word AND percentage, per instruction."""
    word = _coverage_word(pct)
    if word is None:
        return "coverage estimate unavailable"
    return f"{word}, {pct}% of the corridor"


def _long_range_pattern(coverage_pct):
    """Descriptive longer-term pattern phrase instead of a raw
    confidence percentage, per instruction."""
    if coverage_pct is None:
        return "longer-range signal unavailable this cycle"
    if coverage_pct >= 60:
        return "Longer term looks more active with higher rain chances"
    if coverage_pct >= 35:
        return "Longer term is trending toward a wetter pattern"
    if coverage_pct >= 15:
        return "Longer term remains quiet with limited rain chances"
    return "Longer term continues to look mostly dry"


# Rounds to the nearest 0.5" for comparison, per instruction -- tiny
# fluctuations between cycles shouldn't count as a "meaningful change."
NDFD_MEANINGFUL_CHANGE_IN = 0.5
NDFD_MAX_SENDS_PER_DAY = 2


def fetch_ndfd_qpf_totals():
    """Returns {office_name: total_in} -- raw numbers, used both for
    display and for comparing cycle-to-cycle for meaningful change."""
    totals = {}
    for office_name, url in NDFD_OFFICES.items():
        # NOTE: api.weather.gov strictly rejects unrecognized query
        # params (verified live: appending the usual "?_cb=" cache
        # buster returns a hard 400 Bad Request) -- this was silently
        # breaking every single NDFD fetch. No cache buster here;
        # unlike Open-Meteo, this is a live API response, not
        # something that needs busting.
        data = w._fetch_with_retries_bytes(url, f"NDFD:{office_name}")
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
        totals[office_name] = round(total_in, 1)
    return totals or None


NWS_FORECAST_LINKS = {
    "NWS Houston": "https://forecast.weather.gov/MapClick.php?lat=29.76&lon=-95.37",
    "NWS Lake Charles": "https://forecast.weather.gov/MapClick.php?lat=30.23&lon=-93.22",
}


def fetch_ndfd_qpf_by_range():
    """Same NDFD QPF source as fetch_ndfd_qpf_totals, but bucketed by
    local Beaumont-time day into today+tomorrow and days 3-5, per
    instruction -- so those specific ranges can show NWS's own numbers
    alongside the model numbers, not just one flat multi-day total."""
    import datetime as _dt
    now_local = _dt.datetime.now(w.BEAUMONT_TZ)
    today_date = now_local.date()

    today_tomorrow = {}
    days_3_5 = {}
    for office_name, url in NDFD_OFFICES.items():
        data = w._fetch_with_retries_bytes(url, f"NDFD_range:{office_name}")
        if not data:
            continue
        try:
            parsed = json.loads(data.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        qpf = parsed.get("properties", {}).get("quantitativePrecipitation", {}).get("values", [])
        tt_total, d35_total = 0.0, 0.0
        for entry in qpf:
            val = entry.get("value")
            valid_time = entry.get("validTime", "")
            if val is None or "/" not in valid_time:
                continue
            try:
                start_dt = _dt.datetime.fromisoformat(valid_time.split("/")[0]).astimezone(w.BEAUMONT_TZ)
            except ValueError:
                continue
            day_offset = (start_dt.date() - today_date).days
            if 0 <= day_offset <= 1:
                tt_total += val / 25.4
            elif 3 <= day_offset <= 5:
                d35_total += val / 25.4
        today_tomorrow[office_name] = round(tt_total, 1)
        days_3_5[office_name] = round(d35_total, 1)
    return (today_tomorrow or None), (days_3_5 or None)


def describe_ndfd_change(current_totals, previous_totals, send_count_today):
    """Decides whether the NWS comparison is actually worth sending,
    per instruction -- strict: only when something meaningfully changed,
    and capped at NDFD_MAX_SENDS_PER_DAY total for the day. Returns
    (should_send, message_lines, updated_totals_to_store)."""
    if not current_totals:
        return False, None, previous_totals

    if send_count_today >= NDFD_MAX_SENDS_PER_DAY:
        return False, None, current_totals

    if not previous_totals:
        lines = [f"{office}: ~{total}\" over 7 days" for office, total in current_totals.items()]
        return True, lines, current_totals

    changes = []
    for office, total in current_totals.items():
        prev = previous_totals.get(office)
        if prev is None:
            changes.append(f"{office}: new data available, ~{total}\" over 7 days")
        elif abs(total - prev) >= NDFD_MEANINGFUL_CHANGE_IN:
            direction = "up" if total > prev else "down"
            changes.append(f"{office}: {direction} from {prev}\" to {total}\" over 7 days")

    if not changes:
        return False, None, current_totals
    return True, changes, current_totals


PEAK_DAY_CALLOUT_THRESHOLD_PCT = 20
WEEKDAY_NAMES_FULL = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def _peak_day_note(seven_day):
    """Plain fact, not commentary -- just finds the day with the
    highest already-computed rain%, per instruction. Only called out
    when it's a real standout (>= threshold), so a quiet week with
    every day near 0% doesn't get a pointless 'best chance' line."""
    if not seven_day:
        return None
    import datetime as _dt
    now_local = _dt.datetime.now(w.BEAUMONT_TZ)
    best_idx, best_pct = None, -1
    for i, d in enumerate(seven_day):
        if d and d["rain_pct"] > best_pct:
            best_idx, best_pct = i, d["rain_pct"]
    if best_idx is None or best_pct < PEAK_DAY_CALLOUT_THRESHOLD_PCT:
        return None
    day_date = now_local + _dt.timedelta(days=best_idx)
    label = "Today" if best_idx == 0 else WEEKDAY_NAMES_FULL[day_date.weekday()]
    return f"Best chance for coverage/heavier rain this week: {label} (~{best_pct}%)"


SETX_SWLA_WPC_KEYWORDS = [
    "HOUSTON", "GALVESTON", "BEAUMONT", "PORT ARTHUR", "ORANGE",
    "JASPER", "LAKE CHARLES", "SOUTHEAST TEXAS", "SOUTHWEST LOUISIANA",
    "SETX", "SWLA", "GOLDEN TRIANGLE", "SE TEXAS", "SW LOUISIANA",
]


def fetch_setx_swla_wpc_corroboration():
    """If WPC's Excessive Rainfall Discussion carries a Moderate or
    High risk headline (not narrative text -- see WPC_HEADLINE_RE)
    that names our own SETX/SWLA region, regardless of which day
    range it falls in, per instruction: attach NWS Houston/Lake
    Charles' own totals and a link to their forecast. Only when WPC
    actually flags this specific local region -- never unconditionally.
    NDFD totals are only fetched if a match is actually found, so a
    quiet cycle (the common case) doesn't pay for an extra fetch."""
    try:
        blocks = w._fetch_wpc_day_blocks()
    except Exception as e:
        print(f"[SETX/SWLA WPC check] unavailable (non-fatal): {e}")
        return None
    if not blocks:
        return None
    for days, block_text in blocks:
        m = w.WPC_HEADLINE_RE.search(block_text.upper())
        if not m:
            continue
        risk_word, region_text = m.group(1), m.group(2)
        if risk_word not in ("MODERATE", "HIGH"):
            continue
        if not any(kw in region_text for kw in SETX_SWLA_WPC_KEYWORDS):
            continue
        day_str = "/".join(f"Day {d}" for d in days)
        lines = [f"WPC {day_str}: {risk_word.title()} risk of excessive rainfall for SETX/SWLA -- watch closely"]
        ndfd_totals = fetch_ndfd_qpf_totals()
        if ndfd_totals:
            for office, total in ndfd_totals.items():
                link = NWS_FORECAST_LINKS.get(office)
                lines.append(f"  {office}: {total}\" over 7 days" + (f" -- {link}" if link else ""))
        return lines
    return None


def build_setx_swla_section(outlook, seven_day=None, hrrr_grid_lines=None, ndfd_today_tomorrow=None, ndfd_days_3_5=None, wpc_setx_swla_note=None):
    if not outlook:
        return ["- Local SETX/SWLA rainfall data unavailable this cycle."]
    lines = []

    # Today + tomorrow (HRRR's real range): pulled from the 7-Day
    # Forecast's own day 0/1 entries, which already use the dense 9x9
    # grid with HRRR weighted 65% / Euro 35%, per instruction -- must
    # use HRRR on the large grid so a small storm can't fall entirely
    # between points, and this avoids a second, different-grid answer
    # for the same two days.
    lines.append("<b>Today & Tomorrow</b>")
    if seven_day and seven_day[0] and seven_day[1]:
        d0, d1 = seven_day[0], seven_day[1]
        avg_pct = round((d0["rain_pct"] + d1["rain_pct"]) / 2)
        if avg_pct < 10:
            lines.append("- Dry -- HRRR/Euro blend shows mostly dry conditions across the corridor today and tomorrow.")
        else:
            cov = _coverage_word(avg_pct) or "some chances"
            lines.append(f"- {cov.capitalize()} -- HRRR/Euro blend shows ~{avg_pct}% coverage today and tomorrow.")
    else:
        lines.append("- Data unavailable this cycle.")
    if hrrr_grid_lines:
        lines.extend(hrrr_grid_lines)
    if ndfd_today_tomorrow:
        nws_parts = [f"{office} {total}\"" for office, total in ndfd_today_tomorrow.items()]
        lines.append("- NWS: " + ", ".join(nws_parts))
    lines.append("")

    # Days 3-5: pulled from the same dense-grid 7-Day Forecast (day
    # indices 2-4), per instruction -- same reasoning as above, this
    # used to come from the old sparse 5-point grid.
    lines.append("<b>Days 3-5</b>")
    mid_days = [d for d in (seven_day[2:5] if seven_day and len(seven_day) >= 5 else []) if d]
    if mid_days:
        avg_pct = round(sum(d["rain_pct"] for d in mid_days) / len(mid_days))
        cov = _coverage_word(avg_pct) or "mostly dry"
        parts = []
        if outlook["medium_euro_in"] is not None:
            parts.append(f"Euro {outlook['medium_euro_in']}\"")
        if outlook["medium_gfs_in"] is not None:
            parts.append(f"GFS {outlook['medium_gfs_in']}\"")
        model_str = (", ".join(parts) + " -- " if parts else "")
        lines.append(f"- {model_str}{cov} (~{avg_pct}% coverage, dense grid)")
    else:
        lines.append("- Data unavailable this cycle.")
    if ndfd_days_3_5:
        nws_parts = [f"{office} {total}\"" for office, total in ndfd_days_3_5.items()]
        lines.append("- NWS: " + ", ".join(nws_parts))
    lines.append("")

    lines.append("<b>Day 5+</b>")
    lines.append("- " + _long_range_pattern(outlook.get("long_coverage_pct")))
    if outlook.get("max_long_total_in") is not None:
        if outlook.get("sees_1in_plus"):
            if outlook.get("sees_5in_plus"):
                tier = "5\"+"
            elif outlook.get("sees_3in_plus"):
                tier = "3\"+"
            else:
                tier = "1\"+"
            lines.append(f"- Heavier totals possible beyond day 5 -- up to {outlook['max_long_total_in']}\" somewhere in the corridor, {tier} potential")
        else:
            lines.append(f"- Additional rainfall beyond day 5 looks light -- up to {outlook['max_long_total_in']}\" somewhere in the corridor, nothing pointing to 1\"+ totals")
    if outlook["heavy_potential"]:
        lines.append(f"- Heavy rain potential: YES -- up to {outlook['max_hourly_in']}\"/hr somewhere in the corridor (Euro/HRRR/GFS), watch for training storms/localized flooding")
    else:
        lines.append("- Heavy rain potential: nothing pointing to 1\"+/hr training storms right now")

    peak_note = _peak_day_note(seven_day)
    if peak_note:
        lines.append("")
        lines.append(f"- {peak_note}")

    if wpc_setx_swla_note:
        lines.append("")
        lines.extend(wpc_setx_swla_note)
    return lines


# --- Front / dewpoint / wind-shift signal watch ---------------------
# Uses Euro for temperature trends (per instruction, same as Euro for
# precip), GFS to cross-check. Flags things plainly -- no forecast
# discussion, just what the models are showing.

DEWPOINT_DROP_THRESHOLD_F = 15.0
TEMP_DROP_THRESHOLD_F = 8.0
NORTHERLY_MIN_DEG = 315
NORTHERLY_MAX_DEG = 45
# A front should show up across MOST of the region, not just one
# model blip at one point -- per instruction, requires at least this
# fraction of the corridor points to agree before flagging anything.
FRONT_AGREEMENT_FRACTION = 0.6


def _c_to_f(c):
    return c * 9 / 5 + 32


def _is_northerly(deg):
    return deg >= NORTHERLY_MIN_DEG or deg <= NORTHERLY_MAX_DEG


def _format_front_time(iso_str):
    """Converts an hourly UTC timestamp into a plain 'today/tomorrow/
    Weekday around HH AM/PM' label in Beaumont time, per instruction --
    the raw signal only ever reported a magnitude, never when to
    actually expect it."""
    if not iso_str:
        return None
    import datetime as _dt
    try:
        dt_utc = _dt.datetime.fromisoformat(iso_str).replace(tzinfo=_dt.timezone.utc)
    except ValueError:
        return None
    dt_local = dt_utc.astimezone(w.BEAUMONT_TZ)
    now_local = _dt.datetime.now(w.BEAUMONT_TZ)
    day_diff = (dt_local.date() - now_local.date()).days
    if day_diff == 0:
        day_label = "today"
    elif day_diff == 1:
        day_label = "tomorrow"
    else:
        weekday_names = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
        day_label = weekday_names[dt_local.weekday()]
    return f"{day_label} around {dt_local.strftime('%I %p').lstrip('0')}"


def fetch_front_signal():
    """Checks Euro (primary) hourly dewpoint, wind direction, and
    temperature at EACH corridor point separately (Beaumont/Port
    Arthur, Houston, Jasper, Lake Charles, per SETX_SWLA_POINTS) over
    the next 4 days. Only flags a front/cooling signal when a real
    majority of the points agree -- a single point showing a swing is
    normal model noise, not a regional signal, per instruction. Also
    tracks the actual local day/time the sharpest drop happens, and
    what fraction of points agree, per instruction -- a bare
    yes/no + magnitude doesn't say when to expect it or how strong the
    regional agreement really is."""
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
    if not euro_points:
        return None

    n_points = len(euro_points)
    dewpoint_drop_points = 0
    temp_drop_points = 0
    north_shift_points = 0
    max_dewpoint_drop = 0.0
    max_temp_drop = 0.0
    dewpoint_drop_time = None
    temp_drop_time = None
    wind_shift_time = None

    for point in euro_points:
        hourly = point.get("hourly", {})
        times = hourly.get("time", [])
        dp = hourly.get("dewpoint_2m", [])
        temp = hourly.get("temperature_2m", [])
        wdir = hourly.get("wind_direction_10m", [])

        # NOTE: indices must line up with `times` -- do NOT filter out
        # None values before this loop, or the index-to-time mapping
        # silently shifts and points at the wrong hour entirely.
        point_dp_drop, point_dp_idx = 0.0, None
        for i in range(len(dp) - 24):
            if dp[i] is None or dp[i + 24] is None:
                continue
            drop = dp[i] - dp[i + 24]
            if drop > point_dp_drop:
                point_dp_drop, point_dp_idx = drop, i + 24
        if point_dp_drop >= DEWPOINT_DROP_THRESHOLD_F:
            dewpoint_drop_points += 1
        if point_dp_drop > max_dewpoint_drop:
            max_dewpoint_drop = point_dp_drop
            if point_dp_idx is not None and point_dp_idx < len(times):
                dewpoint_drop_time = times[point_dp_idx]

        point_temp_drop, point_temp_idx = 0.0, None
        for i in range(len(temp) - 24):
            if temp[i] is None or temp[i + 24] is None:
                continue
            drop = temp[i] - temp[i + 24]
            if drop > point_temp_drop:
                point_temp_drop, point_temp_idx = drop, i + 24
        if point_temp_drop >= TEMP_DROP_THRESHOLD_F:
            temp_drop_points += 1
        if point_temp_drop > max_temp_drop:
            max_temp_drop = point_temp_drop
            if point_temp_idx is not None and point_temp_idx < len(times):
                temp_drop_time = times[point_temp_idx]

        if wdir and times:
            early = [d for d in wdir[:12] if d is not None]
            later_vals, later_times = wdir[-24:], times[-24:]
            later = [d for d in later_vals if d is not None]
            if early and later:
                early_north = sum(1 for d in early if _is_northerly(d)) / len(early)
                later_north = sum(1 for d in later if _is_northerly(d)) / len(later)
                if later_north >= 0.5 and early_north < 0.3:
                    north_shift_points += 1
                    if wind_shift_time is None:
                        for d, t in zip(later_vals, later_times):
                            if d is not None and _is_northerly(d):
                                wind_shift_time = t
                                break

    dewpoint_widespread = (dewpoint_drop_points / n_points) >= FRONT_AGREEMENT_FRACTION
    temp_widespread = (temp_drop_points / n_points) >= FRONT_AGREEMENT_FRACTION
    wind_widespread = (north_shift_points / n_points) >= FRONT_AGREEMENT_FRACTION

    return {
        "dewpoint_drop_f": round(max_dewpoint_drop, 1),
        "dewpoint_drop_time": _format_front_time(dewpoint_drop_time),
        "dewpoint_agreement_pct": round(100 * dewpoint_drop_points / n_points),
        "temp_drop_f": round(max_temp_drop, 1),
        "temp_drop_time": _format_front_time(temp_drop_time),
        "temp_agreement_pct": round(100 * temp_drop_points / n_points),
        "shift_to_north": wind_widespread,
        "wind_shift_time": _format_front_time(wind_shift_time),
        "front_signal": dewpoint_widespread or wind_widespread,
        "cooling_signal": temp_widespread,
    }


def build_front_signal_section(signal):
    if not signal:
        return None
    lines = []
    if signal["front_signal"]:
        lines.append("FRONT WATCH:")
        if signal["dewpoint_drop_f"] >= DEWPOINT_DROP_THRESHOLD_F:
            when = f", {signal['dewpoint_drop_time']}" if signal.get("dewpoint_drop_time") else ""
            lines.append(f"- Dewpoints dropping sharply and widely across the region -- up to {signal['dewpoint_drop_f']}F drop within 24h{when} ({signal.get('dewpoint_agreement_pct', 0)}% of corridor points agree)")
        if signal["shift_to_north"]:
            when = f" {signal['wind_shift_time']}" if signal.get("wind_shift_time") else ""
            lines.append(f"- Winds shifting more out of the north across most of the region{when} -- front pushing through")
    if signal["cooling_signal"]:
        when = f", {signal['temp_drop_time']}" if signal.get("temp_drop_time") else ""
        lines.append(f"- Meaningful, widespread cooling signal (Euro): up to {signal['temp_drop_f']}F within 24h{when} across most of the corridor ({signal.get('temp_agreement_pct', 0)}% of corridor points agree)")
    return lines or None


# --- Trending (compares last 3 cycles) -------------------------------
# Sent as a short, separate message right after the main cycle update,
# per instruction -- only calls out what's actually changed.

TREND_HISTORY_KEY = "trend_snapshots"
TREND_RAIN_CHANGE_THRESHOLD_IN = 0.3
TREND_COVERAGE_CHANGE_THRESHOLD_PCT = 15
TREND_TEMP_CHANGE_THRESHOLD_F = 5.0
TREND_PRESSURE_CHANGE_THRESHOLD_MB = 3.0


def build_trend_snapshot(setx_swla_outlook, front_signal, gfs_scan, ecmwf_scan, temp_buckets=None):
    """A compact snapshot of the key numbers from this cycle, stored so
    the last 3-4 cycles can be compared against each other, per
    instruction -- not just the immediately previous one. Rainfall
    trend is tracked from Euro specifically, per instruction."""
    snapshot = {}
    if setx_swla_outlook:
        snapshot["short_rain_in"] = setx_swla_outlook.get("short_euro_in")
        snapshot["medium_rain_in"] = setx_swla_outlook.get("medium_euro_in")
        snapshot["coverage_pct"] = setx_swla_outlook.get("coverage_pct")
        snapshot["medium_coverage_pct"] = setx_swla_outlook.get("medium_coverage_pct")
        snapshot["long_coverage_pct"] = setx_swla_outlook.get("long_coverage_pct")
    if front_signal:
        snapshot["temp_drop_f"] = front_signal.get("temp_drop_f")
        snapshot["front_signal"] = front_signal.get("front_signal")
    if temp_buckets:
        snapshot["short_temp_f"] = temp_buckets.get("short_temp_f")
        snapshot["medium_temp_f"] = temp_buckets.get("medium_temp_f")
        snapshot["temp_5_7_f"] = temp_buckets.get("temp_5_7_f")
        snapshot["temp_7_10_f"] = temp_buckets.get("temp_7_10_f")
        snapshot["avg_dewpoint_f"] = temp_buckets.get("avg_dewpoint_f")
    lowest_mb = None
    best_wind_mph = None
    for scan in (gfs_scan, ecmwf_scan):
        if scan and scan.get("results"):
            best = min(scan["results"], key=lambda r: r["mslp_mb"])
            if lowest_mb is None or best["mslp_mb"] < lowest_mb:
                lowest_mb = best["mslp_mb"]
                best_wind_mph = best.get("wind_mph")
    if lowest_mb is not None:
        snapshot["tropical_lowest_mb"] = lowest_mb
        snapshot["tropical_wind_mph"] = best_wind_mph
    return snapshot


def _trend_direction(current, previous, threshold, higher_word, lower_word):
    """Compares oldest vs newest in a list of values (ignoring None),
    returns a direction word or None if unchanged/insufficient data."""
    if current is None or previous is None:
        return None
    diff = current - previous
    if diff >= threshold:
        return higher_word
    if diff <= -threshold:
        return lower_word
    return None


def build_trending_message(cycle_hour_utc, history):
    """history is a list of up to 4 snapshots, newest first. Compares
    the newest against the OLDEST available in that window (2-4 runs),
    per instruction, covering rainfall/coverage by range, tropical
    trend, and temperature/front trend -- not just the single previous
    run."""
    if len(history) < 2:
        return f"Trending ({cycle_hour_utc:02d}Z): Not enough prior cycles yet to compare."

    newest = history[0]
    oldest = history[-1]  # oldest available within the last 2-4 runs
    notes = []

    # Rainfall & coverage, per range.
    short_dir = _trend_direction(newest.get("short_rain_in"), oldest.get("short_rain_in"), 0.2, "wetter", "drier")
    short_cov_dir = _trend_direction(newest.get("coverage_pct"), oldest.get("coverage_pct"), 15, "more coverage", "less coverage")
    if short_dir or short_cov_dir:
        bits = [b for b in (short_dir, short_cov_dir) if b]
        notes.append(f"Short-term trending {' and '.join(bits)} over the last {len(history)} runs.")

    medium_dir = _trend_direction(newest.get("medium_rain_in"), oldest.get("medium_rain_in"), 0.2, "wetter", "drier")
    medium_cov_dir = _trend_direction(newest.get("medium_coverage_pct"), oldest.get("medium_coverage_pct"), 15, "more coverage", "less coverage")
    if medium_dir or medium_cov_dir:
        bits = [b for b in (medium_dir, medium_cov_dir) if b]
        notes.append(f"Medium-term trending {' and '.join(bits)} over the last {len(history)} runs.")

    long_cov_dir = _trend_direction(newest.get("long_coverage_pct"), oldest.get("long_coverage_pct"), 15, "more active", "quieter")
    if long_cov_dir:
        notes.append(f"Longer-term trending {long_cov_dir} over the last {len(history)} runs.")

    # Tropical: pressure (deepening = lower mb = stronger) and wind together.
    cur_mb = newest.get("tropical_lowest_mb")
    prev_mb = oldest.get("tropical_lowest_mb")
    cur_wind = newest.get("tropical_wind_mph")
    prev_wind = oldest.get("tropical_wind_mph")
    if cur_mb is not None and prev_mb is not None:
        mb_diff = prev_mb - cur_mb  # positive = deepening
        wind_diff = (cur_wind - prev_wind) if (cur_wind is not None and prev_wind is not None) else 0
        if mb_diff >= 3.0 or wind_diff >= 10:
            notes.append(f"Tropical signal uptrending -- stronger/more organized than {len(history)} runs ago.")
        elif mb_diff <= -3.0 or wind_diff <= -10:
            notes.append(f"Tropical signal downtrending -- weaker/less organized than {len(history)} runs ago.")
    elif cur_mb is not None and prev_mb is None:
        notes.append("New tropical signal showing up that wasn't there a few runs ago.")

    # Temperature by range, per instruction -- short/medium/5-7/7-10 day.
    for key, label in (
        ("short_temp_f", "Short-term"),
        ("medium_temp_f", "Medium-term"),
        ("temp_5_7_f", "Days 5-7"),
        ("temp_7_10_f", "Days 7-10"),
    ):
        cur_t = newest.get(key)
        prev_t = oldest.get(key)
        temp_dir = _trend_direction(cur_t, prev_t, 3.0, "warmer", "colder")
        if temp_dir:
            notes.append(f"{label} temperatures trending {temp_dir} over the last {len(history)} runs.")

    cur_dp = newest.get("avg_dewpoint_f")
    prev_dp = oldest.get("avg_dewpoint_f")
    if cur_dp is not None and prev_dp is not None and (prev_dp - cur_dp) >= 3.0:
        notes.append(f"Dewpoints dropping over the last {len(history)} runs.")

    # Front trend -- includes the actual before/after numbers, per
    # instruction, not just a bare "stronger/weaker" sentence with
    # nothing behind it.
    cur_drop = newest.get("temp_drop_f")
    prev_drop = oldest.get("temp_drop_f")
    if cur_drop is not None and prev_drop is not None:
        diff = cur_drop - prev_drop
        if diff >= 3.0:
            notes.append(f"Stronger cold front signal developing over the last {len(history)} runs -- max temp drop up from {prev_drop}F to {cur_drop}F within a 24h window.")
        elif diff <= -3.0:
            notes.append(f"Cold front signal weakening over the last {len(history)} runs -- max temp drop down from {prev_drop}F to {cur_drop}F within a 24h window.")
    cur_front = newest.get("front_signal")
    prev_front = oldest.get("front_signal")
    if cur_front and not prev_front:
        notes.append(f"Front signal newly appearing in recent runs -- {cur_drop}F max temp drop within 24h now crossing the regional-agreement threshold.")
    elif prev_front and not cur_front:
        notes.append(f"Front signal fading in recent runs -- max temp drop down to {cur_drop}F, no longer widespread enough across the corridor.")

    if not notes:
        return f"Trending ({cycle_hour_utc:02d}Z): No significant trend -- rain, tropical, and temperature signals holding similar over the last {len(history)} runs."

    return f"Trending ({cycle_hour_utc:02d}Z):\n" + "\n".join(notes)


# --- Extra local conditions (temp/moisture/wind/timing/confidence) --
# Builds on the existing SETX/SWLA section -- per instruction, only
# included when the data actually adds value, not forced every time.

CITY_POINTS = {
    "Beaumont/Port Arthur": (29.99, -94.02),  # midpoint of the two
    "Houston": (29.76, -95.37),
    "Lake Charles": (30.23, -93.22),
}

MEANINGFUL_TEMP_DIFF_F = 4.0


# Extra points blended with Houston/Beaumont for a better high/low
# than a single city point, per instruction.
TEMP_BLEND_EXTRA_POINTS = {
    "Lumberton": (30.27, -94.20),
    "Silsbee": (30.34, -94.18),
}


def fetch_conditions_detail():
    """Highs/lows, heat index, dewpoint trend, wind, rain timing, and a
    simple pattern one-liner -- from Euro, for the local area. Highs/
    lows are blended across Houston-Beaumont-Lumberton-Silsbee, per
    instruction, not a single point."""
    all_points = {**CITY_POINTS, **TEMP_BLEND_EXTRA_POINTS}
    lat_str = ",".join(str(p[0]) for p in all_points.values())
    lon_str = ",".join(str(p[1]) for p in all_points.values())
    cache_buster = int(time.time())
    url = (
        f"https://api.open-meteo.com/v1/ecmwf"
        f"?latitude={lat_str}&longitude={lon_str}"
        f"&hourly=temperature_2m,apparent_temperature,dewpoint_2m,wind_speed_10m,wind_direction_10m,wind_gusts_10m,precipitation"
        f"&daily=temperature_2m_max,temperature_2m_min"
        f"&forecast_days=3&temperature_unit=fahrenheit&wind_speed_unit=mph&_cb={cache_buster}"
    )
    data = w._fetch_with_retries_bytes(url, "ConditionsDetail:ecmwf")
    if not data:
        return None
    try:
        points = json.loads(data.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(points, list) or len(points) < 3:
        return None

    names = list(all_points.keys())
    by_city = dict(zip(names, points))

    # Houston-Beaumont-Lumberton-Silsbee blend for the high/low, per
    # instruction, instead of a single Beaumont/Port Arthur point.
    blend_names = ["Houston", "Beaumont/Port Arthur", "Lumberton", "Silsbee"]
    blend_highs, blend_lows = [], []
    for name in blend_names:
        daily = by_city.get(name, {}).get("daily", {})
        h = daily.get("temperature_2m_max", [])
        l = daily.get("temperature_2m_min", [])
        if h and h[0] is not None:
            blend_highs.append(h[0])
        if l and l[0] is not None:
            blend_lows.append(l[0])
    today_high = round(sum(blend_highs) / len(blend_highs)) if blend_highs else None
    today_low = round(sum(blend_lows) / len(blend_lows)) if blend_lows else None

    bpt = by_city["Beaumont/Port Arthur"]

    diff_notes = []
    for city in ("Houston", "Lake Charles"):
        other_daily = by_city[city].get("daily", {})
        other_highs = other_daily.get("temperature_2m_max", [])
        if today_high is not None and other_highs and other_highs[0] is not None:
            diff = other_highs[0] - today_high
            if abs(diff) >= MEANINGFUL_TEMP_DIFF_F:
                warmer_cooler = "warmer" if diff > 0 else "cooler"
                diff_notes.append(f"{city} looks {abs(round(diff))}F {warmer_cooler}")

    hourly = bpt.get("hourly", {})
    apparent = [v for v in hourly.get("apparent_temperature", [])[:24] if v is not None]
    actual_temp = [v for v in hourly.get("temperature_2m", [])[:24] if v is not None]
    max_heat_index = None
    if apparent and actual_temp:
        peak_idx = actual_temp.index(max(actual_temp))
        if peak_idx < len(apparent) and apparent[peak_idx] - actual_temp[peak_idx] >= 5:
            max_heat_index = round(apparent[peak_idx])

    dp = [v for v in hourly.get("dewpoint_2m", []) if v is not None]
    dewpoint_trend = "steady"
    if len(dp) >= 48:
        first_day_avg = sum(dp[:24]) / 24
        second_day_avg = sum(dp[24:48]) / 24
        if first_day_avg - second_day_avg >= 8:
            dewpoint_trend = "dropping sharply"
        elif first_day_avg - second_day_avg >= 3:
            dewpoint_trend = "dropping"
        elif second_day_avg - first_day_avg >= 3:
            dewpoint_trend = "rising"

    wspd = [v for v in hourly.get("wind_speed_10m", [])[:24] if v is not None]
    wdir = [v for v in hourly.get("wind_direction_10m", [])[:24] if v is not None]
    wind_note = None
    if wspd and wdir:
        avg_speed = round(sum(wspd) / len(wspd))
        avg_dir = sum(wdir) / len(wdir)
        dir_label = _deg_to_compass(avg_dir)
        wind_note = f"{dir_label} around {avg_speed} mph"
        # Strongest gust anywhere across the corridor points over the
        # next 24h, per instruction -- not just Beaumont/Port Arthur's
        # single-point average, so a genuinely gusty spot elsewhere in
        # the area doesn't get averaged away to nothing.
        max_gust = 0.0
        for city_data in by_city.values():
            gusts = city_data.get("hourly", {}).get("wind_gusts_10m", [])[:24]
            for g in gusts:
                if g is not None and g > max_gust:
                    max_gust = g
        if max_gust > 0 and max_gust - avg_speed >= 5:
            wind_note += f", gusts up to {round(max_gust)} mph"

    precip = hourly.get("precipitation", [])[:48]
    rain_timing = _describe_rain_timing(precip)

    return {
        "today_high_f": round(today_high) if today_high is not None else None,
        "today_low_f": round(today_low) if today_low is not None else None,
        "diff_notes": diff_notes,
        "heat_index_f": max_heat_index,
        "dewpoint_trend": dewpoint_trend,
        "wind_note": wind_note,
        "rain_timing": rain_timing,
    }


def _deg_to_compass(deg):
    dirs = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
    idx = round(deg / 45) % 8
    return dirs[idx]


def _describe_rain_timing(precip_48h):
    if not precip_48h or all(v is None or v == 0 for v in precip_48h):
        return None
    morning = sum(v for v in precip_48h[6:12] if v is not None)
    afternoon = sum(v for v in precip_48h[12:18] if v is not None)
    evening = sum(v for v in precip_48h[18:24] if v is not None)
    overnight = sum(v for v in precip_48h[0:6] if v is not None) + sum(v for v in precip_48h[24:30] if v is not None)
    buckets = {"morning": morning, "afternoon": afternoon, "evening": evening, "overnight": overnight}
    total = sum(buckets.values())
    if total <= 0:
        return None
    best = max(buckets, key=lambda k: buckets[k])
    if buckets[best] / total >= 0.5:
        return f"mainly {best}"
    return "on and off through the day"


def build_pattern_oneliner(setx_swla_outlook, front_signal, gfs_scan, ecmwf_scan):
    """A short pattern description, only when there's a clear signal."""
    if front_signal and front_signal.get("front_signal"):
        return "Front approaching"
    lowest_mb = None
    for scan in (gfs_scan, ecmwf_scan):
        if scan and scan.get("results"):
            best = min(scan["results"], key=lambda r: r["mslp_mb"])
            if lowest_mb is None or best["mslp_mb"] < lowest_mb:
                lowest_mb = best["mslp_mb"]
    if lowest_mb is not None and lowest_mb < 1005:
        return "Deep tropical moisture nearby"
    if setx_swla_outlook:
        cov = setx_swla_outlook.get("coverage_pct")
        if cov is not None and cov <= 10:
            return "Ridge in control -- quiet and dry"
        if cov is not None and cov >= 40:
            return "Unsettled pattern -- rain chances each day"
    return None


def build_conditions_section(details, setx_swla_outlook, front_signal, gfs_scan, ecmwf_scan, temp_blend=None):
    if not details:
        return []
    lines = ["", "<b>\U0001F321️ Temperature / Wind / Other</b>"]
    if temp_blend and temp_blend.get("high_f") is not None:
        lines.append(f"- High {temp_blend['high_f']}F, low {temp_blend['low_f']}F (Houston-Beaumont-Lumberton-Silsbee blend)")
    elif details["today_high_f"] is not None or details["today_low_f"] is not None:
        hi = f"{details['today_high_f']}F" if details["today_high_f"] is not None else "n/a"
        lo = f"{details['today_low_f']}F" if details["today_low_f"] is not None else "n/a"
        extra = f" ({'; '.join(details['diff_notes'])})" if details["diff_notes"] else ""
        lines.append(f"- Houston/Beaumont area blend: high {hi}, low {lo}{extra}")
    if details["heat_index_f"] is not None:
        lines.append(f"- Heat index up to {details['heat_index_f']}F today")
    if details["dewpoint_trend"] != "steady":
        lines.append(f"- Dewpoints {details['dewpoint_trend']}")
    if details["wind_note"]:
        lines.append(f"- Wind: {details['wind_note']}")
    cov = setx_swla_outlook.get("coverage_pct") if setx_swla_outlook else None
    # Rain timing only shown when there's an actual meaningful chance
    # of rain, per instruction -- previously this came from a single
    # point's raw hourly trace, which could show "mainly overnight"
    # from a trace amount even when coverage was genuinely ~0%,
    # directly contradicting the "Chance of measurable rain" line
    # right below it.
    if details["rain_timing"] and cov is not None and cov >= 10:
        lines.append(f"- Rain timing: {details['rain_timing']}")
    if cov is not None:
        lines.append(f"- Chance of measurable rain: ~{cov}%")
        if setx_swla_outlook.get("heavy_potential"):
            lines.append("- Chance of 1\"+ in spots: elevated given current signal")
    pattern = build_pattern_oneliner(setx_swla_outlook, front_signal, gfs_scan, ecmwf_scan)
    if pattern:
        lines.append(f"- Pattern: {pattern}")
    return lines


# --- Organized line / widespread linear coverage detection ----------
# HRRR leads for short-term structure/timing, Euro as supporting
# guidance into medium term, per instruction. Points are ordered
# roughly west-to-east/north-to-south along the corridor so adjacent
# simultaneous hits look like a line moving through, not just random
# scattered hits.
LINE_ORDER = ["Houston", "Beaumont", "Port Arthur", "Jasper", "Lake Charles"]
LINE_POINT_COORDS = {
    "Houston": (29.76, -95.37),
    "Beaumont": (30.08, -94.10),
    "Port Arthur": (29.90, -93.94),
    "Jasper": (30.68, -93.99),
    "Lake Charles": (30.23, -93.22),
}
LINE_RAIN_RATE_THRESHOLD_MM = 2.0  # per-hour, roughly 0.08in/hr
LINE_MIN_ADJACENT_POINTS = 3


def fetch_organized_line_signal():
    """Checks HRRR (primary, short-term) and Euro (supporting,
    extends further out) hourly precip at each ordered corridor point
    for hours where several adjacent points hit meaningfully at the
    same time -- a practical proxy for an organized line/broken line
    moving through, versus isolated/scattered hits."""
    lat_str = ",".join(str(LINE_POINT_COORDS[name][0]) for name in LINE_ORDER)
    lon_str = ",".join(str(LINE_POINT_COORDS[name][1]) for name in LINE_ORDER)
    cache_buster = int(time.time())

    def fetch(endpoint, models_param=None, forecast_days=2):
        models_bit = f"&models={models_param}" if models_param else ""
        url = (
            f"https://api.open-meteo.com/v1/{endpoint}"
            f"?latitude={lat_str}&longitude={lon_str}"
            f"&hourly=precipitation&forecast_days={forecast_days}{models_bit}&_cb={cache_buster}"
        )
        data = w._fetch_with_retries_bytes(url, f"LineSignal:{endpoint}:{models_param or 'auto'}")
        if not data:
            return None
        try:
            points = json.loads(data.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None
        return points if isinstance(points, list) else None

    def check_line(points, source_label):
        if not points or len(points) != len(LINE_ORDER):
            return None
        hourly_series = [p.get("hourly", {}).get("precipitation", []) for p in points]
        max_len = min(len(s) for s in hourly_series) if hourly_series else 0
        best_adjacent = 0
        for hour in range(max_len):
            hits = [hourly_series[i][hour] is not None and hourly_series[i][hour] >= LINE_RAIN_RATE_THRESHOLD_MM for i in range(len(LINE_ORDER))]
            # longest run of adjacent True values
            run = 0
            best_run = 0
            for hit in hits:
                run = run + 1 if hit else 0
                best_run = max(best_run, run)
            best_adjacent = max(best_adjacent, best_run)
        if best_adjacent >= LINE_MIN_ADJACENT_POINTS:
            structure = "solid line" if best_adjacent >= len(LINE_ORDER) - 1 else "broken line"
            return {"source": source_label, "structure": structure, "adjacent_points": best_adjacent}
        return None

    hrrr_points = fetch("forecast", models_param="ncep_hrrr_conus", forecast_days=2)
    euro_points = fetch("ecmwf", forecast_days=5)

    hrrr_signal = check_line(hrrr_points, "HRRR")
    euro_signal = check_line(euro_points, "Euro")

    if not hrrr_signal and not euro_signal:
        return None
    return {"hrrr": hrrr_signal, "euro": euro_signal}


def build_organized_line_note(signal):
    if not signal:
        return None
    hrrr = signal.get("hrrr")
    euro = signal.get("euro")
    if hrrr:
        note = f"{hrrr['structure'].capitalize()} of showers/storms showing up in HRRR moving through the Houston-Beaumont-Port Arthur-Jasper-Lake Charles corridor"
        if euro:
            note += " -- Euro supports this continuing into the next couple days"
        return note
    if euro:
        return f"Euro shows a {euro['structure']} of showers/storms with wider coverage pushing across the corridor"
    return None


# --- Temperature gradient detection ----------------------------------
GRADIENT_THRESHOLD_F = 10.0


def fetch_temperature_gradient():
    """Checks for a real north-south temperature contrast across the
    corridor (Jasper representing the north end, Port Arthur/Lake
    Charles the south end) using Euro -- only meaningful when the
    spread is genuinely large, not just normal day-to-day variation."""
    lat_str = ",".join(str(p[0]) for p in SETX_SWLA_POINTS)
    lon_str = ",".join(str(p[1]) for p in SETX_SWLA_POINTS)
    cache_buster = int(time.time())
    url = (
        f"https://api.open-meteo.com/v1/ecmwf"
        f"?latitude={lat_str}&longitude={lon_str}"
        f"&daily=temperature_2m_max,temperature_2m_min"
        f"&forecast_days=3&temperature_unit=fahrenheit&_cb={cache_buster}"
    )
    data = w._fetch_with_retries_bytes(url, "TempGradient:ecmwf")
    if not data:
        return None
    try:
        points = json.loads(data.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(points, list) or len(points) < 5:
        return None

    # SETX_SWLA_POINTS order: Houston, Beaumont, Port Arthur, Jasper, Lake Charles
    jasper = points[3]
    south_points = [points[2], points[4]]  # Port Arthur, Lake Charles

    jasper_highs = jasper.get("daily", {}).get("temperature_2m_max", [])
    if not jasper_highs or jasper_highs[0] is None:
        return None

    south_highs = []
    for p in south_points:
        h = p.get("daily", {}).get("temperature_2m_max", [])
        if h and h[0] is not None:
            south_highs.append(h[0])
    if not south_highs:
        return None

    avg_south_high = sum(south_highs) / len(south_highs)
    gradient = avg_south_high - jasper_highs[0]

    if abs(gradient) < GRADIENT_THRESHOLD_F:
        return None
    return {"gradient_f": round(abs(gradient), 1), "warmer_side": "south" if gradient > 0 else "north"}


def build_temperature_gradient_note(gradient):
    if not gradient:
        return None
    if gradient["warmer_side"] == "south":
        return f"Strong temperature gradient setting up -- colder air to the north, warmer to the south, roughly {gradient['gradient_f']}F difference across the region"
    return f"Sharp temperature contrast developing near the region, about {gradient['gradient_f']}F colder to the south side of the corridor"


# --- Multi-bucket temperature trend (short/medium/5-7/7-10 day) -----
# Per instruction: Houston-Beaumont-Jasper-Lake Charles region,
# temperature trend broken into more granular day ranges than the
# basic front-signal check covers, plus a dewpoint trend value stored
# for run-to-run comparison.

def fetch_temperature_buckets():
    """Average corridor high temps (Euro) for four day ranges: short
    (0-3), medium (3-5), 5-7, and 7-10 days out -- plus an average
    dewpoint value for the next 3 days, all for trend comparison across
    runs (not a single-cycle report by itself)."""
    lat_str = ",".join(str(p[0]) for p in SETX_SWLA_POINTS)
    lon_str = ",".join(str(p[1]) for p in SETX_SWLA_POINTS)
    cache_buster = int(time.time())
    url = (
        f"https://api.open-meteo.com/v1/ecmwf"
        f"?latitude={lat_str}&longitude={lon_str}"
        f"&daily=temperature_2m_max&hourly=dewpoint_2m"
        f"&forecast_days=10&temperature_unit=fahrenheit&_cb={cache_buster}"
    )
    data = w._fetch_with_retries_bytes(url, "TempBuckets:ecmwf")
    if not data:
        return None
    try:
        points = json.loads(data.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(points, list):
        return None

    def bucket_avg(day_start, day_end):
        vals = []
        for point in points:
            highs = point.get("daily", {}).get("temperature_2m_max", [])
            days = [v for v in highs[day_start:day_end] if v is not None]
            if days:
                vals.append(sum(days) / len(days))
        return round(sum(vals) / len(vals), 1) if vals else None

    dp_vals = []
    for point in points:
        dp = point.get("hourly", {}).get("dewpoint_2m", [])[:72]
        dp_vals.extend(v for v in dp if v is not None)
    avg_dewpoint = round(sum(dp_vals) / len(dp_vals), 1) if dp_vals else None

    return {
        "short_temp_f": bucket_avg(0, 3),
        "medium_temp_f": bucket_avg(3, 5),
        "temp_5_7_f": bucket_avg(5, 7),
        "temp_7_10_f": bucket_avg(7, 10),
        "avg_dewpoint_f": avg_dewpoint,
    }


# --- Dense HRRR grid for the local corridor -------------------------
# A real grid (not just 5 city points) spanning Houston-Beaumont-Port
# Arthur-Jasper-Lake Charles, per instruction -- gives both a genuine
# broad-scale (average) total AND the isolated higher-end potential
# that a sparse set of city points would miss. HRRR is ~3km resolution
# so this is a meaningful improvement in what we can actually see.

# Bounding box roughly covering the corridor, with a bit of margin.
HRRR_GRID_LAT_MIN = 29.55
HRRR_GRID_LAT_MAX = 30.75
HRRR_GRID_LON_MIN = -95.55
HRRR_GRID_LON_MAX = -93.10
# 9x9 = 81 points, ~10.3mi x 18.3mi spacing -- roughly halves the
# previous 5x5 gap size, per instruction (denser grid so an isolated
# storm can't fall entirely between points). This grid is shared with
# the 7-day forecast's Euro fetch (7 days x this many points), which
# is the heaviest single request in the pipeline -- verified live that
# going denser (20x20=400 points) works fine for the 2-day HRRR-only
# fetch, but the same density at 7 days of Euro data hit Open-Meteo's
# rate limit. 9x9 is validated reliable for both use cases; Euro's own
# ~9-25km native resolution doesn't reveal more real detail past this
# density anyway, so this isn't a real loss for that portion.
HRRR_GRID_ROWS = 9
HRRR_GRID_COLS = 9


def _hrrr_grid_points():
    lat_step = (HRRR_GRID_LAT_MAX - HRRR_GRID_LAT_MIN) / (HRRR_GRID_ROWS - 1)
    lon_step = (HRRR_GRID_LON_MAX - HRRR_GRID_LON_MIN) / (HRRR_GRID_COLS - 1)
    points = []
    for r in range(HRRR_GRID_ROWS):
        for c in range(HRRR_GRID_COLS):
            lat = round(HRRR_GRID_LAT_MIN + r * lat_step, 3)
            lon = round(HRRR_GRID_LON_MIN + c * lon_step, 3)
            points.append((lat, lon))
    return points


def _nearest_city_label(lat, lon):
    """Rough plain-language location for a grid point, so 'isolated
    higher totals' can be described relative to a place name instead of
    raw coordinates."""
    best_name, best_dist = None, None
    for name, coords in {**CITY_POINTS, "Jasper": (30.68, -93.99), "Lake Charles": (30.23, -93.22)}.items():
        city_lat, city_lon = coords
        dist = ((lat - city_lat) ** 2 + (lon - city_lon) ** 2) ** 0.5
        if best_dist is None or dist < best_dist:
            best_dist, best_name = dist, name
    return best_name


def fetch_hrrr_grid_detail():
    """HRRR (today + tomorrow, its real range) across a dense
    {HRRR_GRID_ROWS}x{HRRR_GRID_COLS} grid over the corridor. Returns
    both the broad-scale average total and the single highest total
    found anywhere in the grid (the 'isolated higher area' signal),
    with an approximate nearby place name. Falls back to a GFS/Euro
    blend on the same grid if the HRRR mirror itself fails, so this
    section doesn't just silently disappear for the cycle."""
    grid_points = _hrrr_grid_points()
    lat_str = ",".join(str(p[0]) for p in grid_points)
    lon_str = ",".join(str(p[1]) for p in grid_points)
    cache_buster = int(time.time())

    def fetch_grid_points(models_param=None):
        models_bit = f"&models={models_param}" if models_param else ""
        url = (
            f"https://api.open-meteo.com/v1/forecast"
            f"?latitude={lat_str}&longitude={lon_str}"
            f"&daily=precipitation_sum{models_bit}"
            f"&forecast_days=2&_cb={cache_buster}"
        )
        data = w._fetch_with_retries_bytes(url, f"HRRRGrid:{models_param or 'hrrr'}")
        if not data:
            return None
        try:
            pts = json.loads(data.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None
        return pts if isinstance(pts, list) and len(pts) == len(grid_points) else None

    points = fetch_grid_points(models_param="ncep_hrrr_conus")
    source_label = "HRRR"
    if not points:
        gfs_points = fetch_grid_points(models_param="gfs_seamless")
        euro_points = fetch_grid_points(models_param="ecmwf_ifs025")
        if gfs_points and euro_points:
            points = []
            for gp, ep in zip(gfs_points, euro_points):
                g_daily = gp.get("daily", {}).get("precipitation_sum", [None, None])
                e_daily = ep.get("daily", {}).get("precipitation_sum", [None, None])
                blended = []
                for i in range(2):
                    gv = g_daily[i] if i < len(g_daily) else None
                    ev = e_daily[i] if i < len(e_daily) else None
                    if gv is not None and ev is not None:
                        blended.append((gv + ev) / 2)
                    else:
                        blended.append(gv if gv is not None else ev)
                points.append({"daily": {"precipitation_sum": blended}})
            source_label = "GFS/Euro (HRRR unavailable)"
        elif gfs_points or euro_points:
            points = gfs_points or euro_points
            source_label = "GFS (HRRR/Euro unavailable)" if gfs_points else "Euro (HRRR/GFS unavailable)"

    if not points:
        return None

    totals_in = []
    max_total = 0.0
    max_coords = None
    hit_count = 0
    for point, (glat, glon) in zip(points, grid_points):
        daily_mm = point.get("daily", {}).get("precipitation_sum", [])
        days = [v for v in daily_mm[:2] if v is not None]
        if not days:
            continue
        total_in = sum(days) / 25.4
        totals_in.append(total_in)
        if total_in >= 2.5:
            hit_count += 1
        if total_in > max_total:
            max_total = total_in
            max_coords = (glat, glon)

    if not totals_in:
        return None

    avg_total = round(sum(totals_in) / len(totals_in), 1)
    coverage_pct = round(100 * hit_count / len(totals_in))
    max_label = _nearest_city_label(*max_coords) if max_coords else None

    return {
        "avg_total_in": avg_total,
        "max_total_in": round(max_total, 1),
        "max_near": max_label,
        "coverage_pct": coverage_pct,
        "coverage_word": _coverage_word(coverage_pct),
        "source_label": source_label,
    }


def build_hrrr_grid_note(detail):
    if not detail:
        return None
    label = detail.get("source_label", "HRRR")
    lines = [f"- {label} grid (today + tomorrow): broad-scale ~{detail['avg_total_in']}\" average across the corridor, {_coverage_label(detail['coverage_pct'])}"]
    if detail["max_total_in"] > detail["avg_total_in"] + 0.3:
        near = f" near {detail['max_near']}" if detail["max_near"] else ""
        lines.append(f"- Isolated higher totals possible: up to {detail['max_total_in']}\"{near} in the {label} grid")
    return lines


# --- 7-Day Forecast section ------------------------------------------
# HRRR for the short-term days (its real range), Euro for the rest,
# per instruction. Grid points: Beaumont, Houston, Lumberton, Silsbee,
# Woodville, Jasper, Orange.

SEVEN_DAY_POINTS = {
    "Beaumont": (30.08, -94.10),
    "Houston": (29.76, -95.37),
    "Lumberton": (30.27, -94.20),
    "Silsbee": (30.34, -94.18),
    "Woodville": (30.78, -94.41),
    "Jasper": (30.92, -93.99),
    "Orange": (30.09, -93.74),
}


def fetch_temperature_blend():
    """High/low blended across Houston-Beaumont-Lumberton-Silsbee, per
    instruction -- not a single Beaumont/Port Arthur point."""
    names = ["Houston", "Beaumont", "Lumberton", "Silsbee"]
    lat_str = ",".join(str(SEVEN_DAY_POINTS[n][0]) for n in names)
    lon_str = ",".join(str(SEVEN_DAY_POINTS[n][1]) for n in names)
    cache_buster = int(time.time())
    url = (
        f"https://api.open-meteo.com/v1/ecmwf"
        f"?latitude={lat_str}&longitude={lon_str}"
        f"&daily=temperature_2m_max,temperature_2m_min"
        f"&forecast_days=1&temperature_unit=fahrenheit&_cb={cache_buster}"
    )
    data = w._fetch_with_retries_bytes(url, "TempBlend:ecmwf")
    if not data:
        return None
    try:
        points = json.loads(data.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(points, list):
        return None
    highs, lows = [], []
    for point in points:
        daily = point.get("daily", {})
        h = daily.get("temperature_2m_max", [])
        l = daily.get("temperature_2m_min", [])
        if h and h[0] is not None:
            highs.append(h[0])
        if l and l[0] is not None:
            lows.append(l[0])
    if not highs or not lows:
        return None
    return {"high_f": round(sum(highs) / len(highs)), "low_f": round(sum(lows) / len(lows))}


def fetch_ensemble_precip_coverage(forecast_days=7):
    """Fraction of GEFS + ECMWF ensemble members showing measurable
    precipitation (>=1mm) at the corridor's core points, per day.

    A single deterministic run (Euro) can show ~0mm at every grid
    point on a real scattered-storm day -- its one solution just
    doesn't happen to put a storm exactly on a grid point or exactly
    on that run. Ensemble spread across many possible solutions
    reveals that real chance instead. Used as a cross-check/floor for
    Euro on days 3-7 (blended in, not a replacement), per instruction:
    catch storms a single-run point-threshold check can miss, without
    over-correcting into being too aggressive."""
    lat_str = ",".join(str(p[0]) for p in SETX_SWLA_POINTS)
    lon_str = ",".join(str(p[1]) for p in SETX_SWLA_POINTS)
    cache_buster = int(time.time())

    def fetch_ensemble(models_param):
        url = (
            f"https://ensemble-api.open-meteo.com/v1/ensemble"
            f"?latitude={lat_str}&longitude={lon_str}"
            f"&daily=precipitation_sum&models={models_param}"
            f"&forecast_days={forecast_days}&_cb={cache_buster}"
        )
        data = w._fetch_with_retries_bytes(url, f"EnsemblePrecip:{models_param}")
        if not data:
            return None
        try:
            pts = json.loads(data.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None
        return pts if isinstance(pts, list) else None

    gefs_points = fetch_ensemble("gfs_seamless")
    ecmwf_points = fetch_ensemble("ecmwf_ifs025_ensemble")
    if not gefs_points and not ecmwf_points:
        return None

    coverage_by_day = {}
    for day_idx in range(forecast_days):
        hits, total = 0, 0
        for points in (gefs_points, ecmwf_points):
            if not points:
                continue
            for point in points:
                daily = point.get("daily", {})
                for key, vals in daily.items():
                    if not key.startswith("precipitation_sum_member"):
                        continue
                    if day_idx < len(vals) and vals[day_idx] is not None:
                        total += 1
                        if vals[day_idx] >= 0.5:
                            hits += 1
        coverage_by_day[day_idx] = round(100 * hits / total) if total else None

    return coverage_by_day


def fetch_seven_day_forecast(ndfd_totals=None):
    """Day-by-day 7-day forecast. Rain coverage is calculated from the
    SAME dense 25-point regional grid used for the HRRR grid detail
    (spanning the Beaumont/China/Nome/Sour Lake/Hampshire/Fannett/
    Winnie area and Chambers/Hardin/Jefferson/Newton/Harris/Orange
    counties), per instruction -- not a handful of city points, so a
    meaningful minority of wet grid points actually shows up as a
    realistic percentage instead of rounding to near-zero.

    Day 1-2: HRRR + Euro grid coverage blended (HRRR weighted higher
    for near-term timing/coverage). Day 3-7: Euro grid coverage as
    primary, with NWS Houston/Lake Charles QPF as a light supporting
    nudge only (never the primary driver), per instruction.

    High/low temps still use the 7-town SEVEN_DAY_POINTS blend."""
    grid_points = _hrrr_grid_points()
    grid_lat_str = ",".join(str(p[0]) for p in grid_points)
    grid_lon_str = ",".join(str(p[1]) for p in grid_points)
    cache_buster = int(time.time())

    def fetch_grid(endpoint, models_param=None, forecast_days=7):
        models_bit = f"&models={models_param}" if models_param else ""
        url = (
            f"https://api.open-meteo.com/v1/{endpoint}"
            f"?latitude={grid_lat_str}&longitude={grid_lon_str}"
            f"&daily=precipitation_sum&forecast_days={forecast_days}{models_bit}&_cb={cache_buster}"
        )
        data = w._fetch_with_retries_bytes(url, f"SevenDayGrid:{endpoint}:{models_param or 'auto'}")
        if not data:
            return None
        try:
            points = json.loads(data.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None
        return points if isinstance(points, list) else None

    def grid_coverage_pct(points, day_idx):
        if not points:
            return None
        hits, total = 0, 0
        for point in points:
            daily_mm = point.get("daily", {}).get("precipitation_sum", [])
            if day_idx < len(daily_mm) and daily_mm[day_idx] is not None:
                total += 1
                if daily_mm[day_idx] >= 2.5:
                    hits += 1
        return round(100 * hits / total) if total else None

    hrrr_grid = fetch_grid("forecast", models_param="ncep_hrrr_conus", forecast_days=2)
    euro_grid = fetch_grid("ecmwf", forecast_days=7)

    # Temperature blend across the 7-town points.
    temp_names = list(SEVEN_DAY_POINTS.keys())
    temp_lat_str = ",".join(str(SEVEN_DAY_POINTS[n][0]) for n in temp_names)
    temp_lon_str = ",".join(str(SEVEN_DAY_POINTS[n][1]) for n in temp_names)

    def fetch_temps(endpoint, models_param=None, forecast_days=7):
        models_bit = f"&models={models_param}" if models_param else ""
        url = (
            f"https://api.open-meteo.com/v1/{endpoint}"
            f"?latitude={temp_lat_str}&longitude={temp_lon_str}"
            f"&daily=temperature_2m_max,temperature_2m_min&forecast_days={forecast_days}{models_bit}"
            f"&temperature_unit=fahrenheit&_cb={cache_buster}"
        )
        data = w._fetch_with_retries_bytes(url, f"SevenDayTemps:{endpoint}:{models_param or 'auto'}")
        if not data:
            return None
        try:
            points = json.loads(data.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None
        return points if isinstance(points, list) else None

    hrrr_temps = fetch_temps("forecast", models_param="ncep_hrrr_conus", forecast_days=2)
    euro_temps = fetch_temps("ecmwf", forecast_days=7)

    def temp_avg(points, day_idx, key):
        if not points:
            return None
        vals = []
        for point in points:
            arr = point.get("daily", {}).get(key, [])
            if day_idx < len(arr) and arr[day_idx] is not None:
                vals.append(arr[day_idx])
        return round(sum(vals) / len(vals)) if vals else None

    if not euro_grid and not hrrr_grid:
        return None

    # NWS QPF as a light supporting signal only, per instruction --
    # a small nudge, never the primary driver of the percentage.
    nws_wet_signal = False
    if ndfd_totals:
        nws_wet_signal = any(v >= 0.3 for v in ndfd_totals.values())

    # Ensemble coverage as a cross-check/floor for days 3-7, per
    # instruction -- catches scattered storm chances a single
    # deterministic Euro run's point-threshold check can miss.
    ensemble_coverage = fetch_ensemble_precip_coverage(forecast_days=7)

    days = []
    for day_idx in range(7):
        if day_idx <= 1:
            hrrr_cov = grid_coverage_pct(hrrr_grid, day_idx)
            euro_cov = grid_coverage_pct(euro_grid, day_idx)
            if hrrr_cov is not None and euro_cov is not None:
                # HRRR weighted higher for near-term, per instruction.
                rain_pct = round(hrrr_cov * 0.65 + euro_cov * 0.35)
            else:
                rain_pct = hrrr_cov if hrrr_cov is not None else euro_cov
            high = temp_avg(hrrr_temps, day_idx, "temperature_2m_max") or temp_avg(euro_temps, day_idx, "temperature_2m_max")
            low = temp_avg(hrrr_temps, day_idx, "temperature_2m_min") or temp_avg(euro_temps, day_idx, "temperature_2m_min")
        else:
            euro_cov = grid_coverage_pct(euro_grid, day_idx)
            ens_cov = ensemble_coverage.get(day_idx) if ensemble_coverage else None
            if euro_cov is not None and ens_cov is not None:
                # Blend deterministic Euro with ensemble spread --
                # averaged, not just taken outright, so a high
                # ensemble spread doesn't override into being overly
                # aggressive on its own, per instruction.
                rain_pct = round((euro_cov + ens_cov) / 2)
            else:
                rain_pct = euro_cov if euro_cov is not None else ens_cov
            if rain_pct is not None and nws_wet_signal:
                rain_pct = min(100, rain_pct + 5)  # light nudge only
            high = temp_avg(euro_temps, day_idx, "temperature_2m_max")
            low = temp_avg(euro_temps, day_idx, "temperature_2m_min")

        if high is None or low is None:
            days.append(None)
            continue
        days.append({"high": high, "low": low, "rain_pct": rain_pct if rain_pct is not None else 0})

    return days


def build_seven_day_section(days):
    if not days or not any(days):
        return None
    lines = ["", "<b>\U0001F4C5 7-Day Forecast</b>"]
    import datetime as _dt
    now_local = _dt.datetime.now(w.BEAUMONT_TZ)
    weekday_names = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    day_labels = []
    for i in range(7):
        d = now_local + _dt.timedelta(days=i)
        wd = weekday_names[d.weekday()]
        day_labels.append(f"Today ({wd})" if i == 0 else wd)
    for i, d in enumerate(days):
        if d is None:
            lines.append(f"{day_labels[i]}: data unavailable")
            continue
        cov = _coverage_word(d["rain_pct"]) or "mostly dry"
        cov_str = f" ({cov})" if d["rain_pct"] >= 10 else ""
        lines.append(f"{day_labels[i]}: High {d['high']} / Low {d['low']} / Rain {d['rain_pct']}%{cov_str}")
    return lines
