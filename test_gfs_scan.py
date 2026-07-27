#!/usr/bin/env python3
"""One-off diagnostic: runs just the GFS deterministic scan and prints
full results/errors, without the rest of the pipeline, so we can see
exactly what's happening."""

import wxmodel_pipeline as w

print("Attempting to find a recent GFS cycle...")
try:
    scan = w.scan_gfs_deterministic()
    if scan:
        print("SUCCESS. Scan result:")
        print(scan)
        print()
        print("Formatted report:")
        print(w.build_gfs_deterministic_report(scan))
    else:
        print("scan_gfs_deterministic() returned None -- see printed reasons above.")
except Exception as e:
    import traceback
    print("EXCEPTION during scan:")
    traceback.print_exc()
