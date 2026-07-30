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

import setx_swla_extra as sx

STATE_FILE = Path(__file__).parent / "hrrr_alert_state.json"
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

    max_gust, max_gust_near = 0.0, None
    for point, (glat, glon) in zip(points, grid_points):
        gusts = point.get("hourly", {}).get("wind_gusts_10m", [])
        for g in gusts:
            if g is not None and g > max_gust:
                max_gust = g
                max_gust_near = sx._nearest_city_label(glat, glon)

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
        "onset_hour_idx": onset_hour_idx,
        "onset_hour_str": onset_hour_str,
        "max_gust_mph": round(max_gust, 1),
        "max_gust_near": max_gust_near,
    }


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
    if not iso_str:
        return "an unclear time"
    try:
        dt = datetime.fromisoformat(iso_str)
    except ValueError:
        return "an unclear time"
    now_local = datetime.now(BEAUMONT_TZ)
    day_diff = (dt.date() - now_local.date()).days
    label = "today" if day_diff == 0 else ("tomorrow" if day_diff == 1 else dt.strftime("%A"))
    return f"{label} around {dt.strftime('%I %p').lstrip('0')}"


def build_hrrr_alert_message(current, reasons):
    now_local = datetime.now(BEAUMONT_TZ)
    lines = ["<b>⛈️ HRRR Update</b> -- Beaumont time", now_local.strftime("%b %-d %I:%M %p").replace(" 0", " "), ""]
    for r in reasons:
        lines.append(f"- {r}")
    lines.append("")
    lines.append(f"Coverage: {current.get('coverage_pct')}% of the corridor")
    if current.get("max_total_in"):
        near = f" near {current.get('max_near')}" if current.get("max_near") else ""
        lines.append(f"Rainfall: up to {current['max_total_in']}\" possible{near}")
    if current.get("onset_hour_str"):
        lines.append(f"Timing: rain looks to move in around {_format_onset(current['onset_hour_str'])}")
    if current.get("max_gust_mph"):
        near = f" near {current.get('max_gust_near')}" if current.get("max_gust_near") else ""
        lines.append(f"Winds: gusts up to {current['max_gust_mph']} mph possible{near}")
    return "\n".join(lines)


def telegram_configured():
    return bool(os.environ.get("WXMODEL_TELEGRAM_BOT_TOKEN")) and bool(os.environ.get("WXMODEL_TELEGRAM_CHAT_ID"))


def send_telegram(text):
    bot_token = os.environ["WXMODEL_TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["WXMODEL_TELEGRAM_CHAT_ID"]
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


def process_hrrr_alert(state):
    current = fetch_hrrr_alert_signal()
    if not current:
        print("HRRR signal unavailable this cycle (non-fatal) -- skipping.")
        return

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
