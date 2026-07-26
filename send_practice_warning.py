#!/usr/bin/env python3
"""One-time script: sends a practice/sample Severe Thunderstorm Warning
through the exact same cleaning/reflow logic as nws_warnings_pipeline.py,
so the person can review the formatting and decide what else to trim or
add. Not part of the regular continuous system -- run once, then delete."""

import json
import os
import re
import urllib.request

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
        elif stripped.startswith("*"):
            flush_buffer()
            buffer.append(stripped)
        else:
            buffer.append(stripped)
    flush_buffer()
    return "\n".join(output_lines)


def extract_tag_line(text):
    for m in re.finditer(r"^([A-Z][A-Z ]+\.\.\.[A-Z][A-Z ]+)\s*$", text, re.M):
        if not m.group(1).startswith("LAT"):
            return m.group(1).strip()
    return None


def main():
    text = SAMPLE_RAW
    idx = text.find("BULLETIN")
    text = text[idx:]
    tag_line = extract_tag_line(text)
    text = text.split("&&")[0].strip()
    text = reflow_text(text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()

    parts = []
    if tag_line:
        parts.append(tag_line)
        parts.append("")
    parts.append("Issued: 1030 AM CDT Sun Jul 26 2026")
    parts.append("")
    parts.append("[PRACTICE/SAMPLE ONLY -- not a real warning] NWS Houston/Galveston -- Severe Thunderstorm Warning")
    parts.append("")
    parts.append(text)
    message = "\n".join(parts)

    bot_token = os.environ["NWS_TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["NWS_TELEGRAM_CHAT_ID"]
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = json.dumps({"chat_id": chat_id, "text": message}).encode("utf-8")
    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=20) as resp:
        result = json.loads(resp.read().decode("utf-8"))
        print(result)


if __name__ == "__main__":
    main()
