#!/usr/bin/env python3
"""
nws_afd_pipeline.py -- Tracks the local NWS Area Forecast Discussion (AFD)
for Houston/Galveston (KHGX) and Lake Charles (KLCH), sending the full
text to the SPC Telegram chat every time either office issues a genuinely
new one. Same reliable pattern as everything else: cache-busted fetch,
full-text dedup, Telegram only, zero AI (this is the forecaster's own
words, US government work, public domain).
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
MAX_ATTEMPTS = 3
RETRY_DELAY_SEC = 5


def _http_get(url, timeout=20):
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
    """Strips the WMO/AFOS routing header lines, keeps everything from
    the product title onward, collapses the page-break control character
    IEM includes at the end, removes the Aviation/Marine/Fire Weather
    sections entirely per instruction, and reflows the hard-wrapped text
    into natural paragraphs for readability."""
    text = text.split("\x01")[-1] if "\x01" in text else text
    text = text.replace("\x03", "").strip()
    text = strip_unwanted_sections(text)
    text = reflow_text(text)
    text = re.sub(r"\n{3,}", "\n\n", text)  # collapse extra blank lines left behind
    return text.strip()


def load_state():
    return json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2))


def telegram_configured():
    return bool(os.environ.get("SPC_TELEGRAM_BOT_TOKEN")) and bool(os.environ.get("SPC_TELEGRAM_CHAT_ID"))


def send_telegram(text):
    bot_token = os.environ["SPC_TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["SPC_TELEGRAM_CHAT_ID"]
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
        raise RuntimeError("Telegram not configured for SPC chat")
    send_telegram(text)
    print("Delivered via Telegram (SPC chat).")


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


def process_office(office_key, state):
    cfg = OFFICES[office_key]
    text, source = fetch_afd(office_key)
    if not text:
        print(f"[{office_key}] Both IEM and NWS failed -- skipping this cycle (non-fatal).")
        return
    print(f"[{office_key}] Fetched from {source}")

    last_text = state.get(office_key, {}).get("last_text")
    if text == last_text:
        print(f"[{office_key}] No change -- not sending.")
        return

    print(f"[{office_key}] New AFD detected -- sending.")
    message = build_message(office_key, text)
    print(f"[{office_key}] Message:\n{message[:500]}...")

    try:
        deliver(message, subject=cfg["label"])
    except Exception as e:
        send_failure_alert(f"{office_key} delivery", str(e))
        return

    print(f"[{office_key}] Sent successfully.")
    state[office_key] = {"last_text": text}
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
