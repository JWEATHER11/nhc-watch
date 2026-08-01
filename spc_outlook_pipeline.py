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
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from pathlib import Path

IEM_BASE = "https://mesonet.agron.iastate.edu/cgi-bin/afos/retrieve.py"

OUTLOOKS = {
    "day1": {
        "pil": "SWODY1",
        "nhc_fallback": "https://www.spc.noaa.gov/products/outlook/day1otlk.html?text",
        "which": "1C",
        "cat": "categorical",
        "label": "SPC Day 1 Convective Outlook",
    },
    "day2": {
        "pil": "SWODY2",
        "nhc_fallback": "https://www.spc.noaa.gov/products/outlook/day2otlk.html?text",
        "which": "2C",
        "cat": "categorical",
        "label": "SPC Day 2 Convective Outlook",
    },
    "day3": {
        "pil": "SWODY3",
        "nhc_fallback": "https://www.spc.noaa.gov/products/outlook/day3otlk.html?text",
        "which": "3C",
        "cat": "categorical",
        "label": "SPC Day 3 Convective Outlook",
    },
    "day48": {
        "pil": "SWOD48",
        "nhc_fallback": "https://www.spc.noaa.gov/products/exper/day4-8/day4-8.html?text",
        # Verified live against IEM autoplot #220's actual <select>
        "which": "0C",  # was "48" -- not a real option, IEM was silently
        "cat": "categorical",  # was "any" -- also not a real option
        # falling back to something else entirely (Day 1's graphic),
        # per instruction -- exactly what was being reported.
        "label": "SPC Day 4-8 Severe Weather Outlook",
    },
}

# Deliberately NOT tracked: Fire Weather Outlook (convective outlooks
# only), and Mesoscale Discussions (outlooks only, per explicit
# instruction -- MCD tracking code removed).

STATE_FILE = Path(__file__).parent / "spc_outlook_state.json"
MAX_ATTEMPTS = 2  # reduced from 3 -- speed, matches wxmodel_pipeline.py fix
RETRY_DELAY_SEC = 2  # reduced from 5 -- speed, matches wxmodel_pipeline.py fix


def _http_get(url, timeout=10):  # reduced from 20 -- speed, matches wxmodel_pipeline.py fix
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
    """SPC's own site moved to a layered interactive map in their March
    2026 redesign -- no more simple static image to hotlink. IEM's
    autoplot #220 generates the equivalent categorical graphic on demand
    and reliably finds the latest issuance for a given 'valid' timestamp
    (confirmed: https://mesonet.agron.iastate.edu/plotting/auto/?q=220),
    so we use that instead."""
    which = OUTLOOKS[day_key]["which"]
    cat = OUTLOOKS[day_key]["cat"]
    now_utc = datetime.now(timezone.utc)
    valid_str = now_utc.strftime("%Y-%m-%d %H%M")
    encoded_valid = urllib.parse.quote(valid_str)
    return (
        f"https://mesonet.agron.iastate.edu/plotting/auto/plot/220/"
        f"which:{which}::cat:{cat}::t:state::csector:conus::"
        f"valid:{encoded_valid}::dpi:100.png"
    )


def day48_risk_days():
    """Day 4-8 doesn't always have a real, drawable risk area -- when
    it doesn't, the graphic just shows a "Potential Too Low" watermark
    (confirmed live). Rather than parse the image, this fetches IEM's
    CSV data endpoint (same underlying data as the graphic, same URL
    pattern with .csv instead of .png) and checks the "threshold"
    column, per instruction: verified live that it's blank for every
    day when there's genuinely no risk area, and populated with real
    category codes (TSTM/MRGL/SLGT/etc.) when there is one -- same
    field Day 1's outlook uses. Returns the list of specific days (as
    strings, e.g. ["4","5"]) that actually have a real outline, so we
    know whether to send the graphic at all, and which day it's for."""
    which = OUTLOOKS["day48"]["which"]
    cat = OUTLOOKS["day48"]["cat"]
    now_utc = datetime.now(timezone.utc)
    valid_str = now_utc.strftime("%Y-%m-%d %H%M")
    encoded_valid = urllib.parse.quote(valid_str)
    url = (
        f"https://mesonet.agron.iastate.edu/plotting/auto/plot/220/"
        f"which:{which}::cat:{cat}::t:state::csector:conus::"
        f"valid:{encoded_valid}::dpi:100.csv"
    )
    text = _fetch_with_retries(url, "IEM:day48csv")
    if not text:
        return None
    lines = text.strip().split("\n")
    if len(lines) < 2:
        return None
    header = lines[0].split(",")
    try:
        threshold_idx = header.index("threshold")
        day_idx = header.index("day")
    except ValueError:
        return None
    days_with_risk = []
    for line in lines[1:]:
        cols = line.split(",")
        if len(cols) <= max(threshold_idx, day_idx):
            continue
        if cols[threshold_idx].strip():
            days_with_risk.append(cols[day_idx].strip())
    return days_with_risk


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


def _telegram_chat_ids():
    """Every chat this bot delivers to -- the original chat, plus any
    additional destinations configured via SPC_TELEGRAM_CHAT_ID_2, _3,
    etc. Same convention as wxmodel_pipeline.py's multi-chat support."""
    ids = [os.environ["SPC_TELEGRAM_CHAT_ID"]]
    i = 2
    while True:
        extra = os.environ.get(f"SPC_TELEGRAM_CHAT_ID_{i}")
        if not extra:
            break
        ids.append(extra)
        i += 1
    return ids


def send_telegram(text):
    bot_token = os.environ["SPC_TELEGRAM_BOT_TOKEN"]
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    chat_ids = _telegram_chat_ids()
    chat_errors = {}
    for chat_id in chat_ids:
        payload = json.dumps({"chat_id": chat_id, "text": text}).encode("utf-8")
        req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"}, method="POST")
        last_err = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                with urllib.request.urlopen(req, timeout=20) as resp:
                    result = json.loads(resp.read().decode("utf-8"))
                    if result.get("ok"):
                        last_err = None
                        break
                    last_err = result.get("description", "Unknown Telegram error")
            except Exception as e:
                last_err = str(e)
            print(f"[Telegram] Attempt {attempt} to {chat_id} failed: {last_err}")
            if attempt < MAX_ATTEMPTS:
                time.sleep(RETRY_DELAY_SEC)
        if last_err:
            chat_errors[chat_id] = last_err
    if len(chat_errors) == len(chat_ids):
        raise RuntimeError(f"Telegram send failed to ALL configured chats: {chat_errors}")


def send_telegram_photo(photo_url, caption=""):
    """Downloads the image ourselves and uploads the bytes directly to
    Telegram (multipart/form-data) -- more reliable than passing the URL
    for Telegram to fetch itself, which can fail with "wrong type of the
    web page content" if Telegram's fetcher doesn't like the response.
    Downloads once, then uploads to every configured chat (see
    _telegram_chat_ids) so a multi-chat setup doesn't re-fetch the same
    image per destination."""
    bot_token = os.environ["SPC_TELEGRAM_BOT_TOKEN"]
    url = f"https://api.telegram.org/bot{bot_token}/sendPhoto"

    try:
        image_req = urllib.request.Request(photo_url, headers={"User-Agent": "spc-outlook-pipeline/1.0"})
        with urllib.request.urlopen(image_req, timeout=20) as img_resp:
            image_bytes = img_resp.read()
    except Exception as e:
        print(f"Graphic download failed (non-fatal, text still sends): {e}")
        return

    boundary = "----spcPhotoBoundary"

    for chat_id in _telegram_chat_ids():
        parts = [
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"chat_id\"\r\n\r\n{chat_id}\r\n".encode("utf-8"),
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"caption\"\r\n\r\n{caption[:1024]}\r\n".encode("utf-8"),
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"photo\"; filename=\"graphic.png\"\r\nContent-Type: image/png\r\n\r\n".encode("utf-8"),
            image_bytes,
            f"\r\n--{boundary}--\r\n".encode("utf-8"),
        ]
        body = b"".join(parts)
        last_err = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                req = urllib.request.Request(
                    url, data=body,
                    headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=20) as resp:
                    result = json.loads(resp.read().decode("utf-8"))
                    if result.get("ok"):
                        last_err = None
                        break
                    last_err = result.get("description", "Unknown Telegram error")
            except Exception as e:
                last_err = str(e)
            print(f"[Telegram photo] Attempt {attempt} to {chat_id} failed: {last_err}")
            if attempt < MAX_ATTEMPTS:
                time.sleep(RETRY_DELAY_SEC)
        if last_err:
            print(f"Graphic send to {chat_id} failed after {MAX_ATTEMPTS} attempts (non-fatal, text still sends): {last_err}")


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
    """Telegram only, no SMS/email fallback for SPC -- per instruction.
    Raises if Telegram isn't set up yet, so callers correctly do NOT mark
    the item as sent, and it'll naturally go out once Telegram is
    configured rather than being silently lost."""
    if not telegram_configured():
        print("Telegram not configured for SPC yet -- skipping (no SMS fallback). Will send once Telegram is set up.")
        raise RuntimeError("SPC Telegram not configured (SMS fallback disabled per instruction)")
    send_telegram(text)
    print("Delivered via Telegram (SPC chat).")


def send_failure_alert(context, error):
    try:
        deliver(f"[spc-outlook-pipeline error] {context}: {error}", subject="spc-outlook-pipeline error")
    except Exception as e:
        print(f"Could not even send the failure alert: {e}", file=sys.stderr)


# SPC's real, fixed daily issuance schedule (UTC hour, minute) for each
# product. Used to tell you exactly when the next one is coming, in
# Beaumont TX time (CDT, UTC-5 in summer).
ISSUANCE_SCHEDULE_UTC = {
    "day1": [(1, 0), (6, 0), (13, 0), (16, 30), (20, 0)],
    "day2": [(6, 0), (17, 30)],
    "day3": [(7, 30)],
    "day48": [(9, 0)],
}

CENTRAL_UTC_OFFSET = 5  # CDT (UTC-5). Change to 6 for CST (winter).


def parse_issued_to_utc(issued_str):
    """Parses the header's issued-time string (e.g. '1252 AM CDT Sun Jul
    27 2026' or '731 AM CDT Tue Jul 25 2026') into a real UTC datetime,
    so we can calculate 'next' from the outlook's own actual issued time
    instead of the script's runtime -- SPC sometimes issues a few
    minutes early/late relative to its nominal schedule, and basing this
    on 'now' caused the next-issuance calculation to sometimes pick the
    SAME slot that was just issued. Uses explicit regex extraction
    rather than strptime's ambiguous handling of 3- vs 4-digit HHMM."""
    if not issued_str:
        return None
    m = re.match(
        r"(\d{3,4})\s+(AM|PM)\s+(?:CDT|CST)\s+\w{3}\s+(\w{3})\s+(\d{1,2})\s+(\d{4})",
        issued_str.strip(),
    )
    if not m:
        return None
    digits, ampm, month_str, day_str, year_str = m.groups()
    digits = digits.zfill(4)
    hour, minute = int(digits[:2]), int(digits[2:])
    if ampm == "PM" and hour != 12:
        hour += 12
    if ampm == "AM" and hour == 12:
        hour = 0
    try:
        dt_naive = datetime.strptime(f"{month_str} {day_str} {year_str}", "%b %d %Y")
        dt_naive = dt_naive.replace(hour=hour, minute=minute)
        return dt_naive + timedelta(hours=CENTRAL_UTC_OFFSET)
    except ValueError:
        return None


def next_issuance_central(day_key, issued_str=None):
    reference_utc = parse_issued_to_utc(issued_str) if issued_str else None
    if reference_utc is None:
        reference_utc = datetime.now(timezone.utc)
    # Small buffer so an outlook issued a few minutes early never gets
    # mistaken for already being its own "next" scheduled slot.
    reference_utc = reference_utc + timedelta(minutes=20)

    today = reference_utc.replace(hour=0, minute=0, second=0, microsecond=0)
    candidates = []
    for day_offset in (0, 1):
        base = today + timedelta(days=day_offset)
        for hour, minute in ISSUANCE_SCHEDULE_UTC[day_key]:
            candidates.append(base + timedelta(hours=hour, minutes=minute))
    upcoming = min(c for c in candidates if c > reference_utc)
    central = upcoming - timedelta(hours=CENTRAL_UTC_OFFSET)
    return central.strftime("%-I:%M %p").lstrip("0") + " CDT " + central.strftime("%a")


def build_message(day_key, text):
    cfg = OUTLOOKS[day_key]
    issued = issued_time_from_header(text)
    parts = [f"⛈️ {cfg['label']}"]
    if issued:
        parts.append(f"📅 Issued: {issued}")
    parts.append("")

    headline = headline_section(text)
    if headline:
        parts.append(f"⚠️ {headline}")
        parts.append("")

    summary = summary_section(text)
    if summary:
        parts.append(f"📝 {summary}")
        parts.append("")

    try:
        next_time = next_issuance_central(day_key, issued_str=issued)
        parts.append(f"⏭️ Next {cfg['label']}: {next_time}")
        parts.append("")
    except Exception as e:
        print(f"[{day_key}] Could not compute next issuance time (non-fatal): {e}")

    parts.append(f"🔗 View live: {cfg['nhc_fallback'].split('?')[0].replace('.html?text', '.html').replace('?text', '')}")
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

    send_graphic = True
    graphic_caption = cfg["label"]
    if day_key == "day48":
        # Day 4-8 doesn't always have a real risk area -- only send
        # the graphic when at least one specific day actually has one,
        # per instruction; otherwise text/forecast only, no "Potential
        # Too Low" placeholder image.
        risk_days = day48_risk_days()
        if risk_days:
            graphic_caption = f"{cfg['label']} (Day {'/'.join(sorted(set(risk_days)))})"
        else:
            send_graphic = False
            print(f"[{day_key}] No real risk area on any day 4-8 this cycle -- skipping graphic, text only.")

    photo_url = graphic_url(day_key) if send_graphic else None
    if telegram_configured() and photo_url:
        try:
            send_telegram_photo(photo_url, caption=graphic_caption)
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


# ===========================================================================
# Watches -- SPC rotates the raw text through 10 slots (SEL0-SEL9), each
# holding one of the 10 most recent watches. We fetch all 10 every cycle,
# pull the actual watch number out of NHC's own text (never guessed), and
# alert on any number we haven't already sent -- catches genuinely new
# watches regardless of which slot they land in.
# ===========================================================================
WATCH_SLOTS = [f"SEL{n}" for n in range(10)]


def watch_number_and_type(text):
    m = re.search(r"(SEVERE\s+THUNDERSTORM|TORNADO)\s+WATCH\s+NUMBER\s+(\d+)", text, re.I)
    if not m:
        return None, None
    watch_type = "Tornado" if "TORNADO" in m.group(1).upper() else "Severe Thunderstorm"
    return watch_type, int(m.group(2))


def watch_graphic_url(watch_num):
    cache_buster = int(time.time())
    return f"https://www.spc.noaa.gov/products/watch/ww{watch_num:04d}.gif?_cb={cache_buster}"


def build_watch_message(watch_type, watch_num, text):
    issued = issued_time_from_header(text)
    parts = [f"🚨 SPC {watch_type} Watch #{watch_num}"]
    if issued:
        parts.append(f"📅 Issued: {issued}")
    parts.append("")
    body = re.sub(r"\s+", " ", text).strip()
    if len(body) > 3500:
        body = body[:3500] + "..."
    parts.append(body)
    parts.append("")
    parts.append(f"🔗 View live: https://www.spc.noaa.gov/products/watch/ww{watch_num:04d}.html")
    return "\n".join(parts)


def is_pds(text):
    return bool(re.search(r"PARTICULARLY\s+DANGEROUS\s+SITUATION", text, re.I))


def process_watches(state):
    """Only PDS (Particularly Dangerous Situation) watches actually get
    sent -- routine watches are tracked as seen (so we don't re-check
    them every loop) but deliberately not delivered, per instruction."""
    sent_numbers = set(state.get("watch_numbers_sent", []))
    newly_sent = []

    for slot in WATCH_SLOTS:
        cache_buster = int(time.time())
        iem_url = f"{IEM_BASE}?pil={slot}&_cb={cache_buster}"
        text = _fetch_with_retries(iem_url, f"IEM:{slot}")
        if not text:
            continue

        watch_type, watch_num = watch_number_and_type(text)
        if watch_num is None or watch_num in sent_numbers:
            continue

        if not is_pds(text):
            print(f"[watch] {watch_type} Watch #{watch_num} is routine (not PDS) -- skipping, per instruction.")
            newly_sent.append(watch_num)
            continue

        print(f"[watch] New PDS {watch_type} Watch #{watch_num} detected.")

        if telegram_configured():
            try:
                send_telegram_photo(watch_graphic_url(watch_num), caption=f"SPC {watch_type} Watch #{watch_num}")
                print(f"[watch] Graphic sent for #{watch_num}.")
            except Exception as e:
                print(f"[watch] Graphic send failed (non-fatal): {e}")

        message = build_watch_message(watch_type, watch_num, text)
        try:
            deliver(message, subject=f"SPC {watch_type} Watch #{watch_num}")
            print(f"[watch] Sent successfully for #{watch_num}.")
            newly_sent.append(watch_num)
        except Exception as e:
            send_failure_alert(f"Watch #{watch_num} delivery", str(e))

    if newly_sent:
        sent_numbers.update(newly_sent)
        # Keep the set from growing forever -- only need recent history to dedupe against.
        state["watch_numbers_sent"] = sorted(sent_numbers)[-200:]
        save_state(state)


def main():
    state = load_state()
    for day_key in OUTLOOKS:
        try:
            process_day(day_key, state)
        except Exception as e:
            print(f"[{day_key}] Unexpected error (non-fatal, continuing to next day): {e}")

    try:
        process_watches(state)
    except Exception as e:
        print(f"[watch] Unexpected error (non-fatal): {e}")


if __name__ == "__main__":
    main()
