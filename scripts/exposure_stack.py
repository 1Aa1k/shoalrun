"""Read a rock's depth off the fact that the lake level moves.

The problem this solves. Every optical method tried on this lake so far asks the
water to be transparent, and it is not: satellite-derived bathymetry scored
AUC 0.507 against the soundings, a coin flip, and the control settled it -- NIR,
which water absorbs almost completely and therefore cannot carry depth,
correlated with depth twice as hard as green. There is no bottom signal in the
photons here. Anything resting on "bottom seen through water" is unverifiable,
which is why 3,549 of 4,908 hazards are tiered `unverified`.

The way around it is to stop looking through the water. This lake is regulated
and its surface moves 417 ha between low and high summer water -- 11% of its own
area. So a rock near the top of the water column is *dry land* in a low-water
flight and submerged in a high-water one. Dry land returns near-infrared. That is
a direct look at a surface, not an inference through a column, and it is the one
optical measurement on this lake that survived the depth null intact.

Two things follow, and they are the whole method:

1. ORDER THE FLIGHTS BY STAGE. `detect_naip.py` counts how many flights a pixel
   looked like land in (`naip_flights: 4.455`) and throws away *which*. But the
   flights are six different lake levels, not six replicates. Ordering them turns
   the stack into a staircase: the level at which a pixel stops being dry is the
   elevation of its top. Counting cannot express that, and worse, counting
   penalises exactly the hazard that matters most -- dry in two flights, drowned
   in four reads as "inconsistent" when that inconsistency IS the measurement.

2. REQUIRE MONOTONICITY. A rock cannot be dry at high water and drowned at low
   water. So the sequence of a real rock's wetness, read in stage order, must be
   monotone. Sun glint, a wave crest, a weed bed and a cloud shadow are under no
   such obligation. This is a physical constraint, not a statistical one, and it
   is the first false-positive filter here that the imagery cannot fake -- the
   existing pipeline has no way to express it.

The statistic is a rank correlation between lake stage and NDWI anomaly, per
pixel, across flights. Anomaly, not raw NDWI: each flight has its own sun angle,
atmosphere and radiometric scaling, so every pixel is referenced to water in its
own neighbourhood in its own flight. Without that the correlation would partly
measure the weather on six days, and since stage is seasonal the bias would not
even be random.

What this does NOT do, stated plainly: it finds nothing that never goes dry. A
rock two metres down at the lowest observed water is invisible to it, exactly as
it is invisible to every other passive optical method. That population needs
sonar. What this covers is the band from the low-water line up -- which is where
the rocks that break propellers live.

Usage:
    .venv/bin/python scripts/exposure_stack.py --stage    # pass 1, cheap
    .venv/bin/python scripts/exposure_stack.py            # pass 2, detection
"""

import argparse
import json
import os
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import planetary_computer
import pystac_client
import rasterio
from pyproj import Transformer
from rasterio.features import rasterize, shapes
from rasterio.transform import from_origin
from rasterio.warp import Resampling, reproject
from scipy import ndimage
from shapely.geometry import mapping, shape
from shapely.ops import transform as shp_transform

sys.path.insert(0, str(Path(__file__).resolve().parent))
from shoalrun_config import lake_crs

ROOT = Path(__file__).resolve().parent.parent
LAKE = ROOT / "data" / "lake.geojson"
PRIORITY = ROOT / "data" / "priority.geojson"
STAGE_OUT = ROOT / "data" / "naip_stage.json"
OUT = ROOT / "data" / "exposure.geojson"

STAC = "https://planetarycomputer.microsoft.com/api/stac/v1"
WORKERS = int(os.environ.get("SHOALRUN_WORKERS", "8"))
STAGE_RES = 5.0           # pass 1: coarse enough to be cheap, fine enough for area
PAD = 32                  # tile overlap in px, so a blob on a seam is not halved

NDWI_LAND = 0.0           # NAIP green-vs-NIR; water sits well above zero
SHORE_BUFFER_M = 8.0      # keep the mixed shoreline pixel out of the statistics
MIN_BLOB_M2 = 4.0         # below this, NAIP's JPEG artefacts start to qualify
MAX_BLOB_M2 = 5000.0      # above this it is an island, and Nate does not want islands
MIN_FLIGHTS = 5           # need most of the ladder present to rank anything
RHO_MIN = 0.80            # Spearman(stage, NDWI anomaly); at n=6, rho>=0.886 is p<0.02
DEEP_REF_MIN_PX = 200     # local water reference needs this many always-wet pixels


def naip_items(catalog, lake_ll):
    items = list(catalog.search(collections=["naip"], bbox=lake_ll.bounds).items())
    by_year = defaultdict(list)
    for it in items:
        by_year[it.datetime.year].append(it)
    return by_year


def read_tile(item, transform, th, tw, crs):
    """Green and NIR for one NAIP scene on the tile grid. Network-bound."""
    with rasterio.open(item.assets["image"].href) as src:
        g = np.zeros((th, tw), "float32")
        n = np.zeros((th, tw), "float32")
        reproject(rasterio.band(src, 2), g, dst_transform=transform, dst_crs=crs,
                  resampling=Resampling.average)
        reproject(rasterio.band(src, 4), n, dst_transform=transform, dst_crs=crs,
                  resampling=Resampling.average)
    return g, n


def flight_ndwi(items, transform, th, tw, crs):
    """Mosaic every scene of one flight onto the tile, then NDWI. NaN where unseen."""
    g = np.zeros((th, tw), "float32")
    n = np.zeros((th, tw), "float32")
    got = np.zeros((th, tw), bool)
    with ThreadPoolExecutor(max_workers=min(WORKERS, max(1, len(items)))) as pool:
        for bg, bn in pool.map(lambda it: read_tile(it, transform, th, tw, crs), items):
            have = (bg + bn) > 0
            fill = have & ~got
            g[fill], n[fill] = bg[fill], bn[fill]
            got |= have
    with np.errstate(invalid="ignore", divide="ignore"):
        ndwi = (g - n) / (g + n)
    ndwi[~got | ((g + n) <= 0)] = np.nan
    return ndwi


def _ranks_within_valid(arr, valid):
    """Rank each pixel's series over its valid entries only, 0-based.

    Masked entries are sorted to the end and their ranks are never read. Ranking
    x this way as well as y matters: a cloudy flight leaves gaps in the stage
    ladder, and carrying the full-ladder ranks through would make a perfectly
    ordered pixel score 0.98 instead of 1.0 -- monotone but not linear in rank.
    """
    n_f = arr.shape[0]
    idx = np.argsort(np.where(valid, arr, np.inf), axis=0)
    out = np.empty(arr.shape, "float32")
    seq = np.broadcast_to(np.arange(n_f, dtype="float32")[:, None, None], arr.shape)
    np.put_along_axis(out, idx, seq.copy(), axis=0)
    return out


def spearman_against(order, stack, valid):
    """Per-pixel Spearman between lake stage and each pixel's NDWI series.

    Returns NaN where the answer would be manufactured rather than measured: a
    pixel observed fewer than three times, or one whose values are flat. A flat
    series is the trap -- argsort hands ties consecutive ranks, so open water
    that never changed would come back as a perfect +1 and be reported as a rock.
    """
    xs = np.broadcast_to(np.asarray(order, "float32")[:, None, None], stack.shape)
    xr = np.where(valid, _ranks_within_valid(xs.copy(), valid), 0.0)
    yr = np.where(valid, _ranks_within_valid(stack, valid), 0.0)

    cnt = valid.sum(axis=0).astype("float32")
    xc = np.where(valid, xr - xr.sum(axis=0) / np.maximum(cnt, 1), 0.0)
    yc = np.where(valid, yr - yr.sum(axis=0) / np.maximum(cnt, 1), 0.0)

    num = (xc * yc).sum(axis=0)
    den = np.sqrt((xc ** 2).sum(axis=0) * (yc ** 2).sum(axis=0))

    hi = np.where(valid, stack, -np.inf).max(axis=0)
    lo = np.where(valid, stack, np.inf).min(axis=0)
    flat = ~np.isfinite(hi) | ~np.isfinite(lo) | ((hi - lo) <= 0)

    with np.errstate(invalid="ignore", divide="ignore"):
        rho = num / den
    return np.where((den > 0) & (cnt >= 3) & ~flat, rho, np.nan)


def monotone_break(dry, order):
    """Where in the stage ladder a pixel stops being dry, and whether it is lawful.

    Read low water to high water, a real rock's dry/wet sequence must be
    1...10...0. `k` is how many of the lowest-water flights it stood dry in --
    the rung its top sits on. `clean` is whether the observed sequence is that
    step function exactly, with no dry flight above a wet one.
    """
    seq = dry[np.argsort(order)]
    k = np.argmin(np.concatenate([seq, np.zeros((1,) + seq.shape[1:], bool)]), axis=0)
    n_f = seq.shape[0]
    idx = np.arange(n_f)[:, None, None]
    clean = np.all(seq == (idx < k), axis=0)
    return k.astype("int8"), clean


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", action="store_true",
                    help="pass 1: measure each flight's water area, write naip_stage.json")
    ap.add_argument("--res", type=float, default=None,
                    help="force one resolution instead of the per-tile priority")
    ap.add_argument("--limit", type=int, default=0, help="stop after N tiles (smoke test)")
    args = ap.parse_args()

    lake_ll = shape(json.loads(LAKE.read_text())["geometry"])
    crs = lake_crs()
    fwd = Transformer.from_crs("EPSG:4326", crs, always_xy=True)
    back = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
    lake = shp_transform(lambda x, y: fwd.transform(x, y), lake_ll)
    inner = lake.buffer(-SHORE_BUFFER_M)

    catalog = pystac_client.Client.open(STAC, modifier=planetary_computer.sign_inplace)
    by_year = naip_items(catalog, lake_ll)
    years = sorted(by_year)
    if len(years) < MIN_FLIGHTS:
        raise SystemExit(f"only {len(years)} NAIP flights; need >= {MIN_FLIGHTS}")
    dates = {y: min(it.datetime for it in by_year[y]).date().isoformat() for y in years}
    print(f"{len(years)} NAIP flights: " + ", ".join(f"{y} ({dates[y]})" for y in years))

    if args.stage:
        return measure_stage(by_year, years, dates, lake, inner, crs)

    if not STAGE_OUT.exists():
        raise SystemExit(f"{STAGE_OUT} missing -- run with --stage first")
    stage = json.loads(STAGE_OUT.read_text())
    order = np.array([stage["rank"][str(y)] for y in years], "float32")
    print("stage order (low water first): " +
          " < ".join(f"{y}" for y in sorted(years, key=lambda y: stage["rank"][str(y)])))

    tiles = json.loads(PRIORITY.read_text())["features"]
    tiles.sort(key=lambda f: (f["properties"]["res_m"], -f["properties"]["water_ha"]))
    if args.limit:
        tiles = tiles[:args.limit]
    print(f"{len(tiles)} tiles to scan")

    feats = []
    stats = defaultdict(int)
    for i, tf in enumerate(tiles, 1):
        p = tf["properties"]
        res = args.res or p["res_m"]
        left, bottom, right, top = p["utm"]
        pad = PAD * res
        left, bottom, right, top = left - pad, bottom - pad, right + pad, top + pad
        tw = int(round((right - left) / res))
        th = int(round((top - bottom) / res))
        transform = from_origin(left, top, res, res)

        water_poly = rasterize([(mapping(inner), 1)], out_shape=(th, tw),
                               transform=transform, dtype="uint8").astype(bool)
        if water_poly.sum() < 100:
            continue

        try:
            grids = [flight_ndwi(by_year[y], transform, th, tw, crs) for y in years]
        except Exception as exc:
            stats["tile_read_failed"] += 1
            print(f"  tile {i}: read failed ({str(exc)[:70]})")
            continue
        stack = np.stack(grids)
        valid = np.isfinite(stack) & water_poly
        enough = valid.sum(axis=0) >= MIN_FLIGHTS
        if enough.sum() < 100:
            stats["tile_too_cloudy"] += 1
            continue

        # Per-flight reference: water in this tile's own neighbourhood on this
        # flight's own day. Removes the flight's sun angle and radiometry, which
        # would otherwise be read as stage because stage is seasonal.
        always_wet = np.all(np.where(valid, stack, 1.0) > NDWI_LAND, axis=0) & enough
        if always_wet.sum() < DEEP_REF_MIN_PX:
            stats["tile_no_water_reference"] += 1
            continue
        ref = np.array([np.nanmedian(g[always_wet]) for g in grids], "float32")
        anom = stack - ref[:, None, None]

        rho = spearman_against(order, np.where(valid, anom, np.nan), valid)
        dry = valid & (stack < NDWI_LAND)
        k, clean = monotone_break(dry, order)
        n_dry = dry.sum(axis=0)

        cand = (
            enough
            & np.isfinite(rho) & (rho >= RHO_MIN)
            & clean
            & (n_dry >= 1)
            & (n_dry < valid.sum(axis=0))   # dry in every flight is an island
        )
        stats["cand_px"] += int(cand.sum())
        if not cand.any():
            continue

        # Trim the pad so a blob is not emitted twice from neighbouring tiles.
        core = np.zeros_like(cand)
        core[PAD:th - PAD, PAD:tw - PAD] = True
        lab, n_lab = ndimage.label(cand)
        px_m2 = res * res
        for geom, val in shapes(lab.astype("int32"), mask=cand, transform=transform):
            if val == 0:
                continue
            sel = lab == val
            area = float(sel.sum()) * px_m2
            if area < MIN_BLOB_M2 or area > MAX_BLOB_M2:
                stats["blob_size_rejected"] += 1
                continue
            if not (sel & core).any():
                continue  # belongs to the neighbouring tile's core
            ys, xs = np.nonzero(sel)
            cx = left + (xs.mean() + 0.5) * res
            cy = top - (ys.mean() + 0.5) * res
            lon, lat = back.transform(cx, cy)
            rung = int(np.median(k[sel]))
            feats.append({
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [round(lon, 7), round(lat, 7)]},
                "properties": {
                    "method": "stage_exposure",
                    "area_m2": round(area, 1),
                    "res_m": res,
                    "rho": round(float(np.median(rho[sel])), 3),
                    "rung": rung,
                    "dry_in": [int(years[j]) for j in np.argsort(order)[:rung]],
                    "n_dry": int(np.median(n_dry[sel])),
                    "n_obs": int(np.median(valid.sum(axis=0)[sel])),
                    "tile_res_reason": p["why"],
                    "shore_m": p["shore_m_median"],
                    "reach_m": p["reach_m_median"],
                },
            })
            stats["blobs"] += 1
        if i % 25 == 0 or i == len(tiles):
            print(f"  {i}/{len(tiles)} tiles | {stats['blobs']} candidates")

    OUT.write_text(json.dumps({
        "type": "FeatureCollection",
        "properties": {
            "method": "lake-stage exposure",
            "flights": {str(y): dates[y] for y in years},
            "stage_rank": stage["rank"],
            "rho_min": RHO_MIN,
            "min_flights": MIN_FLIGHTS,
            "note": "rung is how many of the lowest-water flights the feature stood "
                    "dry in; depth is a rank, not a distance, until the stage ladder "
                    "is anchored to elevations",
            "stats": dict(stats),
        },
        "features": feats,
    }))
    print(f"\n{len(feats)} exposure candidates -> {OUT}")
    print("stats:", dict(stats))


def measure_stage(by_year, years, dates, lake, inner, crs):
    """Pass 1: each flight's own water area, which is a monotone proxy for stage.

    Measured from the flight itself rather than interpolated from a satellite
    pass days away -- on a regulated lake the level can move between them.
    """
    minx, miny, maxx, maxy = lake.bounds
    res = STAGE_RES
    W = int(np.ceil((maxx - minx) / res))
    H = int(np.ceil((maxy - miny) / res))
    transform = from_origin(minx, maxy, res, res)
    water_poly = rasterize([(mapping(inner), 1)], out_shape=(H, W),
                           transform=transform, dtype="uint8").astype(bool)
    print(f"stage grid {W} x {H} @ {res} m ({W*H/1e6:.1f} M px), "
          f"{water_poly.sum()*res*res/1e4:.0f} ha inside the shoreline")

    areas, cover = {}, {}
    for y in years:
        ndwi = flight_ndwi(by_year[y], transform, H, W, crs)
        seen = np.isfinite(ndwi) & water_poly
        cover[y] = float(seen.sum() / max(1, water_poly.sum()))
        wet = (ndwi > NDWI_LAND) & seen
        # Scale to full coverage so a partly-clouded flight is not read as low water.
        areas[y] = float(wet.sum()) * res * res / 1e4 / max(cover[y], 1e-6)
        print(f"  {y} ({dates[y]}): {areas[y]:7.1f} ha water, "
              f"{100*cover[y]:.1f}% of the basin observed")

    lo = min(areas.values())
    hi = max(areas.values())
    rank = {str(y): round((areas[y] - lo) / (hi - lo), 4) for y in years}
    STAGE_OUT.write_text(json.dumps({
        "res_m": res,
        "dates": {str(y): dates[y] for y in years},
        "area_ha": {str(y): round(areas[y], 1) for y in years},
        "coverage": {str(y): round(cover[y], 4) for y in years},
        "rank": rank,
        "spread_ha": round(hi - lo, 1),
        "note": "rank 0 = lowest water observed, 1 = highest; area is a monotone "
                "proxy for stage on a fixed basin",
    }, indent=1))
    print(f"\nspread {hi - lo:.1f} ha across the six flights")
    print("low water -> high water: " +
          " < ".join(str(y) for y in sorted(years, key=lambda y: areas[y])))
    print(f"wrote {STAGE_OUT}")


if __name__ == "__main__":
    main()
