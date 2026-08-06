#!/usr/bin/env python3
"""
nws_afd_pipeline.py -- Tracks the local NWS Area Forecast Discussion (AFD)
for Houston/Galveston (KHGX) and Lake Charles (KLCH). Per instruction,
this no longer forwards the forecaster's full discussion text at all --
it sends the AFD to Claude to check for anything actually impactful
(fronts, storms, flooding, tropical/hurricane, severe weather), and only
delivers a short plain-English summary when something impactful is
present. Routine reissuances with nothing new to report send nothing.
Cache-busted fetch, full-text dedup to avoid re-summarizing unchanged
text, Telegram only.
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


def build_message(office_key, summary):
    cfg = OFFICES[office_key]
    parts = [f"📋 {cfg['label']} -- impact summary", "", summary]
    return "\n".join(parts).strip()


AFD_SUMMARY_PROMPT = """You read National Weather Service Area Forecast Discussions (AFDs) for Houston/Galveston or Lake Charles and decide if there's anything worth telling a Beaumont, TX resident about.

Only flag something if the discussion mentions:
- A cold front (arriving, stalling, lifting back north)
- Storms, especially organized or heavy rain potential
- Flooding or flash flooding risk
- Tropical or hurricane impacts
- Severe weather (tornado, damaging wind, large hail)
- Any other genuinely impactful weather (extreme heat, high wind, etc.)

Routine forecast wording with no real impact (typical daily temps, ordinary rain chances, sea breeze, routine humidity discussion) does NOT count -- skip it.

If NOTHING impactful is present, respond with exactly: NOTHING

If something impactful IS present, write a short, plain-English summary (2-5 sentences, no meteorologist jargon) covering ONLY the impactful part -- not a recap of the whole discussion. Be direct about timing if the forecaster gives it."""


def call_claude_api(afd_text):
    api_key = os.environ["ANTHROPIC_API_KEY"]
    payload = json.dumps({
        "model": "claude-sonnet-5",
        "max_tokens": 300,
        "system": AFD_SUMMARY_PROMPT,
        "messages": [{"role": "user", "content": afd_text}],
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

    body = clean_body(text)
    try:
        summary = call_claude_api(body)
    except Exception as e:
        send_failure_alert(f"{office_key} summarization", str(e))
        return

    office_state["last_text"] = text
    state[office_key] = office_state

    if summary.strip().upper() == "NOTHING":
        print(f"[{office_key}] Updated, but nothing impactful -- not sending.")
        save_state(state)
        return

    if summary.strip() == office_state.get("last_sent_summary"):
        print(f"[{office_key}] Updated, but the impactful content is unchanged from what was already sent -- not resending.")
        save_state(state)
        return

    print(f"[{office_key}] Sending impact summary.")
    message = build_message(office_key, summary)
    print(f"[{office_key}] Message:\n{message}")

    try:
        deliver(message, subject=cfg["label"])
    except Exception as e:
        send_failure_alert(f"{office_key} delivery", str(e))
        return

    print(f"[{office_key}] Sent successfully.")
    office_state["last_sent_summary"] = summary.strip()
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
