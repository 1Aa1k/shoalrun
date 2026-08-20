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


def raise_houses(model: Model, lonlat, meta: dict, foot_mm: float = 2.0,
                 wall_mm: float = 0.9, ridge_mm: float = 0.7,
                 step: int = 1) -> int:
    """Stand a little gabled house on the surface at each lon/lat.

    A disc says "something is here". A house says what. At 1:56,000 a real 12 m
    camp is 0.2 mm across and would not survive slicing, let alone reading, so
    this is a GLYPH -- deliberately oversized, all one size, all one orientation.
    Nothing about its footprint is a measurement, and the notes say so.

    The shape is a square block with a ridge running along it: full wall height
    at the eaves, wall plus ridge along the centre line, linear between. That
    profile is what makes it read as a building from across a room, where a flat
    pad of the same size reads as a lump.

    Added to the surface rather than cut into it, so it survives any layer
    height, and stamped with `maximum` rather than `+=` so two houses 30 m apart
    on a 25 m lattice do not stack into a tower.
    """
    ny, nx = model.z.shape
    # At least three cells across, or there is no room for eave-ridge-eave and
    # the glyph degenerates into the flat pad this exists to avoid.
    half = max(1, int(round(foot_mm / 2.0 / model.cell_mm)))

    # The glyphs are accumulated on their own layer and added to the ground once
    # at the end, for two reasons. Two camps 30 m apart share cells on a 25 m
    # lattice, and adding in place would stack their roofs into a tower. And the
    # roof rides on each cell's OWN ground rather than on one base height: a
    # flat-based block whose corner hangs over the shoreline cliff becomes a
    # 26 mm pillar, which is what the first two attempts at this did.
    glyph = np.zeros_like(model.z)
    hit = 0
    for lon, lat in lonlat:
        col = ((lon - meta["lon0"]) / meta["dlon"] - model.col0) / step
        row = ((lat - meta["lat0"]) / meta["dlat"] - model.row0) / step
        j, i = int(round(col)), int(round(row))
        if not (0 <= i < ny and 0 <= j < nx):
            continue
        hit += 1
        i0, i1 = max(0, i - half), min(ny, i + half + 1)
        j0, j1 = max(0, j - half), min(nx, j + half + 1)
        yy, xx = np.mgrid[i0:i1, j0:j1]
        # Ridge runs east-west, so the roof falls off with distance in rows.
        across = np.abs(yy - i) / max(half, 1)
        roof = wall_mm + ridge_mm * (1.0 - np.clip(across, 0.0, 1.0))
        inside = (np.abs(xx - j) <= half) & (np.abs(yy - i) <= half)
        patch = glyph[i0:i1, j0:j1]
        np.maximum(patch, np.where(inside, roof, 0.0), out=patch)

    # In place: Model is frozen, but the array it holds is the surface every
    # other marker function also writes through.
    model.z[...] += glyph
    return hit


def raise_nubs(model: Model, lonlat, meta: dict, foot_mm: float = 4.0,
               height_mm: float = 2.0, step: int = 1) -> int:
    """Stand a dome at each lon/lat, sized to be found by a fingertip.

    The gabled house glyph is 2 mm across and 1.6 mm tall, which is legible in
    a render and vanishes on the printed object -- it reads as surface texture
    and a finger runs straight over it. A camp you cannot find is not a marker.

    A dome instead of a block for two reasons. It has no flat overhang, so it
    needs no support at any size; and a finger reads a curve as one thing,
    where a 4 mm cube on a terraced hillside feels like more terrain.

    Same accumulate-then-add discipline as the houses: two camps 30 m apart
    share cells on a 25 m lattice, and adding in place would stack them into a
    tower.
    """
    ny, nx = model.z.shape
    rad = max(1, int(round(foot_mm / 2.0 / model.cell_mm)))
    glyph = np.zeros_like(model.z)
    hit = 0
    for lon, lat in lonlat:
        col = ((lon - meta["lon0"]) / meta["dlon"] - model.col0) / step
        row = ((lat - meta["lat0"]) / meta["dlat"] - model.row0) / step
        j, i = int(round(col)), int(round(row))
        if not (0 <= i < ny and 0 <= j < nx):
            continue
        hit += 1
        i0, i1 = max(0, i - rad), min(ny, i + rad + 1)
        j0, j1 = max(0, j - rad), min(nx, j + rad + 1)
        yy, xx = np.mgrid[i0:i1, j0:j1]
        d = np.hypot(yy - i, xx - j) / rad
        dome = height_mm * np.sqrt(np.clip(1.0 - d * d, 0.0, None))
        patch = glyph[i0:i1, j0:j1]
        np.maximum(patch, dome, out=patch)
    model.z[...] += glyph
    return hit


def raise_pylons(model: Model, lonlat, meta: dict, dia_mm: float = 3.0,
                 height_mm: float = 3.5, taper: float = 0.55,
                 step: int = 1) -> int:
    """Stand a post at each lon/lat: flat top, near-vertical sides.

    A dome is the shape of a drip. It reads as a blob or a printing fault
    precisely because nothing man-made is dome-shaped at this size -- the eye
    files it under mistake. A post does not: a flat top and a hard edge are
    machine marks, so the object says somebody put it there on purpose.

    Built as a frustum rather than a cylinder. A dead-vertical 3 mm column on
    a 0.36 mm lattice is a staircase of eight cells, and the taper both hides
    that and gives each layer a little more to sit on than the one below.

    Height is measured from each post's OWN ground, not from a common plane:
    a shoreline camp and a hillside camp are both a camp, and a fixed top
    height would sink one and float the other.
    """
    ny, nx = model.z.shape
    r_base = max(1, int(round(dia_mm / 2.0 / model.cell_mm)))
    r_top = max(0.0, r_base * float(np.clip(taper, 0.0, 1.0)))
    glyph = np.zeros_like(model.z)
    hit = 0
    for lon, lat in lonlat:
        col = ((lon - meta["lon0"]) / meta["dlon"] - model.col0) / step
        row = ((lat - meta["lat0"]) / meta["dlat"] - model.row0) / step
        j, i = int(round(col)), int(round(row))
        if not (0 <= i < ny and 0 <= j < nx):
            continue
        hit += 1
        i0, i1 = max(0, i - r_base), min(ny, i + r_base + 1)
        j0, j1 = max(0, j - r_base), min(nx, j + r_base + 1)
        yy, xx = np.mgrid[i0:i1, j0:j1]
        d = np.hypot(yy - i, xx - j)
        # Full height across the flat top, then straight down the taper to
        # nothing at the base radius.
        span = max(r_base - r_top, 1e-9)
        post = height_mm * np.clip((r_base - d) / span, 0.0, 1.0)
        patch = glyph[i0:i1, j0:j1]
        np.maximum(patch, post, out=patch)
    model.z[...] += glyph
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


def mark_structures(model: Model, meta: dict, step: int = 1,
                    foot_mm: float = 2.0, nub_mm: float = 0.0,
                    nub_h_mm: float = 2.0, pylon_mm: float = 0.0,
                    pylon_h_mm: float = 3.5,
                    pylon_taper: float = 0.55) -> tuple[int, int]:
    """Every camp as a little house, and every pier as a low pad.

    They are what makes the object findable -- the launch, the camps, the pier
    you actually leave from. `address` is in here with `building` and `camp`
    because a Maine E911 address is a camp somebody lives in whether or not a
    volunteer ever traced its roof; the shoreline is closed spruce canopy and
    the buildings under it are invisible to every aerial source tried.

    Piers stay discs: a pier is a line, and a 2 mm gabled house standing on one
    would claim a building on the water.
    """
    camps = _points(STRUCTURES, {"building", "camp", "address"})
    if pylon_mm > 0:
        houses = raise_pylons(model, camps, meta, pylon_mm, pylon_h_mm,
                              pylon_taper, step)
    elif nub_mm > 0:
        houses = raise_nubs(model, camps, meta, nub_mm, nub_h_mm, step)
    else:
        houses = raise_houses(model, camps, meta, foot_mm, 0.9, 0.7, step)
    piers = raise_markers(model, _points(STRUCTURES, {"pier"}), meta,
                          0.6, 0.7, step)
    return houses, piers


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


def shore_mask(depth_m: np.ndarray, grid_m: float, shore_m: float) -> np.ndarray:
    """Which cells survive when the object is cut to the lake's own outline.

    A rectangular slab spends most of its filament and most of its bed on land
    that is only there because the grid is a rectangle. Cutting to the water
    plus a band of shore gives the object the shape of the thing it is of,
    which is also the shape a person recognises across a room.

    Three passes, and each one is load-bearing:

    * **Dilate** the water by `shore_m` so the shoreline is not the razor edge
      of the print. A wall standing exactly on the waterline is the full
      basin depth of unsupported vertical face and the cliff top comes out
      ragged; a rim of land gives it something to stand on.
    * **Largest component only.** The grid catches ponds and river mouths that
      are not this lake. Left in, they slice as separate little objects that
      print detached, get knocked over, and stick to the nozzle.
    * **Fill holes.** An island in the middle is land, not a void -- without
      this it becomes a hole punched clean through the print.
    """
    from scipy import ndimage

    water = np.isfinite(depth_m)
    if not water.any():
        raise SystemExit("no water in the grid -- nothing to trim to")
    cells = int(round(shore_m / grid_m))
    keep = ndimage.binary_dilation(water, iterations=cells) if cells > 0 else water
    labels, n = ndimage.label(keep)
    if n > 1:
        sizes = ndimage.sum(keep, labels, range(1, n + 1))
        keep = labels == (int(np.argmax(sizes)) + 1)
    return ndimage.binary_fill_holes(keep)


def rim_band(mask: np.ndarray, rim_mm: float, width_mm: float) -> np.ndarray:
    """The ring of cells that becomes a flat lip around the trimmed outline.

    Terrain cut to a shoreline ends wherever the ground happened to be, which
    reads as an object someone tore rather than one someone made. A flat band
    at the water plane gives it a deliberate edge, and gives the print a
    continuous foot to stand on instead of whatever the last hill left.

    The width is solved rather than guessed: the rim widens the array, and the
    horizontal scale is taken from the array's width, so `r` cells of rim on a
    `w` cell lake comes out at `r * width_mm / (w + 2r)` mm of print. Setting
    that equal to the asked-for width and solving for `r` is exact, where
    dilating by an estimate and measuring afterwards is not.
    """
    from scipy import ndimage

    if rim_mm * 2 >= width_mm:
        raise SystemExit(f"--rim-mm {rim_mm:g} leaves no room inside a "
                         f"{width_mm:g} mm print")
    w_in = int(np.flatnonzero(mask.any(axis=0)).size)
    r = int(round(rim_mm * w_in / (width_mm - 2 * rim_mm)))
    if r < 1:
        return np.zeros_like(mask)
    return ndimage.binary_dilation(mask, iterations=r) & ~mask


def crop_to_mask(mask: np.ndarray, *fields: np.ndarray):
    """Cut every field down to the mask's bounding box, mask first.

    The horizontal scale is taken from the array's own width, so this has to
    happen BEFORE the surface is built or `--width-mm` would size the rectangle
    the trimmed object was cut out of rather than the object.
    """
    rows = np.flatnonzero(mask.any(axis=1))
    cols = np.flatnonzero(mask.any(axis=0))
    i0, i1 = int(rows[0]), int(rows[-1]) + 1
    j0, j1 = int(cols[0]), int(cols[-1]) + 1
    out = [mask[i0:i1, j0:j1]]
    out += [None if f is None else f[i0:i1, j0:j1] for f in fields]
    return (*out, i0, j0)


def _rock_noise(i: np.ndarray, j: np.ndarray, k: float, seed: float = 0.0):
    """Deterministic value noise in [-1, 1], two octaves, keyed to the lattice.

    Keyed to the SAMPLE INDEX and nothing else, which is the property the mesh
    depends on: a boundary vertex belongs to two wall edges, and both must
    displace it to the same place or the solid opens along every seam.

    Two octaves because one reads as sandpaper. The coarse term is the lump of
    a clod, the fine term is the grain on it.
    """
    def h(a, b, c):
        v = np.sin(a * 12.9898 + b * 78.233 + c * 37.719 + seed) * 43758.5453
        return np.modf(v)[0] * 2.0 - 1.0
    return 0.68 * h(i * 0.22, j * 0.22, k * 0.9) + 0.32 * h(i * 1.0, j * 1.0, k * 2.3)


def masked_solid_triangles(z: np.ndarray, cell_mm: float, mask: np.ndarray,
                           z_bot: np.ndarray | None = None,
                           wall_levels: int = 1,
                           wall_rock_mm: float = 0.0) -> np.ndarray:
    """Close a height field into a solid over an arbitrary outline.

    `solid_triangles` walks one rectangular perimeter. Here the perimeter is
    whatever shape the mask is -- including its inner boundaries, if it has
    any -- so the walls are built per cell instead: every kept quad that has a
    dropped neighbour raises a wall on that one shared edge.

    That is what makes the result manifold by construction rather than by
    luck. A boundary edge belongs to exactly one kept quad, so exactly one wall
    is ever raised on it, and the wall's top and bottom vertices ARE the
    surface vertices, so there is nothing to weld and nothing to leave open.
    """
    ny, nx = z.shape
    if z_bot is None:
        z_bot = np.zeros_like(z)
    xs = np.arange(nx) * cell_mm
    ys = np.arange(ny) * cell_mm
    X, Y = np.meshgrid(xs, ys)

    # A quad survives only with all four corners in. Keeping half-covered quads
    # would need the outline cut between lattice points, and a stair-stepped
    # edge on a 25 m lattice is under half a millimetre of step at this scale.
    quad = (mask[:-1, :-1] & mask[:-1, 1:] & mask[1:, 1:] & mask[1:, :-1])
    if not quad.any():
        raise SystemExit("trim removed everything -- is --shore-m too small?")
    qi, qj = np.nonzero(quad)

    def corner(di, dj, zz):
        """The (di, dj) corner of every kept quad, as points."""
        i, j = qi + di, qj + dj
        return np.stack([X[i, j], Y[i, j], zz[i, j]], axis=1)

    def quads(a, b, c, d):
        return np.concatenate([np.stack([a, b, c], axis=1),
                               np.stack([a, c, d], axis=1)])

    # Counter-clockwise seen from above: A south-west, B south-east, C
    # north-east, D north-west. Every winding below is stated against this.
    A, B, C, D = (0, 0), (0, 1), (1, 1), (1, 0)
    top = quads(*[corner(*k, z) for k in (A, B, C, D)])
    # Reversed, so it faces down and out.
    bottom = quads(*[corner(*k, z_bot) for k in (A, D, C, B)])

    # Walk the boundary counter-clockwise with the interior on the left and the
    # wall faces outward: south A->B, east B->C, north C->D, west D->A.
    padded = np.zeros((quad.shape[0] + 2, quad.shape[1] + 2), bool)
    padded[1:-1, 1:-1] = quad
    sides = [
        (padded[:-2, 1:-1][quad], A, B),   # neighbour to the south
        (padded[1:-1, 2:][quad], B, C),    # east
        (padded[2:, 1:-1][quad], C, D),    # north
        (padded[1:-1, :-2][quad], D, A),   # west
    ]
    # A smooth vertical face reads as a machined block, which is the wrong
    # story for a piece of ground. Splitting the wall into rings and pushing
    # each ring in or out gives it the broken edge of something lifted out of
    # the earth. The displacement dies to nothing at the top and bottom rings:
    # those two share their vertices with the surface and the base, and moving
    # them would tear the solid away from its own top and floor.
    rock = wall_rock_mm > 0
    levels = max(1, wall_levels) if rock else 1
    cx, cy = float(X[mask].mean()), float(Y[mask].mean())

    def ring(pt_lo, pt_hi, idx_i, idx_j, t):
        """One horizontal ring of wall vertices, t from 0 at the bed to 1 up."""
        out = pt_lo + (pt_hi - pt_lo) * t
        if not rock or t <= 0.0 or t >= 1.0:
            return out
        dx, dy = out[:, 0] - cx, out[:, 1] - cy
        n = np.hypot(dx, dy)
        n[n == 0] = 1.0
        # Sine profile: zero at both ends, fattest in the middle of the wall.
        amp = wall_rock_mm * np.sin(np.pi * t) ** 0.6
        d = amp * _rock_noise(idx_i, idx_j, t)
        out = out.copy()
        out[:, 0] += dx / n * d
        out[:, 1] += dy / n * d
        return out

    walls = []
    for present, p, q in sides:
        edge = ~present
        if not edge.any():
            continue
        sel = np.nonzero(edge)[0]
        pb, qb = corner(*p, z_bot)[sel], corner(*q, z_bot)[sel]
        pt, qt = corner(*p, z)[sel], corner(*q, z)[sel]
        pi_, pj_ = (qi + p[0])[sel], (qj + p[1])[sel]
        qi_, qj_ = (qi + q[0])[sel], (qj + q[1])[sel]
        for k in range(levels):
            t0, t1 = k / levels, (k + 1) / levels
            P0, P1 = ring(pb, pt, pi_, pj_, t0), ring(pb, pt, pi_, pj_, t1)
            Q0, Q1 = ring(qb, qt, qi_, qj_, t0), ring(qb, qt, qi_, qj_, t1)
            walls.append(quads(P0, Q0, Q1, P1))

    return np.concatenate([top, *walls, bottom])


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
    ap.add_argument("--land-fraction", type=float, default=None,
                    help="make the printed hills this fraction of the printed "
                         "basin depth, e.g. 0.333; solves --land-exag for you")
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
    ap.add_argument("--trim", action="store_true",
                    help="cut the outline to the lake's own shape instead of a slab")
    ap.add_argument("--shore-m", type=float, default=150.0,
                    help="metres of land kept around the water when --trim is on")
    ap.add_argument("--rim-mm", type=float, default=0.0,
                    help="flat rim at the waterline around the trimmed outline, in mm")
    ap.add_argument("--wall-rock-mm", type=float, default=0.0,
                    help="break the side walls up by this many mm, torn-earth style")
    ap.add_argument("--wall-levels", type=int, default=8,
                    help="horizontal rings the broken wall is built from")
    ap.add_argument("--camp-nub-mm", type=float, default=0.0,
                    help="draw camps as domes this wide instead of house glyphs")
    ap.add_argument("--camp-nub-h-mm", type=float, default=2.0,
                    help="how tall the camp domes stand")
    ap.add_argument("--camp-pylon-mm", type=float, default=0.0,
                    help="draw camps as posts this wide: flat top, hard edge")
    ap.add_argument("--camp-pylon-h-mm", type=float, default=3.5,
                    help="how tall the camp posts stand above their own ground")
    ap.add_argument("--camp-pylon-taper", type=float, default=0.55,
                    help="flat-top fraction of the post; lower is more tapered")
    ap.add_argument("--step", type=int, default=1,
                    help="sample every Nth grid cell; 2 quarters the file")
    ap.add_argument("--soundings", action="store_true",
                    help="raise a pin at each of the 260 real 1954 measurements")
    ap.add_argument("--structures", action="store_true",
                    help="raise a house at every camp and a pad at every pier")
    ap.add_argument("--house-mm", type=float, default=2.0,
                    help="footprint of the house glyph; it is a symbol, not a scale "
                         "footprint, so this is a legibility choice")
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

    # The trim has to happen here: the horizontal scale is taken from the
    # array's width two lines down, so cutting the outline afterwards would
    # leave --width-mm describing the slab the lake was cut out of.
    mask = rim = None
    if args.trim:
        mask = shore_mask(depth, meta["grid_m"], args.shore_m)
        rim = rim_band(mask, args.rim_mm, args.width_mm) if args.rim_mm > 0 else None
        if rim is not None:
            mask = mask | rim
        mask, depth, land, rim, ti, tj = crop_to_mask(mask, depth, land, rim)
        i0 += ti
        j0 += tj
    # With the land in, one vertical scale has to serve 23 m of lake and 230 m
    # of hillside. Fixing the exaggeration would print a 138 mm spire; fixing the
    # total relief instead and solving for the exaggeration keeps the object a
    # printable size and still reports what it did, which is the part that
    # matters. An explicit --exag always wins.
    scale = args.width_mm / (depth.shape[1] * meta["grid_m"])
    # Only the relief the object KEEPS may set the exaggeration. A trimmed print
    # solved against a hilltop 600 m inland is solving for a peak that gets cut
    # off, and comes out a third of the height that was asked for.
    # The rim is flattened to the water plane later, so the hills that happen
    # to stand in it must not set the exaggeration -- the same mistake as
    # solving against a hilltop the trim cuts off, one step further in.
    seen = np.ones(depth.shape, bool) if mask is None else mask
    if rim is not None:
        seen = seen & ~rim
    # Measured on the samples the SURFACE is built from. build_surface takes
    # every --step'th cell, so a summit that falls between samples is not in
    # the object, and solving a height or a ratio against it aims at ground
    # that never gets printed.
    st = args.step
    seen_s = seen[::st, ::st]
    deep_m = float(np.nan_to_num(depth[::st, ::st], nan=0.0)[seen_s].max())
    land_m = float(land[::st, ::st][seen_s].max()) if land is not None else 0.0
    relief_m = deep_m + land_m
    exag = args.exag
    if exag is None:
        exag = min(EXAG_CAP, max(1.0, args.target_mm / max(relief_m * scale, 1e-9)))

    # Asking for the hills to stand a third of the basin's depth is a
    # statement about the OBJECT, not about the ground: it fixes the printed
    # ratio and lets the exaggeration fall out of it. Solved rather than dialled
    # in by hand, so it survives a change of width, crust, or lake.
    if args.land_fraction is not None and args.land_exag is None:
        if land is None or land_m <= 0:
            raise SystemExit("--land-fraction needs terrain; drop --no-terrain")
        args.land_exag = args.land_fraction * deep_m * exag / land_m

    land_exag = args.land_exag
    if land_exag is None and args.exag is None and land is not None:
        # Auto mode with the two split apart would be guessing at two unknowns
        # from one target height. Auto stays single-scale; splitting them is an
        # explicit act.
        land_exag = exag

    model = build_surface(depth, meta["grid_m"], args.width_mm, exag,
                          args.base_mm, args.step, origin + i0, origin + j0, land,
                          land_exag, args.step_ft, args.land_step_ft)

    # Flattened before the markers go on: a pin is clamped to the water plane,
    # and the rim IS the water plane, so doing this afterwards would shave the
    # pins nearest the shore off at the ankle.
    sub_rim = None if rim is None else rim[::args.step, ::args.step]
    if sub_rim is not None:
        model.z[sub_rim] = args.base_mm + model.max_depth_m * model.mm_per_m

    pins = mark_soundings(model, meta, step=args.step) if args.soundings else 0
    built = piers = 0
    if args.structures:
        built, piers = mark_structures(model, meta, step=args.step,
                                       foot_mm=args.house_mm,
                                       nub_mm=args.camp_nub_mm,
                                       nub_h_mm=args.camp_nub_h_mm,
                                       pylon_mm=args.camp_pylon_mm,
                                       pylon_h_mm=args.camp_pylon_h_mm,
                                       pylon_taper=args.camp_pylon_taper)

    floor = shell_floor(model.z, args.shell_mm) if args.shell_mm > 0 else None
    # The mask is subsampled exactly the way build_surface subsamples the grid,
    # or the outline would drift off the surface it is cutting.
    sub = None if mask is None else mask[::args.step, ::args.step]

    def close_solid(zz, bot):
        if sub is None:
            return solid_triangles(zz, model.cell_mm, bot)
        return masked_solid_triangles(zz, model.cell_mm, sub, bot,
                                      wall_levels=args.wall_levels,
                                      wall_rock_mm=args.wall_rock_mm)

    tris = close_solid(model.z, floor)
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
    # Measured over the cells the object actually KEEPS. Reporting the whole
    # field's maximum after a trim describes a hilltop that was cut off, and
    # the number people check the print against is its height.
    kept = model.z if sub is None else model.z[sub]
    tall_mm = float(kept.max())             # the print's own height, land included
    plane_mm = args.base_mm + depth_mm
    relief_mm = max(0.0, tall_mm - plane_mm)
    land_relief_m = (relief_mm / model.mm_per_m_land
                     if model.mm_per_m_land > 0 else 0.0)
    size = args.out.stat().st_size / 1e6
    print(f"wrote {args.out}  ({size:.1f} MB, {len(tris):,} triangles)")
    print(f"  {nx} x {ny} samples at {model.cell_mm:.3f} mm")
    print(f"  {args.width_mm:.0f} x {ny * model.cell_mm:.0f} x "
          f"{tall_mm:.1f} mm" + ("  (trimmed to the shoreline)" if sub is not None else ""))
    print(f"  {scale_txt}")
    print(f"  deepest point {model.max_depth_m * FT_PER_M:.0f} ft "
          f"-> {depth_mm:.1f} mm below the water plane")
    if use_terrain:
        print(f"  highest ground {land_relief_m:.0f} m "
              f"-> {relief_mm:.1f} mm above it")
    if args.soundings:
        print(f"  {pins} sounding pins")
    if args.structures:
        print(f"  {built} houses raised, {piers} pier markers")
    vol = mesh_volume_mm3(tris)
    print(f"  closed solid: {is_closed(tris)}, volume {vol / 1000:.0f} cm3")
    est = filament_estimate(tris, vol, infill=args.infill)
    sup = {"grams": 0.0, "area_share": 0.0}
    whole = close_solid(model.z, None)
    solid_g = filament_estimate(whole, mesh_volume_mm3(whole),
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
    trim_note = (
        f"\n  The outline is the SHORELINE, not a map sheet: the object is cut to the\n"
        f"  lake's own shape plus {args.shore_m:.0f} m of shore. Anything past that band is not\n"
        f"  missing data, it is off the edge of the object on purpose.\n"
        if sub is not None else ""
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
        f"  {built} houses and {piers} piers are raised on the land. The houses are\n"
        f"  a glyph, not a footprint: every one is the same {args.house_mm:.1f} mm block\n"
        f"  with a ridge, because a real 12 m camp is 0.2 mm at this scale and would\n"
        f"  not survive slicing. Treat them as positions only -- the shape, size and\n"
        f"  orientation carry no information.\n"
        f"    Sources: OSM traced buildings and camps, plus Maine E911 addresses for\n"
        f"  the camps nobody has traced. The shoreline here is closed spruce canopy,\n"
        f"  so lidar and aerial imagery both fail to see roofs under it -- E911 is\n"
        f"  the address an ambulance is sent to, which exists regardless.\n\n"
        if args.structures else ""
    )
    notes = args.out.with_suffix(".txt")
    notes.write_text(
        f"""Millinocket Lake, Maine -- printable bathymetry
{scale_txt}
{args.width_mm:.0f} x {ny * model.cell_mm:.0f} x {tall_mm:.1f} mm

What this is
{what}
{trim_note}
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
