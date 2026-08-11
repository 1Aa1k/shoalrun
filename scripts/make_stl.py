#!/usr/bin/env python3
"""Turn the depth grid into a printable solid.

The lake as an object: a slab whose top face is the land plane at the 1954
water surface, with the basin carved into it. Islands stand at the plane
because they are land; the shoreline is the cliff between the two.

Two things about the scale are worth knowing before printing one:

* **The depth has to be exaggerated or there is nothing to see.** At 200 mm
  across, this lake is 1:50,000 and its deepest point is 23 m -- 0.45 mm, one
  and a bit layer lines. The default 30x makes the basin 13.6 mm deep. Every
  print of this kind exaggerates; the honest part is saying so, which is why
  the exaggeration is stamped into the STL header and the printed notes.
* **There is no land relief.** Everything above the waterline is flat, because
  this project has no elevation data for the land -- 3DEP lidar exists for this
  quad and has never been fetched. Flat is not a claim that it is flat.

And the bottom itself is 260 lead-line soundings from August 1954 on 12
transects about 530 m apart, interpolated. A smooth printed basin looks far
more authoritative than that deserves. `--soundings` raises a pin at every
real measurement so the object shows you where the data actually is; the
smooth parts between the pins are interpolation.

Usage:
    .venv/bin/python scripts/make_stl.py                     # 200 mm, 30x
    .venv/bin/python scripts/make_stl.py --width-mm 280 --soundings
    .venv/bin/python scripts/make_stl.py --step 2            # half resolution
"""

from __future__ import annotations

import argparse
import base64
import json
import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
DEPTH_GRID = ROOT / "data" / "depth_grid.json"
SOUNDINGS = ROOT / "data" / "soundings.geojson"
OUT_DIR = ROOT / "dist" / "print"

FT_PER_M = 3.280839895


@dataclass(frozen=True)
class Model:
    """Everything downstream needs to describe or verify the solid."""

    z: np.ndarray          # (ny, nx) top surface, mm above the print bed
    cell_mm: float         # horizontal spacing of the samples
    mm_per_m: float        # vertical scale, already including exaggeration
    scale_denom: float     # 1:N horizontal
    max_depth_m: float
    row0: int = 0          # where the crop was taken from, in source cells
    col0: int = 0


def load_depth_grid(path: Path = DEPTH_GRID) -> tuple[np.ndarray, dict]:
    """Depth in metres, NaN where there is no water. Row 0 is the south edge."""
    meta = json.loads(path.read_text())
    raw = np.frombuffer(base64.b64decode(meta["depths_b64"]), dtype=np.uint8)
    raw = raw.reshape(meta["ny"], meta["nx"])
    depth = raw.astype(np.float64)
    depth[raw == meta["nodata"]] = np.nan
    if meta.get("units", "ft") == "ft":
        depth /= FT_PER_M
    return depth, meta


def crop_to_water(depth_m: np.ndarray, margin_cells: int) -> tuple[np.ndarray, int, int]:
    """Trim the dead land margin off the grid.

    The depth grid is a rectangle around the lake, and a third of it is land
    that prints as a flat shelf -- filament and bed space spent on nothing.
    Returns the cropped grid and the (row, col) origin it was cut from, which
    the sounding pins need to stay in register.
    """
    water = np.isfinite(depth_m)
    rows = np.flatnonzero(water.any(axis=1))
    cols = np.flatnonzero(water.any(axis=0))
    if not len(rows) or not len(cols):
        return depth_m, 0, 0
    i0 = max(0, rows[0] - margin_cells)
    i1 = min(depth_m.shape[0], rows[-1] + 1 + margin_cells)
    j0 = max(0, cols[0] - margin_cells)
    j1 = min(depth_m.shape[1], cols[-1] + 1 + margin_cells)
    return depth_m[i0:i1, j0:j1], int(i0), int(j0)


def build_surface(depth_m: np.ndarray, grid_m: float, width_mm: float,
                  exag: float, base_mm: float, step: int = 1,
                  row0: int = 0, col0: int = 0) -> Model:
    """Top surface of the slab, in millimetres above the bed.

    Land is the plane; water hangs below it by its own exaggerated depth. The
    slab is thick enough that `base_mm` of material remains under the deepest
    point, so the print never opens a hole in its own floor.
    """
    # Scale comes from the FULL grid's extent. Taking it after subsampling would
    # make --step silently change the scale of the model instead of its
    # resolution, which is the whole thing --step is not supposed to do.
    span_m = depth_m.shape[1] * grid_m         # the grid's own east-west extent
    if step > 1:
        depth_m = depth_m[::step, ::step]
    ny, nx = depth_m.shape

    scale = width_mm / span_m                  # mm of model per metre of lake
    cell_mm = grid_m * scale * step
    mm_per_m = scale * exag

    d = np.nan_to_num(depth_m, nan=0.0)        # land reads as zero depth
    max_depth_m = float(d.max())
    plane = base_mm + max_depth_m * mm_per_m
    return Model(
        z=plane - d * mm_per_m,
        cell_mm=cell_mm,
        mm_per_m=mm_per_m,
        scale_denom=1.0 / scale * 1000.0,      # mm per metre -> 1:N
        max_depth_m=max_depth_m,
        row0=row0,
        col0=col0,
    )


def mark_soundings(model: Model, meta: dict, height_mm: float = 0.7,
                   radius_mm: float = 0.9, step: int = 1) -> int:
    """Raise a pin on the floor at every real 1954 measurement.

    Returns how many landed inside the grid. A pin is added to the floor rather
    than cut into it so it survives at any layer height, and it is deliberately
    shorter than the shallowest interesting relief so it cannot be mistaken for
    a shoal.
    """
    if not SOUNDINGS.exists():
        return 0
    gj = json.loads(SOUNDINGS.read_text())
    ny, nx = model.z.shape
    rad_cells = max(1, int(round(radius_mm / model.cell_mm)))
    plane = float(model.z.max())
    hit = 0
    for feat in gj.get("features", []):
        lon, lat = feat["geometry"]["coordinates"][:2]
        # The grid is a plate carree lattice, so the index is a plain division.
        col = ((lon - meta["lon0"]) / meta["dlon"] - model.col0) / step
        row = ((lat - meta["lat0"]) / meta["dlat"] - model.row0) / step
        j, i = int(round(col)), int(round(row))
        if not (0 <= i < ny and 0 <= j < nx):
            continue
        hit += 1
        i0, i1 = max(0, i - rad_cells), min(ny, i + rad_cells + 1)
        j0, j1 = max(0, j - rad_cells), min(nx, j + rad_cells + 1)
        yy, xx = np.mgrid[i0:i1, j0:j1]
        disc = np.hypot(yy - i, xx - j) <= rad_cells
        patch = model.z[i0:i1, j0:j1]
        # Clamped to the water plane: a pin in two feet of water would otherwise
        # stand proud of the surface and read as an island.
        patch[disc] = np.minimum(patch[disc] + height_mm, plane)
    return hit


def solid_triangles(z: np.ndarray, cell_mm: float) -> np.ndarray:
    """Close the height field into a manifold solid: top, four walls, bottom.

    Every edge of the result is shared by exactly two triangles -- the walls
    reuse the top surface's own boundary vertices rather than recomputing them,
    which is the only way that can be true by construction instead of by luck.
    """
    ny, nx = z.shape
    xs = np.arange(nx) * cell_mm
    ys = np.arange(ny) * cell_mm
    X, Y = np.meshgrid(xs, ys)

    def corners(a, b, c, d):
        """Two triangles per quad, wound counter-clockwise seen from outside."""
        return np.concatenate([
            np.stack([a, b, c], axis=1),
            np.stack([a, c, d], axis=1),
        ])

    def pts(xx, yy, zz):
        return np.stack([xx.ravel(), yy.ravel(), zz.ravel()], axis=1)

    top = corners(
        pts(X[:-1, :-1], Y[:-1, :-1], z[:-1, :-1]),
        pts(X[:-1, 1:], Y[:-1, 1:], z[:-1, 1:]),
        pts(X[1:, 1:], Y[1:, 1:], z[1:, 1:]),
        pts(X[1:, :-1], Y[1:, :-1], z[1:, :-1]),
    )

    # Perimeter, counter-clockwise seen from above, each vertex once. The walls
    # and the bottom are both built from this one list -- a bottom made of two
    # big triangles would leave a T-junction at every one of the perimeter
    # vertices the walls do use, which is not a closed solid.
    per_x = np.concatenate([xs, np.full(ny - 1, xs[-1]), xs[-2::-1], np.zeros(ny - 2)])
    per_y = np.concatenate([np.zeros(nx), ys[1:], np.full(nx - 1, ys[-1]), ys[-2:0:-1]])
    per_z = np.concatenate([z[0, :], z[1:, -1], z[-1, -2::-1], z[-2:0:-1, 0]])

    lo = np.stack([per_x, per_y, np.zeros(len(per_x))], axis=1)
    hi = np.stack([per_x, per_y, per_z], axis=1)
    nxt = lambda a: np.roll(a, -1, axis=0)
    walls = corners(lo, nxt(lo), nxt(hi), hi)

    # Bottom fans from the centre rather than from a corner: a corner fan puts
    # every vertex along its own two edges in a zero-area triangle.
    centre = np.tile([[xs[-1] / 2, ys[-1] / 2, 0.0]], (len(lo), 1))
    bottom = np.stack([centre, nxt(lo), lo], axis=1)

    return np.concatenate([top, walls, bottom])


def mesh_volume_mm3(tris: np.ndarray) -> float:
    """Signed volume by the divergence theorem.

    Positive means the winding is consistently outward. This is the cheapest
    test that a solid is actually a solid and not an inside-out one, which
    slices into something hollow-looking and wrong.
    """
    a, b, c = tris[:, 0], tris[:, 1], tris[:, 2]
    return float(np.sum(np.einsum("ij,ij->i", a, np.cross(b, c))) / 6.0)


def is_closed(tris: np.ndarray) -> bool:
    """Every directed edge must have exactly one opposite twin."""
    quant = np.round(tris.reshape(-1, 3), 5)
    _, idx = np.unique(quant, axis=0, return_inverse=True)
    idx = idx.reshape(-1, 3)
    edges = np.concatenate([idx[:, [0, 1]], idx[:, [1, 2]], idx[:, [2, 0]]])
    key = np.sort(edges, axis=1)
    _, counts = np.unique(key, axis=0, return_counts=True)
    return bool(np.all(counts == 2))


def write_binary_stl(path: Path, tris: np.ndarray, header: str = "") -> None:
    n = len(tris)
    normals = np.cross(tris[:, 1] - tris[:, 0], tris[:, 2] - tris[:, 0])
    lens = np.linalg.norm(normals, axis=1)
    normals[lens > 0] /= lens[lens > 0, None]

    rec = np.zeros(n, dtype=np.dtype([("n", "<f4", 3), ("v", "<f4", (3, 3)),
                                      ("attr", "<u2")]))
    rec["n"] = normals
    rec["v"] = tris
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as fh:
        fh.write(header.encode("ascii", "replace")[:80].ljust(80, b" "))
        fh.write(np.uint32(n).tobytes())
        fh.write(rec.tobytes())


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--width-mm", type=float, default=200.0,
                    help="east-west size of the print (CR-10 bed is 300)")
    ap.add_argument("--exag", type=float, default=30.0,
                    help="vertical exaggeration; 1 is true scale and invisible")
    ap.add_argument("--base-mm", type=float, default=3.0,
                    help="material left under the deepest point")
    ap.add_argument("--margin-mm", type=float, default=4.0,
                    help="flat land border kept around the lake")
    ap.add_argument("--step", type=int, default=1,
                    help="sample every Nth grid cell; 2 quarters the file")
    ap.add_argument("--soundings", action="store_true",
                    help="raise a pin at each of the 260 real 1954 measurements")
    ap.add_argument("--out", type=Path, default=OUT_DIR / "millinocket.stl")
    args = ap.parse_args()

    depth, meta = load_depth_grid()
    margin_cells = max(0, int(round(args.margin_mm / args.width_mm * depth.shape[1])))
    depth, row0, col0 = crop_to_water(depth, margin_cells)
    model = build_surface(depth, meta["grid_m"], args.width_mm, args.exag,
                          args.base_mm, args.step, row0, col0)

    pins = mark_soundings(model, meta, step=args.step) if args.soundings else 0

    tris = solid_triangles(model.z, model.cell_mm)
    scale_txt = f"1:{model.scale_denom:,.0f} horiz, {args.exag:g}x vertical exaggeration"
    write_binary_stl(args.out, tris,
                     f"Millinocket Lake bathymetry, MDIFW 1954, {scale_txt}")

    ny, nx = model.z.shape
    depth_mm = model.max_depth_m * model.mm_per_m
    size = args.out.stat().st_size / 1e6
    print(f"wrote {args.out}  ({size:.1f} MB, {len(tris):,} triangles)")
    print(f"  {nx} x {ny} samples at {model.cell_mm:.3f} mm")
    print(f"  {args.width_mm:.0f} x {ny * model.cell_mm:.0f} x "
          f"{args.base_mm + depth_mm:.1f} mm")
    print(f"  {scale_txt}")
    print(f"  deepest point {model.max_depth_m * FT_PER_M:.0f} ft "
          f"-> {depth_mm:.1f} mm of relief")
    if args.soundings:
        print(f"  {pins} sounding pins")
    print(f"  closed solid: {is_closed(tris)}, "
          f"volume {mesh_volume_mm3(tris) / 1000:.0f} cm3")

    notes = args.out.with_suffix(".txt")
    notes.write_text(
        f"""Millinocket Lake, Maine -- printable bathymetry
{scale_txt}
{args.width_mm:.0f} x {ny * model.cell_mm:.0f} x {args.base_mm + depth_mm:.1f} mm

What this is
  The lake bottom as a slab: the flat top face is the water surface, the basin
  is carved into it, and the islands stand at the surface because they are land.

What it is not
  Depth is exaggerated {args.exag:g}x. At true scale the deepest point of this
  lake would be {model.max_depth_m * model.mm_per_m / args.exag:.2f} mm -- less than
  three layer lines.

  Land is flat because there is no elevation data here, not because it is flat.

  The bottom comes from 260 lead-line soundings taken in August 1954 on 12
  transects about 530 m apart, interpolated between. 42% of the lake is more
  than 200 m from any real measurement. Run with --soundings to raise a pin at
  every measured point; the smooth surface between the pins is arithmetic.

Printing (CR-10, 0.4 mm nozzle, PLA)
  No supports and no raft: the deepest overhang is the shoreline cliff, which
  spans one cell.
  0.2 mm layers gives {int((args.base_mm + depth_mm) / 0.2)} layers.
  Print it flat on the bed, bottom face down.
""")
    print(f"wrote {notes}")


if __name__ == "__main__":
    main()
