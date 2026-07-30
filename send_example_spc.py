#!/usr/bin/env python3
"""One-off resend of the current SPC Day 1 outlook, bypassing the
normal "only send if text changed" dedup -- for testing delivery (e.g.
confirming a new chat destination actually receives it), same purpose
as send_example.py for the WXMODEL chat."""

import spc_outlook_pipeline as s

day_key = "day1"
cfg = s.OUTLOOKS[day_key]
text, source = s.fetch_outlook_text(day_key)
if not text:
    print("Could not fetch the Day 1 outlook from either source.")
    raise SystemExit(1)

print(f"Fetched from {source}")

photo_url = s.graphic_url(day_key)
if s.telegram_configured():
    try:
        s.send_telegram_photo(photo_url, caption=cfg["label"])
        print("Graphic sent.")
    except Exception as e:
        print(f"Graphic send failed (non-fatal): {e}")

message = s.build_message(day_key, text)
print(f"Message:\n{message}")
s.deliver(message, subject=cfg["label"])
print("SENT")
