#!/usr/bin/env python3
"""Fetch the land around the lake: 3DEP lidar bare-earth DTM at 2 m.

Everything in this project so far stops at the waterline. The depth grid is a
rectangle of lake with NaN outside it, so the 3D print has been a basin cut into
a flat sheet -- land drawn flat because nothing here knew otherwise.

`ME_Eastern_B1_2017` covers this quad and has been sitting untouched since the
optical work started (`docs/handoffs/2026-08-08-lake-stage-exposure-null.md`
lists it as the one honest route never taken). It is bare-earth, so the print
gets hillsides rather than treetops -- DSM would put 20 m of spruce on every
slope and read as noise at 1:50,000.

Output lands on the depth grid's own lattice, extended outward by `--pad-m`, so
merging the two is an integer offset and not a resampling.

    .venv/bin/python scripts/fetch_terrain.py               # 1500 m of shore
    .venv/bin/python scripts/fetch_terrain.py --pad-m 4000  # the hills behind it
"""

from __future__ import annotations

import argparse
import base64
import json
from pathlib import Path

import numpy as np
import planetary_computer
import pystac_client
import rasterio
from rasterio.warp import Resampling, reproject

ROOT = Path(__file__).resolve().parent.parent
DEPTH_GRID = ROOT / "data" / "depth_grid.json"
OUT = ROOT / "data" / "terrain.npz"

STAC = "https://planetarycomputer.microsoft.com/api/stac/v1"
COLLECTION = "3dep-lidar-dtm"   # bare earth; -dsm is the same lidar with the trees on
MAX_ITEMS = 60                  # hard bound so a wide --pad-m cannot run away
M_PER_DEG_LAT = 111_320.0


def target_lattice(meta: dict, pad_m: float) -> dict:
    """The depth grid's lattice, grown outward by whole cells.

    Whole cells matter: a fractional offset would mean resampling the depth grid
    to meet the terrain, and the depth grid is already an interpolation of 260
    soundings. Interpolating it twice is how a survey turns into a drawing.
    """
    step_m = meta["grid_m"]
    pad = int(np.ceil(pad_m / step_m))
    return {
        "lon0": meta["lon0"] - pad * meta["dlon"],
        "lat0": meta["lat0"] - pad * meta["dlat"],
        "dlon": meta["dlon"],
        "dlat": meta["dlat"],
        "nx": meta["nx"] + 2 * pad,
        "ny": meta["ny"] + 2 * pad,
        "pad": pad,
        "grid_m": step_m,
    }


def fetch(lat_lattice: dict, verbose: bool = True) -> np.ndarray:
    """Mosaic every DTM tile that touches the lattice into one float32 array.

    Tiles are reprojected one at a time into the target and merged where the
    target is still empty, rather than accumulated and averaged: neighbouring
    3DEP tiles overlap by a few pixels and averaging across a seam smears the
    edge of every cliff that happens to fall on one.
    """
    L = lat_lattice
    west = L["lon0"]
    south = L["lat0"]
    east = west + L["nx"] * L["dlon"]
    north = south + L["ny"] * L["dlat"]
    bbox = [west, south, east, north]

    catalog = pystac_client.Client.open(STAC, modifier=planetary_computer.sign_inplace)
    items = list(catalog.search(collections=[COLLECTION], bbox=bbox,
                                limit=MAX_ITEMS).items())
    if not items:
        raise SystemExit(f"no {COLLECTION} tiles over {bbox}")
    if len(items) >= MAX_ITEMS:
        raise SystemExit(f"{len(items)} tiles hit the {MAX_ITEMS} cap -- lower --pad-m")

    dst = np.full((L["ny"], L["nx"]), np.nan, dtype=np.float32)
    dst_transform = rasterio.transform.from_origin(
        west, north, L["dlon"], L["dlat"])          # north-up, so rows run north to south

    for n, item in enumerate(items, 1):
        with rasterio.open(item.assets["data"].href) as src:
            tile = np.full(dst.shape, np.nan, dtype=np.float32)
            reproject(
                source=rasterio.band(src, 1),
                destination=tile,
                dst_transform=dst_transform,
                dst_crs="EPSG:4326",
                dst_nodata=np.nan,
                # Bilinear, because 2 m data going onto a 25 m lattice is being
                # decimated 12x -- nearest would sample one pixel in 150 and
                # turn a smooth slope into stair steps that are not there.
                resampling=Resampling.bilinear,
            )
        fresh = np.isnan(dst) & np.isfinite(tile)
        dst[fresh] = tile[fresh]
        if verbose:
            print(f"  [{n}/{len(items)}] {item.id}  "
                  f"{np.isfinite(dst).mean() * 100:5.1f}% covered")

    # Rows currently run north to south; the depth grid runs south to north.
    return dst[::-1]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pad-m", type=float, default=1500.0,
                    help="metres of land to fetch beyond the depth grid")
    ap.add_argument("--out", type=Path, default=OUT)
    args = ap.parse_args()

    meta = json.loads(DEPTH_GRID.read_text())
    lattice = target_lattice(meta, args.pad_m)
    print(f"target {lattice['nx']} x {lattice['ny']} cells at {lattice['grid_m']:.0f} m "
          f"({lattice['pad']} cells of pad)")

    elev = fetch(lattice)
    good = np.isfinite(elev)
    if not good.any():
        raise SystemExit("every tile came back empty")

    # Water is the giveaway that this is lidar: the lake returns almost nothing,
    # so the DTM over it is void or interpolated flat. Report it rather than
    # quietly filling it -- the depth grid is the authority inside the shoreline.
    np.savez_compressed(
        args.out,
        elev=elev,
        lon0=lattice["lon0"], lat0=lattice["lat0"],
        dlon=lattice["dlon"], dlat=lattice["dlat"],
        pad=lattice["pad"], grid_m=lattice["grid_m"],
        source=f"{COLLECTION} ME_Eastern_B1_2017, bare earth, 2 m, "
               f"bilinear onto {lattice['grid_m']:.0f} m",
    )
    lo, hi = np.nanpercentile(elev[good], [1, 99])
    print(f"wrote {args.out}  ({args.out.stat().st_size / 1e6:.1f} MB)")
    print(f"  {good.mean() * 100:.1f}% of cells have data")
    print(f"  elevation {np.nanmin(elev):.1f} to {np.nanmax(elev):.1f} m "
          f"(1-99%: {lo:.1f} to {hi:.1f})")


if __name__ == "__main__":
    main()
