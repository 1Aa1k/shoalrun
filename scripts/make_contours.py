"""Interpolate the MDIFW soundings into 10 ft depth contours.

260 soundings over 34.5 km2 is roughly one measurement per 13 hectares -- far too
sparse to contour on its own. What rescues it is that the shoreline is a known
zero-depth boundary: every metre of the 99 km shoreline, and every one of the 74
island edges, is a depth-0 constraint. Densifying those rings into synthetic
zero-points anchors the interpolation everywhere the transects do not reach, and
stops the surface from extrapolating deep water into the shallows.

The result is an INTERPOLATED SURFACE, not a survey. Between transect lines it is
an educated guess, and the source soundings are from August 1954. A rock does not
appear in a 10 ft contour. Contours are context for the hazard layer; they are
not a substitute for it.
"""

import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from pyproj import Transformer

import sys as _sys; _sys.path.insert(0, str(Path(__file__).resolve().parent))
from shoalrun_config import lake_crs
LAKE_CRS = lake_crs()
from scipy.interpolate import griddata
from scipy.ndimage import gaussian_filter
from shapely.geometry import LineString, MultiLineString, mapping, shape
from shapely.ops import transform as shp_transform

ROOT = Path(__file__).resolve().parent.parent
LAKE = ROOT / "data" / "lake.geojson"
SOUND = ROOT / "data" / "soundings.geojson"
OUT = ROOT / "data" / "contours.geojson"

INTERVAL_FT = 10          # what Nate asked for
GRID_M = 25.0             # finer than this is false precision given the inputs
SHORE_STEP_M = 40.0       # spacing of synthetic zero-depth points along shore
SMOOTH_SIGMA = 1.6        # grid cells; tames triangulation facets from griddata
MIN_CONTOUR_PTS = 8       # drop slivers


def densify(line, step):
    """Points every `step` metres along a line."""
    n = max(2, int(line.length / step))
    return [line.interpolate(i / n, normalized=True) for i in range(n + 1)]


def main():
    lake_ll = shape(json.loads(LAKE.read_text())["geometry"])
    fwd = Transformer.from_crs("EPSG:4326", LAKE_CRS, always_xy=True)
    back = Transformer.from_crs(LAKE_CRS, "EPSG:4326", always_xy=True)
    lake = shp_transform(lambda x, y: fwd.transform(x, y), lake_ll)

    pts = json.loads(SOUND.read_text())["features"]
    sx, sy, sd = [], [], []
    for f in pts:
        lon, lat = f["geometry"]["coordinates"]
        x, y = fwd.transform(lon, lat)
        sx.append(x)
        sy.append(y)
        sd.append(f["properties"]["depth_ft"])
    print(f"{len(sd)} soundings, {min(sd):.0f}-{max(sd):.0f} ft")

    # Shoreline and island edges as depth-0 control points.
    polys = list(lake.geoms) if hasattr(lake, "geoms") else [lake]
    shore_pts = []
    for p in polys:
        for ring in [p.exterior, *p.interiors]:
            shore_pts.extend(densify(LineString(ring.coords), SHORE_STEP_M))
    print(f"{len(shore_pts)} synthetic zero-depth shoreline points")

    X = np.array(sx + [p.x for p in shore_pts])
    Y = np.array(sy + [p.y for p in shore_pts])
    D = np.array(sd + [0.0] * len(shore_pts))

    minx, miny, maxx, maxy = lake.bounds
    gx = np.arange(minx, maxx + GRID_M, GRID_M)
    gy = np.arange(miny, maxy + GRID_M, GRID_M)
    GX, GY = np.meshgrid(gx, gy)

    # Linear on the Delaunay triangulation, then nearest to fill the convex-hull
    # exterior. Cubic overshoots badly with data this sparse and invents depths
    # deeper than anything measured.
    grid = griddata((X, Y), D, (GX, GY), method="linear")
    holes = np.isnan(grid)
    if holes.any():
        grid[holes] = griddata((X, Y), D, (GX[holes], GY[holes]), method="nearest")

    grid = gaussian_filter(grid, SMOOTH_SIGMA)
    grid = np.clip(grid, 0, None)

    # Mask outside the lake so contours never run across land or islands.
    from rasterio.features import rasterize
    from rasterio.transform import from_origin

    transform = from_origin(gx[0], gy[-1], GRID_M, GRID_M)
    mask = rasterize(
        [(mapping(lake), 1)], out_shape=(len(gy), len(gx)), transform=transform, dtype="uint8"
    ).astype(bool)
    mask = mask[::-1]  # rasterize writes north-up; our grid is south-up
    grid = np.where(mask, grid, np.nan)

    inside = grid[np.isfinite(grid)]
    print(f"interpolated surface: 0-{inside.max():.0f} ft (survey max 86 ft)")

    levels = np.arange(INTERVAL_FT, np.nanmax(grid) + INTERVAL_FT, INTERVAL_FT)
    cs = plt.contour(GX, GY, grid, levels=levels)

    feats = []
    for level, seg_list in zip(cs.levels, cs.allsegs):
        for seg in seg_list:
            if len(seg) < MIN_CONTOUR_PTS:
                continue
            line = LineString(seg)
            line = shp_transform(lambda x, y: back.transform(x, y), line)
            feats.append(
                {
                    "type": "Feature",
                    "properties": {"depth_ft": int(level)},
                    "geometry": mapping(line),
                }
            )

    counts = {}
    for f in feats:
        counts[f["properties"]["depth_ft"]] = counts.get(f["properties"]["depth_ft"], 0) + 1
    print(f"\ncontours at {INTERVAL_FT} ft: {len(feats)} lines")
    for d in sorted(counts):
        print(f"  {d:3d} ft: {counts[d]} lines")

    OUT.write_text(json.dumps({"type": "FeatureCollection", "features": feats}))
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
