#!/usr/bin/env python3
"""
hrrr_alert_pipeline.py -- Standalone "sees a storm and messages you"
watcher, using HRRR (Open-Meteo's mirror, same dense 9x9 corridor grid
as the rest of this system) instead of raw radar (MRMS/NEXRAD raw
files are a much heavier lift -- new binary-format dependencies, real
radar signal processing for rotation -- and were explicitly deferred
as a separate, bigger project).

HRRR updates hourly, but most updates are noise. This only alerts on
genuine CHANGE from what was last actually sent, not on every refresh:
- Coverage jumps by a real amount since the last alert
- A new isolated max total crosses a real threshold
- Storm timing (onset) shifts materially, or a new signal appears
- A strong wind gust newly shows up

Sent to the same WXMODEL Telegram chat as Model Watch. Same reliable
pattern as everything else: cache-busted fetch, retries, Telegram
only, zero AI in any number.
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import href_check
import setx_swla_extra as sx

STATE_FILE = Path(__file__).parent / "hrrr_alert_state.json"
HREF_RECHECK_INTERVAL_MIN = 60
BEAUMONT_TZ = ZoneInfo("America/Chicago")
MAX_ATTEMPTS = 2
RETRY_DELAY_SEC = 2

HRRR_ALERT_COVERAGE_JUMP_PCT = 20
HRRR_ALERT_MAX_TOTAL_THRESHOLD_IN = 1.5
HRRR_ALERT_ONSET_SHIFT_HOURS = 2
HRRR_ALERT_GUST_THRESHOLD_MPH = 50
HRRR_ALERT_ONSET_HOURLY_IN = 0.02  # ~0.5mm/hr, light-rain onset
HRRR_ALERT_ONSET_AGREEMENT_FRACTION = 0.15


def _http_get_bytes(url, timeout=10):
    req = urllib.request.Request(url, headers={"User-Agent": "hrrr-alert-pipeline/1.0"})
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


def fetch_hrrr_alert_signal():
    """HRRR-only, hourly-resolution signal across the dense corridor
    grid: coverage/max isolated total (reuses the proven dense-grid
    daily-total logic), rain onset timing, and max wind gust. If HRRR
    itself is unavailable this cycle, returns None -- comparing a
    different model's numbers against HRRR alert history wouldn't
    mean anything."""
    detail = sx.fetch_hrrr_grid_detail()
    if not detail or detail.get("source_label") != "HRRR":
        return None

    grid_points = sx._hrrr_grid_points()
    lat_str = ",".join(str(p[0]) for p in grid_points)
    lon_str = ",".join(str(p[1]) for p in grid_points)
    cache_buster = int(time.time())
    url = (
        f"https://api.open-meteo.com/v1/forecast"
        f"?latitude={lat_str}&longitude={lon_str}"
        f"&hourly=precipitation,wind_gusts_10m&models=ncep_hrrr_conus"
        f"&forecast_days=2&windspeed_unit=mph&precipitation_unit=inch&_cb={cache_buster}"
    )
    data = _fetch_with_retries_bytes(url, "HRRRAlertHourly")
    if not data:
        return None
    try:
        points = json.loads(data.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(points, list) or len(points) != len(grid_points):
        return None

    hours = points[0].get("hourly", {}).get("time", [])
    n_hours = len(hours)

    onset_hour_idx = None
    for h in range(n_hours):
        hit_count, total = 0, 0
        for point in points:
            precip = point.get("hourly", {}).get("precipitation", [])
            if h < len(precip) and precip[h] is not None:
                total += 1
                if precip[h] >= HRRR_ALERT_ONSET_HOURLY_IN:
                    hit_count += 1
        if total and (hit_count / total) >= HRRR_ALERT_ONSET_AGREEMENT_FRACTION:
            onset_hour_idx = h
            break

    # Peak gust at each point (across the whole forecast window), then
    # only trust the single highest one if at least one neighboring
    # grid point also shows a meaningfully elevated peak -- same
    # "don't trust an isolated grid cell" fix applied to
    # fetch_hrrr_grid_detail's precip max after a lone artifact spike
    # (109mm/4.3in at one cell, 0 at every neighbor) triggered a false
    # alert. See radar_prototype/NOTES.md and setx_swla_extra.py's
    # _has_neighbor_support for the same reasoning.
    peak_gust_grid = {}
    peak_gust_hour = {}
    for idx, point in enumerate(points):
        gust_series = point.get("hourly", {}).get("wind_gusts_10m", [])
        valid = [(h, g) for h, g in enumerate(gust_series) if g is not None]
        if valid:
            row, col = idx // sx.HRRR_GRID_COLS, idx % sx.HRRR_GRID_COLS
            best_h, best_g = max(valid, key=lambda hg: hg[1])
            peak_gust_grid[(row, col)] = best_g
            peak_gust_hour[(row, col)] = best_h

    def _gust_has_neighbor_support(row, col, value):
        if value < 35.0:
            return True
        neighbor_vals = [
            peak_gust_grid[(row + dr, col + dc)]
            for dr in (-1, 0, 1)
            for dc in (-1, 0, 1)
            if (dr, dc) != (0, 0) and (row + dr, col + dc) in peak_gust_grid
        ]
        if not neighbor_vals:
            return True
        return max(neighbor_vals) >= max(15.0, value * 0.5)

    max_gust, max_gust_near, max_gust_hour_idx = 0.0, None, None
    for (row, col), peak in peak_gust_grid.items():
        if peak > max_gust and _gust_has_neighbor_support(row, col, peak):
            max_gust = peak
            glat, glon = grid_points[row * sx.HRRR_GRID_COLS + col]
            max_gust_near = sx._nearest_city_label(glat, glon)
            max_gust_hour_idx = peak_gust_hour[(row, col)]

    max_gust_hour_str = None
    if max_gust_hour_idx is not None and max_gust_hour_idx < len(hours):
        try:
            gust_dt_utc = datetime.fromisoformat(hours[max_gust_hour_idx]).replace(tzinfo=timezone.utc)
            max_gust_hour_str = gust_dt_utc.astimezone(BEAUMONT_TZ).isoformat()
        except ValueError:
            pass

    onset_hour_str = None
    if onset_hour_idx is not None and onset_hour_idx < len(hours):
        try:
            onset_dt_utc = datetime.fromisoformat(hours[onset_hour_idx]).replace(tzinfo=timezone.utc)
            onset_hour_str = onset_dt_utc.astimezone(BEAUMONT_TZ).isoformat()
        except ValueError:
            pass

    return {
        "coverage_pct": detail["coverage_pct"],
        "max_total_in": detail["max_total_in"],
        "max_near": detail.get("max_near"),
        "max_day_label": detail.get("max_day_label"),
        "onset_hour_idx": onset_hour_idx,
        "onset_hour_str": onset_hour_str,
        "max_gust_mph": round(max_gust, 1),
        "max_gust_near": max_gust_near,
        "max_gust_hour_str": max_gust_hour_str,
        "run_cycle_str": estimate_hrrr_cycle().isoformat(),
    }


def estimate_hrrr_cycle():
    """HRRR runs hourly (not the 6-hourly 00/06/12/18Z cadence of
    GFS/ECMWF), and Open-Meteo's response doesn't expose the exact init
    time either -- this estimates the most recent likely run using
    HRRR's typical ~70-90 minute publication delay, same spirit as
    wxmodel_pipeline.py's estimate_model_cycle for the 6-hourly models."""
    now_utc = datetime.now(timezone.utc)
    effective_time = now_utc - timedelta(hours=1.5)
    return effective_time.replace(minute=0, second=0, microsecond=0)


def check_hrrr_alert_trigger(current, last):
    """Compares current signal against the last-ALERTED snapshot (not
    last-checked), so slow drift across many small checks still gets
    caught once it adds up. Only alerts on genuine change, not every
    hourly refresh."""
    if not current:
        return False, []
    reasons = []
    last = last or {}

    cov_now = current.get("coverage_pct") or 0
    cov_before = last.get("coverage_pct") or 0
    if cov_now - cov_before >= HRRR_ALERT_COVERAGE_JUMP_PCT:
        reasons.append(f"Coverage increasing -- now {cov_now}%, was {cov_before}%")

    total_now = current.get("max_total_in") or 0
    total_before = last.get("max_total_in") or 0
    if total_now >= HRRR_ALERT_MAX_TOTAL_THRESHOLD_IN and total_before < HRRR_ALERT_MAX_TOTAL_THRESHOLD_IN:
        near = f" near {current.get('max_near')}" if current.get("max_near") else ""
        reasons.append(f"Heavier rainfall showing up -- up to {total_now}\"{near}")

    onset_now = current.get("onset_hour_idx")
    onset_before = last.get("onset_hour_idx")
    if onset_now is not None and onset_before is not None and abs(onset_now - onset_before) >= HRRR_ALERT_ONSET_SHIFT_HOURS:
        direction = "earlier" if onset_now < onset_before else "later"
        reasons.append(f"Timing shifted {direction} -- now looks like around {_format_onset(current.get('onset_hour_str'))}")
    elif onset_now is not None and onset_before is None:
        reasons.append(f"New rain signal showing up around {_format_onset(current.get('onset_hour_str'))}")

    gust_now = current.get("max_gust_mph") or 0
    gust_before = last.get("max_gust_mph") or 0
    if gust_now >= HRRR_ALERT_GUST_THRESHOLD_MPH and gust_before < HRRR_ALERT_GUST_THRESHOLD_MPH:
        near = f" near {current.get('max_gust_near')}" if current.get("max_gust_near") else ""
        reasons.append(f"Strong wind gusts possible -- up to {gust_now} mph{near}")

    return (len(reasons) > 0), reasons


def _format_onset(iso_str):
    """Always names the actual weekday (Thursday, Friday, ...) rather
    than just 'today'/'tomorrow' -- per instruction, don't make the
    reader work out which real day that refers to themselves."""
    if not iso_str:
        return "an unclear time"
    try:
        dt = datetime.fromisoformat(iso_str)
    except ValueError:
        return "an unclear time"
    now_local = datetime.now(BEAUMONT_TZ)
    day_diff = (dt.date() - now_local.date()).days
    weekday = dt.strftime("%A")
    if day_diff == 0:
        label = f"{weekday} (today)"
    elif day_diff == 1:
        label = f"{weekday} (tomorrow)"
    else:
        label = weekday
    return f"{label} around {dt.strftime('%I %p').lstrip('0')}"


def _rain_day_label(max_day_label, now_local):
    """The rainfall max is a daily total (today+tomorrow summed), not an
    hourly onset like the wind gust, so it needs its own day label built
    from today's actual date rather than reusing _format_onset. Same
    weekday-name-first convention as everywhere else -- 'today'/'tomorrow'
    alone makes the reader do the date math themselves."""
    if not max_day_label:
        return None
    if max_day_label == "tomorrow":
        target = now_local.date() + timedelta(days=1)
        return f"{target.strftime('%A')} (tomorrow)"
    return f"{now_local.strftime('%A')} (today)"


def build_hrrr_alert_message(current, reasons):
    now_local = datetime.now(BEAUMONT_TZ)
    run_str = "unknown"
    if current.get("run_cycle_str"):
        try:
            # Z time only, never converted to local -- per instruction,
            # this needs to match the convention every other model graphic
            # (WeatherBell, etc.) uses so the run can actually be found
            # and cross-checked, not translated into something that has
            # to be converted back.
            run_dt_utc = datetime.fromisoformat(current["run_cycle_str"]).astimezone(timezone.utc)
            run_str = f"{run_dt_utc.hour:02d}Z run (est.)"
        except ValueError:
            pass
    lines = [
        "⛈️ <b>HRRR Update</b>",
        f"📅 {now_local.strftime('%A, %b %-d · %I:%M %p').replace(' 0', ' ')} (Beaumont time)",
        f"🛰️ HRRR {run_str}",
        "",
    ]
    for r in reasons:
        lines.append(f"🔔 {r}")
    if reasons:
        lines.append("")
    # This tracks whether heavy rain is spread across a large area or
    # just one or two spots -- separate from whether ANY storm is
    # happening (that's the Rain/Wind lines below). Kept as plain
    # as possible after two rounds of this still reading as jargon: no
    # inch thresholds, no "corridor," no percentage math to decode.
    cov = current.get("coverage_pct") or 0
    if cov == 0:
        lines.append("📊 Storms today look isolated, not widespread.")
    elif cov < 40:
        lines.append("📊 Heavy rain possible in a few spots, not widespread.")
    else:
        lines.append("📊 Heavy rain looks widespread, not just isolated spots.")
    if current.get("max_total_in"):
        near = f" near {current.get('max_near')}" if current.get("max_near") else ""
        day = _rain_day_label(current.get("max_day_label"), now_local)
        when = f" -- {day}" if day else ""
        lines.append(f"💧 Rain: up to {current['max_total_in']}\"{near}{when}")
    if current.get("onset_hour_str"):
        lines.append(f"⏱️ Rain moving in: around {_format_onset(current['onset_hour_str'])}")
    if current.get("max_gust_mph"):
        near = f" near {current.get('max_gust_near')}" if current.get("max_gust_near") else ""
        when = f" -- {_format_onset(current['max_gust_hour_str'])}" if current.get("max_gust_hour_str") else ""
        lines.append(f"💨 Wind: gusts up to {current['max_gust_mph']} mph{near}{when}")
    href_line = _href_summary_line(current.get("href"), now_local)
    if href_line:
        lines.append(href_line)
    return "\n".join(lines)


def telegram_configured():
    return bool(os.environ.get("WXMODEL_TELEGRAM_BOT_TOKEN")) and bool(os.environ.get("WXMODEL_TELEGRAM_CHAT_ID"))


def _telegram_chat_ids():
    """Same multi-destination convention as wxmodel_pipeline.py --
    WXMODEL_TELEGRAM_CHAT_ID_2, _3, etc. for any additional chats
    (e.g. a meteorologist team group) beyond the original one."""
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
    chat_ids = _telegram_chat_ids()
    errors = {}
    for chat_id in chat_ids:
        payload = json.dumps({"chat_id": chat_id, "text": text, "parse_mode": "HTML"}).encode("utf-8")
        req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"}, method="POST")
        last_err = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                with urllib.request.urlopen(req, timeout=20) as resp:
                    result = json.loads(resp.read().decode("utf-8"))
                    if result.get("ok"):
                        last_err = None
                        break
                    last_err = result.get("description", "Unknown Telegram error")
            except Exception as e:
                last_err = str(e)
            if attempt < MAX_ATTEMPTS:
                time.sleep(RETRY_DELAY_SEC)
        if last_err:
            errors[chat_id] = last_err
            print(f"Telegram send to {chat_id} failed: {last_err}")
    if len(errors) == len(chat_ids):
        raise RuntimeError(f"Telegram send failed to ALL configured chats: {errors}")


def deliver(text):
    if not telegram_configured():
        print("Telegram not configured -- skipping.")
        raise RuntimeError("WXMODEL Telegram not configured")
    send_telegram(text)


def send_failure_alert(context, error):
    try:
        deliver(f"[hrrr-alert-pipeline error] {context}: {error}")
    except Exception as e:
        print(f"Could not even send the failure alert: {e}", file=sys.stderr)


def load_state():
    return json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2))


def _get_href_corroboration(state):
    """HREF is fetched from raw NOMADS grib files (~2 small byte-range
    downloads + a parse), not Open-Meteo -- cheap, but no reason to
    redo it every 30-min loop tick when the underlying HREF cycle only
    updates 4x/day. Cached in state and only refreshed once an hour;
    any fetch failure just falls back to the last good cached read
    (or None) rather than blocking the core HRRR alert."""
    cached = state.get("href_cache") or {}
    fetched_at = cached.get("fetched_at")
    stale = True
    if fetched_at:
        try:
            age_min = (datetime.now(timezone.utc) - datetime.fromisoformat(fetched_at)).total_seconds() / 60
            stale = age_min >= HREF_RECHECK_INTERVAL_MIN
        except ValueError:
            stale = True
    if stale:
        try:
            result = href_check.fetch_href_corroboration()
        except Exception:
            result = None
        if result:
            state["href_cache"] = {"fetched_at": datetime.now(timezone.utc).isoformat(), "data": result}
            return result
    return cached.get("data")


HREF_NOTABLE_THRESHOLD_PCT = 40


def _href_summary_line(href_data, now_local):
    """A second, independent high-res model (HREF, 10 separate runs)
    checking the same question as the Rain line above. First two
    phrasings ("X% agreeing", "X of 10 runs agree") both still read as
    unexplained jargon. Plain "X% chance of rain" is the one phrasing
    everyone already knows from a normal weather forecast, so that's
    what this uses -- just the single most notable finding, if any."""
    if not href_data:
        return None
    try:
        cycle = datetime.fromisoformat(href_data["cycle"])
    except (KeyError, ValueError):
        return None
    best = None  # (day_label, city, pct)
    for day_key, offset_hours in (("day1", 12), ("day2", 36)):
        sampled = href_data.get(day_key)
        if not sampled:
            continue
        midpoint_local = (cycle + timedelta(hours=offset_hours)).astimezone(BEAUMONT_TZ)
        day_diff = (midpoint_local.date() - now_local.date()).days
        weekday = midpoint_local.strftime("%A")
        if day_diff == 0:
            label = f"{weekday} (today)"
        elif day_diff == 1:
            label = f"{weekday} (tomorrow)"
        else:
            label = weekday
        best_city, best_pct = max(sampled.items(), key=lambda kv: kv[1])
        if best is None or best_pct > best[2]:
            best = (label, best_city, best_pct)
    if best is None or best[2] < HREF_NOTABLE_THRESHOLD_PCT:
        return None
    label, city, pct = best
    return f"🎲 Second opinion (separate high-res model): {pct}% chance {city} sees 0.5\"+ rain {label}"


def process_hrrr_alert(state):
    current = fetch_hrrr_alert_signal()
    if not current:
        print("HRRR signal unavailable this cycle (non-fatal) -- skipping.")
        return

    current["href"] = _get_href_corroboration(state)

    last = state.get("last_alerted")
    should_alert, reasons = check_hrrr_alert_trigger(current, last)
    if not should_alert:
        print(f"No meaningful change from last alert -- not sending. Current: {current}")
        return

    print(f"Meaningful change detected -- sending. Reasons: {reasons}")
    message = build_hrrr_alert_message(current, reasons)
    print(f"Message:\n{message}")

    try:
        deliver(message)
    except Exception as e:
        send_failure_alert("HRRR alert delivery", str(e))
        return

    print("Sent successfully.")
    state["last_alerted"] = current
    save_state(state)


def main():
    state = load_state()
    try:
        process_hrrr_alert(state)
    except Exception as e:
        print(f"Unexpected error (non-fatal): {e}")


if __name__ == "__main__":
    main()
