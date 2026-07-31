#!/usr/bin/env python3
"""One-off resend of the current HRRR alert signal, bypassing the
normal "only send on genuine change" dedup -- for testing delivery
(e.g. confirming a message-format fix actually renders right), same
purpose as send_example.py for the main Model Watch report."""

import hrrr_alert_pipeline as h

current = h.fetch_hrrr_alert_signal()
if not current:
    print("HRRR signal unavailable this cycle.")
    raise SystemExit(1)
current["href"] = h._get_href_corroboration(h.load_state())

print(f"Current signal: {current}")
reasons = ["Test resend -- current HRRR signal, not necessarily a real change"]
message = h.build_hrrr_alert_message(current, reasons)
print(message)
h.deliver(message)
print("SENT")
