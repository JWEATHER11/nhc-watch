#!/usr/bin/env python3
"""One-off diagnostic: tests the full combined-cycle report with all
new features (RI/weakening trend, tier labels, track splits, Gulf
Coast rainfall) end-to-end, then sends the real message."""

import time as _time
import wxmodel_pipeline as w

start = _time.time()

print("Fetching GFS (16 days)...")
gfs_scan = w.fetch_model_grid("gfs", forecast_days=16)
print("GFS:", "OK" if gfs_scan else "FAILED/EMPTY")

print("Fetching Euro (15 days)...")
ecmwf_scan = w.fetch_model_grid("ecmwf", forecast_days=15)
print("Euro:", "OK" if ecmwf_scan else "FAILED/EMPTY")

print("Fetching AIFS (15 days)...")
aifs_scan = w.fetch_model_grid("ecmwf", models_param="ecmwf_aifs025_single", label="OpenMeteo:AIFS", forecast_days=15)
print("AIFS:", "OK" if aifs_scan else "FAILED/EMPTY")

ensemble_signals = {}
for model_key in w.ENSEMBLE_MODELS:
    _, model_name = w.ENSEMBLE_MODELS[model_key]
    ensemble_signals[model_key] = w.fetch_ensemble_genesis_signal(model_key)
    print(f"{model_name}:", "signal found" if ensemble_signals[model_key] else "no signal (quiet)")

print("Fetching NHC outlook summary...")
nhc_summary = w.fetch_nhc_outlook_summary()
print("NHC:", nhc_summary)

print("Fetching Gulf Coast rainfall...")
try:
    rainfall_flags = w.fetch_gulf_coast_rainfall()
    print("Rainfall flags:", rainfall_flags)
except Exception as e:
    import traceback
    print("EXCEPTION in rainfall check:")
    traceback.print_exc()
    rainfall_flags = None

cycle_hour_utc = int(w.current_cycle_key().split("T")[1])
try:
    message = w.build_combined_cycle_report(cycle_hour_utc, gfs_scan, ecmwf_scan, aifs_scan, ensemble_signals, nhc_summary, rainfall_flags)
except Exception as e:
    import traceback
    print("EXCEPTION building report:")
    traceback.print_exc()
    raise

print()
print("=== FULL MESSAGE ===")
print(message)
print("=== END MESSAGE ===")
print(f"Total time: {_time.time()-start:.1f}s")

print()
print("Testing named-storm path (RI/weakening) with active storms if any...")
storms = w.fetch_active_atlantic_storms()
print(f"Active Atlantic storms: {len(storms)}")
for storm in storms:
    print(f"  {storm.get('id')}: {storm.get('name')}")
    adeck_text = w.fetch_adeck(storm.get("id", ""))
    if adeck_text:
        rows = w.parse_adeck(adeck_text)
        summaries = w.summarize_model_guidance(rows)
        report = w.build_storm_report(storm, summaries)
        print("  Report:")
        print(report)

print()
print("Sending combined cycle message for real via deliver()...")
try:
    w.deliver(message)
    print("SUCCESS: message delivered.")
except Exception as e:
    import traceback
    traceback.print_exc()
