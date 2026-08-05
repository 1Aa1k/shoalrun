"""How was the 1954 survey actually shaped? The UI caveat has to say something true.

"260 soundings over 34.5 km2, one per 13 hectares" was the old wording. It is
arithmetically correct and it misleads, because it implies the measurements are
spread evenly. They are not. Read the scanned survey sheet (data/millinocket_survey.pdf,
page 2) and the structure is obvious to the eye: the depths sit in straight
east-west rows. A boat ran transects and sounded along them. Between the lines
there is nothing at all.

That distinction matters for a hazard app. Along a transect the surface is
measured. Across the gap it is invented, and the gap is the part a boat crosses
when it runs north-south. A user is owed the second number, not the first.

This script measures the geometry so the caveat is a reproducible claim rather
than an impression from looking at a scan.
"""

import json
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
SOUND = ROOT / "data" / "soundings.geojson"

# Two soundings closer than this in latitude belong to the same transect. The
# survey rows are hundreds of metres apart and each row wanders by well under
# 100 m, so the split is not sensitive to the exact value -- verified by the
# stability check at the bottom.
ROW_GAP_M = 90.0

M_PER_DEG_LAT = 110540.0


def transects(lats, gap_m=ROW_GAP_M):
    """Group sorted latitudes into runs separated by more than gap_m."""
    rows = [[lats[0]]]
    for v in lats[1:]:
        if (v - rows[-1][-1]) * M_PER_DEG_LAT > gap_m:
            rows.append([])
        rows[-1].append(v)
    return rows


def main():
    feats = json.loads(SOUND.read_text())["features"]
    lats = np.sort(np.array([f["geometry"]["coordinates"][1] for f in feats]))
    rows = transects(lats)

    # Singletons are stray soundings (an island margin, a cove), not survey
    # lines. Counting them as transects would overstate the coverage.
    lines = [r for r in rows if len(r) >= 5]
    print(f"{len(lats)} soundings -> {len(rows)} latitude bands, "
          f"{len(lines)} of them real transects (>= 5 soundings)")
    for r in lines:
        print(f"  n={len(r):3d}  lat {np.mean(r):.5f}  "
              f"wander {(max(r) - min(r)) * M_PER_DEG_LAT:4.0f} m")

    centres = [np.mean(r) for r in lines]
    gaps = np.diff(centres) * M_PER_DEG_LAT
    print(f"\ngap between transects: mean {gaps.mean():.0f} m, "
          f"min {gaps.min():.0f} m, max {gaps.max():.0f} m")
    print(f"north-south extent of the surveyed water: "
          f"{(lats.max() - lats.min()) * M_PER_DEG_LAT / 1000:.1f} km")

    # The honest headline: crossing the lake north-south, this is the longest
    # stretch over which the depth shown was never measured.
    print(f"\nCAVEAT NUMBER: up to {gaps.max():.0f} m between measured lines")

    # Stability: the finding must not be an artifact of ROW_GAP_M.
    print("\nsensitivity of the transect count to the grouping threshold:")
    for g in (50, 70, 90, 120, 150):
        n = len([r for r in transects(lats, g) if len(r) >= 5])
        print(f"  gap {g:3d} m -> {n} transects")


if __name__ == "__main__":
    main()
