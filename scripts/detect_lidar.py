#!/usr/bin/env python3
"""Rocks that stood above the water when the lidar flew.

Every optical route on this lake is closed. Satellite-derived bathymetry scored
AUC 0.507 against the soundings, and the lake-stage exposure route died because
all six NAIP flights are July-September on a regulated lake held near full pool.
The common cause is that no photon comes back off the bottom through this water.

Lidar sidesteps the argument entirely. 1064 nm does not penetrate water either,
which is the point: a return from inside the lake polygon is a return off
something solid that was ABOVE the surface. No inference, no water column, no
threshold on a colour.

Two facts make this worth doing rather than merely sound:

  1. The DSM keeps its voids. 3.24% of in-lake cells are NaN, so the product is
     gridded returns and not an interpolated skin -- a fill would have smeared
     any rock into its surroundings and quietly manufactured detections.
  2. The flight caught a drawdown. In-lake returns sit at 145.668 m with a
     3.2 cm sd; that is 477.9 ft, about 5 ft below the 483 ft full pool. So this
     exposes the class NAIP structurally cannot: rock that is dry at low water
     and a foot under the prop at full pond.

What this does NOT give you: anything below the waterline at flight time. A rock
6 ft down on the day is as invisible here as it is to NAIP. This measures one
horizontal slice of the hazard field, taken at a known stage, and the honest use
of it is as ground truth -- real positives with real footprints -- for detectors
that must work where no lidar exists.

    .venv/bin/python scripts/fetch_terrain.py --collection 3dep-lidar-dsm \
        --res-m 2 --pad-m 0 --out data/dsm_2m.npz
    .venv/bin/python scripts/detect_lidar.py
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
from rasterio.features import rasterize
from rasterio.transform import from_origin
from scipy import ndimage
from shapely.geometry import shape

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
DSM = DATA / "dsm_2m.npz"
OUT = DATA / "rocks_lidar.geojson"

# Distance from the mapped shoreline that the mask ignores. The OSM shoreline and
# the 2017 lidar were drawn from different sources at different lake stages, so
# a cell right at the line is as likely to be beach as water. 10 m is wider than
# the disagreement measured between them.
SHORE_BUFFER_M = 10.0

# A return this far above the water plane is not water. The plane's own sd is
# 3.2 cm, so 0.15 m is roughly 5 sd -- high enough that surface noise cannot
# reach it, low enough to keep rock that was barely awash on the day.
MIN_HEIGHT_M = 0.15

# Below this a "detection" is one or two cells, which at 2 m is inside the
# product's own horizontal uncertainty. 12 m2 is three cells.
MIN_AREA_M2 = 12.0

# Above this it is not a boat hazard you steer around, it is an island OSM
# missed, and it gets tagged rather than dropped.
ISLAND_AREA_M2 = 2000.0


def lake_mask(ny: int, nx: int, lon0: float, lat0: float,
              dlon: float, dlat: float) -> np.ndarray:
    """The lake polygon on the DSM's own lattice, islands already excluded.

    lake.geojson carries 74 interior rings, so rasterizing it as a polygon
    removes the mapped islands for free. Rasterio writes north-up; the DSM array
    runs south to north, so the result is flipped to match rather than the array
    being flipped to match it.
    """
    poly = shape(json.loads((DATA / "lake.geojson").read_text())["geometry"])
    tr = from_origin(lon0, lat0 + ny * dlat, dlon, dlat)
    north_up = rasterize([(poly, 1)], out_shape=(ny, nx), transform=tr, dtype="uint8")
    return north_up[::-1].astype(bool)


def water_plane(elev: np.ndarray, mask: np.ndarray) -> float:
    """Elevation of the lake surface on the day of the flight.

    Two passes. The median over every in-lake return is already dominated by
    water, but islands and rock pull it up a little; excluding everything more
    than 0.5 m above that first estimate and re-taking the median removes them
    without needing to know where they are.
    """
    v = elev[mask]
    v = v[np.isfinite(v)]
    first = float(np.median(v))
    return float(np.median(v[v < first + 0.5]))


def components(height: np.ndarray, min_cells: int):
    """Label 8-connected blobs standing above the water plane.

    8-connected, not 4: a boulder that lands diagonally across the 2 m grid is
    one rock, and 4-connectivity would report it as two.
    """
    lab, n = ndimage.label(height > 0, structure=np.ones((3, 3), int))
    sizes = np.bincount(lab.ravel())
    keep = np.flatnonzero(sizes >= min_cells)
    keep = keep[keep != 0]
    return lab, keep


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dsm", type=Path, default=DSM)
    ap.add_argument("--min-height-m", type=float, default=MIN_HEIGHT_M)
    ap.add_argument("--min-area-m2", type=float, default=MIN_AREA_M2)
    ap.add_argument("--shore-buffer-m", type=float, default=SHORE_BUFFER_M)
    ap.add_argument("--out", type=Path, default=OUT)
    args = ap.parse_args()

    d = np.load(args.dsm, allow_pickle=True)
    elev = d["elev"]
    lon0, lat0 = float(d["lon0"]), float(d["lat0"])
    dlon, dlat = float(d["dlon"]), float(d["dlat"])
    cell_m = float(d["grid_m"])
    ny, nx = elev.shape

    mask = lake_mask(ny, nx, lon0, lat0, dlon, dlat)
    pad = int(round(args.shore_buffer_m / cell_m))
    inner = ndimage.binary_erosion(mask, np.ones((2 * pad + 1, 2 * pad + 1), bool))

    plane = water_plane(elev, inner)
    print(f"lake surface at flight time: {plane:.3f} m = {plane * 3.28084:.1f} ft")
    print(f"in-lake cells {inner.sum():,}, voids {np.isnan(elev[inner]).mean() * 100:.2f}%")

    height = np.where(inner & np.isfinite(elev), elev - plane, -1.0)
    above = np.where(height >= args.min_height_m, height, 0.0)

    cell_area = cell_m * cell_m
    min_cells = max(1, int(round(args.min_area_m2 / cell_area)))
    lab, keep = components(above, min_cells)
    print(f"blobs >= {args.min_area_m2:.0f} m2 above +{args.min_height_m:.2f} m: {len(keep)}")

    # objects=... so each slice is the blob's own bounding box; without it every
    # measurement below would scan the whole 19 M cell grid once per blob.
    slices = ndimage.find_objects(lab)
    feats = []
    for lid in keep:
        sl = slices[lid - 1]
        sub = lab[sl] == lid
        h = above[sl][sub]
        rows, cols = np.nonzero(sub)
        r = rows.mean() + sl[0].start
        c = cols.mean() + sl[1].start
        area = sub.sum() * cell_area
        feats.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [
                round(lon0 + (c + 0.5) * dlon, 7),
                round(lat0 + (r + 0.5) * dlat, 7),
            ]},
            "properties": {
                "lon": round(lon0 + (c + 0.5) * dlon, 7),
                "lat": round(lat0 + (r + 0.5) * dlat, 7),
                "area_m2": round(float(area), 1),
                "height_max_m": round(float(h.max()), 2),
                "height_mean_m": round(float(h.mean()), 2),
                "class": "island" if area > ISLAND_AREA_M2 else "rock",
                "source": "3dep-lidar-dsm-2m",
                "stage_ft": round(plane * 3.28084, 1),
                "verdict": "above_water_at_flight",
            },
        })

    feats.sort(key=lambda f: -f["properties"]["area_m2"])
    n_isl = sum(1 for f in feats if f["properties"]["class"] == "island")
    print(f"  rock {len(feats) - n_isl}, island (>{ISLAND_AREA_M2:.0f} m2) {n_isl}")

    hs = np.array([f["properties"]["height_max_m"] for f in feats])
    ar = np.array([f["properties"]["area_m2"] for f in feats])
    print(f"  height_max median {np.median(hs):.2f} m, 90th {np.percentile(hs, 90):.2f} m")
    print(f"  area     median {np.median(ar):.0f} m2, 90th {np.percentile(ar, 90):.0f} m2")

    args.out.write_text(json.dumps({
        "type": "FeatureCollection",
        "properties": {
            "source": "USGS_LPC_ME_Eastern_B1_2017 DSM 2 m via Planetary Computer",
            "water_plane_m": round(plane, 3),
            "water_plane_ft": round(plane * 3.28084, 1),
            "min_height_m": args.min_height_m,
            "min_area_m2": args.min_area_m2,
            "shore_buffer_m": args.shore_buffer_m,
            "note": "above the waterline at flight time only; says nothing about "
                    "rock that was submerged on the day",
        },
        "features": feats,
    }))
    print(f"wrote {args.out} ({args.out.stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
