#!/usr/bin/env python3
"""Find the camps around the lake in lidar, not in a map someone drew.

OSM has 271 structures on this lake. Rural Maine is thinly mapped and the camps
are the landmarks people actually navigate by -- "the one past the green
boathouse" -- so the map having only what a volunteer happened to trace is a
real gap.

3DEP's height-above-ground product is the same 2017 flight as the terrain, with
the ground subtracted. A camp is then simply a patch that stands 2-14 m up. So
is every spruce on the shore, and there are rather more of those, which is why
the discriminator is not height:

    a roof is FLAT and a tree is not.

Height above ground gets a building and a tree to the same number. Roughness --
the local spread of that height over a few metres -- separates them completely:
a roof plane varies by centimetres across itself, a canopy by metres. Then shape
does the rest, because a roof is convex and roughly rectangular while a clump of
touching crowns is neither.

Everything this finds is a DETECTION, not a survey. Output carries `detected:
true` and the height and area it was found with, so nothing downstream can mix
it up with a traced footprint.

    .venv/bin/python scripts/fetch_terrain.py --collection 3dep-lidar-hag \\
        --res-m 2 --pad-m 300 --out data/hag_2m.npz
    .venv/bin/python scripts/detect_structures.py
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import numpy as np
from scipy import ndimage
from shapely.geometry import box, mapping, shape
from shapely.ops import unary_union

ROOT = Path(__file__).resolve().parent.parent
HAG = ROOT / "data" / "hag_2m.npz"
DEPTH_GRID = ROOT / "data" / "depth_grid.json"
OSM = ROOT / "data" / "structures.geojson"
OUT = ROOT / "data" / "structures_lidar.geojson"

# A camp is a single storey on posts; a lodge is two. Below 2 m is a woodpile, a
# boulder, or the lidar's own noise over brush; above 14 m is a tree that the
# roughness test would probably have caught anyway.
MIN_H = 2.2
MAX_H = 14.0

# Local spread of the height, in metres, over a 5 m window. A roof plane is flat
# to within a few centimetres across that distance even when it is pitched; a
# canopy is not flat at any scale.
MAX_ROUGH = 0.85
ROUGH_WIN = 3          # cells, so 6 m at 2 m resolution

# Area bounds in square metres. An outhouse is about 2 m square and a camp 40 to
# 150; 900 is generous for a lodge and keeps a merged row of trees out.
MIN_AREA = 16.0
MAX_AREA = 900.0

# Fraction of its own bounding box a component fills. A building is convex and
# nearly rectangular; touching tree crowns make an L or a ring and fill maybe a
# third of the box they span.
MIN_EXTENT = 0.45
MAX_SPAN_M = 70.0      # nothing on this lake is bigger, and a long thin
                       # component that passes everything else is a road cut

MATCH_M = 18.0         # how close a detection has to be to count as the same
                       # building OSM already has


def load_hag(path: Path = HAG) -> tuple[np.ndarray, dict]:
    z = np.load(path, allow_pickle=False)
    meta = {k: float(z[k]) for k in ("lon0", "lat0", "dlon", "dlat", "grid_m")}
    return z["elev"].astype(np.float32), meta


def water_mask(shape_hw: tuple[int, int], meta: dict) -> np.ndarray:
    """True where the depth grid says lake.

    Detections land in the water otherwise: the lidar's height above ground is
    meaningless over a surface it got almost no return from, and what comes back
    is noise that occasionally looks flat and building-sized.
    """
    dg = json.loads(DEPTH_GRID.read_text())
    import base64
    d = np.frombuffer(base64.b64decode(dg["depths_b64"]), dtype=np.uint8)
    d = d.reshape(dg["ny"], dg["nx"])
    ny, nx = shape_hw
    # Map every cell of this lattice through lon/lat into the depth grid, rather
    # than assuming an integer ratio between the two.
    rows = np.arange(ny) * meta["dlat"] + meta["lat0"]
    cols = np.arange(nx) * meta["dlon"] + meta["lon0"]
    j = np.rint((cols - dg["lon0"]) / dg["dlon"]).astype(np.int64)
    i = np.rint((rows - dg["lat0"]) / dg["dlat"]).astype(np.int64)
    ok_j = (j >= 0) & (j < dg["nx"])
    ok_i = (i >= 0) & (i < dg["ny"])
    out = np.zeros(shape_hw, dtype=bool)
    sub = d[np.clip(i, 0, dg["ny"] - 1)][:, np.clip(j, 0, dg["nx"] - 1)]
    out[np.ix_(ok_i, ok_j)] = (sub != dg["nodata"])[np.ix_(ok_i, ok_j)]
    return out


def roughness(hag: np.ndarray, valid: np.ndarray, win: int = ROUGH_WIN) -> np.ndarray:
    """Local standard deviation of height, computed only over valid cells.

    NaNs have to be excluded rather than zero-filled: a zero-filled hole reads
    as a cliff and puts a rough ring around every void, which is exactly where
    the shoreline is.
    """
    h = np.where(valid, hag, 0.0).astype(np.float64)
    v = valid.astype(np.float64)
    k = (win, win)
    n = ndimage.uniform_filter(v, size=k, mode="nearest")
    mean = ndimage.uniform_filter(h, size=k, mode="nearest") / np.maximum(n, 1e-6)
    sq = ndimage.uniform_filter(h * h, size=k, mode="nearest") / np.maximum(n, 1e-6)
    return np.sqrt(np.maximum(sq - mean * mean, 0.0))


def detect(hag: np.ndarray, meta: dict, verbose: bool = True,
           max_rough: float = MAX_ROUGH) -> list[dict]:
    cell = meta["grid_m"]
    cell_area = cell * cell
    valid = np.isfinite(hag)

    tall = valid & (hag >= MIN_H) & (hag <= MAX_H)
    flat = roughness(hag, valid) <= max_rough
    on_land = ~water_mask(hag.shape, meta)

    mask = tall & flat & on_land
    # Opening drops the single flat cells scattered through the canopy; closing
    # then fills the chimney or the branch that punched a hole in a roof.
    mask = ndimage.binary_opening(mask, np.ones((3, 3)))
    mask = ndimage.binary_closing(mask, np.ones((3, 3)))

    labels, n = ndimage.label(mask)
    if verbose:
        print(f"  {tall.sum():,} cells tall enough, {(tall & flat).sum():,} of those flat, "
              f"{n:,} components")

    out = []
    for lab, sl in enumerate(ndimage.find_objects(labels), start=1):
        if sl is None:
            continue
        sub = labels[sl] == lab
        cells = int(sub.sum())
        area = cells * cell_area
        if area < MIN_AREA or area > MAX_AREA:
            continue
        h_rows = sl[0].stop - sl[0].start
        h_cols = sl[1].stop - sl[1].start
        span_m = max(h_rows, h_cols) * cell
        if span_m > MAX_SPAN_M:
            continue
        if cells / (h_rows * h_cols) < MIN_EXTENT:
            continue

        rr, cc = np.nonzero(sub)
        rr = rr + sl[0].start
        cc = cc + sl[1].start
        heights = hag[rr, cc]
        # The footprint as the union of the cells themselves, simplified. A
        # convex hull would square off every porch and dock the model onto a
        # shape the lidar did not see.
        squares = [box(meta["lon0"] + c * meta["dlon"], meta["lat0"] + r * meta["dlat"],
                       meta["lon0"] + (c + 1) * meta["dlon"],
                       meta["lat0"] + (r + 1) * meta["dlat"]) for r, c in zip(rr, cc)]
        poly = unary_union(squares).simplify(meta["dlon"] * 0.4)
        if poly.is_empty:
            continue
        out.append({
            "poly": poly,
            "lon": float(poly.centroid.x),
            "lat": float(poly.centroid.y),
            "height_m": round(float(np.median(heights)), 1),
            "area_m2": int(round(area)),
        })
    return out


def osm_points() -> list[tuple[float, float]]:
    if not OSM.exists():
        return []
    gj = json.loads(OSM.read_text())
    pts = []
    for f in gj.get("features", []):
        g = f.get("geometry")
        if not g:
            continue
        c = shape(g).centroid
        pts.append((c.x, c.y))
    return pts


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path, default=OUT)
    ap.add_argument("--max-rough", type=float, default=MAX_ROUGH)
    args = ap.parse_args()

    hag, meta = load_hag()
    print(f"hag {hag.shape[1]} x {hag.shape[0]} at {meta['grid_m']:.0f} m")
    found = detect(hag, meta, max_rough=args.max_rough)

    known = osm_points()
    # Degrees per metre at this latitude, for the match radius.
    dlat_m = 1.0 / 111_320.0
    dlon_m = dlat_m / math.cos(math.radians(meta["lat0"]))
    new = 0
    for f in found:
        f["known"] = any(
            abs(f["lon"] - x) < MATCH_M * dlon_m and abs(f["lat"] - y) < MATCH_M * dlat_m
            for x, y in known)
        new += not f["known"]

    gj = {
        "type": "FeatureCollection",
        "meta": {
            "source": "3DEP lidar height-above-ground, ME_Eastern_B1_2017, 2 m",
            "note": "detections, not surveyed footprints",
            "rule": f"{MIN_H}-{MAX_H} m tall, roughness <= {MAX_ROUGH} m, "
                    f"{MIN_AREA}-{MAX_AREA} m2, fills >= {MIN_EXTENT} of its box",
        },
        "features": [
            {"type": "Feature",
             "properties": {"kind": "building", "detected": True,
                            "height_m": f["height_m"], "area_m2": f["area_m2"],
                            "in_osm": f["known"],
                            "source": "3dep-lidar-hag-2017"},
             "geometry": mapping(f["poly"])}
            for f in found
        ],
    }
    args.out.write_text(json.dumps(gj))

    heights = sorted(f["height_m"] for f in found)
    areas = sorted(f["area_m2"] for f in found)
    print(f"wrote {args.out}")
    print(f"  {len(found):,} structures detected, {new:,} not in OSM "
          f"({len(found) - new:,} matched within {MATCH_M:.0f} m of an OSM one)")
    print(f"  OSM has {len(known):,} to compare against")
    if found:
        print(f"  height median {heights[len(heights) // 2]:.1f} m, "
              f"area median {areas[len(areas) // 2]} m2")


if __name__ == "__main__":
    main()
