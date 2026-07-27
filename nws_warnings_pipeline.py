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
    "tor": {"pil_prefix": "TOR", "label": "Tornado Warning", "color": "#e60000"},
    "svr": {"pil_prefix": "SVR", "label": "Severe Thunderstorm Warning", "color": "#ffd400"},
    "ffw": {"pil_prefix": "FFW", "label": "Flash Flood Warning", "color": "#00a651"},
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


def parse_polygon_coords(text):
    """Extracts the exact warning polygon from the raw product's own
    'LAT...LON' line -- the same coordinates NWS/SPC themselves draw --
    and converts NWS's 4-digit lat/lon notation (e.g. '2995 9403' =
    29.95N, 94.03W) into proper decimal degree pairs for a map overlay."""
    m = re.search(r"LAT\.\.\.LON((?:\s+\d{3,5}){4,})", text)
    if not m:
        return None
    nums = [int(n) for n in m.group(1).split()]
    if len(nums) < 6 or len(nums) % 2 != 0:
        return None
    coords = []
    for i in range(0, len(nums), 2):
        lat = nums[i] / 100.0
        lon = -(nums[i + 1] / 100.0)
        coords.append((lon, lat))
    if coords[0] != coords[-1]:
        coords.append(coords[0])
    return coords


def build_warning_graphic_url(raw_text, warn_key):
    """Real warning polygon (from the product's own LAT...LON line) on a
    clean, modern Geoapify map. Filled with a light, semi-transparent
    version of the warning-type color (red=Tornado, yellow=Severe
    Thunderstorm, green=Flash Flood) so the map underneath stays
    readable, matching the reference NWS social graphic style."""
    coords = parse_polygon_coords(raw_text)
    api_key = os.environ.get("GEOAPIFY_API_KEY")
    if not coords or not api_key:
        return None
    color = WARNING_TYPES[warn_key]["color"].replace("#", "%23")
    coord_str = ",".join(f"{lon},{lat}" for lon, lat in coords)
    geometry = f"polygon:{coord_str};fillcolor:{color};fillopacity:0.35;linecolor:{color};linewidth:3"

    # Zoom out a bit from a tight fit so nearby towns/roads are visible
    # for context, without zooming out so far the warning area looks tiny.
    lons = [c[0] for c in coords]
    lats = [c[1] for c in coords]
    min_lon, max_lon = min(lons), max(lons)
    min_lat, max_lat = min(lats), max(lats)
    pad_lon = max((max_lon - min_lon) * 0.6, 0.08)
    pad_lat = max((max_lat - min_lat) * 0.6, 0.08)
    area = f"rect:{min_lon - pad_lon},{min_lat - pad_lat},{max_lon + pad_lon},{max_lat + pad_lat}"

    return (
        f"https://maps.geoapify.com/v1/staticmap?style=osm-carto"
        f"&width=800&height=600&area={area}&geometry={geometry}&apiKey={api_key}"
    )


def add_title_banner(image_bytes, title_text, banner_color):
    """Composites a colored title banner above the map (matching the
    reference NWS social-graphic style), using PIL since we already
    control the raw image bytes. Falls back to the plain image
    untouched if PIL isn't available or anything goes wrong -- this is
    cosmetic only and must never break the actual graphic send."""
    try:
        from PIL import Image, ImageDraw, ImageFont
        import io as _io

        map_img = Image.open(_io.BytesIO(image_bytes)).convert("RGB")
        width, height = map_img.size
        banner_height = 90

        canvas = Image.new("RGB", (width, height + banner_height), banner_color)
        canvas.paste(map_img, (0, banner_height))

        draw = ImageDraw.Draw(canvas)
        try:
            font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 34)
        except Exception:
            font = ImageFont.load_default()

        bbox = draw.textbbox((0, 0), title_text, font=font)
        text_width = bbox[2] - bbox[0]
        text_height = bbox[3] - bbox[1]
        text_x = (width - text_width) // 2
        text_y = (banner_height - text_height) // 2 - bbox[1]
        draw.text((text_x, text_y), title_text, fill="white", font=font)

        out = _io.BytesIO()
        canvas.save(out, format="JPEG", quality=90)
        return out.getvalue()
    except Exception as e:
        print(f"Title banner compositing failed (non-fatal, using plain map): {e}")
        return image_bytes


def send_telegram_photo(photo_url, caption="", title_text=None, banner_color="#333333"):
    """Downloads the image ourselves, composites a title banner onto it,
    and uploads the bytes directly to Telegram (multipart/form-data),
    instead of asking Telegram to fetch the URL itself -- confirmed more
    reliable; passing the raw URL to Telegram got rejected with 'wrong
    type of the web page content' even though the URL is a genuinely
    valid image. Single attempt, short timeout -- the graphic is
    best-effort only and must NEVER meaningfully delay the text."""
    bot_token = os.environ["NWS_TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["NWS_TELEGRAM_CHAT_ID"]
    try:
        image_req = urllib.request.Request(photo_url, headers={"User-Agent": "nws-warnings-pipeline/1.0"})
        with urllib.request.urlopen(image_req, timeout=8) as img_resp:
            image_bytes = img_resp.read()

        if title_text:
            image_bytes = add_title_banner(image_bytes, title_text, banner_color)

        boundary = "----warningPhotoBoundary"
        parts = [
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"chat_id\"\r\n\r\n{chat_id}\r\n".encode("utf-8"),
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"caption\"\r\n\r\n{caption[:1024]}\r\n".encode("utf-8"),
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"photo\"; filename=\"warning.jpg\"\r\nContent-Type: image/jpeg\r\n\r\n".encode("utf-8"),
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
            if result.get("ok"):
                return
            print(f"Graphic send failed (non-fatal, text sends regardless): {result.get('description', 'Unknown Telegram error')}")
    except Exception as e:
        print(f"Graphic send failed (non-fatal, text sends regardless): {e}")


def reflow_text(text):
    """NWS text products are hard-wrapped at a fixed width -- rejoins
    wrapped lines back into natural, readable paragraphs, preserving
    real structure: bullet lines (*), section markers, and blank-line
    paragraph breaks."""
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
        elif stripped.startswith("*"):
            flush_buffer()
            buffer.append(stripped)
        else:
            buffer.append(stripped)
    flush_buffer()
    return "\n".join(output_lines)


def extract_tag_line(text):
    """Finds the warning's summary tag, e.g. 'FLASH FLOOD...RADAR
    INDICATED', 'TORNADO...RADAR INDICATED', or 'THUNDERSTORM DAMAGE
    THREAT...CONSIDERABLE' -- normally buried near the bottom of the raw
    product, but per instruction this goes at the very top instead.
    Explicitly excludes digits (and the LAT...LON line) so it never
    mistakes the polygon coordinate line for the actual tag."""
    for m in re.finditer(r"^([A-Z][A-Z ]+\.\.\.[A-Z][A-Z ]+)\s*$", text, re.M):
        if not m.group(1).startswith("LAT"):
            return m.group(1).strip()
    return None


def strip_boilerplate_lines(text):
    text = re.sub(r"^BULLETIN\s*-\s*EAS ACTIVATION REQUESTED\s*\n?", "", text, flags=re.M)
    text = re.sub(r"^The National Weather Service in .+ has issued a\s*\n?", "", text, flags=re.M)
    return text


def strip_precautionary_section(text):
    """Removes the PRECAUTIONARY/PREPAREDNESS ACTIONS section entirely,
    per instruction."""
    return re.sub(
        r"\n\s*PRECAUTIONARY/PREPAREDNESS ACTIONS\.\.\.[\s\S]*?(?=\n\s*\n|\Z)",
        "",
        text,
    )


def convert_times(text):
    """'1030 AM CDT' -> '10:30 AM' -- drops the timezone abbreviation
    and adds a colon, per instruction."""
    def repl(m):
        digits, ampm = m.group(1), m.group(2)
        if len(digits) == 3:
            digits = "0" + digits
        return f"{int(digits[:2])}:{digits[2:]} {ampm}"
    return re.sub(
        r"\b(\d{3,4})\s?(AM|PM)\s+(?:CDT|CST|EDT|EST|MDT|MST|PDT|PST)\b",
        repl,
        text,
    )


def strip_bullet_markers(text):
    return re.sub(r"^\*\s*", "", text, flags=re.M)


def relocate_warning_for_block(text):
    """Moves the '[Type] Warning for... [county list]' block from its
    normal position near the top to the very end of the message, per
    instruction. Non-greedy match stops at the first blank line so it
    never eats into the next paragraph."""
    m = re.search(r"[A-Za-z ]+ Warning for\.\.\.\n[\s\S]*?(?=\n\s*\n)", text)
    if not m:
        return text
    block = text[m.start():m.end()].strip()
    remainder = (text[:m.start()] + text[m.end():]).strip()
    return remainder + "\n\n" + block


def strip_wmo_afos_header(text):
    """Strips the leading routing header (sequence number, WMO
    abbreviated heading, AFOS PIL) down to the readable bulletin text.
    'BULLETIN' is a highly reliable marker across warning types --
    used as the primary method since it's more robust than searching
    for a VTEC line, which isn't always present verbatim in every
    product variant."""
    idx = text.find("BULLETIN")
    if idx >= 0:
        return text[idx:]
    return text


def extract_until_time(text):
    """Pulls the 'Until [time].' bullet out entirely so it can be
    combined with the Issued time at the top instead, per instruction --
    so it's immediately clear when the warning starts and ends."""
    m = re.search(r"\*?\s*Until\s+([\d:]+\s*(?:AM|PM)(?:\s+[A-Z]{3,4})?)\.?\s*\n?", text)
    if not m:
        return text, None
    until_raw = m.group(1).strip()
    new_text = text[:m.start()] + text[m.end():]
    return new_text, until_raw


def clean_body(text):
    """Strips the WMO/AFOS routing header (via 'BULLETIN', the reliable
    primary marker, falling back to VTEC-line search) and everything
    from the first '&&' onward (LAT/LON polygon block, '$$', forecaster
    name); pulls out the summary tag line and the 'Until' end time to
    use separately at the top; strips boilerplate intro lines and the
    Precautionary/Preparedness section; converts times to a cleaner
    format; strips '*' bullet markers; moves the county-list block to
    the end; and reflows hard-wrapped text into natural paragraphs."""
    text = strip_wmo_afos_header(text)
    if not text.startswith("BULLETIN"):
        m = re.search(r"/[OX]\.\w+\.\w{4}\.\w{2}\.\w\.\d{4}\.[^\n]*\n(?:/[^\n]*\n)*", text)
        if m:
            text = text[m.end():]
        elif "\x01" in text:
            text = text.split("\x01")[-1]
    text = text.replace("\x03", "").strip()

    tag_line = extract_tag_line(text)

    text = text.split("&&")[0].strip()

    text = strip_boilerplate_lines(text)
    text = strip_precautionary_section(text)
    text = relocate_warning_for_block(text)
    text, until_raw = extract_until_time(text)
    text = convert_times(text)
    until_time = convert_times(until_raw) if until_raw else None
    text = strip_bullet_markers(text)

    text = reflow_text(text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text, tag_line, until_time


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
    body, tag_line, until_time = clean_body(text)
    parts = []
    if tag_line:
        parts.append(tag_line)
        parts.append("")
    if issued:
        issued_clean = convert_times(issued)
        if until_time:
            parts.append(f"Issued: {issued_clean} -- Until: {until_time}")
        else:
            parts.append(f"Issued: {issued_clean}")
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

    # Graphic goes first when it works, but it's a single fast attempt
    # (see send_telegram_photo) wrapped in try/except -- if it's slow,
    # fails, or errors in ANY way, we fall through immediately and the
    # text below still sends no matter what. Nothing about the graphic
    # can ever block, delay, or prevent the text.
    if telegram_configured():
        try:
            photo_url = build_warning_graphic_url(text, warn_key)
            if photo_url:
                title = WARNING_TYPES[warn_key]["label"]
                banner_color = WARNING_TYPES[warn_key]["color"]
                caption = f"NWS {OFFICES[office_key]}"
                send_telegram_photo(photo_url, caption=caption, title_text=title, banner_color=banner_color)
                print(f"[{pil}] Graphic sent.")
            else:
                print(f"[{pil}] No polygon found or Geoapify key missing -- skipping graphic (non-fatal).")
        except Exception as e:
            print(f"[{pil}] Graphic failed (non-fatal, text sends regardless): {e}")

    # Text is the priority and is guaranteed to send here regardless of
    # whatever happened with the graphic above.
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
