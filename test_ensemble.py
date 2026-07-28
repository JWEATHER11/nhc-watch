#!/usr/bin/env python3
"""One-off diagnostic: tests the ensemble genesis signal check for all
three models (GEFS, ECMWF ensemble, Google WeatherNext AI), printing
full results/errors."""

import wxmodel_pipeline as w

for model_key in w.ENSEMBLE_MODELS:
    _, model_name = w.ENSEMBLE_MODELS[model_key]
    print(f"=== Testing {model_name} ===")
    try:
        signal = w.fetch_ensemble_genesis_signal(model_key)
        if signal:
            print("SUCCESS. Signal found:")
            print(signal)
            print()
            print("Formatted report:")
            print(w.build_ensemble_report(model_name, signal))
        else:
            print("No findings above threshold anywhere in the domain (this is a valid, expected result on a quiet day).")
    except Exception as e:
        import traceback
        print("EXCEPTION during check:")
        traceback.print_exc()
    print()
