#!/usr/bin/env python3
"""
wpc_ero_pipeline.py -- Tracks the WPC (Weather Prediction Center)
Excessive Rainfall Discussion (QPFERD), which covers Day 1 through
Day 7 (Days 1-3 individually, Days 4-5 and 6-7 combined in the same
discussion text). Only sends when a day's section BOTH mentions the
Houston/Galveston (SETX) or Lake Charles (SWLA) region AND carries a
Slight, Moderate, or High risk -- Marginal-only mentions are skipped
entirely, per instruction. Each matching day gets its own graphic.

Sent to the NWS Telegram chat, same reliable pattern as everything
else: cache-busted fetch, dedup, Telegram only, no AI.
"""

import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

IEM_BASE = "https://mesonet.agron.iastate.edu/cgi-bin/afos/retrieve.py"
STATE_FILE = Path(__file__).parent / "wpc_ero_state.json"
MAX_ATTEMPTS = 2  # reduced from 3 -- speed, matches wxmodel_pipeline.py fix
RETRY_DELAY_SEC = 2  # reduced from 5 -- speed, matches wxmodel_pipeline.py fix

# Region keywords covering the Houston-to-Jasper, Beaumont/Port
# Arthur/Orange-to-Lake Charles area (SETX / SWLA).
REGION_KEYWORDS = [
    "HOUSTON", "GALVESTON", "BEAUMONT", "PORT ARTHUR", "ORANGE",
    "JASPER", "LAKE CHARLES", "SOUTHEAST TEXAS", "SOUTHWEST LOUISIANA",
    "SETX", "SWLA", "GOLDEN TRIANGLE", "SE TEXAS", "SW LOUISIANA",
]
NON_MARGINAL_RISK_WORDS = ["HIGH RISK", "MODERATE RISK", "SLIGHT RISK"]

DAY_LABELS = {
    1: "1E", 2: "2E", 3: "3E", 4: "4E", 5: "5E", 6: "6E", 7: "7E",
}


def _http_get(url, timeout=10):  # reduced from 20 -- speed, matches wxmodel_pipeline.py fix
    req = urllib.request.Request(url, headers={"User-Agent": "wpc-ero-pipeline/1.0"})
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


def fetch_ero_discussion():
    cache_buster = int(time.time())
    url = f"{IEM_BASE}?pil=QPFERD&_cb={cache_buster}"
    return _fetch_with_retries(url, "IEM:QPFERD")


def clean_body(text):
    text = text.split("\x01")[-1] if "\x01" in text else text
    return text.replace("\x03", "").strip()


def split_into_day_blocks(text):
    """Splits the full discussion into per-day (or per-day-pair) blocks,
    e.g. 'Day 1', 'Day 2', 'Day 4 and Day 5', 'Day 6 and Day 7'. Returns
    a list of (day_numbers, block_text) tuples."""
    pattern = re.compile(
        r"^Day (\d+)(?:\s+and\s+Day\s+(\d+))?\s*\n\s*Valid",
        re.M,
    )
    matches = list(pattern.finditer(text))
    blocks = []
    for i, m in enumerate(matches):
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        block_text = text[start:end].strip()
        days = [int(m.group(1))]
        if m.group(2):
            days.append(int(m.group(2)))
        blocks.append((days, block_text))
    return blocks


def block_matches_region_and_risk(block_text):
    """True only if the block mentions our region AND a non-marginal
    (Slight/Moderate/High) risk -- Marginal-only mentions never match,
    per instruction."""
    upper = block_text.upper()
    has_region = any(kw in upper for kw in REGION_KEYWORDS)
    has_risk = any(kw in upper for kw in NON_MARGINAL_RISK_WORDS)
    return has_region and has_risk


def graphic_url(day_num):
    which = DAY_LABELS[day_num]
    now_utc_str = time.strftime("%Y-%m-%d %H%M", time.gmtime())
    encoded_valid = urllib.parse.quote(now_utc_str)
    cache_buster = int(time.time())
    return (
        f"https://mesonet.agron.iastate.edu/plotting/auto/plot/220/"
        f"which:{which}::cat:any::t:state::csector:conus::"
        f"valid:{encoded_valid}::dpi:100.png&_cb={cache_buster}"
    )


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
            if attempt < MAX_ATTEMPTS:
                time.sleep(RETRY_DELAY_SEC)
        else:
            raise RuntimeError(f"Telegram send failed after {MAX_ATTEMPTS} attempts on chunk {idx}: {last_err}")


def send_telegram_photo(photo_url, caption=""):
    """Downloads the image ourselves and uploads the bytes directly to
    Telegram (multipart/form-data) -- more reliable than passing the URL
    for Telegram to fetch itself. Single fast attempt, short timeout --
    graphic is best-effort only and must never delay the text."""
    bot_token = os.environ["NWS_TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["NWS_TELEGRAM_CHAT_ID"]
    try:
        image_req = urllib.request.Request(photo_url, headers={"User-Agent": "wpc-ero-pipeline/1.0"})
        with urllib.request.urlopen(image_req, timeout=8) as img_resp:
            image_bytes = img_resp.read()

        boundary = "----wpcPhotoBoundary"
        parts = [
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"chat_id\"\r\n\r\n{chat_id}\r\n".encode("utf-8"),
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"caption\"\r\n\r\n{caption[:1024]}\r\n".encode("utf-8"),
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"photo\"; filename=\"graphic.png\"\r\nContent-Type: image/png\r\n\r\n".encode("utf-8"),
            image_bytes,
            f"\r\n--{boundary}--\r\n".encode("utf-8"),
        ]
        body = b"".join(parts)

        url = f"https://api.telegram.org/bot{bot_token}/sendPhoto"
        req = urllib.request.Request(
            url, data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=8) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            if not result.get("ok"):
                print(f"Graphic send failed (non-fatal): {result.get('description')}")
    except Exception as e:
        print(f"Graphic send failed (non-fatal): {e}")


def deliver(text, subject="WPC ERO Update"):
    if not telegram_configured():
        print("Telegram not configured -- skipping (no SMS fallback).")
        raise RuntimeError("Telegram not configured for NWS chat")
    send_telegram(text)


def send_failure_alert(context, error):
    try:
        deliver(f"[wpc-ero-pipeline error] {context}: {error}")
    except Exception as e:
        print(f"Could not even send the failure alert: {e}", file=sys.stderr)


def build_message(days, block_text):
    day_str = "/".join(f"Day {d}" for d in days)
    return f"🌊 WPC Excessive Rainfall Outlook -- {day_str}\n\n{block_text}"


def process_ero(state):
    raw = fetch_ero_discussion()
    if not raw:
        print("QPFERD fetch failed this cycle (non-fatal).")
        return
    text = clean_body(raw)
    blocks = split_into_day_blocks(text)
    if not blocks:
        print("No day blocks parsed from QPFERD -- unexpected format, skipping this cycle.")
        return

    for days, block_text in blocks:
        if not block_matches_region_and_risk(block_text):
            continue

        key = "_".join(str(d) for d in days)
        last_block = state.get(key)
        if block_text == last_block:
            print(f"[Day {key}] Region+risk match, but unchanged -- not resending.")
            continue

        print(f"[Day {key}] Region+risk match (non-Marginal) -- sending.")

        # Graphic first (bounded, fast, never blocks text below), then
        # the text -- text is guaranteed regardless of graphic outcome.
        if telegram_configured():
            for d in days:
                send_telegram_photo(graphic_url(d), caption=f"WPC Day {d} Excessive Rainfall Outlook")

        message = build_message(days, block_text)
        try:
            deliver(message, subject=f"WPC ERO Day {key}")
        except Exception as e:
            send_failure_alert(f"Day {key} delivery", str(e))
            continue

        print(f"[Day {key}] Sent successfully.")
        state[key] = block_text
        save_state(state)


def main():
    state = load_state()
    try:
        process_ero(state)
    except Exception as e:
        print(f"Unexpected error (non-fatal): {e}")


if __name__ == "__main__":
    main()
