#!/usr/bin/env python3
"""Bake the printable solid into a standalone 3D page.

Same height field the STL is cut from, so what spins in the browser is the
object that comes off the bed -- not a prettier cousin of it. One file, no
network, no library: open it or serve it and it works.

The exaggeration slider is live because the honest answer to "how deep is the
lake really" is a slider you can drag to 0.2x and watch the whole thing flatten.

    .venv/bin/python scripts/make_print_viewer.py --soundings --structures
    python3 -m http.server 9035 --directory dist   # then /print/viewer.html
"""

from __future__ import annotations

import argparse
import base64
import json
from pathlib import Path

import numpy as np

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))

from make_stl import (
    FT_PER_M,
    TERRAIN,
    EXAG_CAP,
    build_surface,
    crop_to_water,
    land_relative,
    load_depth_grid,
    load_terrain,
    mark_soundings,
    mark_structures,
    water_plane_m,
)

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "web" / "printview.template.html"
OUT = ROOT / "dist" / "print" / "viewer.html"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--width-mm", type=float, default=200.0)
    ap.add_argument("--exag", type=float, default=None)
    ap.add_argument("--target-mm", type=float, default=40.0)
    ap.add_argument("--base-mm", type=float, default=3.0)
    ap.add_argument("--land-pad-m", type=float, default=600.0)
    ap.add_argument("--no-terrain", action="store_true")
    # Default 2 rather than 1: the browser has to build the index buffer at load
    # time, and 155k vertices is a visible pause on a phone for detail no eye
    # picks out of a spinning object.
    ap.add_argument("--step", type=int, default=2)
    ap.add_argument("--soundings", action="store_true")
    ap.add_argument("--structures", action="store_true")
    ap.add_argument("--out", type=Path, default=OUT)
    args = ap.parse_args()

    depth, meta = load_depth_grid()
    land = None
    origin = 0
    water_ft = 0.0

    use_terrain = TERRAIN.exists() and not args.no_terrain
    if use_terrain:
        elev, pad = load_terrain()
        ny, nx = depth.shape
        full = np.full(elev.shape, np.nan)
        full[pad:pad + ny, pad:pad + nx] = depth
        water_ft = water_plane_m(full, elev) * FT_PER_M
        land = land_relative(full, elev, water_ft / FT_PER_M)
        depth = full
        origin = -pad
        margin_cells = int(round(args.land_pad_m / meta["grid_m"]))
    else:
        margin_cells = 8

    depth, i0, j0 = crop_to_water(depth, margin_cells)
    if land is not None:
        land = land[i0:i0 + depth.shape[0], j0:j0 + depth.shape[1]]

    scale = args.width_mm / (depth.shape[1] * meta["grid_m"])
    relief_m = float(np.nan_to_num(depth, nan=0.0).max())
    if land is not None:
        relief_m += float(land.max())
    exag = args.exag
    if exag is None:
        exag = min(EXAG_CAP, max(1.0, args.target_mm / max(relief_m * scale, 1e-9)))

    model = build_surface(depth, meta["grid_m"], args.width_mm, exag, args.base_mm,
                          args.step, origin + i0, origin + j0, land)

    pins = mark_soundings(model, meta, step=args.step) if args.soundings else 0
    built = piers = 0
    if args.structures:
        built, piers = mark_structures(model, meta, step=args.step)

    # The water plane is wherever land stops and water starts, which after
    # build_surface is simply the height a zero-depth cell landed at.
    tall = float(model.z.max())
    plane = args.base_mm + model.max_depth_m * model.mm_per_m

    # uint16 over the model's own range: 43 mm in 65,535 steps is well under a
    # micron, four orders finer than the printer resolves, at half the bytes of
    # float32 and a third of JSON numbers.
    zmax = max(tall, 1e-6)
    quant = np.clip(np.round(model.z / zmax * 65535.0), 0, 65535).astype("<u2")

    ny, nx = model.z.shape
    sub = (f"{args.width_mm:.0f} x {ny * model.cell_mm:.0f} x {tall:.1f} mm at "
           f"1:{model.scale_denom:,.0f}, printed at {exag:.3g}x vertical. ")
    sub += (f"Land is 3DEP lidar bare earth, 2017; the lake is 260 soundings from "
            f"1954, interpolated. Waterline {water_ft:.0f} ft."
            if use_terrain else
            "Land is flat because no elevation data is loaded.")

    payload = {
        "nx": nx, "ny": ny,
        "cell": round(model.cell_mm, 6),
        "zscale": zmax / 65535.0,
        "z": base64.b64encode(quant.tobytes()).decode("ascii"),
        "plane": round(plane, 4),
        "base": round(args.base_mm, 4),
        "tall": round(tall, 4),
        "markers": bool(args.soundings or args.structures),
        "sub": sub,
    }

    html = TEMPLATE.read_text().replace("__DATA__", json.dumps(payload))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(html)

    print(f"wrote {args.out}  ({args.out.stat().st_size / 1e6:.1f} MB, "
          f"{nx * ny:,} vertices)")
    print(f"  {args.width_mm:.0f} x {ny * model.cell_mm:.0f} x {tall:.1f} mm, "
          f"{exag:.3g}x vertical")
    if use_terrain:
        print(f"  waterline {water_ft:.0f} ft, highest ground "
              f"{model.land_relief_m:.0f} m")
    if pins or built or piers:
        print(f"  {pins} sounding pins, {built} building markers, {piers} piers")


if __name__ == "__main__":
    main()
