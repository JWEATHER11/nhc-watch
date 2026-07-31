#!/usr/bin/env python3
import wxmodel_pipeline as w
import setx_swla_extra as sx

gfs_scan = w.fetch_model_grid("gfs", forecast_days=16)
ecmwf_scan = w.fetch_model_grid("ecmwf", forecast_days=15)
ensemble_signals = {}
for model_key in w.ENSEMBLE_MODELS:
    ensemble_signals[model_key] = w.fetch_ensemble_genesis_signal(model_key)
state = w.load_state()
genesis_trend_notes = w._update_genesis_trend(ensemble_signals, state)
nhc_summary = w.fetch_nhc_outlook_summary()
rainfall_flags = w.fetch_gulf_coast_rainfall()
setx_swla_outlook = sx.fetch_setx_swla_rainfall_outlook()
if setx_swla_outlook:
    setx_swla_outlook["long_trend_label"] = sx.compute_day5_trend_label(setx_swla_outlook.get("long_coverage_pct"), dict(state))
front_signal = sx.fetch_front_signal()
line_signal = sx.fetch_organized_line_signal()
temp_gradient = sx.fetch_temperature_gradient()
ndfd_totals = sx.fetch_ndfd_qpf_totals()
should_send_ndfd, ndfd_summary, _ = sx.describe_ndfd_change(ndfd_totals, None, 0)
cycle_hour_utc = int(w.current_cycle_key().split("T")[1])
message = w.build_combined_cycle_report(cycle_hour_utc, gfs_scan, ecmwf_scan, ensemble_signals, nhc_summary, rainfall_flags, setx_swla_outlook, ndfd_summary, front_signal, line_signal, temp_gradient, should_send_ndfd, genesis_trend_notes=genesis_trend_notes)
print(message)
w.deliver(message)
print("SENT")
