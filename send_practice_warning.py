#!/usr/bin/env python3
"""One-time script: sends a practice/sample Severe Thunderstorm Warning
through the exact same cleaning/reflow logic as nws_warnings_pipeline.py,
so the person can review the formatting. Not part of the regular
continuous system -- run once, then delete."""

import json
import os
import re
import urllib.request
import time as _time

SAMPLE_RAW = """123
WUUS54 KHGX 261530
SVRHGX

BULLETIN - EAS ACTIVATION REQUESTED
Severe Thunderstorm Warning
National Weather Service Houston/Galveston TX
1030 AM CDT Sun Jul 26 2026

The National Weather Service in Houston/Galveston has issued a

* Severe Thunderstorm Warning for...
  Southeastern Jefferson County in southeastern Texas...
  Southern Orange County in southeastern Texas...

* Until 1115 AM CDT.

* At 1029 AM CDT, a severe thunderstorm was located near Port Arthur,
  moving northeast at 20 mph.

  HAZARD...60 mph wind gusts and quarter size hail.
  SOURCE...Radar indicated.
  IMPACT...Hail damage to vehicles is expected. Expect wind damage
           to roofs, siding, and trees.

* Locations impacted include...
  Port Arthur, Groves, Nederland, Bridge City and Orangefield.

PRECAUTIONARY/PREPAREDNESS ACTIONS...
For your protection move to an interior room on the lowest floor of a
building.

&&

LAT...LON 2995 9403 2994 9410 2988 9411 2987 9403
TIME...MOT...LOC 1529Z 224DEG 18KT 2991 9408
HAIL...1.00IN
WIND...60MPH
THUNDERSTORM DAMAGE THREAT...SIGNIFICANT

$$

Roberts"""


def reflow_text(text):
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
        else:
            buffer.append(stripped)
    flush_buffer()
    return "\n".join(output_lines)


def extract_tag_line(text):
    for m in re.finditer(r"^([A-Z][A-Z ]+\.\.\.[A-Z][A-Z ]+)\s*$", text, re.M):
        if not m.group(1).startswith("LAT"):
            return m.group(1).strip()
    return None


def strip_boilerplate_lines(text):
    text = re.sub(r"^BULLETIN\s*-\s*EAS ACTIVATION REQUESTED\s*\n?", "", text, flags=re.M)
    text = re.sub(r"^The National Weather Service in .+ has issued a\s*\n?", "", text, flags=re.M)
    return text


def strip_precautionary_section(text):
    return re.sub(
        r"\n\s*PRECAUTIONARY/PREPAREDNESS ACTIONS\.\.\.[\s\S]*?(?=\n\s*\n|\Z)",
        "",
        text,
    )


def convert_times(text):
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
    m = re.search(r"[A-Za-z ]+ Warning for\.\.\.\n[\s\S]*?(?=\n\s*\n)", text)
    if not m:
        return text
    block = text[m.start():m.end()].strip()
    remainder = (text[:m.start()] + text[m.end():]).strip()
    return remainder + "\n\n" + block


def strip_wmo_afos_header(text):
    idx = text.find("BULLETIN")
    if idx >= 0:
        return text[idx:]
    return text


def extract_until_time(text):
    m = re.search(r"\*?\s*Until\s+([\d:]+\s*(?:AM|PM)(?:\s+[A-Z]{3,4})?)\.?\s*\n?", text)
    if not m:
        return text, None
    until_raw = m.group(1).strip()
    new_text = text[:m.start()] + text[m.end():]
    return new_text, until_raw


def clean_body(text):
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


def main():
    body, tag_line, until_time = clean_body(SAMPLE_RAW)
    parts = []
    if tag_line:
        parts.append(tag_line)
        parts.append("")
    if until_time:
        parts.append(f"Issued: 10:30 AM Sun Jul 26 2026 -- Until: {until_time}")
    else:
        parts.append("Issued: 10:30 AM Sun Jul 26 2026")
    parts.append("")
    parts.append(body)
    message = "\n".join(parts)

    bot_token = os.environ["NWS_TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["NWS_TELEGRAM_CHAT_ID"]

    def parse_polygon_coords(t):
        m = re.search(r"LAT\.\.\.LON((?:\s+\d{3,5}){4,})", t)
        if not m:
            return None
        nums = [int(n) for n in m.group(1).split()]
        coords = []
        for i in range(0, len(nums), 2):
            coords.append((-(nums[i + 1] / 100.0), nums[i] / 100.0))
        if coords[0] != coords[-1]:
            coords.append(coords[0])
        return coords

    coords = parse_polygon_coords(SAMPLE_RAW)
    geoapify_key = os.environ["GEOAPIFY_API_KEY"]
    color = "#ffd400"  # Severe Thunderstorm = yellow
    coord_str = ",".join(f"{lon},{lat}" for lon, lat in coords)
    geometry = f"polygon:{coord_str};fillcolor:{color};fillopacity:0.35;linecolor:{color};linewidth:3"
    graphic_url = (
        f"https://maps.geoapify.com/v1/staticmap?style=osm-carto"
        f"&width=800&height=600&geometry={geometry}&apiKey={geoapify_key}"
    )
    photo_url = f"https://api.telegram.org/bot{bot_token}/sendPhoto"
    title = "\u26a0\ufe0f SEVERE THUNDERSTORM WARNING \u26a0\ufe0f\nNWS Houston/Galveston [PRACTICE ONLY]"
    photo_payload = json.dumps({"chat_id": chat_id, "photo": graphic_url, "caption": title}).encode("utf-8")
    photo_req = urllib.request.Request(photo_url, data=photo_payload, headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(photo_req, timeout=20) as resp:
        print("Photo result:", json.loads(resp.read().decode("utf-8")))

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = json.dumps({"chat_id": chat_id, "text": message}).encode("utf-8")
    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=20) as resp:
        print("Text result:", json.loads(resp.read().decode("utf-8")))


if __name__ == "__main__":
    main()
