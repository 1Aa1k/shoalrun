"""Export the interpolated depth surface as a compact grid the browser can read.

Contours used to be baked at one fixed interval by `make_contours.py`, which
meant the only way to see a different interval was a rebuild. Shipping the
surface itself instead moves that decision to runtime: the app runs marching
squares on this grid, so the interval becomes a slider, and the 3D viewer builds
its mesh from the same numbers the contour lines come from. One interpolation,
two consumers, no chance of them disagreeing about where 20 ft is.

Encoding is uint8 feet, base64. The survey max is 86 ft and the interpolated
surface never exceeds it, so a byte per cell is lossless here at 1 ft
resolution -- which is already finer than 1954 soundings on a regulated lake
deserve. 255 is the out-of-lake sentinel. That keeps the payload around 100 KB
where JSON floats would be ~1 MB, and the whole app has to fit on a phone with
no cell service.
"""

import base64
import json
import sys
from pathlib import Path

import numpy as np
from scipy.spatial import cKDTree

sys.path.insert(0, str(Path(__file__).resolve().parent))
from make_contours import GRID_M, SOUND, build_surface

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "depth_grid.json"

# Depth is stored as a byte, so anything at or above this cannot be represented.
# The surface maxes out around 78 ft; a value this high means the interpolation
# has gone wrong, and silently clipping it would hide that.
NODATA = 255
MAX_DEPTH_FT = 254

# Distance to the nearest 1954 sounding, quantised to a byte. 8 m per step
# carries 2032 m. That looks like far more headroom than 662 m of transect gap
# needs, and it is not: the eastern arm and the outlet were barely sounded at
# all, and water there sits 1859 m from the nearest measurement. A 4 m step
# tripped the guard below, which is the guard working. 8 m is still finer than
# a lead-line fix from a boat in 1954 was ever good for.
REACH_STEP_M = 8.0
REACH_MAX_M = REACH_STEP_M * (NODATA - 1)


def survey_reach(gx, gy, finite, fwd):
    """Distance from every water cell to the nearest actual 1954 sounding.

    The depth surface is one continuous sheet, which makes all of it look
    equally known. It is not. The survey boat ran twelve east-west lines
    (scripts/survey_geometry.py); along them the depth was measured, and across
    the 430-662 m between them it is interpolation. That difference is invisible
    in a shaded surface and it is exactly what a boat crosses running
    north-south, so the app has to be able to draw it.

    Note this deliberately measures distance to a SOUNDING, not to the nearest
    interpolation constraint. `build_surface` also pins the surface to depth 0
    along the shoreline, and those synthetic points do carry real information --
    but "the shore is at the shore" is not a depth measurement, and folding them
    in here would paint the nearshore band as well-surveyed when nobody ever put
    a lead line in it.
    """
    feats = json.loads(SOUND.read_text())["features"]
    xy = np.array([fwd.transform(*f["geometry"]["coordinates"])
                   for f in feats])

    # Cell centres in projected metres, same axis order as the depth grid.
    mx, my = np.meshgrid(gx, gy)
    dist, _ = cKDTree(xy).query(np.column_stack([mx.ravel(), my.ravel()]))
    dist = dist.reshape(mx.shape)

    worst = float(dist[finite].max())
    if worst > REACH_MAX_M:
        raise ValueError(
            f"a water cell sits {worst:.0f} m from the nearest sounding, past "
            f"the {REACH_MAX_M:.0f} m this encoding carries -- widen it rather "
            "than saturating, or the map will claim coverage it does not have"
        )

    # Round down: at the boundary between two steps, report the SHORTER
    # distance as the one that gets rounded up. Overstating how far you are
    # from a measurement is the safe direction on a hazard app.
    q = np.where(finite, np.ceil(dist / REACH_STEP_M), NODATA)
    return q.astype(np.uint8), worst


def main():
    grid, gx, gy, lake, fwd, back = build_surface()

    finite = np.isfinite(grid)
    deepest = grid[finite].max()
    if deepest > MAX_DEPTH_FT:
        raise ValueError(
            f"surface reaches {deepest:.0f} ft, beyond the {MAX_DEPTH_FT} ft the "
            "uint8 encoding can carry -- widen the encoding rather than clipping"
        )

    # Round rather than truncate: at a 1 ft quantum, truncation biases the whole
    # surface half a foot shallow, and shallow-biased depth on a boat app is the
    # wrong direction to be wrong in.
    quant = np.where(finite, np.rint(np.nan_to_num(grid, nan=0.0)), NODATA)
    quant = quant.astype(np.uint8)

    reach, worst = survey_reach(gx, gy, finite, fwd)

    # Grid corner and step are carried in lon/lat so the browser needs no
    # projection library. The lake is 9 km across at 45.7N, where treating the
    # grid as locally rectangular in degrees costs well under a metre -- far
    # inside the 25 m cell.
    lon0, lat0 = back.transform(gx[0], gy[0])
    lon1, lat1 = back.transform(gx[-1], gy[-1])

    payload = {
        "nx": int(len(gx)),
        "ny": int(len(gy)),
        "lon0": round(lon0, 7),
        "lat0": round(lat0, 7),
        # Degrees per cell, derived from the true corner-to-corner span so any
        # projection stretch is absorbed into the step instead of accumulating.
        "dlon": round((lon1 - lon0) / (len(gx) - 1), 9),
        "dlat": round((lat1 - lat0) / (len(gy) - 1), 9),
        "grid_m": GRID_M,
        "nodata": NODATA,
        "max_ft": int(deepest),
        "units": "ft",
        # Row 0 is the SOUTH edge; rows ascend north. Stated explicitly because
        # getting this backwards flips the lake and still looks plausible.
        "row_order": "south_to_north",
        "source": "MDIFW soundings Aug 1954 (rev Jan 1979), interpolated",
        "depths_b64": base64.b64encode(quant.tobytes()).decode("ascii"),
        # Metres to the nearest real sounding, same grid, same sentinel.
        "reach_step_m": REACH_STEP_M,
        "reach_max_m": round(worst),
        "reach_b64": base64.b64encode(reach.tobytes()).decode("ascii"),
    }

    OUT.write_text(json.dumps(payload, separators=(",", ":")))
    water = int(finite.sum())
    print(
        f"wrote {OUT}  ({OUT.stat().st_size / 1024:.0f} KB base64) "
        f"{payload['nx']}x{payload['ny']} @ {GRID_M:.0f} m, "
        f"{water} water cells, 0-{deepest:.0f} ft, "
        f"furthest water from a sounding {worst:.0f} m"
    )
    far = int((reach[finite] * REACH_STEP_M > 200).sum())
    print(f"  {far} of {water} water cells ({100 * far / water:.0f}%) sit more "
          f"than 200 m from any measurement")


if __name__ == "__main__":
    main()
