#!/usr/bin/env python3
"""
nhc_hdob_pipeline.py -- Tracks High-Density Observations (HDOB) from recon
aircraft, but ONLY alerts when something genuinely stands out -- not every
30-second reading.

The key comparison is against NHC's own CURRENT OFFICIAL intensity (read
from pipeline_state.json, shared with nhc_pipeline.py), not just this
mission's own running peak. A recon reading that's merely "the highest
we've seen in the last 10 minutes" isn't news if it's still below what NHC
already has as the official current wind/pressure -- that's expected noise.
What's actually worth a message:

  - Winds holding at or above NHC's official current wind (confirms the
    storm is at least as strong as advertised, or stronger)
  - Pressure dropping meaningfully below NHC's official current pressure
    (a real intensification signal)
  - The reverse of either -- winds notably below official, or pressure
    notably above official and rising -- as a weakening signal

Each of those only fires once per meaningful threshold crossed, not on
every subsequent reading that's still in the same range, so this doesn't
turn into a stream of near-duplicate alerts.

Field format is the official NHC HD/HA data line spec:
  hhmmss LLLLH NNNNNW PPPP GGGGG XXXX sTTT sddd wwwSSS MMM KKK ppp FF
See: https://www.nhc.noaa.gov/abouthdobs_2007.shtml
"""

import json
import os
import re
import smtplib
import sys
import time
import urllib.error
import urllib.request
from email.mime.text import MIMEText
from pathlib import Path

IEM_BASE = "https://mesonet.agron.iastate.edu/cgi-bin/afos/retrieve.py"
HDOB_PIL = "AHONT1"  # Atlantic HDOB from NHC
NHC_HDOB_URL = "https://www.nhc.noaa.gov/text/URNT15-USAF.shtml?text"

STATE_FILE = Path(__file__).parent / "hdob_state.json"
ADVISORY_STATE_FILE = Path(__file__).parent / "pipeline_state.json"  # shared w/ nhc_pipeline.py
CENTRAL_UTC_OFFSET = 5

MAX_ATTEMPTS = 3
RETRY_DELAY_SEC = 5

# --- Significance thresholds -- tuned to only fire on things a met would
# actually call out, not routine noise ---
WIND_STEADY_MARGIN_MPH = 3       # within this of official = "confirmed steady"
WIND_RE_ALERT_DELTA_MPH = 5      # must climb this much further to alert again
PRESSURE_DROP_THRESHOLD_MB = 3   # must be at least this far below official to matter
PRESSURE_RE_ALERT_DELTA_MB = 3   # must drop this much further to alert again
WEAKENING_WIND_MARGIN_MPH = 8    # this far below official = worth flagging as weakening
WEAKENING_PRESSURE_MARGIN_MB = 3 # this far above official = worth flagging as weakening


def _http_get(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": "nhc-hdob-pipeline/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _fetch_with_retries(url, label):
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            text = _http_get(url)
            if text and text.strip():
                return text
            print(f"[{label}] Attempt {attempt}: empty response from {url}")
        except Exception as e:
            print(f"[{label}] Attempt {attempt} failed ({url}): {e}")
        if attempt < MAX_ATTEMPTS:
            time.sleep(RETRY_DELAY_SEC)
    return None


def fetch_hdob():
    iem_url = f"{IEM_BASE}?pil={HDOB_PIL}"
    text = _fetch_with_retries(iem_url, f"IEM:{HDOB_PIL}")
    if text:
        return text, "IEM"
    print(f"[{HDOB_PIL}] IEM failed, falling back to NHC...")
    text = _fetch_with_retries(NHC_HDOB_URL, f"NHC:{HDOB_PIL}")
    if text:
        return text, "NHC"
    return None, "FAILED"


def kt_to_mph(kt):
    return round(kt * 1.15078)


def zulu_to_central(day, hhmmss):
    hour, minute = int(hhmmss[:2]), int(hhmmss[2:4])
    hour -= CENTRAL_UTC_OFFSET
    if hour < 0:
        hour += 24
        day -= 1
    period = "PM" if hour >= 12 else "AM"
    hour12 = hour % 12 or 12
    return f"{hour12}:{minute:02d} {period} CDT, day {day}"


# ---------------------------------------------------------------------------
# Parser -- exact fixed-field format from NHC's HDOB spec (see docstring)
# ---------------------------------------------------------------------------
HDOB_LINE_RE = re.compile(
    r"^(\d{6})\s+(\d{2})(\d{2})([NS])\s+(\d{3})(\d{2})([EW])\s+"
    r"(\d{4})\s+(\d{5})\s+(\d{4})\s+"
    r"([+-]\d{3})\s+([+-]\d{3}|///)\s+"
    r"(\d{3})(\d{3})\s+(\d{3})\s+(\d{3})\s+(\d{3}|///)\s+(\d{2})",
    re.M,
)

MISSION_ID_RE = re.compile(r"^([A-Z0-9]+)\s+(\S+)\s+(.+?)\s+HDOB\s+(\d+)\s+(\d{8})", re.M)


def parse_hdob_bulletin(text):
    mission_match = MISSION_ID_RE.search(text)
    aircraft = mission_match.group(1) if mission_match else None
    storm_name = mission_match.group(3).strip() if mission_match else None
    yyyymmdd = mission_match.group(5) if mission_match else None
    day = int(yyyymmdd[6:8]) if yyyymmdd else None

    obs = []
    for m in HDOB_LINE_RE.finditer(text):
        hhmmss = m.group(1)
        static_press_raw = m.group(8)
        extrap_or_dvalue_raw = m.group(10)

        static_press_mb = int(static_press_raw) / 10
        sfc_pressure_mb = None
        if static_press_mb >= 550.0:
            raw = int(extrap_or_dvalue_raw)
            sfc_pressure_mb = raw / 10 if raw >= 7000 else (1000 + raw / 10)

        peak_fl_wind_kt = int(m.group(14))
        peak_sfmr_kt_raw = m.group(15)
        peak_sfmr_kt = int(peak_sfmr_kt_raw) if peak_sfmr_kt_raw != "///" else None

        obs.append({
            "hhmmss": hhmmss,
            "day": day,
            "local_time": zulu_to_central(day, hhmmss) if day else hhmmss,
            "sfc_pressure_mb": sfc_pressure_mb,
            "peak_fl_wind_kt": peak_fl_wind_kt,
            "peak_sfmr_kt": peak_sfmr_kt,
        })
    return aircraft, storm_name, obs


def load_json(path):
    return json.loads(path.read_text()) if path.exists() else {}


def save_json(path, data):
    path.write_text(json.dumps(data, indent=2))


def get_official_intensity():
    """Reads NHC's current official wind/pressure from the shared advisory
    state file. Returns (wind_mph, pressure_mb, storm_name) or (None, None,
    None) if unavailable -- in which case we fall back to pure
    mission-peak tracking rather than blocking entirely."""
    adv_state = load_json(ADVISORY_STATE_FILE)
    last = adv_state.get("last", {})
    return last.get("wind_mph"), last.get("pressure_mb"), last.get("status_and_name")


HDOB_VOICE_PROMPT = """You write a SHORT alert (2-3 sentences max) for a Gulf Coast / Southeast Texas audience about what hurricane recon is finding RIGHT NOW, compared to NHC's official current stated intensity.

House style: use "&" not "and", no Oxford comma, capitalize "Tropical" always, confident & conversational, collective "we".

You'll be told whether this is a STRENGTHENING signal (recon winds holding at/above official, or pressure dropping below official), a WEAKENING signal (recon winds notably below official, or pressure notably above official and rising), or STEADY. Lead with what this means for the storm's trend -- this is meant to feel like "here's what recon is telling us right now," the kind of thing a meteorologist would flag as worth watching. Use only the facts given. No hashtags, at most one emoji. Output ONLY the alert text, nothing else."""


def call_claude_api(facts_summary):
    api_key = os.environ["ANTHROPIC_API_KEY"]
    payload = json.dumps({
        "model": "claude-sonnet-5",
        "max_tokens": 250,
        "system": HDOB_VOICE_PROMPT,
        "messages": [{"role": "user", "content": facts_summary}],
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=payload,
        headers={"Content-Type": "application/json", "x-api-key": api_key, "anthropic-version": "2023-06-01"},
        method="POST",
    )
    last_err = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                text_blocks = [b["text"] for b in data.get("content", []) if b.get("type") == "text"]
                result = "\n".join(text_blocks).strip()
                if result:
                    return result
                last_err = "Empty response from Claude API"
        except urllib.error.HTTPError as e:
            last_err = f"HTTP {e.code}: {e.read().decode('utf-8', errors='replace')[:200]}"
        except Exception as e:
            last_err = str(e)
        print(f"[Claude API] Attempt {attempt} failed: {last_err}")
        if attempt < MAX_ATTEMPTS:
            time.sleep(RETRY_DELAY_SEC)
    raise RuntimeError(f"Claude API failed after {MAX_ATTEMPTS} attempts: {last_err}")


def telegram_configured():
    return bool(os.environ.get("TELEGRAM_BOT_TOKEN")) and bool(os.environ.get("TELEGRAM_CHAT_ID"))


def send_telegram(text):
    bot_token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = json.dumps({"chat_id": chat_id, "text": text}).encode("utf-8")
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
        print(f"[Telegram] Attempt {attempt} failed: {last_err}")
        if attempt < MAX_ATTEMPTS:
            time.sleep(RETRY_DELAY_SEC)
    raise RuntimeError(f"Telegram send failed after {MAX_ATTEMPTS} attempts: {last_err}")


def send_email_sms_fallback(text, subject="HDOB Update"):
    smtp_server = os.environ["SMTP_SERVER"]
    smtp_port = int(os.environ.get("SMTP_PORT", "587"))
    smtp_user = os.environ["SMTP_USER"]
    smtp_pass = os.environ["SMTP_PASS"]
    to_addr = os.environ["ALERT_TO"]
    msg = MIMEText(text)
    msg["Subject"] = subject
    msg["From"] = smtp_user
    msg["To"] = to_addr
    with smtplib.SMTP(smtp_server, smtp_port) as server:
        server.starttls()
        server.login(smtp_user, smtp_pass)
        server.sendmail(smtp_user, [to_addr], msg.as_string())


def deliver(text, subject="HDOB Update"):
    if telegram_configured():
        send_telegram(text)
        print("Delivered via Telegram.")
    else:
        send_email_sms_fallback(text, subject=subject)
        print("Delivered via email-to-SMS fallback.")


def send_failure_alert(context, error):
    try:
        deliver(f"[nhc-hdob-pipeline error] {context}: {error}", subject="nhc-hdob-pipeline error")
    except Exception as e:
        print(f"Could not even send the failure alert: {e}", file=sys.stderr)


def main():
    text, source = fetch_hdob()
    if not text:
        print("No HDOB data available (likely no active mission right now). Exiting quietly.")
        return
    print(f"HDOB fetched from {source}")

    aircraft, storm_name, obs = parse_hdob_bulletin(text)
    if not obs:
        print("HDOB product found but no parseable observation lines. Exiting quietly.")
        return

    state = load_json(STATE_FILE)
    last_seen_time = state.get("last_seen_hhmmss")

    # Only look at observations newer than what we've already processed.
    new_obs = [o for o in obs if not last_seen_time or o["hhmmss"] > last_seen_time]
    if not new_obs:
        print("No new observations since last check. Not sending an update.")
        return

    latest_time_seen = max(o["hhmmss"] for o in new_obs)
    state["last_seen_hhmmss"] = latest_time_seen

    # Batch stats for just this new chunk of observations -- this is what
    # we compare against NHC's official numbers, not any single blip.
    sfmr_vals = [o["peak_sfmr_kt"] for o in new_obs if o["peak_sfmr_kt"]]
    fl_vals = [o["peak_fl_wind_kt"] for o in new_obs if o["peak_fl_wind_kt"]]
    press_vals = [o["sfc_pressure_mb"] for o in new_obs if o["sfc_pressure_mb"]]

    batch_wind_kt = max(sfmr_vals) if sfmr_vals else (max(fl_vals) if fl_vals else None)
    batch_wind_mph = kt_to_mph(batch_wind_kt) if batch_wind_kt else None
    batch_min_pressure_mb = min(press_vals) if press_vals else None
    latest_time = new_obs[-1]["local_time"]

    official_wind_mph, official_pressure_mb, official_name = get_official_intensity()
    print(f"Official (NHC advisory) intensity: {official_wind_mph} mph, {official_pressure_mb} mb")
    print(f"This batch: peak wind {batch_wind_mph} mph, min pressure {batch_min_pressure_mb} mb, at {latest_time}")

    save_json(STATE_FILE, state)  # persist last_seen_hhmmss regardless of whether we alert

    if official_wind_mph is None and official_pressure_mb is None:
        print("No official intensity available to compare against -- skipping this cycle rather than guessing.")
        return

    alert_reason = None
    signal_type = None  # "strengthening", "weakening"

    last_alert_wind = state.get("last_alert_wind_mph")
    last_alert_pressure = state.get("last_alert_pressure_mb")

    if batch_wind_mph is not None and official_wind_mph is not None:
        if batch_wind_mph >= official_wind_mph - WIND_STEADY_MARGIN_MPH:
            if last_alert_wind is None or batch_wind_mph >= last_alert_wind + WIND_RE_ALERT_DELTA_MPH:
                alert_reason = f"Recon winds ({batch_wind_mph} mph) are holding at or above NHC's official current {official_wind_mph} mph."
                signal_type = "strengthening"
                state["last_alert_wind_mph"] = batch_wind_mph
        elif batch_wind_mph <= official_wind_mph - WEAKENING_WIND_MARGIN_MPH:
            if last_alert_wind is None or batch_wind_mph <= last_alert_wind - WIND_RE_ALERT_DELTA_MPH:
                alert_reason = f"Recon winds ({batch_wind_mph} mph) are notably below NHC's official current {official_wind_mph} mph."
                signal_type = "weakening"
                state["last_alert_wind_mph"] = batch_wind_mph

    if batch_min_pressure_mb is not None and official_pressure_mb is not None and not alert_reason:
        if batch_min_pressure_mb <= official_pressure_mb - PRESSURE_DROP_THRESHOLD_MB:
            if last_alert_pressure is None or batch_min_pressure_mb <= last_alert_pressure - PRESSURE_RE_ALERT_DELTA_MB:
                alert_reason = f"Recon pressure ({batch_min_pressure_mb:.1f} mb) is dropping meaningfully below NHC's official current {official_pressure_mb} mb."
                signal_type = "strengthening"
                state["last_alert_pressure_mb"] = batch_min_pressure_mb
        elif batch_min_pressure_mb >= official_pressure_mb + WEAKENING_PRESSURE_MARGIN_MB:
            if last_alert_pressure is None or batch_min_pressure_mb >= last_alert_pressure + PRESSURE_RE_ALERT_DELTA_MB:
                alert_reason = f"Recon pressure ({batch_min_pressure_mb:.1f} mb) is notably above NHC's official current {official_pressure_mb} mb & rising."
                signal_type = "weakening"
                state["last_alert_pressure_mb"] = batch_min_pressure_mb

    if not alert_reason:
        print("Nothing significant enough to alert on this cycle (within normal range of official intensity).")
        save_json(STATE_FILE, state)
        return

    facts_lines = [f"Signal type: {signal_type}", alert_reason]
    if aircraft:
        facts_lines.append(f"Aircraft: {aircraft}")
    if storm_name or official_name:
        facts_lines.append(f"Storm: {storm_name or official_name}")
    if official_wind_mph is not None:
        facts_lines.append(f"NHC official current wind: {official_wind_mph} mph")
    if official_pressure_mb is not None:
        facts_lines.append(f"NHC official current pressure: {official_pressure_mb} mb")
    if batch_wind_mph is not None:
        facts_lines.append(f"Recon peak wind this batch: {batch_wind_mph} mph at {latest_time}")
    if batch_min_pressure_mb is not None:
        facts_lines.append(f"Recon minimum pressure this batch: {batch_min_pressure_mb:.1f} mb at {latest_time}")
    facts_summary = "\n".join(facts_lines)
    print(f"Alert-worthy signal found:\n{facts_summary}")

    try:
        narrative = call_claude_api(facts_summary)
    except Exception as e:
        send_failure_alert("Claude API rewrite step", str(e))
        sys.exit(1)

    header = "Recon Signal: Strengthening" if signal_type == "strengthening" else "Recon Signal: Weakening"
    full_message = f"{header}\n\n{narrative}"
    print(f"Full message:\n{full_message}")

    try:
        deliver(full_message, subject=header)
    except Exception as e:
        send_failure_alert("Delivery", str(e))
        sys.exit(1)

    print("Sent successfully.")
    save_json(STATE_FILE, state)


if __name__ == "__main__":
    main()
