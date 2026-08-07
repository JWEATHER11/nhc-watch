#!/usr/bin/env python3
"""
metar_storm_pipeline.py -- Real-observation "a storm is actually
happening right now" watcher, using live METAR/ASOS station reports
across the SETX/SWLA corridor -- NOT model output (per instruction:
"i dont want to use model data to tell me if real storms are
happening, model data can be wrong"). This is ground truth: a station
reporting "TS" is a human/automated instrument confirming a
thunderstorm is physically overhead right now.

Deliberately does NOT try to be radar -- true gap-free radar coverage
(NEXRAD/MRMS) is a separate, bigger project (raw binary decode, real
radar signal processing). This is the lighter-weight, still-genuinely-
real complement: point observations at 7 corridor airports, updated
roughly hourly (more often when conditions are changing -- stations
issue SPECI reports on significant changes).

Alerts only on a hazard NEWLY appearing at a station (not a repeat of
an already-alerted, still-ongoing condition), per the same "don't
send the same stuff over and over" principle applied to the AFD
throttle earlier. Sent to the NWS Telegram chat, alongside Houston
and Lake Charles content -- this is real-observation/radar-adjacent
work, kept separate from the model-based WXMODEL chat.
"""

import json
import math
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

STATE_FILE = Path(__file__).parent / "metar_storm_state.json"
BEAUMONT_TZ = ZoneInfo("America/Chicago")
MAX_ATTEMPTS = 2
RETRY_DELAY_SEC = 2

# Expanded from the original 7 to real, verified IEM ASOS/AWOS
# stations across the SETX/SWLA corridor -- per instruction to add
# more station coverage for catching rain starting, heavy totals, and
# wind gusts. JAS (Jasper) closes a real gap: HRRR flagged a wind gust
# near Jasper earlier with no station there to confirm it on the
# ground. Verified each of these actually reports live via IEM before
# adding (mesonet.agron.iastate.edu/geojson/network/TX_ASOS.geojson
# and LA_ASOS.geojson), not guessed from memory.
CORRIDOR_STATIONS = {
    "BPT": "Beaumont/Port Arthur",
    "BMT": "Beaumont Municipal",
    "IAH": "Houston Intercontinental",
    "HOU": "Houston Hobby",
    "DWH": "Houston/D.W. Hooks (north Houston)",
    "EFD": "Houston/Ellington (south Houston)",
    "SGR": "Sugar Land",
    "CXO": "Conroe/Montgomery County",
    "LVJ": "Pearland",
    "GLS": "Galveston",
    "LCH": "Lake Charles",
    "CWF": "Lake Charles/Chennault",
    "LFT": "Lafayette",
    "LFK": "Lufkin",
    "JAS": "Jasper",
    "ORG": "Orange",
    "DRI": "De Ridder",
    "3R7": "Jennings",
    "UTS": "Huntsville",
    "T78": "Liberty",
}

GUST_THRESHOLD_KT = 40  # ~46 mph -- a real, useful heads-up level, below the official 50kt severe criterion
HEAVY_RAIN_HOURLY_IN = 0.5

KT_TO_MPH = 1.15078

# Second, much denser layer: local flood-control rain gauges (Jefferson
# County Drainage District, Harris County Flood Control District,
# etc.), all aggregated for free through the same IEM feed as a single
# "TX_DCP" network -- per instruction to get much heavier station
# density across SETX (Beaumont/Port Arthur/Orange/Winnie/Jasper) and
# near Houston. These only report rainfall (no wind or present-weather
# sensors), so they only ever contribute the heavy_rain hazard below.
# Verified live via IEM (mesonet.agron.iastate.edu/geojson/network/
# TX_DCP.geojson) before adding -- 521 stations matched, 437 actively
# reporting -- not guessed. Fetched and filtered once per process
# lifetime (cached below) rather than every 25s cycle, since the
# station list doesn't change often.
RAINGAUGE_NETWORK_URL = "https://mesonet.agron.iastate.edu/geojson/network/TX_DCP.geojson"
RAINGAUGE_COUNTIES = {
    "Harris", "Jefferson", "Orange", "Chambers", "Jasper", "Hardin",
    "Newton", "Tyler", "Liberty", "Montgomery", "Galveston",
}
RAINGAUGE_CENTER_LAT, RAINGAUGE_CENTER_LON = 30.086, -94.207  # Beaumont
RAINGAUGE_RADIUS_MI = 90

_raingauge_cache = None


def _haversine_mi(lat1, lon1, lat2, lon2):
    r = 3958.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def get_raingauge_stations():
    """Fetches and filters the TX_DCP network down to SETX/Houston-area
    flood-control rain gauges, cached for the life of the process --
    non-fatal on failure, just means this cycle runs with ASOS-only
    coverage. Returns {station_id: station_name}."""
    global _raingauge_cache
    if _raingauge_cache is not None:
        return _raingauge_cache
    data = _fetch_with_retries_bytes(RAINGAUGE_NETWORK_URL, "METARStorm:raingauge_network")
    if not data:
        return {}
    try:
        parsed = json.loads(data.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {}
    stations = {}
    for feat in parsed.get("features", []):
        props = feat.get("properties", {})
        if props.get("county") not in RAINGAUGE_COUNTIES:
            continue
        lon, lat = feat["geometry"]["coordinates"]
        if _haversine_mi(RAINGAUGE_CENTER_LAT, RAINGAUGE_CENTER_LON, lat, lon) > RAINGAUGE_RADIUS_MI:
            continue
        sid = props.get("sid") or props.get("id")
        if sid:
            stations[sid] = props.get("sname") or sid
    _raingauge_cache = stations
    print(f"[raingauge] Loaded {len(stations)} SETX/Houston-area flood-control rain gauges.")
    return stations


def _http_get_bytes(url, timeout=10):
    req = urllib.request.Request(url, headers={"User-Agent": "metar-storm-pipeline/1.0"})
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


def fetch_corridor_conditions():
    """Live, observed (not forecast) current conditions across the
    ASOS corridor (full met data: wind/wxcodes/rain) plus the SETX/
    Houston flood-control rain gauge network (rain-only), via IEM's
    currents.json in one combined request."""
    all_stations = list(CORRIDOR_STATIONS.keys()) + list(get_raingauge_stations().keys())
    stations = ",".join(all_stations)
    url = f"https://mesonet.agron.iastate.edu/api/1/currents.json?station={stations}"
    data = _fetch_with_retries_bytes(url, "METARStorm:currents")
    if not data:
        return None
    try:
        parsed = json.loads(data.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    return parsed.get("data")


def classify_hazards(ob):
    """Returns a dict of {hazard_key: description} for whatever real,
    observed hazards this specific station report shows right now.
    Ordered roughly by severity -- funnel/tornado first."""
    hazards = {}
    wxcodes = (ob.get("wxcodes") or "").upper()

    if "FC" in wxcodes.split() or "+FC" in wxcodes:
        hazards["funnel"] = "Funnel cloud / tornado reported"
    if "TS" in wxcodes:
        hazards["thunderstorm"] = "Thunderstorm reported"
    if "GR" in wxcodes:
        hazards["hail"] = "Hail reported"

    gust = ob.get("gust")
    if gust is not None and gust >= GUST_THRESHOLD_KT:
        mph = round(gust * KT_TO_MPH)
        hazards["gust"] = f"Wind gust to {mph} mph observed"

    phour = ob.get("phour")
    if phour is not None and phour >= HEAVY_RAIN_HOURLY_IN:
        hazards["heavy_rain"] = f"Heavy rain observed -- {phour}\" in the last hour"

    return hazards


HAZARD_EMOJI = {
    "funnel": "🌪️",
    "thunderstorm": "⛈️",
    "hail": "🧊",
    "gust": "💨",
    "heavy_rain": "💧",
}


def build_message(new_hazards_by_station):
    now_local = datetime.now(BEAUMONT_TZ)
    lines = [
        "🚨 <b>Real Storm Watch</b> -- station observations (not model)",
        f"📅 {now_local.strftime('%A, %b %-d %I:%M %p').replace(' 0', ' ')} (Beaumont time)",
        "",
    ]
    for station, hazards in new_hazards_by_station.items():
        name = CORRIDOR_STATIONS.get(station) or get_raingauge_stations().get(station, station)
        lines.append(f"<b>{name} ({station})</b>")
        for hazard_key, desc in hazards.items():
            lines.append(f"{HAZARD_EMOJI.get(hazard_key, '⚠️')} {desc}")
        lines.append("")
    return "\n".join(lines).rstrip()


def telegram_configured():
    return bool(os.environ.get("NWS_TELEGRAM_BOT_TOKEN")) and bool(os.environ.get("NWS_TELEGRAM_CHAT_ID"))


def send_telegram(text):
    bot_token = os.environ["NWS_TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["NWS_TELEGRAM_CHAT_ID"]
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    # Telegram caps a single message at 4096 chars -- with hundreds of
    # rain gauges now in play, a widespread event could list enough
    # stations to exceed that, so chunk rather than let the send fail.
    max_len = 4000
    chunks = [text[i:i + max_len] for i in range(0, len(text), max_len)] or [text]
    for chunk in chunks:
        payload = json.dumps({"chat_id": chat_id, "text": chunk, "parse_mode": "HTML"}).encode("utf-8")
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
            raise RuntimeError(f"Telegram send failed after {MAX_ATTEMPTS} attempts: {last_err}")


def deliver(text):
    if not telegram_configured():
        print("Telegram not configured -- skipping.")
        raise RuntimeError("NWS Telegram not configured")
    send_telegram(text)


def send_failure_alert(context, error):
    try:
        deliver(f"[metar-storm-pipeline error] {context}: {error}")
    except Exception as e:
        print(f"Could not even send the failure alert: {e}", file=sys.stderr)


def load_state():
    return json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2))


def process_metar_storm(state):
    obs = fetch_corridor_conditions()
    if not obs:
        print("Corridor conditions unavailable this cycle (non-fatal) -- skipping.")
        return

    active_hazards = state.get("active_hazards", {})
    new_active_hazards = {}
    new_hazards_by_station = {}

    for ob in obs:
        station = ob.get("station")
        if not station:
            continue
        hazards = classify_hazards(ob)
        if hazards:
            new_active_hazards[station] = list(hazards.keys())
        prev_hazards = set(active_hazards.get(station, []))
        newly_appeared = {k: v for k, v in hazards.items() if k not in prev_hazards}
        if newly_appeared:
            new_hazards_by_station[station] = newly_appeared
            print(f"[{station}] New hazard(s): {list(newly_appeared.keys())}")
        elif hazards:
            print(f"[{station}] Hazard(s) ongoing, already alerted: {list(hazards.keys())}")

    state["active_hazards"] = new_active_hazards
    save_state(state)

    if not new_hazards_by_station:
        print("No newly-appearing real hazards this cycle -- not sending.")
        return

    message = build_message(new_hazards_by_station)
    print(f"Sending -- {message}")
    try:
        deliver(message)
    except Exception as e:
        send_failure_alert("Real storm watch delivery", str(e))
        return
    print("Sent successfully.")


def main():
    state = load_state()
    try:
        process_metar_storm(state)
    except Exception as e:
        print(f"Unexpected error (non-fatal): {e}")


if __name__ == "__main__":
    main()
