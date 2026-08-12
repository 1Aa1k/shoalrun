#!/usr/bin/env python3
"""Find camps in NAIP aerial imagery at 0.6 m, by rectangularity.

Nate looked at the map and said houses are missing. He is right -- overlaying
what the map draws onto the 2023 aerial shows camp after camp with a dock and no
marker. Getting from "he is right" to "here they are" took several attempts, and
the failures are the useful part:

  1. Lidar height alone: 11,431 hits, median height 10.9 m. Trees.
  2. Microsoft's building model: 104 footprints, 3 that OSM lacked.
  3. Maine E911: 144 addresses. Real, and on the map -- but an address is not a
     building. A camp with a bunkhouse, a shed and a boathouse is one address.
  4. "Not vegetation" in NAIP: 1,901 hits, and a random twelve contained ZERO
     roofs. On a drawdown lake in September, bare ground is exposed lakebed.
  5. Not-vegetation AND standing up in lidar: better in principle, and it put
     detections in open water, because water reads as bare (its NDVI really is
     near zero) and lidar gets no return off a lake so its height there is noise.
     Masking the water left boulder fields and gravel roads.

Every one of those tested "is this pixel bare", which the shoreline is full of.
None tested the thing that actually distinguishes a building:

    a roof is a RECTANGLE. Bog, boulders and washouts are not.

So the shape test here is fill of the MINIMUM ROTATED rectangle, not of the
axis-aligned bounding box. That distinction is the whole difference: a camp at
40 degrees to north fills barely half its axis-aligned box -- the earlier
attempts were penalising real buildings and passing blobs -- while it fills over
0.8 of its rotated one. Nothing natural on this shoreline does that.

Everything found is a DETECTION, tagged `detected: true`, never merged silently
with a traced footprint. A roof under closed canopy is invisible to any of this,
which is why the E911 addresses stay on the map beside these.

    .venv/bin/python scripts/detect_roofs_naip.py
    .venv/bin/python scripts/detect_roofs_naip.py --year 2021
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import numpy as np
import planetary_computer
import pystac_client
import rasterio
from pyproj import Transformer
from rasterio.windows import Window, from_bounds
from scipy import ndimage
from shapely.geometry import MultiPoint, Point, shape

ROOT = Path(__file__).resolve().parent.parent
LAKE = ROOT / "data" / "lake.geojson"
STRUCT = ROOT / "data" / "structures.geojson"
OUT = ROOT / "data" / "structures_naip.geojson"

STAC = "https://planetarycomputer.microsoft.com/api/stac/v1"

# Vegetation index below which a surface is bare, and water index above which it
# is lake. Both are loose on purpose -- they are a first cut that throws away
# most of the scene, and the rectangularity test does the real work.
NDVI_BARE = 0.20
NDWI_WATER = 0.02

# Fill of the MINIMUM ROTATED rectangle. This is the load-bearing filter. A
# building, at any angle, fills 0.75-0.95 of the tightest rectangle that
# contains it. A patch of bog fills 0.5-0.6, a boulder field less.
MIN_RECT_FILL = 0.72

# Longest side over shortest. A dock is a 2 m x 20 m sliver and would sail
# through the fill test, being an excellent rectangle.
MAX_ASPECT = 3.2

# Area in square metres. A camp is 40-150, a lodge up to 400; below 20 is a
# shed roof or a parked boat, above 500 a parking area.
MIN_AREA_M2 = 20.0
MAX_AREA_M2 = 500.0

NEAR_M = 400.0        # how far inland to keep detections
MATCH_M = 25.0        # how close counts as already on the map

BLOCK = 2048
HALO = 64
M_PER_DEG_LAT = 111_320.0


def rect_fill(blob: np.ndarray) -> tuple[float, float]:
    """(fill of the tightest rotated rectangle, aspect ratio of that rectangle).

    Built from the pixels' corner points rather than their centres: using
    centres makes every rectangle half a pixel small on each side, which reads
    as over-full and lets small ragged blobs through.
    """
    rr, cc = np.nonzero(blob)
    if rr.size < 4:
        return 0.0, 99.0
    pts = np.concatenate([
        np.stack([cc + dc, rr + dr], axis=1)
        for dr, dc in ((0, 0), (0, 1), (1, 0), (1, 1))
    ])
    rect = MultiPoint(pts).minimum_rotated_rectangle
    if rect.is_empty or getattr(rect, "area", 0.0) <= 0:
        return 0.0, 99.0
    xs, ys = rect.exterior.coords.xy
    sides = [math.dist((xs[i], ys[i]), (xs[i + 1], ys[i + 1])) for i in range(4)]
    long_side, short_side = max(sides), min(sides)
    if short_side <= 0:
        return 0.0, 99.0
    return float(rr.size / rect.area), float(long_side / short_side)


def detect_in_tile(src, win, cell_m):
    """Rectangular bare blobs in one window, as (row, col, area_m2, fill)."""
    out = []
    min_cells = MIN_AREA_M2 / (cell_m * cell_m)
    max_cells = MAX_AREA_M2 / (cell_m * cell_m)

    for r0 in range(0, int(win.height), BLOCK):
        for c0 in range(0, int(win.width), BLOCK):
            rs = max(0, r0 - HALO)
            cs = max(0, c0 - HALO)
            re = min(int(win.height), r0 + BLOCK + HALO)
            ce = min(int(win.width), c0 + BLOCK + HALO)
            sub = Window(win.col_off + cs, win.row_off + rs, ce - cs, re - rs)
            arr = src.read([1, 2, 4], window=sub).astype(np.float32)
            if not arr.size:
                continue
            red, green, nir = arr[0], arr[1], arr[2]
            with np.errstate(invalid="ignore", divide="ignore"):
                ndvi = (nir - red) / np.maximum(nir + red, 1e-6)
                ndwi = (green - nir) / np.maximum(green + nir, 1e-6)

            lit = arr.sum(0) > 30           # the black no-data collar
            cand = (ndvi < NDVI_BARE) & (ndwi < NDWI_WATER) & lit
            # Open then close: kill the single-pixel canopy gaps, then fill the
            # chimney or the branch lying across a roof.
            cand = ndimage.binary_opening(cand, np.ones((3, 3)))
            cand = ndimage.binary_closing(cand, np.ones((5, 5)))

            lab, n = ndimage.label(cand)
            if not n:
                continue
            for i, sl in enumerate(ndimage.find_objects(lab), start=1):
                if sl is None:
                    continue
                blob = lab[sl] == i
                cells = int(blob.sum())
                if cells < min_cells or cells > max_cells:
                    continue
                fill, aspect = rect_fill(blob)
                if fill < MIN_RECT_FILL or aspect > MAX_ASPECT:
                    continue
                h = sl[0].stop - sl[0].start
                w = sl[1].stop - sl[1].start
                cr = sl[0].start + h / 2
                cc_ = sl[1].start + w / 2
                # The neighbouring block owns anything centred in the halo.
                if not (r0 - rs <= cr < min(r0 + BLOCK, int(win.height)) - rs):
                    continue
                if not (c0 - cs <= cc_ < min(c0 + BLOCK, int(win.width)) - cs):
                    continue
                out.append((rs + cr, cs + cc_, cells * cell_m * cell_m, fill))
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--year", type=int, default=None)
    ap.add_argument("--near-m", type=float, default=NEAR_M)
    ap.add_argument("--match-m", type=float, default=MATCH_M)
    ap.add_argument("--out", type=Path, default=OUT)
    args = ap.parse_args()

    gj = json.loads(LAKE.read_text())
    lake = shape(gj["features"][0]["geometry"]
                 if gj.get("type") == "FeatureCollection" else gj)
    west, south, east, north = lake.bounds
    dlat = 1.0 / M_PER_DEG_LAT
    dlon = dlat / math.cos(math.radians((south + north) / 2))
    bbox = (west - args.near_m * dlon, south - args.near_m * dlat,
            east + args.near_m * dlon, north + args.near_m * dlat)

    cat = pystac_client.Client.open(STAC, modifier=planetary_computer.sign_inplace)
    items = list(cat.search(collections=["naip"], bbox=list(bbox)).items())
    if not items:
        raise SystemExit(f"no NAIP over {bbox}")
    years = sorted({i.datetime.year for i in items})
    year = args.year or years[-1]
    items = [i for i in items if i.datetime.year == year]
    if not items:
        raise SystemExit(f"no NAIP for {year}; have {years}")
    print(f"NAIP {year}, {len(items)} tiles (available {years})")

    found = []
    for k, item in enumerate(items, 1):
        with rasterio.open(item.assets["image"].href) as src:
            fwd = Transformer.from_crs("EPSG:4326", src.crs, always_xy=True)
            inv = Transformer.from_crs(src.crs, "EPSG:4326", always_xy=True)
            x0, y0 = fwd.transform(bbox[0], bbox[1])
            x1, y1 = fwd.transform(bbox[2], bbox[3])
            try:
                w = from_bounds(min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1),
                                src.transform)
            except ValueError:
                continue
            # Clipped to the tile: from_bounds happily returns a window hanging
            # off the edge, and reading that pads with zeros that look like roof.
            r0 = max(0, int(w.row_off))
            c0 = max(0, int(w.col_off))
            r1 = min(src.height, int(w.row_off + w.height))
            c1 = min(src.width, int(w.col_off + w.width))
            if r1 <= r0 or c1 <= c0:
                continue
            win = Window(c0, r0, c1 - c0, r1 - r0)
            hits = detect_in_tile(src, win, abs(src.transform.a))
            for rr, cc, area, fill in hits:
                x, y = src.xy(win.row_off + rr, win.col_off + cc)
                lon, lat = inv.transform(x, y)
                found.append({"lon": lon, "lat": lat, "area_m2": int(round(area)),
                              "fill": round(fill, 2)})
            print(f"  [{k}/{len(items)}] {item.id}: {len(hits)} rectangles",
                  flush=True)

    near = []
    for f in found:
        d_m = lake.distance(Point(f["lon"], f["lat"])) * M_PER_DEG_LAT
        if d_m <= args.near_m:
            f["dist_m"] = round(d_m)
            near.append(f)

    known = []
    if STRUCT.exists():
        known = [shape(x["geometry"]).centroid
                 for x in json.loads(STRUCT.read_text())["features"]
                 if x.get("geometry")]
    tol = args.match_m * dlat
    new = 0
    for f in near:
        p = Point(f["lon"], f["lat"])
        f["on_map"] = any(p.distance(k) <= tol for k in known)
        new += not f["on_map"]

    out = {
        "type": "FeatureCollection",
        "meta": {
            "source": f"NAIP {year} aerial, 0.6 m",
            "note": "detections, not surveyed footprints",
            "rule": f"NDVI < {NDVI_BARE}, not water, {MIN_AREA_M2}-{MAX_AREA_M2} m2, "
                    f"fills >= {MIN_RECT_FILL} of its MINIMUM ROTATED rectangle, "
                    f"aspect <= {MAX_ASPECT}",
        },
        "features": [
            {"type": "Feature",
             "properties": {"kind": "building", "detected": True,
                            "source": f"naip-{year}", "area_m2": f["area_m2"],
                            "fill": f["fill"], "dist_m": f["dist_m"],
                            "on_map": f["on_map"]},
             "geometry": {"type": "Point",
                          "coordinates": [round(f["lon"], 6), round(f["lat"], 6)]}}
            for f in near
        ],
    }
    args.out.write_text(json.dumps(out))
    print(f"wrote {args.out}")
    print(f"  {len(found)} rectangles, {len(near)} within {args.near_m:.0f} m of water")
    print(f"  {new} not already drawn; the map draws {len(known)} structures")


if __name__ == "__main__":
    main()
