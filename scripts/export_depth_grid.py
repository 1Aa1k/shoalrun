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

sys.path.insert(0, str(Path(__file__).resolve().parent))
from make_contours import GRID_M, build_surface

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "depth_grid.json"

# Depth is stored as a byte, so anything at or above this cannot be represented.
# The surface maxes out around 78 ft; a value this high means the interpolation
# has gone wrong, and silently clipping it would hide that.
NODATA = 255
MAX_DEPTH_FT = 254


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
    }

    OUT.write_text(json.dumps(payload, separators=(",", ":")))
    water = int(finite.sum())
    print(
        f"wrote {OUT}  ({OUT.stat().st_size / 1024:.0f} KB base64) "
        f"{payload['nx']}x{payload['ny']} @ {GRID_M:.0f} m, "
        f"{water} water cells, 0-{deepest:.0f} ft"
    )


if __name__ == "__main__":
    main()
