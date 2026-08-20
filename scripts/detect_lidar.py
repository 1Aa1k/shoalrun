#!/usr/bin/env python3
"""Things that stood above the water when the lidar flew. Mostly not rock.

DO NOT SHIP THIS AS A HAZARD LAYER. Measured against the 32 hand-mapped
reference rocks it is WORSE THAN CHANCE, and it is kept only because the
measurement is worth having and the next person will otherwise have the same
idea. `docs/handoffs/2026-08-19-lidar-above-water-null.md` has the numbers.

The idea was sound and the physics is real. Every optical route on this lake is
closed because no photon comes back off the bottom through tannic water; 1064 nm
does not penetrate water either, and that is the point. A return from inside the
lake polygon is a return off something solid that was above the surface. No
inference, no water column, no threshold on a colour.

Two things still hold and are worth keeping:

  1. The DSM keeps its voids. 3.19% of in-lake cells are NaN, so the product is
     gridded returns and not an interpolated skin.
  2. The flight caught a drawdown. In-lake returns sit at 145.668 m with a
     3.2 cm sd -- 477.9 ft, about 5 ft below the 483 ft full pool. The lidar saw
     this lake lower than any of the six NAIP flights ever did.

What killed it is what killed the camps sweep on the same shoreline: canopy.
Spruce leaning off the bank returns lidar from inside the lake polygon, metres
above the water, and no shore buffer that still keeps rock can exclude it --
the reference rocks sit a median 1.6 m from the shoreline. Scored in height
bands, every band from 0.15 m to 99 m loses to a shore-matched null, and every
band fires on the 137 known-empty `open_water` marks two to three times more
often than that null does. There is no band where this works.

The lift looks strongly positive (+25%) against a UNIFORM null, which is the
control the older scripts use. That control is not honest here: rocks hug the
shoreline and so does everything else that returns lidar near the waterline.

    .venv/bin/python scripts/fetch_terrain.py --collection 3dep-lidar-dsm \
        --res-m 2 --pad-m 0 --out data/dsm_2m.npz
    .venv/bin/python scripts/detect_lidar.py
    .venv/bin/python scripts/score_lidar.py     # the number that matters
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
# a cell right at the line is as likely to be beach as water.
#
# This was 10 m and that was a serious error, caught by scoring: the reference
# rocks on this lake sit a median 1.6 m from the shoreline, so a 10 m buffer
# excluded almost the entire population the detector exists to find, then scored
# 31% recall on what was left. 4 m is enough to keep the mapped line's own
# disagreement out without deleting the answer.
SHORE_BUFFER_M = 4.0

# Side of the block the local water surface is measured over, and the minimum
# number of water cells a block needs before its own median is trusted.
#
# One elevation for the whole lake is wrong in principle -- flight lines are
# flown at different times and wind pushes the surface up at one end. Measured
# here it is nearly right in practice: local planes vary 12 cm from p1 to p99
# and only 2 blocks of 3,626 clear the detection threshold on plane alone. It is
# corrected anyway because 12 cm against a 15 cm threshold leaves no margin, and
# because "measured, small" is a different claim from "assumed zero".
PLANE_BLOCK_M = 100.0
PLANE_MIN_CELLS = 200

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


def local_plane(elev: np.ndarray, mask: np.ndarray, global_m: float,
                cell_m: float, block_m: float = PLANE_BLOCK_M,
                min_cells: int = PLANE_MIN_CELLS) -> np.ndarray:
    """The water surface as a smooth field rather than one number.

    Blocks with too little water to measure -- a bay full of island, the middle
    of a shoal -- are filled from their neighbours by nearest value rather than
    dropped to the global plane, because dropping them reintroduces exactly the
    step this is here to remove, and puts it at the edge of every island.
    """
    B = max(1, int(round(block_m / cell_m)))
    water = mask & np.isfinite(elev) & (np.abs(elev - global_m) < 0.5)
    by, bx = elev.shape[0] // B, elev.shape[1] // B

    # Sums and counts per block via reshape, so this is two reductions rather
    # than a Python loop over ~7,500 blocks.
    trim = (slice(0, by * B), slice(0, bx * B))
    w = water[trim].reshape(by, B, bx, B)
    e = np.where(water[trim], elev[trim], 0.0).reshape(by, B, bx, B)
    cnt = w.sum(axis=(1, 3))
    # Mean, not median: a median needs the values kept, and at this block count
    # the difference is under a millimetre on a surface whose sd is 3 cm.
    coarse = np.where(cnt >= min_cells, e.sum(axis=(1, 3)) / np.maximum(cnt, 1), np.nan)

    good = np.isfinite(coarse)
    if not good.any():
        return np.full(elev.shape, global_m, dtype=np.float32)
    idx = ndimage.distance_transform_edt(~good, return_distances=False,
                                         return_indices=True)
    filled = coarse[tuple(idx)]

    full = ndimage.zoom(filled, (elev.shape[0] / by, elev.shape[1] / bx), order=1)
    return full[: elev.shape[0], : elev.shape[1]].astype(np.float32)


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

    surface = local_plane(elev, inner, plane, cell_m)
    dev = (surface[inner] - plane) * 100
    print(f"local surface deviates {np.percentile(dev, 1):+.1f} to "
          f"{np.percentile(dev, 99):+.1f} cm from the global plane")

    # float32 throughout: elev is float32 and `plane` being a Python float would
    # promote the whole 19 M cell grid to float64 for no gain.
    height = np.where(inner & np.isfinite(elev), elev - surface,
                      np.float32(-1.0)).astype(np.float32)
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
                "shore_buffer_m": args.shore_buffer_m,
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
