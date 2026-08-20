#!/usr/bin/env python3
"""Bake the printable solid into a standalone 3D page.

Same height field the STL is cut from, so what spins in the browser is the
object that comes off the bed -- not a prettier cousin of it. One file, no
network, no library: open it or serve it and it works.

Depth and land get their own live sliders, locked together by default. Split
them and the page says so in the legend, because at that point the two halves of
the object can no longer be compared against each other by eye -- a slope running
into the water changes gradient at the shoreline for no reason but the setting.

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
    crop_to_mask,
    rim_band,
    shore_mask,
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
    ap.add_argument("--land-exag", type=float, default=None)
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
    ap.add_argument("--trim", action="store_true",
                    help="cut the outline to the lake's own shape")
    ap.add_argument("--shore-m", type=float, default=150.0,
                    help="metres of land kept around the water when --trim is on")
    ap.add_argument("--rim-mm", type=float, default=0.0,
                    help="flat rim at the waterline around the trimmed outline, in mm")
    ap.add_argument("--camp-nub-mm", type=float, default=0.0,
                    help="draw camps as domes this wide instead of house glyphs")
    ap.add_argument("--camp-nub-h-mm", type=float, default=2.0,
                    help="how tall the camp domes stand")
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

    # Same cut, same order as the STL: before the scale is taken off the
    # array's width, or the page and the print disagree about how big it is.
    mask = rim = None
    if args.trim:
        mask = shore_mask(depth, meta["grid_m"], args.shore_m)
        rim = rim_band(mask, args.rim_mm, args.width_mm) if args.rim_mm > 0 else None
        if rim is not None:
            mask = mask | rim
        mask, depth, land, rim, ti, tj = crop_to_mask(mask, depth, land, rim)
        i0 += ti
        j0 += tj

    scale = args.width_mm / (depth.shape[1] * meta["grid_m"])
    seen = np.ones(depth.shape, bool) if mask is None else mask
    if rim is not None:
        seen = seen & ~rim          # the rim is flattened; its hills are not relief
    relief_m = float(np.nan_to_num(depth, nan=0.0)[seen].max())
    if land is not None:
        relief_m += float(land[seen].max())
    exag = args.exag
    if exag is None:
        exag = min(EXAG_CAP, max(1.0, args.target_mm / max(relief_m * scale, 1e-9)))

    land_exag = args.land_exag if args.land_exag is not None else exag
    model = build_surface(depth, meta["grid_m"], args.width_mm, exag, args.base_mm,
                          args.step, origin + i0, origin + j0, land, land_exag)

    # The markers ship as a second copy of the surface rather than baked in, so
    # the page can turn them off. Somebody looking at 260 pins with no label
    # asks what the dots are, and the answer -- these are the only real
    # measurements, everything else is interpolation -- is the whole point of
    # having drawn them.
    sub_rim = None if rim is None else rim[::args.step, ::args.step]
    if sub_rim is not None:
        model.z[sub_rim] = args.base_mm + model.max_depth_m * model.mm_per_m

    plain_z = model.z.copy()
    pins = mark_soundings(model, meta, step=args.step) if args.soundings else 0
    built = piers = 0
    if args.structures:
        built, piers = mark_structures(model, meta, step=args.step,
                                       nub_mm=args.camp_nub_mm,
                                       nub_h_mm=args.camp_nub_h_mm)
    marked_z = model.z.copy() if (pins or built or piers) else None
    model.z[...] = plain_z

    sub_mask = None if mask is None else mask[::args.step, ::args.step]
    shown = np.maximum(plain_z, marked_z if marked_z is not None else plain_z)
    tall = float(shown.max() if sub_mask is None else shown[sub_mask].max())
    plane = args.base_mm + model.max_depth_m * model.mm_per_m

    # uint16 over the model's own range: 43 mm in 65,535 steps is well under a
    # micron, four orders finer than the printer resolves, at half the bytes of
    # float32 and a third of JSON numbers.
    zmax = max(tall, 1e-6)
    def quantize(z):
        q = np.clip(np.round(z / zmax * 65535.0), 0, 65535).astype("<u2")
        return base64.b64encode(q.tobytes()).decode("ascii")

    ny, nx = model.z.shape
    sub = (f"{args.width_mm:.0f} x {ny * model.cell_mm:.0f} x {tall:.1f} mm at "
           f"1:{model.scale_denom:,.0f}, printed at {exag:.3g}x vertical. ")
    sub += (f"Land is 3DEP lidar bare earth, 2017; the lake is 260 soundings from "
            f"1954, interpolated. Waterline {water_ft:.0f} ft."
            if use_terrain else
            "Land is flat because no elevation data is loaded.")

    bits = []
    if pins:
        bits.append(f"{pins} soundings, 1954")
    if built or piers:
        bits.append(f"{built} houses and {piers} piers "
                    f"(OSM + Maine E911)")
    mark_text = " + ".join(bits) + " (oversized)" if bits else ""
    # What the toggle should call them, so the button says what it does rather
    # than "measurements" when the things on screen are houses.
    mark_noun = ("houses" if (built and not pins) else
                 "houses and pins" if (built and pins) else "measurements")

    payload = {
        "nx": nx, "ny": ny,
        "cell": round(model.cell_mm, 6),
        "zscale": zmax / 65535.0,
        "z": quantize(plain_z),
        "zm": quantize(marked_z) if marked_z is not None else None,
        # Which samples a marker stands on. Without this the houses are drawn in
        # the terrain's own colour ramp and a 1.6 mm glyph on a 43 mm model is
        # invisible from anywhere but a raking angle -- which is exactly how the
        # first build of this looked.
        "mk": (base64.b64encode(
            ((marked_z - plain_z) > 1e-6).astype("uint8").tobytes()).decode("ascii")
            if marked_z is not None else None),
        "keep": (base64.b64encode(sub_mask.astype("uint8").tobytes()).decode("ascii")
                 if sub_mask is not None else None),
        "markText": mark_text,
        "markNoun": mark_noun,
        "plane": round(plane, 4),
        "exag": round(exag, 6),
        "landExag": round(land_exag, 6),
        # Millimetres of baked model per foot of real relief, one per side.
        # The page terraces in feet, so it needs the conversion the bake used.
        "mmPerFtDeep": round(model.mm_per_m / FT_PER_M, 8),
        "mmPerFtLand": round(model.mm_per_m_land / FT_PER_M, 8),
        "base": round(args.base_mm, 4),
        "tall": round(tall, 4),
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
        # Off the KEPT cells, or a trimmed page reports a hilltop it cut off.
        relief_mm = max(0.0, tall - plane)
        print(f"  waterline {water_ft:.0f} ft, highest ground "
              f"{relief_mm / model.mm_per_m_land if model.mm_per_m_land else 0:.0f} m")
    if pins or built or piers:
        print(f"  {pins} sounding pins, {built} building markers, {piers} piers")


if __name__ == "__main__":
    main()
