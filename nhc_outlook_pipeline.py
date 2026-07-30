#!/usr/bin/env python3
"""
nhc_outlook_pipeline.py -- Tracks NHC's Tropical Weather Outlook (TWOAT),
a basin-wide product covering ALL areas being monitored for potential
development across the North Atlantic, Caribbean, and Gulf -- separate
from the storm-specific advisory pipeline.

Issued ~4x daily (roughly 2/8 AM & 2/8 PM Eastern) with special updates
issued anytime conditions warrant. Sent to Telegram every time the
content genuinely changes -- deduped on the full text, not just a
timestamp, since NHC can reissue this with the same header at times but
updated content.

Pure text relay, no AI -- this is a US government work (public domain),
NHC's own words, same approach as the Discussion product.
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
OUTLOOK_PIL = "TWOAT"
NHC_OUTLOOK_URL = "https://www.nhc.noaa.gov/text/MIATWOAT.shtml?text"

STATE_FILE = Path(__file__).parent / "outlook_state.json"

MAX_ATTEMPTS = 2  # reduced from 3 -- speed, matches wxmodel_pipeline.py fix
RETRY_DELAY_SEC = 2  # reduced from 5 -- speed, matches wxmodel_pipeline.py fix


def _http_get(url, timeout=10):  # reduced from 20 -- speed, matches wxmodel_pipeline.py fix
    req = urllib.request.Request(url, headers={"User-Agent": "nhc-outlook-pipeline/1.0"})
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


def fetch_outlook():
    cache_buster = int(time.time())
    iem_url = f"{IEM_BASE}?pil={OUTLOOK_PIL}&_cb={cache_buster}"
    text = _fetch_with_retries(iem_url, f"IEM:{OUTLOOK_PIL}")
    if text:
        return text, "IEM"
    print(f"[{OUTLOOK_PIL}] IEM failed, falling back to NHC...")
    nhc_url = f"{NHC_OUTLOOK_URL}&_cb={cache_buster}"
    text = _fetch_with_retries(nhc_url, f"NHC:{OUTLOOK_PIL}")
    if text:
        return text, "NHC"
    return None, "FAILED"


def build_outlook_graphic_url():
    """Atlantic 7-Day Outlook graphic -- confirmed live URL. NHC overwrites
    this same file with the current graphic; a cache-buster query param
    forces Telegram to fetch fresh every time instead of reusing a
    previously cached copy of this same URL."""
    cache_buster = int(time.time())
    return f"https://www.nhc.noaa.gov/xgtwo/resize/xgtwo_atl_7d0_w1920.png?_cb={cache_buster}"


def send_telegram_photo(photo_url, caption=""):
    """Downloads the image ourselves and uploads the bytes directly to
    Telegram (multipart/form-data), instead of asking Telegram to fetch
    the URL itself -- more reliable; some URLs get rejected by Telegram
    with 'wrong type of the web page content' even when genuinely valid
    images."""
    bot_token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    last_err = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            image_req = urllib.request.Request(photo_url, headers={"User-Agent": "nhc-outlook-pipeline/1.0"})
            with urllib.request.urlopen(image_req, timeout=15) as img_resp:
                image_bytes = img_resp.read()

            boundary = "----outlookPhotoBoundary"
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
    print(f"Outlook graphic send failed after {MAX_ATTEMPTS} attempts (non-fatal): {last_err}")


def issued_time_from_header(text):
    m = re.search(r"^\s*\d{3,4}\s+[AP]M\s+[A-Z]{2,4}\s+\w{3}\s+\w{3}\s+\d{1,2}\s+\d{4}\s*$", text, re.M)
    return m.group(0).strip() if m else None


def parse_areas(text):
    """Each numbered area looks like:
    1. Southwestern Gulf:
    <description paragraph>
    * Formation chance through 48 hours...low...10 percent.
    * Formation chance through 7 days...low...10 percent.
    """
    areas = []
    pattern = re.compile(
        r"^\s*(\d+)\.\s+(.+?):\s*\n(.*?)"
        r"\*\s*Formation chance through 48 hours\.\.\.(\w+)\.\.\.(?:near )?(\d+) percent\.\s*\n"
        r"\*\s*Formation chance through 7 days\.\.\.(\w+)\.\.\.(?:near )?(\d+) percent\.",
        re.I | re.M | re.S,
    )
    for m in pattern.finditer(text):
        desc = re.sub(r"\s+", " ", m.group(3)).strip()
        areas.append({
            "number": m.group(1),
            "region": m.group(2).strip(),
            "description": desc,
            "chance_48h_category": m.group(4).strip().title(),
            "chance_48h_pct": int(m.group(5)),
            "chance_7day_category": m.group(6).strip().title(),
            "chance_7day_pct": int(m.group(7)),
        })
    return areas


def no_development_expected(text):
    return bool(re.search(r"Tropical cyclone formation is not expected during the next 7 days", text, re.I))


def active_systems_summary(text):
    m = re.search(r"Active Systems:\s*\n(.*?)\n\s*\n", text, re.I | re.S)
    if not m:
        return None
    block = re.sub(r"\s+", " ", m.group(1)).strip()
    return block if block else None


def load_state():
    return json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2))


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


def send_email_sms_fallback(text, subject="NHC Outlook Update"):
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


def deliver(text, subject="NHC Outlook Update"):
    """Telegram only -- no SMS/email fallback, per explicit instruction."""
    if not telegram_configured():
        print("Telegram not configured -- skipping (no SMS fallback, per instruction).")
        raise RuntimeError("Telegram not configured (SMS fallback disabled per instruction)")
    send_telegram(text)
    print("Delivered via Telegram.")


def send_failure_alert(context, error):
    try:
        deliver(f"[nhc-outlook-pipeline error] {context}: {error}", subject="nhc-outlook-pipeline error")
    except Exception as e:
        print(f"Could not even send the failure alert: {e}", file=sys.stderr)


def build_message(text):
    issued = issued_time_from_header(text)
    parts = []
    if issued:
        parts.append(f"Issued: {issued}")
        parts.append("")
    parts.append("NHC 7-Day Tropical Weather Outlook")
    parts.append("")

    active = active_systems_summary(text)
    if active:
        parts.append(f"Active Systems: {active}")
        parts.append("")

    if no_development_expected(text):
        parts.append("Tropical cyclone formation is not expected during the next 7 days.")
        parts.append("")
        parts.append("View live: https://www.nhc.noaa.gov/gtwo.php?basin=atlc&fdays=7")
        return "\n".join(parts)

    areas = parse_areas(text)
    if not areas:
        parts.append("(No numbered disturbance areas parsed from this outlook -- see hurricanes.gov for the full text.)")
        parts.append("")
        parts.append("View live: https://www.nhc.noaa.gov/gtwo.php?basin=atlc&fdays=7")
        return "\n".join(parts)

    for area in areas:
        parts.append(f"{area['number']}. {area['region']}:")
        parts.append(area["description"])
        parts.append(f"48-hr formation chance: {area['chance_48h_category']} ({area['chance_48h_pct']}%)")
        parts.append(f"7-day formation chance: {area['chance_7day_category']} ({area['chance_7day_pct']}%)")
        parts.append("")

    parts.append("View live: https://www.nhc.noaa.gov/gtwo.php?basin=atlc&fdays=7")
    return "\n".join(parts).rstrip()


def main():
    text, source = fetch_outlook()
    if not text:
        send_failure_alert("Fetching Tropical Weather Outlook", "Both IEM and NHC failed")
        sys.exit(1)
    print(f"Outlook fetched from {source}")

    state = load_state()
    last_text = state.get("last_outlook_text")

    if text == last_text:
        print("No change in outlook text -- not sending an update.")
        return

    message = build_message(text)
    print(f"Outlook message:\n{message}")

    if telegram_configured():
        try:
            send_telegram_photo(build_outlook_graphic_url(), caption="NHC Atlantic 7-Day Tropical Weather Outlook")
            print("Outlook graphic sent.")
        except Exception as e:
            print(f"Outlook graphic send failed (non-fatal): {e}")

    try:
        deliver(message, subject="NHC 7-Day Tropical Weather Outlook")
    except Exception as e:
        send_failure_alert("Outlook delivery", str(e))
        sys.exit(1)

    print("Sent successfully.")
    state["last_outlook_text"] = text
    save_state(state)


if __name__ == "__main__":
    main()
