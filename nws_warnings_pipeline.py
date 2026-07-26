#!/usr/bin/env python3
"""
nws_warnings_pipeline.py -- Tracks Tornado Warnings (TOR), Severe
Thunderstorm Warnings (SVR), and Flash Flood Warnings (FFW) from both
NWS Houston/Galveston (HGX) and NWS Lake Charles (LCH) -- covering the
full Houston-to-Jasper, Beaumont/Port Arthur/Orange-to-Lake Charles
region. Sent to the NWS Telegram chat, same reliable pattern as
everything else: cache-busted fetch, full-text dedup, Telegram only, no
AI (relaying the forecaster's own words, public domain).

Graphic (the actual warning polygon map, like what gets posted on
Twitter) is not yet included here -- that requires a VTEC-based polygon
lookup that needs more research to get right, rather than guess at like
we did with the SPC graphics. Text-only for now, graphic to follow.
"""

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

IEM_BASE = "https://mesonet.agron.iastate.edu/cgi-bin/afos/retrieve.py"

WARNING_TYPES = {
    "tor": {"pil_prefix": "TOR", "label": "Tornado Warning"},
    "svr": {"pil_prefix": "SVR", "label": "Severe Thunderstorm Warning"},
    "ffw": {"pil_prefix": "FFW", "label": "Flash Flood Warning"},
}

OFFICES = {
    "hgx": "Houston/Galveston",
    "lch": "Lake Charles",
}

STATE_FILE = Path(__file__).parent / "nws_warnings_state.json"
MAX_ATTEMPTS = 3
RETRY_DELAY_SEC = 5


def _http_get(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": "nws-warnings-pipeline/1.0"})
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


def fetch_warning(warn_key, office_key):
    pil = f"{WARNING_TYPES[warn_key]['pil_prefix']}{office_key.upper()}"
    cache_buster = int(time.time())
    iem_url = f"{IEM_BASE}?pil={pil}&_cb={cache_buster}"
    text = _fetch_with_retries(iem_url, f"IEM:{pil}")
    return text, pil


def issued_time_from_header(text):
    m = re.search(r"^\s*\d{3,4}\s+[AP]M\s+[A-Z]{2,4}\s+\w{3}\s+\w{3}\s+\d{1,2}\s+\d{4}\s*$", text, re.M)
    return m.group(0).strip() if m else None


def vtec_event_number(text):
    """Extracts the VTEC event tracking number (ETN), e.g. from
    /O.NEW.KHGX.TO.W.0012.260726T1200Z-260726T1300Z/ -- used purely for
    dedup, so we know a genuinely new warning was issued even if the
    text looks similar to a followup statement."""
    m = re.search(r"/[OX]\.\w+\.\w{4}\.\w{2}\.\w\.(\d{4})\.", text)
    return m.group(1) if m else None


# Bounding box covering the full Houston-to-Jasper, Beaumont/Port
# Arthur/Orange-to-Lake Charles region -- confirmed working via IEM's
# radmap.php tool, which has a genuine documented "sbw" (Storm Based
# Warning) layer option.
REGION_BBOX = "-95.5,29.0,-92.5,31.0"


def build_warning_graphic_url():
    cache_buster = int(time.time())
    return (
        f"https://mesonet.agron.iastate.edu/GIS/radmap.php?"
        f"width=800&height=600&bbox={REGION_BBOX}"
        f"&layers[]=uscounties&layers[]=nexrad&layers[]=sbw"
        f"&_cb={cache_buster}"
    )


def send_telegram_photo(photo_url, caption=""):
    bot_token = os.environ["NWS_TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["NWS_TELEGRAM_CHAT_ID"]
    url = f"https://api.telegram.org/bot{bot_token}/sendPhoto"
    payload = json.dumps({"chat_id": chat_id, "photo": photo_url, "caption": caption[:1024]}).encode("utf-8")
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
        print(f"[Telegram photo] Attempt {attempt} failed: {last_err}")
        if attempt < MAX_ATTEMPTS:
            time.sleep(RETRY_DELAY_SEC)
    print(f"Graphic send failed after {MAX_ATTEMPTS} attempts (non-fatal, text still sends): {last_err}")


def clean_body(text):
    text = text.split("\x01")[-1] if "\x01" in text else text
    text = text.replace("\x03", "").strip()
    return text


def load_state():
    return json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2))


def telegram_configured():
    return bool(os.environ.get("NWS_TELEGRAM_BOT_TOKEN")) and bool(os.environ.get("NWS_TELEGRAM_CHAT_ID"))


def send_telegram(text):
    bot_token = os.environ["NWS_TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["NWS_TELEGRAM_CHAT_ID"]
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    max_len = 4000
    chunks = [text[i:i + max_len] for i in range(0, len(text), max_len)] or [text]
    for idx, chunk in enumerate(chunks, 1):
        payload = json.dumps({"chat_id": chat_id, "text": chunk}).encode("utf-8")
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
            print(f"[Telegram] Chunk {idx} attempt {attempt} failed: {last_err}")
            if attempt < MAX_ATTEMPTS:
                time.sleep(RETRY_DELAY_SEC)
        else:
            raise RuntimeError(f"Telegram send failed after {MAX_ATTEMPTS} attempts on chunk {idx}: {last_err}")


def deliver(text, subject="NWS Warning"):
    if not telegram_configured():
        print("Telegram not configured -- skipping (no SMS fallback).")
        raise RuntimeError("Telegram not configured for NWS chat")
    send_telegram(text)
    print("Delivered via Telegram (NWS chat).")


def send_failure_alert(context, error):
    try:
        deliver(f"[nws-warnings-pipeline error] {context}: {error}", subject="nws-warnings-pipeline error")
    except Exception as e:
        print(f"Could not even send the failure alert: {e}", file=sys.stderr)


def build_message(warn_key, office_key, text):
    label = WARNING_TYPES[warn_key]["label"]
    office_name = OFFICES[office_key]
    issued = issued_time_from_header(text)
    body = clean_body(text)
    parts = []
    if issued:
        parts.append(f"Issued: {issued}")
        parts.append("")
    parts.append(f"NWS {office_name} -- {label}")
    parts.append("")
    parts.append(body)
    return "\n".join(parts).strip()


def process_warning(warn_key, office_key, state):
    text, pil = fetch_warning(warn_key, office_key)
    if not text:
        print(f"[{pil}] No active/recent warning found (normal most of the time) -- skipping quietly.")
        return

    key = f"{warn_key}_{office_key}"
    last_etn = state.get(key, {}).get("last_etn")
    last_text = state.get(key, {}).get("last_text")
    etn = vtec_event_number(text)

    # Prefer the VTEC event number for dedup (a genuinely new warning
    # always gets a new ETN) but fall back to full-text comparison if
    # somehow no VTEC line is found.
    is_new = (etn is not None and etn != last_etn) or (etn is None and text != last_text)
    if not is_new:
        print(f"[{pil}] No change -- not sending.")
        return

    print(f"[{pil}] New warning detected (ETN={etn}) -- sending.")

    if telegram_configured():
        try:
            send_telegram_photo(build_warning_graphic_url(), caption=f"{OFFICES[office_key]} {WARNING_TYPES[warn_key]['label']}")
            print(f"[{pil}] Graphic sent.")
        except Exception as e:
            print(f"[{pil}] Graphic send failed (non-fatal): {e}")

    message = build_message(warn_key, office_key, text)
    print(f"[{pil}] Message:\n{message[:400]}...")

    try:
        deliver(message, subject=f"NWS {OFFICES[office_key]} {WARNING_TYPES[warn_key]['label']}")
    except Exception as e:
        send_failure_alert(f"{pil} delivery", str(e))
        return

    print(f"[{pil}] Sent successfully.")
    state[key] = {"last_etn": etn, "last_text": text}
    save_state(state)


def main():
    state = load_state()
    for warn_key in WARNING_TYPES:
        for office_key in OFFICES:
            try:
                process_warning(warn_key, office_key, state)
            except Exception as e:
                print(f"[{warn_key}_{office_key}] Unexpected error (non-fatal, continuing): {e}")


if __name__ == "__main__":
    main()
