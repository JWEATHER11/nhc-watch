# NEXRAD corridor prototype -- validation notes

## Bucket
`noaa-nexrad-level2` (the bucket referenced by almost every Py-ART
tutorial and the NCEI metadata page) has been deprecated since
Sept 1 2025 and returns 403 Forbidden. The current bucket is
`unidata-nexrad-level2`, confirmed live via the AWS Open Data
Registry. Any future radar work in this repo should use that bucket.

## NaN handling fix (2026-07-30)
`nearest_reflectivity()` originally skipped any gate whose value was
`NaN`, treating it as "no data." That's wrong: Py-ART masks a gate as
NaN when the return is below the detectable-signal threshold -- i.e.
the radar checked that spot and found no significant echo. That's a
real, valid "clear" reading, not a coverage gap. Fixed to treat NaN
as 0.0 dBZ (a genuine "nothing there" reading) instead of skipping
it, and only skip a grid point when there's no real gate within
`MAX_GATE_DISTANCE_MI` (5mi) at all.

Before the fix: 35/81 grid points had "valid" data on a quiet night.
After: 81/81 -- because the other 46 always had a real (near, valid)
gate, it just legitimately read "no echo."

## Two-case validation (2026-07-30)
Ran the corridor check against two real cases using the fixed logic:

**Quiet night (KHGX 2026-07-30 13:03 UTC, KLCH 13:07 UTC, live fetch):**
- 81/81 grid points checked
- 0 hits >= 20 dBZ
- Max reflectivity: 12.5 dBZ near (29.55, -94.938)
- Correct: no storms, low-level scattered light returns only.

**Hurricane Beryl Houston landfall (KHGX 2024-07-08 13:38 UTC,
archived volume `KHGX20240708_133847_V06`):**
- 81/81 grid points checked
- 56/81 (69%) hits >= 20 dBZ
- Max reflectivity: 46.5 dBZ near (30.15, -94.631)
- Correct: real, known severe landfall shows heavy, widespread,
  believable reflectivity -- not zero, not clipped/saturated.

Both directions (correctly quiet, correctly picks up a known real
storm) check out. This is what justified promoting the corridor logic
out of prototype status into `radar_watch_pipeline.py` at the repo
root, which reuses this same grid + nearest-gate logic and adds
state-based dedup + Telegram delivery to the NWS chat.

**Houston derecho, May 16 2024 (KHGX 23:31 UTC, archived volume
`KHGX20240516_233117_V06`):** a fast, damaging squall line (100 mph
gusts, 4 tornadoes, 7 deaths, downtown Houston window damage) --
picked as a real "ordinary severe convection" case, distinct in kind
from a broad hurricane. First attempt used the wrong time window
(guessed 10-11 UTC / early morning without checking a source; the
storm actually hit Houston "shortly before 6pm CDT" = ~23:00 UTC per
news coverage) and showed a clean corridor -- a reminder to verify a
real event's actual time against a source before treating a null
result as meaningful. At the corrected time:
- 81/81 grid points checked
- 24/81 (30%) storm-intensity gates (>= 35 dBZ), 56% coverage >= 20 dBZ
- Max reflectivity: 54.5 dBZ near Houston -- correctly crosses into
  the severe/hail-possible tier
- Both trigger paths fired correctly: new storm-intensity areas
  (Beaumont/Port Arthur, Houston, Jasper, Lake Charles) and the
  severe-tier crossing.

Three real cases now checked, each a different shape of event (null,
broad hurricane, fast squall line) -- all came out correct.

## Still open / to keep watching
- `unidata-nexrad-level2` volume cadence is roughly 4-6 minutes;
  a 10-minute poll cycle should never miss more than one volume, but
  hasn't been stress-tested against a fast-moving squall line yet.
- This prototype file (`nexrad_corridor_check.py`) is left as-is for
  reference/manual re-validation -- the live pipeline lives at
  `../radar_watch_pipeline.py` and is NOT a straight import of this
  file (kept self-contained, matching every other pipeline in this
  repo).
