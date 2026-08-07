"""Decide where on this lake detection resolution is worth paying for.

Nate's steer, and it inverts the earlier framing: the water that matters is the
water *away* from the mainland shore. Everyone slows down near the bank. The
hazard that wrecks a lower unit is the one sitting in open water where a boat is
on plane, or in a stretch the 1954 survey never sounded so nothing on the chart
would warn you.

Two things this gets right that a plain shore buffer does not:

  Islands are not shore. Water 20 m off an island can be 600 m from the mainland,
  and it is open water in every sense that matters to a boat crossing it. The
  lake polygon carries its 74 islands as interior rings, so "distance to shore"
  is measured to the *exterior* ring only. Islands stay in the geometry as land
  -- a detector must not call one a rock -- but they do not make water safe.

  Survey gaps are their own priority. The 1954 survey ran 12 east-west transects
  about 530 m apart and measured nothing between them, so 42% of the lake sits
  more than 200 m from any sounding. Interpolated depth there is a drawing, not a
  measurement. Imagery is the only independent look those areas will get.

Output: data/priority.geojson -- one feature per tile, carrying the target
resolution the detector should spend there.
"""

import base64
import json
import sys
from pathlib import Path

import numpy as np
from pyproj import Transformer
from rasterio.features import rasterize
from rasterio.transform import from_origin
from scipy import ndimage
from shapely.geometry import Polygon, box, mapping, shape
from shapely.ops import transform as shp_transform

sys.path.insert(0, str(Path(__file__).resolve().parent))
from shoalrun_config import lake_crs

ROOT = Path(__file__).resolve().parent.parent
LAKE = ROOT / "data" / "lake.geojson"
DEPTH = ROOT / "data" / "depth_grid.json"
OUT = ROOT / "data" / "priority.geojson"

GRID_M = 10.0     # working raster for the distance transforms
TILE_M = 200.0    # detector work unit

# Thresholds. OPEN_M is "far enough from the mainland that a boat is moving";
# GAP_M is the survey-reach distance past which the charted depth is interpolation.
OPEN_M = 120.0
GAP_M = 200.0
FAR_GAP_M = 400.0

RES_HIGH = 0.3    # native only on the 2023 flight; 2018/2021 upsample from 0.6
RES_MID = 0.6
RES_BASE = 1.0


def load_reach():
    """Metres from each depth-grid cell to the nearest 1954 sounding."""
    d = json.loads(DEPTH.read_text())
    if "reach_b64" not in d:
        return None
    raw = np.frombuffer(base64.b64decode(d["reach_b64"]), dtype=np.uint8)
    g = raw.reshape(d["ny"], d["nx"]).astype("float32")
    g[g == d.get("nodata", 255)] = np.nan
    return {
        "grid": g * float(d["reach_step_m"]),
        "lon0": d["lon0"], "lat0": d["lat0"],
        "dlon": d["dlon"], "dlat": d["dlat"],
        "ny": d["ny"], "nx": d["nx"],
        "row_order": d.get("row_order", "south_to_north"),
    }


def main():
    gj = json.loads(LAKE.read_text())
    lake_ll = shape(gj["geometry"])
    crs = lake_crs()
    fwd = Transformer.from_crs("EPSG:4326", crs, always_xy=True)
    back = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
    lake = shp_transform(lambda x, y: fwd.transform(x, y), lake_ll)

    # Mainland: the exterior ring filled in, holes ignored. Distance to its edge
    # is what "distance from shore" has to mean once islands stop counting.
    mainland = Polygon(lake.exterior.coords)
    islands = [Polygon(r) for r in lake.interiors]
    print(f"lake {lake.area/1e6:.1f} km2, {len(islands)} islands, "
          f"mainland shore {mainland.exterior.length/1000:.1f} km")

    minx, miny, maxx, maxy = lake.bounds
    minx, miny = np.floor(minx / GRID_M) * GRID_M, np.floor(miny / GRID_M) * GRID_M
    maxx, maxy = np.ceil(maxx / GRID_M) * GRID_M, np.ceil(maxy / GRID_M) * GRID_M
    W = int((maxx - minx) / GRID_M)
    H = int((maxy - miny) / GRID_M)
    transform = from_origin(minx, maxy, GRID_M, GRID_M)

    inside = rasterize([(mapping(mainland), 1)], out_shape=(H, W), transform=transform,
                       dtype="uint8").astype(bool)
    island_mask = np.zeros((H, W), bool)
    if islands:
        island_mask = rasterize([(mapping(p), 1) for p in islands], out_shape=(H, W),
                                transform=transform, dtype="uint8").astype(bool)
    water = inside & ~island_mask

    # EDT inside the filled mainland gives distance to the mainland shoreline.
    # Islands are excluded from the mask being measured, deliberately -- carving
    # them out first would make island-adjacent water read as "near shore".
    shore_m = ndimage.distance_transform_edt(inside) * GRID_M
    island_m = ndimage.distance_transform_edt(~island_mask) * GRID_M

    reach = load_reach()
    reach_m = np.full((H, W), np.nan, "float32")
    if reach is not None:
        rows, cols = np.mgrid[0:H, 0:W]
        xs = minx + (cols + 0.5) * GRID_M
        ys = maxy - (rows + 0.5) * GRID_M
        lon, lat = back.transform(xs, ys)
        ci = np.round((lon - reach["lon0"]) / reach["dlon"]).astype(int)
        ri = np.round((lat - reach["lat0"]) / reach["dlat"]).astype(int)
        if reach["row_order"] != "south_to_north":
            ri = reach["ny"] - 1 - ri
        ok = (ci >= 0) & (ci < reach["nx"]) & (ri >= 0) & (ri < reach["ny"])
        reach_m[ok] = reach["grid"][ri[ok], ci[ok]]
        print(f"reach grid loaded; median over water {np.nanmedian(reach_m[water]):.0f} m")
    else:
        print("no reach grid in depth_grid.json -- survey-gap priority disabled")

    wa = water.sum() * GRID_M * GRID_M / 1e4
    print(f"water {wa:.0f} ha | >{OPEN_M:.0f} m from mainland: "
          f"{100*np.mean(shore_m[water] > OPEN_M):.0f}% | "
          f">{GAP_M:.0f} m from a sounding: "
          f"{100*np.nanmean(reach_m[water] > GAP_M):.0f}%")

    # --- tiles ---
    feats = []
    counts = {RES_HIGH: 0, RES_MID: 0, RES_BASE: 0}
    step = int(TILE_M / GRID_M)
    for y0 in range(0, H, step):
        for x0 in range(0, W, step):
            y1, x1 = min(H, y0 + step), min(W, x0 + step)
            w = water[y0:y1, x0:x1]
            if w.sum() < 4:
                continue
            s = shore_m[y0:y1, x0:x1][w]
            r = reach_m[y0:y1, x0:x1][w]
            i = island_m[y0:y1, x0:x1][w]
            open_water = float(np.mean(s > OPEN_M))
            gap = float(np.nanmean(r > GAP_M)) if np.isfinite(r).any() else 0.0
            far_gap = float(np.nanmean(r > FAR_GAP_M)) if np.isfinite(r).any() else 0.0

            if open_water > 0.5 and gap > 0.5:
                res, why = RES_HIGH, "open water the 1954 survey never sounded"
            elif open_water > 0.5 or far_gap > 0.5:
                res, why = RES_MID, ("open water" if open_water > 0.5
                                     else "far outside the survey's reach")
            else:
                res, why = RES_BASE, "near the mainland shore"
            counts[res] += 1

            left, top = minx + x0 * GRID_M, maxy - y0 * GRID_M
            right, bottom = minx + x1 * GRID_M, maxy - y1 * GRID_M
            poly_ll = shp_transform(lambda x, y: back.transform(x, y),
                                    box(left, bottom, right, top))
            feats.append({
                "type": "Feature",
                "geometry": mapping(poly_ll),
                "properties": {
                    "res_m": res,
                    "why": why,
                    "water_px": int(w.sum()),
                    "water_ha": round(float(w.sum()) * GRID_M * GRID_M / 1e4, 2),
                    "shore_m_median": round(float(np.median(s)), 1),
                    "island_m_median": round(float(np.median(i)), 1),
                    "reach_m_median": (round(float(np.nanmedian(r)), 1)
                                       if np.isfinite(r).any() else None),
                    "open_water_frac": round(open_water, 3),
                    "gap_frac": round(gap, 3),
                    "utm": [round(left, 1), round(bottom, 1), round(right, 1), round(top, 1)],
                },
            })

    total = sum(counts.values())
    ha = {res: sum(f["properties"]["water_ha"] for f in feats
                   if f["properties"]["res_m"] == res) for res in counts}
    print(f"{total} tiles @ {TILE_M:.0f} m")
    for res in (RES_HIGH, RES_MID, RES_BASE):
        print(f"  {res} m: {counts[res]:4d} tiles  {ha[res]:7.0f} ha "
              f"({100*ha[res]/max(1e-9, sum(ha.values())):.0f}% of water)")

    OUT.write_text(json.dumps({
        "type": "FeatureCollection",
        "properties": {
            "tile_m": TILE_M, "grid_m": GRID_M,
            "open_m": OPEN_M, "gap_m": GAP_M, "far_gap_m": FAR_GAP_M,
            "note": "distance to shore is measured to the mainland ring only; "
                    "islands are land but do not count as shore",
            "tiles": counts, "water_ha": ha,
        },
        "features": feats,
    }))
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
