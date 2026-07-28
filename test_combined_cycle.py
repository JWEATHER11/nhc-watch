#!/usr/bin/env python3
"""One-off diagnostic: tests the full combined-cycle report build (GFS
+ Euro + all side notes) then sends it for real."""

import wxmodel_pipeline as w

print("Current cycle key:", w.current_cycle_key())
print()

print("Fetching GFS...")
gfs_scan = w.fetch_model_grid("gfs")
print("GFS result:", "OK" if gfs_scan else "FAILED/EMPTY")

print("Fetching Euro...")
ecmwf_scan = w.fetch_model_grid("ecmwf")
print("Euro result:", "OK" if ecmwf_scan else "FAILED/EMPTY")

ensemble_signals = {}
for model_key in w.ENSEMBLE_MODELS:
    print(f"Fetching ensemble: {model_key}...")
    ensemble_signals[model_key] = w.fetch_ensemble_genesis_signal(model_key)
    print(f"  -> {'signal found' if ensemble_signals[model_key] else 'no signal (quiet)'}")

print("Fetching NHC outlook summary...")
nhc_summary = w.fetch_nhc_outlook_summary()
print("NHC summary:", nhc_summary)

cycle_hour_utc = int(w.current_cycle_key().split("T")[1])
message = w.build_combined_cycle_report(cycle_hour_utc, gfs_scan, ecmwf_scan, ensemble_signals, nhc_summary)
print()
print("=== FULL MESSAGE ===")
print(message)
print("=== END MESSAGE ===")
print()

print("Sending for real via deliver()...")
try:
    w.deliver(message)
    print("SUCCESS: message delivered.")
except Exception as e:
    import traceback
    traceback.print_exc()
