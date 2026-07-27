#!/usr/bin/env python3
"""One-off diagnostic: tests the new Open-Meteo based GFS and ECMWF
deterministic grid scan, printing full results/errors."""

import wxmodel_pipeline as w

for model_key, endpoint, name in [("gfs_det", "gfs", "GFS Deterministic"), ("ecmwf_det", "ecmwf", "ECMWF Deterministic")]:
    print(f"=== Testing {name} ===")
    try:
        scan = w.fetch_model_grid(endpoint)
        if scan:
            print("SUCCESS. Raw scan result:")
            print(scan)
            print()
            print("Formatted report:")
            print(w.build_model_report(name, scan))
        else:
            print("fetch_model_grid() returned None -- see printed reasons above.")
    except Exception as e:
        import traceback
        print("EXCEPTION during scan:")
        traceback.print_exc()
    print()
