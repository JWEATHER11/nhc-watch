#!/usr/bin/env python3
"""One-off diagnostic: tests the extended short/medium/long-range scan
for GFS, Euro, AIFS, and all three ensembles -- confirms nothing breaks
or times out with the larger data pulls, and sends the real combined
message."""

import time as _time
import wxmodel_pipeline as w

start = _time.time()

print("Fetching GFS (16 days)...")
gfs_scan = w.fetch_model_grid("gfs", forecast_days=16)
print("GFS:", "OK" if gfs_scan else "FAILED/EMPTY", f"({_time.time()-start:.1f}s elapsed)")
if gfs_scan:
    print("  Hours covered:", [r["fh"] for r in gfs_scan["results"]])

print("Fetching Euro (15 days)...")
ecmwf_scan = w.fetch_model_grid("ecmwf", forecast_days=15)
print("Euro:", "OK" if ecmwf_scan else "FAILED/EMPTY", f"({_time.time()-start:.1f}s elapsed)")
if ecmwf_scan:
    print("  Hours covered:", [r["fh"] for r in ecmwf_scan["results"]])

print("Fetching AIFS (15 days)...")
aifs_scan = w.fetch_model_grid("ecmwf", models_param="ecmwf_aifs025_single", label="OpenMeteo:AIFS", forecast_days=15)
print("AIFS:", "OK" if aifs_scan else "FAILED/EMPTY", f"({_time.time()-start:.1f}s elapsed)")
if aifs_scan:
    print("  Hours covered:", [r["fh"] for r in aifs_scan["results"]])

ensemble_signals = {}
for model_key in w.ENSEMBLE_MODELS:
    _, model_name = w.ENSEMBLE_MODELS[model_key]
    print(f"Fetching ensemble: {model_name}...")
    ensemble_signals[model_key] = w.fetch_ensemble_genesis_signal(model_key)
    print(f"  -> {'signal found' if ensemble_signals[model_key] else 'no signal (quiet)'} ({_time.time()-start:.1f}s elapsed)")

print("Fetching NHC outlook summary...")
nhc_summary = w.fetch_nhc_outlook_summary()
print("NHC:", nhc_summary, f"({_time.time()-start:.1f}s elapsed)")

cycle_hour_utc = int(w.current_cycle_key().split("T")[1])
message = w.build_combined_cycle_report(cycle_hour_utc, gfs_scan, ecmwf_scan, ensemble_signals, nhc_summary)
print()
print("=== FULL MESSAGE ===")
print(message)
print("=== END MESSAGE ===")
print(f"Total time: {_time.time()-start:.1f}s")
print()

print("Sending for real via deliver()...")
try:
    w.deliver(message)
    print("SUCCESS: message delivered.")
except Exception as e:
    import traceback
    traceback.print_exc()
