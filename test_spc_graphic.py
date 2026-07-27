#!/usr/bin/env python3
"""One-off diagnostic: tests SPC graphic URL construction and actual
Telegram photo send, to see exactly what's failing."""

import spc_outlook_pipeline as s

print("telegram_configured():", s.telegram_configured())

url = s.graphic_url("day1")
print("Graphic URL for day1:", url)

print()
print("Attempting to send this graphic to Telegram...")
try:
    s.send_telegram_photo(url, caption="TEST: SPC Day 1 graphic")
    print("send_telegram_photo() completed without raising an exception.")
except Exception as e:
    import traceback
    print("EXCEPTION during send:")
    traceback.print_exc()
