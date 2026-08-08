#!/usr/bin/env python3
"""One-off practice send for metar_storm_pipeline.py -- injects
synthetic hazards across an ASOS station and two KFDM WeatherNet
stations to confirm real Telegram delivery end-to-end in the current
message format, since real conditions don't always cooperate for
testing. Does NOT touch the real state file, so it won't interfere
with the actual pipeline's dedup.

Reflects the current live format as of 2026-08-08: rain totals only
alert at 1in and up, one line per station per cycle (no repeats),
observation timestamps included next to rain/wind data, no
"(Beaumont time)" clutter, 40mph+ gust/wind threshold."""

import metar_storm_pipeline as m

synthetic_hazards = {
    "BPT": {
        "thunderstorm": "Thunderstorm reported [PRACTICE ONLY]",
        "gust": "High wind -- 46 mph observed (as of 4:52 PM) [PRACTICE ONLY]",
    },
    "Port Acres ES": {
        "rain_today_2.0": "Rain total climbing -- now 2.3\" (as of 4:55 PM) [PRACTICE ONLY]",
    },
    "Winnie": {
        "gust": "High wind -- 41 mph observed (as of 4:50 PM) [PRACTICE ONLY]",
        "rain_today_1.0": "Rain total climbing -- now 1.1\" (as of 4:45 PM) [PRACTICE ONLY]",
    },
}

message = m.build_message(synthetic_hazards)
message = "⚠️ PRACTICE TEST -- not a real report, just showing the current format ⚠️\n\n" + message
print(message)
m.deliver(message)
print("SENT")
