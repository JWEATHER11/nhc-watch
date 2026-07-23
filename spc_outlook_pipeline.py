#!/usr/bin/env python3
"""
spc_outlook_pipeline.py -- Tracks SPC's Convective Outlooks (severe
thunderstorm/tornado/hail/wind risk), Day 1 through Day 3 individually
plus a combined Day 4-8, and sends each one to its own Telegram chat the
moment it genuinely updates.

Deliberately does NOT track SPC's Fire Weather Outlook -- convective
(severe storm) outlooks only, per explicit instruction.

Same pattern as the NHC pipelines: graphic sent first (fast), then the
text/discussion once fetched and verified genuinely new (Discussion
Number/text-diff style dedup -- here, full-text comparison per outlook
day, since these aren't individually numbered the way NHC discussions
are). Cache-busted on every single fetch so nothing stale ever gets
served as if new.

Each of Day 1/2/3/4-8 is checked and tracked completely independently,
since they update on different real-world schedules (Day 1 up to 5x/day,
Day 2 2x/day, Day 3 1-2x/day, Day 4-8 1x/day).
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

OUTLOOKS = {
    "day1": {
        "pil": "SWODY1",
        "nhc_fallback": "https://www.spc.noaa.gov/products/outlook/day1otlk.html?text",
        "graphic": "https://www.spc.noaa.gov/products/outlook/day1otlk.gif",
        "label": "SPC Day 1 Convective Outlook",
    },
    "day2": {
        "pil": "SWODY2",
        "nhc_fallback": "https://www.spc.noaa.gov/products/outlook/day2otlk.html?text",
        "graphic": "https://www.spc.noaa.gov/products/outlook/day2otlk.gif",
        "label": "SPC Day 2 Convective Outlook",
    },
    "day3": {
        "pil": "SWODY3",
        "nhc_fallback": "https://www.spc.noaa.gov/products/outlook/day3otlk.html?text",
        "graphic": "https://www.spc.noaa.gov/products/outlook/day3otlk.gif",
        "label": "SPC Day 3 Convective Outlook",
    },
    "day48": {
        "pil": "SWOD48",
        "nhc_fallback": "https://www.spc.noaa.gov/products/exper/day4-8/day4-8.html?text",
        "graphic": "https://www.spc.noaa.gov/products/exper/day4-8/day48prob.gif",
        "label": "SPC Day 4-8 Severe Weather Outlook",
    },
    "mcd": {
        "pil": "SWOMCD",
        "nhc_fallback": "https://www.spc.noaa.gov/products/md/latest.html?text",
        "graphic": None,  # MCDs don't have a stable "latest" graphic URL like the outlooks do
        "label": "SPC Mesoscale Discussion",
    },
}

# Fire Weather Outlook (SWOFWO / FWDY1 etc.) is deliberately NOT tracked
# here -- convective/severe weather products only, per explicit instruction.

STATE_FILE = Path(__file__).parent / "spc_outlook_state.json"
MAX_ATTEMPTS = 3
RETRY_DELAY_SEC = 5


def _http_get(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": "spc-outlook-pipeline/1.0"})
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


def fetch_outlook_text(day_key):
    cfg = OUTLOOKS[day_key]
    cache_buster = int(time.time())
    iem_url = f"{IEM_BASE}?pil={cfg['pil']}&_cb={cache_buster}"
    text = _fetch_with_retries(iem_url, f"IEM:{cfg['pil']}")
    if text:
        return text, "IEM"
    print(f"[{cfg['pil']}] IEM failed, falling back to SPC directly...")
    fallback_url = f"{cfg['nhc_fallback']}&_cb={cache_buster}"
    text = _fetch_with_retries(fallback_url, f"SPC:{cfg['pil']}")
    if text:
        return text, "SPC"
    return None, "FAILED"


def graphic_url(day_key):
    base = OUTLOOKS[day_key]["graphic"]
    if not base:
        return None
    cache_buster = int(time.time())
    return f"{base}?_cb={cache_buster}"


def issued_time_from_header(text):
    m = re.search(r"^\s*\d{3,4}\s+[AP]M\s+[A-Z]{2,4}\s+\w{3}\s+\w{3}\s+\d{1,2}\s+\d{4}\s*$", text, re.M)
    return m.group(0).strip() if m else None


def summary_section(text):
    m = re.search(r"\.\.\.SUMMARY\.\.\.\s*\n(.*?)\n\s*\n", text, re.I | re.S)
    if m:
        return re.sub(r"\s+", " ", m.group(1)).strip()
    return None


def headline_section(text):
    """The '...THERE IS A [RISK] ...' line(s) right after the header --
    SPC's own plain-language headline for this outlook."""
    m = re.search(r"\n\.\.\.(THERE IS[^\n]*(?:\n[^\n.]+)*)\.\.\.", text, re.I)
    if m:
        return re.sub(r"\s+", " ", m.group(1)).strip()
    return None


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


def send_telegram_photo(photo_url, caption=""):
    bot_token = os.environ["SPC_TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["SPC_TELEGRAM_CHAT_ID"]
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


def send_email_sms_fallback(text, subject="SPC Outlook Update"):
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


def deliver(text, subject="SPC Outlook Update"):
    if telegram_configured():
        send_telegram(text)
        print("Delivered via Telegram (SPC chat).")
    else:
        send_email_sms_fallback(text, subject=subject)
        print("Delivered via email-to-SMS fallback.")


def send_failure_alert(context, error):
    try:
        deliver(f"[spc-outlook-pipeline error] {context}: {error}", subject="spc-outlook-pipeline error")
    except Exception as e:
        print(f"Could not even send the failure alert: {e}", file=sys.stderr)


def build_message(day_key, text):
    cfg = OUTLOOKS[day_key]
    issued = issued_time_from_header(text)
    parts = []
    if issued:
        parts.append(f"Issued: {issued}")
        parts.append("")
    parts.append(cfg["label"])
    parts.append("")

    headline = headline_section(text)
    if headline:
        parts.append(headline)
        parts.append("")

    summary = summary_section(text)
    if summary:
        parts.append(summary)
        parts.append("")

    parts.append(f"View live: {cfg['nhc_fallback'].split('?')[0].replace('.html?text', '.html').replace('?text', '')}")
    return "\n".join(parts).rstrip()


def process_day(day_key, state):
    cfg = OUTLOOKS[day_key]
    text, source = fetch_outlook_text(day_key)
    if not text:
        print(f"[{day_key}] Both IEM and SPC failed -- skipping this cycle (non-fatal).")
        return
    print(f"[{day_key}] Fetched from {source}")

    last_text = state.get(day_key, {}).get("last_text")
    if text == last_text:
        print(f"[{day_key}] No change -- not sending.")
        return

    print(f"[{day_key}] New content detected -- sending graphic first, then text.")

    photo_url = graphic_url(day_key)
    if telegram_configured() and photo_url:
        try:
            send_telegram_photo(photo_url, caption=cfg["label"])
            print(f"[{day_key}] Graphic sent.")
        except Exception as e:
            print(f"[{day_key}] Graphic send failed (non-fatal): {e}")

    message = build_message(day_key, text)
    print(f"[{day_key}] Message:\n{message}")

    try:
        deliver(message, subject=cfg["label"])
    except Exception as e:
        send_failure_alert(f"{day_key} delivery", str(e))
        return

    print(f"[{day_key}] Sent successfully.")
    state[day_key] = {"last_text": text}
    save_state(state)


def main():
    state = load_state()
    for day_key in OUTLOOKS:
        try:
            process_day(day_key, state)
        except Exception as e:
            print(f"[{day_key}] Unexpected error (non-fatal, continuing to next day): {e}")


if __name__ == "__main__":
    main()
