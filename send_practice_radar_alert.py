#!/usr/bin/env python3
"""One-off practice send for radar_watch_pipeline.py -- injects a
synthetic storm-intensity reading to confirm real Telegram delivery
end-to-end, since real conditions don't always cooperate for testing.
Does NOT touch the real state file, so it won't interfere with the
actual pipeline's dedup."""

import radar_watch_pipeline as r

synthetic_current = {
    "checked": 81,
    "total_points": 81,
    "rain_coverage_pct": 74,
    "storm_cities": ["Beaumont/Port Arthur", "Houston"],
    "storm_gate_count": 12,
    "max_dbz": 47.5,
    "max_near": "Beaumont/Port Arthur",
    "sites_used": ["KHGX", "KLCH"],
}
reasons = [
    "Storm-intensity radar echo (>= 35 dBZ) newly showing up near Beaumont/Port Arthur, Houston [PRACTICE ONLY]"
]

message = r.build_radar_message(synthetic_current, reasons)
message = "⚠️ PRACTICE TEST -- not a real report ⚠️\n\n" + message
print(message)
r.deliver(message)
print("SENT")
