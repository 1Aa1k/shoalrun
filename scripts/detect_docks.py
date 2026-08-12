#!/usr/bin/env python3
"""Find camps by their docks, which is the one thing the trees do not hide.

Five attempts at finding roofs failed (see `detect_roofs_naip.py` for the list
and what each one actually measured). They all failed the same way: every test
asked "is this patch bare", and a September drawdown shoreline is full of bare
things that are not buildings -- exposed lakebed, bog, boulder fields, gravel
roads -- while the roofs that are there sit under closed spruce canopy.

A dock has none of those problems:

    it is a hard object sitting ON OPEN WATER.

No canopy can cover it. Its background is the most uniform surface on the lake.
The test is not "is it bare" but "is it not-water, INSIDE the water" -- a
question with almost no natural answers. And a dock means a camp, because nobody
builds one for the woods behind it.

The claim is deliberately narrow: this finds docks, and each dock is evidence of
a camp on the bank behind it. That is why the output carries `kind: "pier"` and
`detected: true` and never claims to be a building footprint.

Scored against the 38 piers OSM has already traced here, so the recall number in
the summary is measured rather than asserted.

    .venv/bin/python scripts/detect_docks.py
    .venv/bin/python scripts/detect_docks.py --year 2021    # cross-check
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
from rasterio.features import rasterize
from rasterio.windows import Window, from_bounds
from scipy import ndimage
from shapely.geometry import MultiPoint, Point, shape
from shapely.ops import transform as shp_transform

ROOT = Path(__file__).resolve().parent.parent
LAKE = ROOT / "data" / "lake.geojson"
STRUCT = ROOT / "data" / "structures.geojson"
OUT = ROOT / "data" / "docks.geojson"

STAC = "https://planetarycomputer.microsoft.com/api/stac/v1"

# Water index above which a NAIP pixel is open water. Water absorbs NIR almost
# completely, so this split is unusually clean at 0.6 m.
NDWI_WATER = 0.02

# A dock is 1-3 m wide and 4-30 m long. Area bounds catch both ends; the aspect
# floor is what separates a dock from a swim float or a rock.
MIN_AREA_M2 = 4.0
MAX_AREA_M2 = 220.0
MIN_ASPECT = 1.8
MAX_LEN_M = 45.0

# How far inside the lake polygon to look. Docks are attached to the bank, and
# looking further out finds boats and buoys, which are not evidence of anything
# fixed. Negative buffer, so this is a band along the inside of the shoreline.
BAND_M = 60.0

BLOCK = 2048
HALO = 64
M_PER_DEG_LAT = 111_320.0


def oriented(blob: np.ndarray) -> tuple[float, float]:
    """(aspect ratio, long side in pixels) of the tightest rotated rectangle."""
    rr, cc = np.nonzero(blob)
    if rr.size < 3:
        return 0.0, 0.0
    pts = np.concatenate([
        np.stack([cc + dc, rr + dr], axis=1)
        for dr, dc in ((0, 0), (0, 1), (1, 0), (1, 1))
    ])
    rect = MultiPoint(pts).minimum_rotated_rectangle
    if rect.is_empty or getattr(rect, "area", 0.0) <= 0:
        return 0.0, 0.0
    xs, ys = rect.exterior.coords.xy
    sides = [math.dist((xs[i], ys[i]), (xs[i + 1], ys[i + 1])) for i in range(4)]
    long_side, short_side = max(sides), min(sides)
    if short_side <= 0:
        return 0.0, long_side
    return long_side / short_side, long_side


def shore_band(lake, src, win) -> np.ndarray:
    """Mask of the window that lies within BAND_M inside the shoreline.

    Rasterised from the polygon rather than derived from the imagery: the
    imagery's own idea of where the water starts is the thing being measured,
    and using it to define the search area would make the test circular.
    """
    to_src = Transformer.from_crs("EPSG:4326", src.crs, always_xy=True).transform
    poly = shp_transform(to_src, lake)
    band = poly.difference(poly.buffer(-BAND_M))
    if band.is_empty:
        return np.zeros((int(win.height), int(win.width)), dtype=bool)
    return rasterize(
        [(band, 1)],
        out_shape=(int(win.height), int(win.width)),
        transform=src.window_transform(win),
        fill=0,
        dtype="uint8",
    ).astype(bool)


def detect_in_tile(src, win, cell_m, lake):
    """Dock-shaped not-water blobs inside the shore band."""
    out = []
    min_cells = MIN_AREA_M2 / (cell_m * cell_m)
    max_cells = MAX_AREA_M2 / (cell_m * cell_m)
    band_all = shore_band(lake, src, win)
    if not band_all.any():
        return out

    for r0 in range(0, int(win.height), BLOCK):
        for c0 in range(0, int(win.width), BLOCK):
            rs = max(0, r0 - HALO)
            cs = max(0, c0 - HALO)
            re = min(int(win.height), r0 + BLOCK + HALO)
            ce = min(int(win.width), c0 + BLOCK + HALO)
            band = band_all[rs:re, cs:ce]
            if not band.any():
                continue
            sub = Window(win.col_off + cs, win.row_off + rs, ce - cs, re - rs)
            arr = src.read([2, 4], window=sub).astype(np.float32)   # green, NIR
            if not arr.size:
                continue
            green, nir = arr[0], arr[1]
            with np.errstate(invalid="ignore", divide="ignore"):
                ndwi = (green - nir) / np.maximum(green + nir, 1e-6)
            lit = arr.sum(0) > 30
            hard = band & lit & (ndwi <= NDWI_WATER)
            # A single not-water pixel on the lake is a whitecap. Opening kills
            # those; closing then joins the planks of a dock the sun blew out.
            hard = ndimage.binary_opening(hard, np.ones((2, 2)))
            hard = ndimage.binary_closing(hard, np.ones((3, 3)))

            lab, n = ndimage.label(hard)
            if not n:
                continue
            for i, sl in enumerate(ndimage.find_objects(lab), start=1):
                if sl is None:
                    continue
                blob = lab[sl] == i
                cells = int(blob.sum())
                if cells < min_cells or cells > max_cells:
                    continue
                aspect, long_px = oriented(blob)
                if aspect < MIN_ASPECT or long_px * cell_m > MAX_LEN_M:
                    continue
                h = sl[0].stop - sl[0].start
                w = sl[1].stop - sl[1].start
                cr = sl[0].start + h / 2
                cc_ = sl[1].start + w / 2
                if not (r0 - rs <= cr < min(r0 + BLOCK, int(win.height)) - rs):
                    continue
                if not (c0 - cs <= cc_ < min(c0 + BLOCK, int(win.width)) - cs):
                    continue
                out.append((rs + cr, cs + cc_, cells * cell_m * cell_m,
                            aspect, long_px * cell_m))
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--year", type=int, default=None)
    ap.add_argument("--match-m", type=float, default=30.0)
    ap.add_argument("--out", type=Path, default=OUT)
    args = ap.parse_args()

    gj = json.loads(LAKE.read_text())
    lake = shape(gj["features"][0]["geometry"]
                 if gj.get("type") == "FeatureCollection" else gj)
    bbox = lake.bounds

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
            r0 = max(0, int(w.row_off))
            c0 = max(0, int(w.col_off))
            r1 = min(src.height, int(w.row_off + w.height))
            c1 = min(src.width, int(w.col_off + w.width))
            if r1 <= r0 or c1 <= c0:
                continue
            win = Window(c0, r0, c1 - c0, r1 - r0)
            hits = detect_in_tile(src, win, abs(src.transform.a), lake)
            for rr, cc, area, aspect, length in hits:
                x, y = src.xy(win.row_off + rr, win.col_off + cc)
                lon, lat = inv.transform(x, y)
                found.append({"lon": lon, "lat": lat, "area_m2": int(round(area)),
                              "aspect": round(aspect, 1),
                              "length_m": int(round(length))})
            print(f"  [{k}/{len(items)}] {item.id}: {len(hits)} docks", flush=True)

    # Score against the piers OSM has traced. This is the whole point of picking
    # docks: unlike roofs, there is a labelled set to be measured against.
    piers, buildings = [], []
    if STRUCT.exists():
        for f in json.loads(STRUCT.read_text())["features"]:
            if not f.get("geometry"):
                continue
            c = shape(f["geometry"]).centroid
            (piers if f["properties"]["kind"] == "pier" else buildings).append(c)
    dlat = 1.0 / M_PER_DEG_LAT
    tol = args.match_m * dlat
    pts = [Point(f["lon"], f["lat"]) for f in found]
    recall = sum(1 for p in piers if any(p.distance(q) <= tol for q in pts))

    for f, p in zip(found, pts):
        f["near_building_m"] = (
            int(round(min(p.distance(b) for b in buildings) * M_PER_DEG_LAT))
            if buildings else None)

    out = {
        "type": "FeatureCollection",
        "meta": {
            "source": f"NAIP {year} aerial, 0.6 m",
            "note": "detected docks; each one implies a camp on the bank behind it",
            "rule": f"not water (NDWI <= {NDWI_WATER}) inside a {BAND_M} m band "
                    f"within the shoreline, {MIN_AREA_M2}-{MAX_AREA_M2} m2, "
                    f"aspect >= {MIN_ASPECT}, length <= {MAX_LEN_M} m",
        },
        "features": [
            {"type": "Feature",
             "properties": {"kind": "pier", "detected": True,
                            "source": f"naip-{year}", "area_m2": f["area_m2"],
                            "aspect": f["aspect"], "length_m": f["length_m"],
                            "near_building_m": f["near_building_m"]},
             "geometry": {"type": "Point",
                          "coordinates": [round(f["lon"], 6), round(f["lat"], 6)]}}
            for f in found
        ],
    }
    args.out.write_text(json.dumps(out))
    print(f"wrote {args.out}")
    print(f"  {len(found)} docks detected")
    if piers:
        print(f"  recall against OSM's traced piers: {recall}/{len(piers)} "
              f"({recall / len(piers) * 100:.0f}%)")
    far = sum(1 for f in found
              if f["near_building_m"] is not None and f["near_building_m"] > 60)
    print(f"  {far} are more than 60 m from any building the map draws")


if __name__ == "__main__":
    main()
