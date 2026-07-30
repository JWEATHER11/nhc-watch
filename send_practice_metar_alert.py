#!/usr/bin/env python3
"""One-off practice send for metar_storm_pipeline.py -- injects a
synthetic thunderstorm + gust hazard at Beaumont/Port Arthur to
confirm real Telegram delivery end-to-end, since real conditions
don't always cooperate for testing. Does NOT touch the real state
file, so it won't interfere with the actual pipeline's dedup."""

import metar_storm_pipeline as m

synthetic_hazards = {
    "BPT": {
        "thunderstorm": "Thunderstorm reported [PRACTICE ONLY]",
        "gust": "Wind gust to 52 mph observed [PRACTICE ONLY]",
    }
}

message = m.build_message(synthetic_hazards)
message = "⚠️ PRACTICE TEST -- not a real report ⚠️\n\n" + message
print(message)
m.deliver(message)
print("SENT")
