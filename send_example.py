#!/usr/bin/env python3
import wxmodel_pipeline as w
import setx_swla_extra as sx

gfs_scan = w.fetch_model_grid("gfs", forecast_days=16)
ecmwf_scan = w.fetch_model_grid("ecmwf", forecast_days=15)
aifs_scan = w.fetch_model_grid("ecmwf", models_param="ecmwf_aifs025_single", label="OpenMeteo:AIFS", forecast_days=15)
ensemble_signals = {}
for model_key in w.ENSEMBLE_MODELS:
    ensemble_signals[model_key] = w.fetch_ensemble_genesis_signal(model_key)
nhc_summary = w.fetch_nhc_outlook_summary()
rainfall_flags = w.fetch_gulf_coast_rainfall()
setx_swla_outlook = sx.fetch_setx_swla_rainfall_outlook()
front_signal = sx.fetch_front_signal()
line_signal = sx.fetch_organized_line_signal()
temp_gradient = sx.fetch_temperature_gradient()
ndfd_totals = sx.fetch_ndfd_qpf_totals()
_, ndfd_summary, _ = sx.describe_ndfd_change(ndfd_totals, None, 0)
cycle_hour_utc = int(w.current_cycle_key().split("T")[1])
message = w.build_combined_cycle_report(cycle_hour_utc, gfs_scan, ecmwf_scan, aifs_scan, ensemble_signals, nhc_summary, rainfall_flags, setx_swla_outlook, ndfd_summary, front_signal, line_signal, temp_gradient)
print(message)
w.deliver(message)
print("SENT")
