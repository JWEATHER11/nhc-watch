#!/usr/bin/env python3
"""
nws_afd_pipeline.py -- Tracks the local NWS Area Forecast Discussion (AFD)
for Houston/Galveston (KHGX) and Lake Charles (KLCH). AFDs get re-issued
several times a day with mostly-routine wording changes, so this caps
delivery to one AM send and one PM/evening send per office per day --
per instruction, sending on every single text diff was "the same stuff
over and over again." A newly-appearing severe-weather keyword bypasses
the cap and sends immediately regardless of how many times already sent
today. Same reliable pattern as everything else otherwise: cache-busted
fetch, full-text dedup, Telegram only, zero AI (this is the forecaster's
own words, US government work, public domain).
"""

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

IEM_BASE = "https://mesonet.agron.iastate.edu/cgi-bin/afos/retrieve.py"
BEAUMONT_TZ = ZoneInfo("America/Chicago")

OFFICES = {
    "hgx": {
        "pil": "AFDHGX",
        "nhc_fallback": "https://forecast.weather.gov/product.php?site=HGX&product=AFD&issuedby=HGX",
        "label": "NWS Houston/Galveston Area Forecast Discussion",
    },
    "lch": {
        "pil": "AFDLCH",
        "nhc_fallback": "https://forecast.weather.gov/product.php?site=LCH&product=AFD&issuedby=LCH",
        "label": "NWS Lake Charles Area Forecast Discussion",
    },
}

STATE_FILE = Path(__file__).parent / "nws_afd_state.json"
MAX_ATTEMPTS = 2  # reduced from 3 -- speed, matches wxmodel_pipeline.py fix
RETRY_DELAY_SEC = 2  # reduced from 5 -- speed, matches wxmodel_pipeline.py fix


def _http_get(url, timeout=10):  # reduced from 20 -- speed, matches wxmodel_pipeline.py fix
    req = urllib.request.Request(url, headers={"User-Agent": "nws-afd-pipeline/1.0"})
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


def fetch_afd(office_key):
    cfg = OFFICES[office_key]
    cache_buster = int(time.time())
    iem_url = f"{IEM_BASE}?pil={cfg['pil']}&_cb={cache_buster}"
    text = _fetch_with_retries(iem_url, f"IEM:{cfg['pil']}")
    if text:
        return text, "IEM"
    print(f"[{cfg['pil']}] IEM failed, falling back to weather.gov...")
    text = _fetch_with_retries(cfg["nhc_fallback"], f"NWS:{cfg['pil']}")
    if text:
        return text, "NWS"
    return None, "FAILED"


def issued_time_from_header(text):
    m = re.search(r"^\s*\d{3,4}\s+[AP]M\s+[A-Z]{2,4}\s+\w{3}\s+\w{3}\s+\d{1,2}\s+\d{4}\s*$", text, re.M)
    return m.group(0).strip() if m else None


# Sections we never want -- stripped out entirely before sending, per
# instruction. Matches the AFD's own ".SECTIONNAME..." ... "&&" format.
UNWANTED_SECTIONS = ["AVIATION", "MARINE", "FIRE WEATHER"]


def strip_unwanted_sections(text):
    for section in UNWANTED_SECTIONS:
        pattern = re.compile(
            rf"\n\s*\.{re.escape(section)}\.\.\..*?\n\s*&&\s*\n",
            re.S,
        )
        text = pattern.sub("\n", text)
    return text


def reflow_text(text):
    """NWS text products are hard-wrapped at a fixed width (old teletype
    convention) -- this rejoins wrapped lines back into natural, readable
    paragraphs for Telegram, while preserving real structure: section
    headers (.DISCUSSION...), separators (&&, $$), "Issued at" lines, and
    bullet points (each "- ..." item stays its own line, with its
    wrapped continuation lines folded back into it)."""
    lines = text.split("\n")
    output_lines = []
    buffer = []

    def flush_buffer():
        if buffer:
            output_lines.append(" ".join(buffer))
            buffer.clear()

    for line in lines:
        stripped = line.strip()
        if not stripped:
            flush_buffer()
            output_lines.append("")
        elif stripped.startswith(".") or stripped in ("&&", "$$") or re.match(r"^Issued at", stripped):
            flush_buffer()
            output_lines.append(stripped)
        elif stripped.startswith("- "):
            flush_buffer()
            buffer.append(stripped)
        else:
            buffer.append(stripped)
    flush_buffer()
    return "\n".join(output_lines)


def clean_body(text):
    """Keeps ONLY the Key Messages and Discussion sections -- strips the
    WMO/AFOS routing header, the '...New X, Y, Z...' indicator line,
    Aviation/Marine/Fire Weather, Watches/Warnings/Advisories, the
    trailing '&&' / '$$' markers, and the admin footer line, then
    reflows the hard-wrapped text into natural paragraphs."""
    text = text.split("\x01")[-1] if "\x01" in text else text
    text = text.replace("\x03", "").strip()

    # Strip the raw routing header (e.g. "924\nFXUS64 KLCH 261725\nAFDLCH")
    # -- start from the actual title line onward instead.
    m = re.search(r"Area Forecast Discussion", text)
    if m:
        text = text[m.start():]

    # Strip the "...New DISCUSSION, AVIATION, MARINE, FIRE WEATHER..." line.
    text = re.sub(r"^\s*\.\.\.New[^\n]*\.\.\.\s*\n", "", text, flags=re.M)

    # Keep only Key Messages + Discussion. Text splits on "&&" into:
    # [0]=Key Messages, [1]=Discussion, [2+]=Watches/Warnings/$$/footer.
    # Keep segments 0 and 1, drop everything else, and drop the "&&"
    # characters themselves entirely (never wanted in the output).
    segments = text.split("&&")
    if len(segments) >= 2:
        text = segments[0].strip() + "\n\n" + segments[1].strip()
    else:
        text = segments[0].strip()

    text = reflow_text(text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


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
    # Telegram caps a single message at 4096 chars -- AFDs can run long.
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


def deliver(text, subject="NWS AFD Update"):
    """Telegram only -- no SMS/email fallback, matching every other
    pipeline in this system. Raises if not configured, so callers
    correctly don't mark the item as sent."""
    if not telegram_configured():
        print("Telegram not configured -- skipping (no SMS fallback).")
        raise RuntimeError("Telegram not configured for NWS chat")
    send_telegram(text)
    print("Delivered via Telegram (NWS chat).")


def send_failure_alert(context, error):
    try:
        deliver(f"[nws-afd-pipeline error] {context}: {error}", subject="nws-afd-pipeline error")
    except Exception as e:
        print(f"Could not even send the failure alert: {e}", file=sys.stderr)


def build_message(office_key, text):
    cfg = OFFICES[office_key]
    issued = issued_time_from_header(text)
    body = clean_body(text)
    parts = []
    if issued:
        parts.append(f"Issued: {issued}")
        parts.append("")
    parts.append(cfg["label"])
    parts.append("")
    parts.append(body)
    return "\n".join(parts).strip()


AFD_SEVERE_KEYWORDS = [
    "TORNADO", "SEVERE THUNDERSTORM", "FLASH FLOOD EMERGENCY", "FLASH FLOOD WARNING",
    "PARTICULARLY DANGEROUS SITUATION", "EXCESSIVE HEAT WARNING", "HIGH WIND WARNING",
    "HURRICANE WARNING", "TROPICAL STORM WARNING", "STORM SURGE WARNING",
]


def _is_big_change(new_text, old_text):
    """A newly-appearing severe-weather keyword bypasses the AM/PM
    send cap below, per instruction -- routine wording tweaks between
    issuances should not, but a genuine new severe signal always
    should, immediately, regardless of how many times already sent
    today."""
    if not old_text:
        return True
    new_upper, old_upper = new_text.upper(), old_text.upper()
    return any(kw in new_upper and kw not in old_upper for kw in AFD_SEVERE_KEYWORDS)


def _afd_slot(now_local):
    """AM = before noon, PM/evening = noon onward, per instruction."""
    return "am" if now_local.hour < 12 else "pm"


def process_office(office_key, state):
    cfg = OFFICES[office_key]
    text, source = fetch_afd(office_key)
    if not text:
        print(f"[{office_key}] Both IEM and NWS failed -- skipping this cycle (non-fatal).")
        return
    print(f"[{office_key}] Fetched from {source}")

    office_state = state.get(office_key, {})
    last_text = office_state.get("last_text")
    if text == last_text:
        print(f"[{office_key}] No change -- not sending.")
        return

    now_local = datetime.now(BEAUMONT_TZ)
    today_str = now_local.strftime("%Y-%m-%d")
    slot = _afd_slot(now_local)
    big_change = _is_big_change(text, last_text)

    if office_state.get("send_day") != today_str:
        office_state["send_day"] = today_str
        office_state["slots_sent"] = []

    if not big_change and slot in office_state.get("slots_sent", []):
        print(f"[{office_key}] Updated, but {slot.upper()} slot already sent today and nothing severe -- not resending (routine update).")
        office_state["last_text"] = text
        state[office_key] = office_state
        save_state(state)
        return

    print(f"[{office_key}] Sending -- {'severe keyword newly appeared' if big_change else slot.upper() + ' slot'}.")
    message = build_message(office_key, text)
    print(f"[{office_key}] Message:\n{message[:500]}...")

    try:
        deliver(message, subject=cfg["label"])
    except Exception as e:
        send_failure_alert(f"{office_key} delivery", str(e))
        return

    print(f"[{office_key}] Sent successfully.")
    office_state["last_text"] = text
    office_state["slots_sent"] = list(set(office_state.get("slots_sent", []) + [slot]))
    state[office_key] = office_state
    save_state(state)


def main():
    state = load_state()
    for office_key in OFFICES:
        try:
            process_office(office_key, state)
        except Exception as e:
            print(f"[{office_key}] Unexpected error (non-fatal, continuing): {e}")


if __name__ == "__main__":
    main()
