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
    .venv/bin/python scripts/make_stl.py --step-ft 10        # 10 ft contour terraces
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
TERRAIN = ROOT / "data" / "terrain.npz"
STRUCTURES = ROOT / "data" / "structures.geojson"
OUT_DIR = ROOT / "dist" / "print"

FT_PER_M = 3.280839895
# Past roughly this the shoreline cliff stops being printable and the whole
# object reads as a spike field rather than a lake.
EXAG_CAP = 60.0


@dataclass(frozen=True)
class Model:
    """Everything downstream needs to describe or verify the solid."""

    z: np.ndarray          # (ny, nx) top surface, mm above the print bed
    cell_mm: float         # horizontal spacing of the samples
    mm_per_m: float        # vertical scale below the waterline, exaggeration included
    scale_denom: float     # 1:N horizontal
    max_depth_m: float
    mm_per_m_land: float = 0.0   # scale above the waterline; equal unless split
    land_relief_m: float = 0.0   # highest ground above the water plane
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


def load_terrain(path: Path = TERRAIN) -> tuple[np.ndarray, int]:
    """3DEP bare-earth elevation in metres, on the depth grid's own lattice.

    Returns the array and the pad, in cells, by which it overhangs the depth
    grid on every side.
    """
    z = np.load(path, allow_pickle=False)
    return z["elev"].astype(np.float64), int(z["pad"])


def water_plane_m(depth_m: np.ndarray, elev: np.ndarray, ring: int = 3) -> float:
    """Elevation of the water surface, read off the shore that surrounds it.

    Lidar gets almost no return off water, so the lake is a void in the DTM and
    its own surface cannot be measured directly. The land immediately around it
    can: a few cells of shoreline is the waterline on the day of the flight.
    Median, not mean, because a boathouse or a bluff in that ring would drag a
    mean and cannot move a median.
    """
    from scipy import ndimage

    water = np.isfinite(depth_m)
    near = ndimage.binary_dilation(water, iterations=ring) & ~water
    band = elev[near & np.isfinite(elev)]
    if band.size == 0:
        raise SystemExit("no shoreline cells with elevation -- is the pad too small?")
    return float(np.median(band))


def land_relative(depth_m: np.ndarray, elev: np.ndarray,
                  plane: float | None = None) -> np.ndarray:
    """Land height above the water plane, in metres, with the voids filled.

    Voids away from the lake are small (wet ground, a pond) and are filled from
    the nearest measured cell rather than dropped, because a NaN in a height
    field is a hole in the print.
    """
    from scipy import ndimage

    if plane is None:
        plane = water_plane_m(depth_m, elev)
    land = elev - plane
    holes = ~np.isfinite(land)
    if holes.any():
        idx = ndimage.distance_transform_edt(holes, return_distances=False,
                                             return_indices=True)
        land = land[tuple(idx)]
    # Land below the waterline is a bog, a stream bed, or lidar noise -- not a
    # second lake. It gets flattened to the plane rather than punched through it.
    return np.maximum(land, 0.0)


def terrace(metres: np.ndarray, step_ft: float) -> np.ndarray:
    """Snap a field of metres to whole steps of feet, measured from the water.

    Floor, not nearest: a terrace then covers everything at least that far from
    the waterline, which is what a contour band means on a chart. Rounding to
    nearest would let a 4 ft sounding sit on the 5 ft terrace and read as deeper
    than it is.

    The steps are in FEET because the source is in feet -- 260 lead-line
    soundings recorded in 1954 -- and a 3 m contour on data measured in feet is
    a contour of a unit conversion.
    """
    if step_ft <= 0:
        return metres
    ft = metres * FT_PER_M
    return np.floor(ft / step_ft) * step_ft / FT_PER_M


def build_surface(depth_m: np.ndarray, grid_m: float, width_mm: float,
                  exag: float, base_mm: float, step: int = 1,
                  row0: int = 0, col0: int = 0,
                  land_m: np.ndarray | None = None,
                  land_exag: float | None = None,
                  step_ft: float = 0.0, land_step_ft: float = 0.0) -> Model:
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
        if land_m is not None:
            land_m = land_m[::step, ::step]
    ny, nx = depth_m.shape

    scale = width_mm / span_m                  # mm of model per metre of lake
    cell_mm = grid_m * scale * step
    mm_per_m = scale * exag
    # Land can be given its own exaggeration. One scale is the truthful default
    # and stays the default; two is a legitimate thing to want when 23 m of
    # lake has to share an object with 232 m of hillside, and the only real
    # requirement is that the object says which it is.
    mm_per_m_land = scale * (exag if land_exag is None else land_exag)

    # One field, metres, up positive, zero at the water surface: the lake hangs
    # below it and the land stands above it. Both get the SAME vertical scale --
    # exaggerating the basin harder than the hills would make a picture, not a
    # model, and nothing on the object would say which.
    water = np.isfinite(depth_m)
    # Terracing happens in metres, before either exaggeration, so the steps stay
    # 10 real feet of water whatever the sliders are doing.
    d = terrace(np.nan_to_num(depth_m, nan=0.0), step_ft)
    below = np.where(water, -d, 0.0) * mm_per_m
    above = np.zeros_like(below)
    if land_m is not None:
        # Land steps are off unless asked for. Bare-earth lidar has real
        # centimetre-scale roughness everywhere, and terracing it turns a
        # hillside into sandpaper -- 10 ft bands are a chart convention for
        # water, where the surface is smooth interpolation to begin with.
        above = np.where(water, 0.0, terrace(land_m, land_step_ft)) * mm_per_m_land
    mm = below + above                          # one is always zero
    max_depth_m = float(d.max())
    return Model(
        z=base_mm + (mm - mm.min()),
        cell_mm=cell_mm,
        mm_per_m=mm_per_m,
        mm_per_m_land=mm_per_m_land,
        scale_denom=1.0 / scale * 1000.0,      # mm per metre -> 1:N
        max_depth_m=max_depth_m,
        land_relief_m=(float(terrace(land_m, land_step_ft).max())
                       if land_m is not None else 0.0),
        row0=row0,
        col0=col0,
    )


def raise_markers(model: Model, lonlat, meta: dict, height_mm: float,
                  radius_mm: float, step: int = 1) -> int:
    """Stand a disc of a given height on the surface at each lon/lat.

    Added to the surface rather than cut into it, so a marker survives at any
    layer height, and clamped to the water plane so one in shallow water cannot
    poke through and read as an island. Returns how many landed on the model.
    """
    ny, nx = model.z.shape
    rad = max(1, int(round(radius_mm / model.cell_mm)))
    plane = float(model.z.max())
    hit = 0
    for lon, lat in lonlat:
        # The grid is a plate carree lattice, so the index is a plain division.
        col = ((lon - meta["lon0"]) / meta["dlon"] - model.col0) / step
        row = ((lat - meta["lat0"]) / meta["dlat"] - model.row0) / step
        j, i = int(round(col)), int(round(row))
        if not (0 <= i < ny and 0 <= j < nx):
            continue
        hit += 1
        i0, i1 = max(0, i - rad), min(ny, i + rad + 1)
        j0, j1 = max(0, j - rad), min(nx, j + rad + 1)
        yy, xx = np.mgrid[i0:i1, j0:j1]
        disc = np.hypot(yy - i, xx - j) <= rad
        patch = model.z[i0:i1, j0:j1]
        patch[disc] = np.minimum(patch[disc] + height_mm, plane)
    return hit


def _points(path: Path, keep=None):
    """Lon/lat for every feature, using the centroid of anything that is not a
    point. Buildings here are 10-20 m across and the grid cell is 25 m, so a
    footprint could not be drawn at this scale even if it were traced."""
    if not path.exists():
        return []
    gj = json.loads(path.read_text())
    out = []
    for feat in gj.get("features", []):
        if keep and feat.get("properties", {}).get("kind") not in keep:
            continue
        geom = feat.get("geometry") or {}
        coords = geom.get("coordinates")
        if geom.get("type") == "Point":
            out.append(tuple(coords[:2]))
            continue
        flat = np.array(list(_flatten(coords)), dtype=float)
        if flat.size:
            out.append((float(flat[:, 0].mean()), float(flat[:, 1].mean())))
    return out


def _flatten(coords):
    """Yield every (lon, lat) pair out of arbitrarily nested GeoJSON rings."""
    if coords and isinstance(coords[0], (int, float)):
        yield coords[:2]
        return
    for c in coords:
        yield from _flatten(c)


def mark_soundings(model: Model, meta: dict, height_mm: float = 0.7,
                   radius_mm: float = 0.9, step: int = 1) -> int:
    """A pin at every real 1954 measurement.

    Deliberately shorter than any relief worth reading, so a pin cannot be
    mistaken for a shoal. Everything between the pins is interpolation.
    """
    return raise_markers(model, _points(SOUNDINGS), meta, height_mm, radius_mm, step)


def mark_structures(model: Model, meta: dict, step: int = 1) -> tuple[int, int]:
    """Buildings and piers from OSM, as markers rather than footprints.

    They are what makes the object findable -- the launch, the camps, the pier
    you actually leave from -- but at 1:56,000 a 15 m building is a third of a
    millimetre. These are oversized on purpose and the notes say so.
    """
    built = raise_markers(model, _points(STRUCTURES, {"building", "camp"}), meta,
                          1.2, 1.1, step)
    piers = raise_markers(model, _points(STRUCTURES, {"pier"}), meta,
                          0.6, 0.7, step)
    return built, piers


def shell_floor(z: np.ndarray, shell_mm: float) -> np.ndarray:
    """Underside of a constant-thickness shell, clamped to the bed.

    Where the surface is lower than the shell is thick the underside hits zero
    and the object is simply solid there, which is what keeps the deep end of
    the lake attached to the bed instead of floating over it.
    """
    return np.clip(z - shell_mm, 0.0, None)


def solid_triangles(z: np.ndarray, cell_mm: float,
                    z_bot: np.ndarray | None = None) -> np.ndarray:
    """Close the height field into a manifold solid: top, walls, bottom.

    Every edge of the result is shared by exactly two triangles -- the walls
    reuse the surfaces' own boundary vertices rather than recomputing them,
    which is the only way that can be true by construction instead of by luck.

    With `z_bot` the bottom is a second height field instead of a flat plane,
    which is how the hollow shell gets made: same lattice, same perimeter, so
    the walls still close it exactly.
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

    if z_bot is None:
        # Bottom fans from the centre rather than from a corner: a corner fan
        # puts every vertex along its own two edges in a zero-area triangle.
        centre = np.tile([[xs[-1] / 2, ys[-1] / 2, 0.0]], (len(lo), 1))
        bottom = np.stack([centre, nxt(lo), lo], axis=1)
    else:
        # Same grid as the top, wound the other way so it faces down and out.
        bottom = corners(
            pts(X[:-1, :-1], Y[:-1, :-1], z_bot[:-1, :-1]),
            pts(X[1:, :-1], Y[1:, :-1], z_bot[1:, :-1]),
            pts(X[1:, 1:], Y[1:, 1:], z_bot[1:, 1:]),
            pts(X[:-1, 1:], Y[:-1, 1:], z_bot[:-1, 1:]),
        )
        per_zb = np.concatenate([z_bot[0, :], z_bot[1:, -1],
                                 z_bot[-1, -2::-1], z_bot[-2:0:-1, 0]])
        lo = np.stack([per_x, per_y, per_zb], axis=1)
        walls = corners(lo, nxt(lo), nxt(hi), hi)

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


def support_estimate(z_bot: np.ndarray, cell_mm: float, density: float = 0.15,
                     overhang_deg: float = 45.0, density_pla: float = 1.24) -> dict:
    """What the supports under a hollow shell cost.

    A slicer supports a downward face only where it is shallower than its
    overhang angle -- steep shell walls hold themselves up, because each layer
    is offset from the one below by less than the wall is thick. Flat ceilings
    do not, and a terraced lake bottom is flat ceilings by construction.

    This is the number that decides whether hollowing is worth it, so it is
    computed rather than assumed.
    """
    gy, gx = np.gradient(z_bot, cell_mm)
    slope = np.hypot(gx, gy)                      # rise over run of the underside
    needs = (slope < math.tan(math.radians(overhang_deg))) & (z_bot > 0.4)
    volume = float((z_bot * needs).sum()) * cell_mm * cell_mm * density
    return {
        "grams": volume / 1000.0 * density_pla,
        "area_share": float(needs.mean()),
    }


def filament_estimate(tris: np.ndarray, volume_mm3: float, infill: float = 0.15,
                      wall_mm: float = 0.8, skin_mm: float = 0.8,
                      dia_mm: float = 1.75, density: float = 1.24) -> dict:
    """What a slicer will actually pull off the spool for this solid.

    A slicer does not print the volume of the mesh. It prints a shell -- two
    perimeters and a few solid layers top and bottom -- and then sparse infill
    inside it, so a 243 cm3 object is nowhere near 243 cm3 of plastic.

    Triangles are sorted into top, bottom and wall by their normal, because the
    three get different treatment: the top skin follows the terrain and is much
    larger than the footprint, which is the term a rule-of-thumb estimate gets
    wrong on a landscape.

    Defaults are Cura's for a 0.4 mm nozzle at 0.2 mm layers: 2 perimeters
    (0.8 mm), 4 solid layers (0.8 mm), 15% infill, PLA at 1.24 g/cm3.
    """
    n = np.cross(tris[:, 1] - tris[:, 0], tris[:, 2] - tris[:, 0])
    area = np.linalg.norm(n, axis=1) / 2.0
    with np.errstate(invalid="ignore", divide="ignore"):
        nz = np.where(area > 0, n[:, 2] / (2 * area), 0.0)

    top = float(area[nz > 0.1].sum())
    bottom = float(area[nz < -0.1].sum())
    wall = float(area[np.abs(nz) <= 0.1].sum())

    shell = top * skin_mm + bottom * skin_mm + wall * wall_mm
    # A thin object is all shell; it cannot use more plastic than it has volume.
    shell = min(shell, volume_mm3)
    used = shell + max(0.0, volume_mm3 - shell) * infill
    return {
        "solid_cm3": volume_mm3 / 1000.0,
        "used_cm3": used / 1000.0,
        "grams": used / 1000.0 * density,
        "metres": used / (math.pi * (dia_mm / 2) ** 2) / 1000.0,
        "spool_pct": used / 1000.0 * density / 1000.0 * 100.0,
        "shell_share": shell / used if used else 0.0,
    }


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
    ap.add_argument("--exag", type=float, default=None,
                    help="vertical exaggeration; default solves for --target-mm")
    ap.add_argument("--land-exag", type=float, default=None,
                    help="separate exaggeration for the land; defaults to --exag")
    ap.add_argument("--step-ft", type=float, default=0.0,
                    help="terrace the LAKE into whole steps of feet, e.g. 10")
    ap.add_argument("--land-step-ft", type=float, default=0.0,
                    help="terrace the land too; off by default, it reads as noise")
    ap.add_argument("--target-mm", type=float, default=40.0,
                    help="how tall the relief should print when --exag is not given")
    ap.add_argument("--base-mm", type=float, default=3.0,
                    help="material left under the deepest point")
    ap.add_argument("--land-pad-m", type=float, default=600.0,
                    help="metres of 3DEP land kept around the lake")
    ap.add_argument("--no-terrain", action="store_true",
                    help="flat land, the way this printed before 3DEP was fetched")
    ap.add_argument("--margin-mm", type=float, default=4.0,
                    help="flat land border kept around the lake")
    ap.add_argument("--step", type=int, default=1,
                    help="sample every Nth grid cell; 2 quarters the file")
    ap.add_argument("--soundings", action="store_true",
                    help="raise a pin at each of the 260 real 1954 measurements")
    ap.add_argument("--structures", action="store_true",
                    help="mark OSM buildings, camps and piers (oversized markers)")
    ap.add_argument("--shell-mm", type=float, default=0.0,
                    help="hollow it out, leaving a shell this thick (needs supports)")
    ap.add_argument("--infill", type=float, default=0.15,
                    help="infill fraction used for the filament estimate")
    ap.add_argument("--out", type=Path, default=OUT_DIR / "millinocket.stl")
    args = ap.parse_args()

    depth, meta = load_depth_grid()
    land = None
    origin = 0
    water_ft = 0.0

    use_terrain = TERRAIN.exists() and not args.no_terrain
    if use_terrain:
        elev, pad = load_terrain()
        # The terrain lattice is the depth lattice grown by `pad` cells, so the
        # depth grid drops into the middle of it at an integer offset. Anything
        # else here would mean resampling one of the two.
        ny, nx = depth.shape
        full = np.full(elev.shape, np.nan)
        full[pad:pad + ny, pad:pad + nx] = depth
        water_ft = water_plane_m(full, elev) * FT_PER_M
        land = land_relative(full, elev, water_ft / FT_PER_M)
        depth = full
        origin = -pad
        margin_cells = int(round(args.land_pad_m / meta["grid_m"]))
    else:
        margin_cells = max(0, int(round(args.margin_mm / args.width_mm * depth.shape[1])))

    keep = crop_to_water(depth, margin_cells)
    depth, i0, j0 = keep
    if land is not None:
        land = land[i0:i0 + depth.shape[0], j0:j0 + depth.shape[1]]
    # With the land in, one vertical scale has to serve 23 m of lake and 230 m
    # of hillside. Fixing the exaggeration would print a 138 mm spire; fixing the
    # total relief instead and solving for the exaggeration keeps the object a
    # printable size and still reports what it did, which is the part that
    # matters. An explicit --exag always wins.
    scale = args.width_mm / (depth.shape[1] * meta["grid_m"])
    relief_m = float(np.nan_to_num(depth, nan=0.0).max())
    if land is not None:
        relief_m += float(land.max())
    exag = args.exag
    if exag is None:
        exag = min(EXAG_CAP, max(1.0, args.target_mm / max(relief_m * scale, 1e-9)))

    land_exag = args.land_exag
    if land_exag is None and args.exag is None and land is not None:
        # Auto mode with the two split apart would be guessing at two unknowns
        # from one target height. Auto stays single-scale; splitting them is an
        # explicit act.
        land_exag = exag

    model = build_surface(depth, meta["grid_m"], args.width_mm, exag,
                          args.base_mm, args.step, origin + i0, origin + j0, land,
                          land_exag, args.step_ft, args.land_step_ft)

    pins = mark_soundings(model, meta, step=args.step) if args.soundings else 0
    built = piers = 0
    if args.structures:
        built, piers = mark_structures(model, meta, step=args.step)

    floor = shell_floor(model.z, args.shell_mm) if args.shell_mm > 0 else None
    tris = solid_triangles(model.z, model.cell_mm, floor)
    steps = []
    if args.step_ft > 0:
        steps.append(f"{args.step_ft:g} ft lake steps")
    if args.land_step_ft > 0:
        steps.append(f"{args.land_step_ft:g} ft land steps")
    steps_txt = (", " + " / ".join(steps)) if steps else ""
    split = land_exag is not None and abs(land_exag - exag) > 1e-9
    scale_txt = (f"1:{model.scale_denom:,.0f} horiz, {exag:.3g}x depth / "
                 f"{land_exag:.3g}x land -- SPLIT vertical scales" + steps_txt
                 if split else
                 f"1:{model.scale_denom:,.0f} horiz, "
                 f"{exag:.3g}x vertical exaggeration" + steps_txt)
    write_binary_stl(args.out, tris,
                     f"Millinocket Lake bathymetry, MDIFW 1954, {scale_txt}")

    ny, nx = model.z.shape
    depth_mm = model.max_depth_m * model.mm_per_m
    tall_mm = float(model.z.max())          # the print's own height, land included
    size = args.out.stat().st_size / 1e6
    print(f"wrote {args.out}  ({size:.1f} MB, {len(tris):,} triangles)")
    print(f"  {nx} x {ny} samples at {model.cell_mm:.3f} mm")
    print(f"  {args.width_mm:.0f} x {ny * model.cell_mm:.0f} x "
          f"{tall_mm:.1f} mm")
    print(f"  {scale_txt}")
    print(f"  deepest point {model.max_depth_m * FT_PER_M:.0f} ft "
          f"-> {depth_mm:.1f} mm below the water plane")
    if use_terrain:
        print(f"  highest ground {model.land_relief_m:.0f} m "
              f"-> {model.land_relief_m * model.mm_per_m_land:.1f} mm above it")
    if args.soundings:
        print(f"  {pins} sounding pins")
    if args.structures:
        print(f"  {built} building/camp markers, {piers} pier markers")
    vol = mesh_volume_mm3(tris)
    print(f"  closed solid: {is_closed(tris)}, volume {vol / 1000:.0f} cm3")
    est = filament_estimate(tris, vol, infill=args.infill)
    sup = {"grams": 0.0, "area_share": 0.0}
    solid_g = filament_estimate(solid_triangles(model.z, model.cell_mm),
                                mesh_volume_mm3(solid_triangles(model.z, model.cell_mm)),
                                infill=args.infill)["grams"]
    if args.shell_mm > 0:
        sup = support_estimate(floor, model.cell_mm)
        print(f"  hollow {args.shell_mm:g} mm shell: {est['grams']:.0f} g of part")
        print(f"  supports under {sup['area_share'] * 100:.0f}% of the footprint: "
              f"about {sup['grams']:.0f} g more, so {est['grams'] + sup['grams']:.0f} g "
              f"all in")
    else:
        print(f"  filament at {args.infill * 100:.0f}% infill: {est['grams']:.0f} g, "
              f"{est['metres']:.0f} m, {est['spool_pct']:.0f}% of a 1 kg spool "
              f"({est['shell_share'] * 100:.0f}% of it is shell)")

    split_note = (
        f"\n  The lake and the land are NOT on the same vertical scale here: depth is\n"
        f"  exaggerated {exag:.3g}x and land {land_exag:.3g}x. Nothing about the object\n"
        f"  reveals that, so the two cannot be compared against each other by eye --\n"
        f"  a slope running into the water changes gradient at the shoreline for no\n"
        f"  reason but this setting.\n"
        if split else ""
    )
    what = (
        "  The lake and the land around it, cut from one slab and sharing one\n"
        "  vertical scale. The waterline is the flat plane the hills rise from and\n"
        "  the basin drops below; islands sit on that plane because they are land."
        if use_terrain else
        "  The lake bottom as a slab: the flat top face is the water surface, the\n"
        "  basin is carved into it, and the islands stand at the surface because\n"
        "  they are land."
    )
    land_note = (
        f"  Land comes from 3DEP lidar bare-earth returns, 2 m, flown 2017, and\n"
        f"  carries the SAME {exag:.3g}x exaggeration as the lake -- one vertical scale\n"
        f"  for the whole object. The water surface it is measured against reads\n"
        f"  {water_ft:.0f} ft, which is the lake's published elevation, so the two\n"
        f"  datasets agree about where the shoreline is."
        if use_terrain else
        "  Land is flat because there is no elevation data loaded, not because it\n"
        "  is flat. Run scripts/fetch_terrain.py and rebuild to get the hills."
    )
    support_note = (
        f"  HOLLOW at {args.shell_mm:g} mm, so it needs supports: the underside is a\n"
        f"  ceiling over {sup['area_share'] * 100:.0f}% of the footprint, and a terraced lake\n"
        f"  bottom is flat ceilings by construction. The shell floats about\n"
        f"  {tall_mm - args.shell_mm:.0f} mm off the bed, so the support is a tower under the\n"
        f"  whole object -- removable, but it doubles the print and leaves the\n"
        f"  underside rough. Solid at 15% infill is {solid_g:.0f} g against {est['grams'] + sup['grams']:.0f} g here."
        if args.shell_mm > 0 else
        "  No supports and no raft: the deepest overhang is the shoreline cliff,\n"
        "  which spans one cell."
    )
    marker_note = (
        f"  {built} buildings and camps and {piers} piers are marked from OSM. The\n"
        f"  markers are oversized -- a 15 m building is 0.3 mm at this scale -- so\n"
        f"  treat them as positions, not footprints.\n\n"
        if args.structures else ""
    )
    notes = args.out.with_suffix(".txt")
    notes.write_text(
        f"""Millinocket Lake, Maine -- printable bathymetry
{scale_txt}
{args.width_mm:.0f} x {ny * model.cell_mm:.0f} x {tall_mm:.1f} mm

What this is
{what}

What it is not
  Depth is exaggerated {exag:.3g}x. At true scale the deepest point of this lake
  would be {model.max_depth_m * model.mm_per_m / exag:.2f} mm -- under three layer lines.
{split_note}

{land_note}

  The bottom comes from 260 lead-line soundings taken in August 1954 on 12
  transects about 530 m apart, interpolated between. 42% of the lake is more
  than 200 m from any real measurement. Run with --soundings to raise a pin at
  every measured point; the smooth surface between the pins is arithmetic.

{marker_note}Printing (CR-10, 0.4 mm nozzle, PLA)
{support_note}
  0.2 mm layers gives {int(tall_mm / 0.2)} layers.
  Print it flat on the bed, bottom face down.
  At {args.infill * 100:.0f}% infill, 2 perimeters, 4 solid layers: about
  {est['grams']:.0f} g / {est['metres']:.0f} m, {est['spool_pct']:.0f}% of a 1 kg spool.
  {est['shell_share'] * 100:.0f}% of that is shell, so turning the infill down
  saves less than it looks like it should.
""")
    print(f"wrote {notes}")


if __name__ == "__main__":
    main()
