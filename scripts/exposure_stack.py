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
STABILITY = ROOT / "data" / "stage_stability.json"
OUT = ROOT / "data" / "exposure.geojson"

STAC = "https://planetarycomputer.microsoft.com/api/stac/v1"
WORKERS = int(os.environ.get("SHOALRUN_WORKERS", "8"))
STAGE_RES = 5.0           # pass 1: coarse enough to be cheap, fine enough for area
STAGE_COLLAR_M = 200.0    # measure past the mapped shoreline so high water can read high
PAD = 32                  # tile overlap in px, so a blob on a seam is not halved

NDWI_LAND = 0.0           # dry/wet split for a single pixel: rock is negative

# Measuring the lake's AREA needs its own threshold, and it cannot be a constant.
# Two attempts failed in opposite directions. At NDWI > 0 the 2018 flight read
# 286 ha above every other, and its extra ring had a median NDWI of +0.065 --
# saturated ground inside the collar, not lake. Raising the bar to 0.40 then put
# 2015 at 2186 ha, 63% of the lake, because it cut into that flight's real water.
#
# The flights are not radiometrically comparable: flight_quality.json has 2015 at
# contrast 3.87 and 2021 at 25.74, and water medians of 90.6 against 47.3. One
# number cannot separate land from water across a 6x spread in contrast, so the
# split is chosen per flight from that flight's own histogram.
#
# It is estimated on a band straddling the mapped shoreline, not on the whole
# window. Otsu assumes two modes of comparable weight; the full window is ~90%
# water, so the between-class variance peaks *inside* the water distribution and
# reports a lake that lost a third of its area. Within STAGE_BAND_M of the shore
# the two classes are balanced and the split is well posed. Clamping the answer
# instead was tried and merely pinned every flight to the clamp.
STAGE_BAND_M = 150.0

# A flight only joins the ladder if its measured area is insensitive to where the
# land/water split is put. stage_stability.py sweeps the threshold from 0.15 to
# 0.45 and watches each flight: five of the six move under 8% of their own area
# across that range, and 2015 moves 49% -- it is the contrast-3.87 flight, and
# its real water sits so low in NDWI that any defensible threshold eats it.
# Three of the four order flips in the sweep involve 2015 and no other pair of
# the remaining five flips except the two at the bottom, which are 44-94 ha apart
# and genuinely tied.
#
# Expressed as a measured rule rather than a hand-picked exclusion, so a rerun on
# another lake drops whatever its own bad flight turns out to be.
STAGE_SENSITIVITY_MAX = 0.10
STAGE_NDWI_SANE = (-0.2, 0.7)   # a split outside this is a broken scene, not a lake
SHORE_BUFFER_M = 8.0      # keep the mixed shoreline pixel out of the statistics
MIN_BLOB_M2 = 4.0         # below this, NAIP's JPEG artefacts start to qualify
MAX_BLOB_M2 = 5000.0      # above this it is an island, and Nate does not want islands
MIN_FLIGHTS = 5           # a ladder with fewer rungs than this cannot be ordered
MIN_OBS = 4               # per pixel. Distinct from MIN_FLIGHTS: with a 5-rung
                          # ladder, demanding all 5 lets one bad scene erase a
                          # pixel entirely, and NAIP quad seams do exactly that
DEEP_REF_MIN_PX = 200     # local water reference needs this many always-wet pixels

# The signature is a STEP, not a ramp. There is no bottom signal in this water,
# so a rock reads as dry land until the moment it drowns and then reads as plain
# water -- it does not fade through intermediate values. Scoring it with a rank
# correlation was wrong and the tests caught it: across six flights the order
# *within* the dry group and *within* the wet group is noise, so a real rock
# scores about 0.54 and a 0.80 gate throws it away.
#
# Three independent things have to hold instead, and each can fail on its own:
DRY_SIGMA = 6.0           # dry flights must sit emphatically below local water
WET_SIGMA = 3.0           # drowned flights must look like ordinary water, not
                          # a permanently dark pixel -- that is weed or shadow
MIN_WATER_SIGMA = 0.004   # floor on the noise estimate, so a freakishly uniform
                          # tile cannot make every pixel look significant


def otsu(values, sane=STAGE_NDWI_SANE):
    """Otsu split between the land and water modes of one flight.

    Feed it a balanced sample -- pixels straddling the shoreline. Given the whole
    lake window it will do the wrong thing, correctly: with 90% of the sample in
    one mode the optimal two-class split falls inside that mode.
    """
    lo, hi = sane
    v = values[np.isfinite(values)]
    if v.size < 1000:
        return float(np.median(v)) if v.size else (lo + hi) / 2
    edges = np.linspace(-1.0, 1.0, 257)
    counts = np.histogram(v, bins=edges)[0].astype("float64")
    total = counts.sum()
    mids = (edges[:-1] + edges[1:]) / 2
    w0 = np.cumsum(counts) / total
    m0 = np.cumsum(counts * mids) / total
    with np.errstate(invalid="ignore", divide="ignore"):
        between = (m0[-1] * w0 - m0) ** 2 / (w0 * (1 - w0))
    between[~np.isfinite(between)] = -1
    thr = float(mids[int(np.argmax(between))])
    return min(max(thr, lo), hi)


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


def score_tile(stack, water, order):
    """Everything the detector decides about one tile, with no network in the way.

    Split out from the tile loop so it can be driven with a synthetic rock and
    checked end to end. The wiring is where this would fail quietly: a reference
    taken over the wrong pixels, or a sign convention flipped between the rho
    test and the dry test, still yields a map -- just not of rocks.

    Returns (candidate mask, rho, rung, reasons) where reasons carries the
    counts that explain a tile producing nothing.
    """
    valid = np.isfinite(stack) & water
    enough = valid.sum(axis=0) >= MIN_OBS
    reasons = {"enough_px": int(enough.sum())}
    if enough.sum() < 100:
        return None, None, None, dict(reasons, skipped="too few flights over water")

    # Reference: pixels this tile saw as water on every flight. Subtracting each
    # flight's own water level removes that day's sun angle and radiometry --
    # without it the correlation partly measures six days of weather, and since
    # stage is seasonal that bias would not even be random.
    always_wet = np.all(np.where(valid, stack, 1.0) > NDWI_LAND, axis=0) & enough
    reasons["water_reference_px"] = int(always_wet.sum())
    if always_wet.sum() < DEEP_REF_MIN_PX:
        return None, None, None, dict(reasons, skipped="no stable water to reference")

    ref = np.array([np.nanmedian(g[always_wet]) for g in stack], "float32")
    anom = np.where(valid, stack - ref[:, None, None], np.nan)
    sigma = max(float(np.nanstd(anom[:, always_wet])), MIN_WATER_SIGMA)
    reasons["water_sigma"] = round(sigma, 5)

    dry = valid & (stack < NDWI_LAND)
    rung, clean = monotone_break(dry, order)
    n_dry = dry.sum(axis=0)
    n_obs = valid.sum(axis=0)

    # Split each pixel's series at its own rung and measure both halves against
    # the water this tile saw. A real rock is far below water while dry and
    # indistinguishable from water once drowned; a weed bed fails the second
    # test, and marginal glint fails the first.
    with np.errstate(invalid="ignore"):
        dry_mean = np.where(n_dry > 0, np.nansum(np.where(dry, anom, 0), axis=0)
                            / np.maximum(n_dry, 1), np.nan)
        wet = valid & ~dry
        n_wet = wet.sum(axis=0)
        wet_mean = np.where(n_wet > 0, np.nansum(np.where(wet, anom, 0), axis=0)
                            / np.maximum(n_wet, 1), np.nan)
    dry_margin = -dry_mean / sigma
    wet_margin = np.abs(wet_mean) / sigma

    cand = (
        enough
        & clean
        & (n_dry >= 1)                   # never dry means never seen directly
        & (n_dry < n_obs)                # dry in every flight is an island
        & np.isfinite(dry_margin) & (dry_margin >= DRY_SIGMA)
        & np.isfinite(wet_margin) & (wet_margin <= WET_SIGMA)
    )
    # Reported, not gated: with six flights the ordering inside each half is
    # noise, so this is a diagnostic rather than a test.
    rho = spearman_against(order, anom, valid)
    reasons.update(monotone_pass=int((clean & (n_dry >= 1) & (n_dry < n_obs)).sum()),
                   dry_pass=int((np.isfinite(dry_margin) & (dry_margin >= DRY_SIGMA)).sum()),
                   wet_pass=int((np.isfinite(wet_margin) & (wet_margin <= WET_SIGMA)).sum()),
                   candidates=int(cand.sum()))
    return cand, {"rho": rho, "dry_margin": dry_margin, "wet_margin": wet_margin}, rung, reasons


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", action="store_true",
                    help="pass 1: measure each flight's water area, write naip_stage.json")
    ap.add_argument("--res", type=float, default=None,
                    help="force one resolution instead of the per-tile priority")
    ap.add_argument("--limit", type=int, default=0, help="stop after N tiles (smoke test)")
    ap.add_argument("--shuffle", type=int, default=0, metavar="SEED",
                    help="negative control: permute the stage order. If this finds "
                         "as much as the real order does, the ordering is doing "
                         "nothing and the result is an artefact of the imagery.")
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
    dropped = [y for y in years if str(y) not in stage["rank"]]
    years = [y for y in years if str(y) in stage["rank"]]
    if dropped:
        print(f"excluded by the stability check: {dropped} "
              f"(see naip_stage.json 'rejected')")
    if len(years) < MIN_FLIGHTS:
        raise SystemExit(f"only {len(years)} flights in the ladder; need >= {MIN_FLIGHTS}")
    order = np.array([stage["rank"][str(y)] for y in years], "float32")
    out_path = OUT
    if args.shuffle:
        order = np.random.default_rng(args.shuffle).permutation(order)
        out_path = OUT.with_name(f"exposure_shuffled_{args.shuffle}.geojson")
        print(f"NEGATIVE CONTROL: stage order permuted with seed {args.shuffle}")
    print("stage order (low water first): " +
          " < ".join(str(years[j]) for j in np.argsort(order)))

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
        cand, score, k, reasons = score_tile(stack, water_poly, order)
        if cand is None:
            stats[reasons["skipped"]] += 1
            continue
        valid = np.isfinite(stack) & water_poly
        n_dry = (valid & (stack < NDWI_LAND)).sum(axis=0)
        stats["cand_px"] += reasons["candidates"]
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
                    "dry_sigma": round(float(np.median(score["dry_margin"][sel])), 2),
                    "wet_sigma": round(float(np.median(score["wet_margin"][sel])), 2),
                    "rho": round(float(np.nanmedian(score["rho"][sel])), 3),
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

    out_path.write_text(json.dumps({
        "type": "FeatureCollection",
        "properties": {
            "method": "lake-stage exposure",
            "shuffled_seed": args.shuffle or None,
            "flights": {str(y): dates[y] for y in years},
            "stage_rank": stage["rank"],
            "dry_sigma_min": DRY_SIGMA,
            "wet_sigma_max": WET_SIGMA,
            "min_flights": MIN_FLIGHTS,
            "note": "rung is how many of the lowest-water flights the feature stood "
                    "dry in; depth is a rank, not a distance, until the stage ladder "
                    "is anchored to elevations",
            "stats": dict(stats),
        },
        "features": feats,
    }))
    print(f"\n{len(feats)} exposure candidates -> {out_path}")
    print("stats:", dict(stats))


def measure_stage(by_year, years, dates, lake, inner, crs):
    """Pass 1: each flight's own water area, which is a monotone proxy for stage.

    Measured from the flight itself rather than interpolated from a satellite
    pass days away -- on a regulated lake the level can move between them.

    Measured over a collar *outside* the mapped shoreline, not inside it. The
    first version counted water within `lake.buffer(-8 m)` and so could never
    report more water than the OSM polygon holds: five flights came back within
    26 ha of that 3369 ha ceiling, which is a saturated ruler, not a flat lake.
    High water has to be able to read as high.

    The collar lets the outlet river and the next pond over into frame, so the
    count is restricted to the connected water body containing the lake's
    deepest-inland pixel.
    """
    res = STAGE_RES
    window_poly = lake.buffer(STAGE_COLLAR_M)
    minx, miny, maxx, maxy = window_poly.bounds
    W = int(np.ceil((maxx - minx) / res))
    H = int(np.ceil((maxy - miny) / res))
    transform = from_origin(minx, maxy, res, res)
    window = rasterize([(mapping(window_poly), 1)], out_shape=(H, W),
                       transform=transform, dtype="uint8").astype(bool)
    mapped = rasterize([(mapping(lake), 1)], out_shape=(H, W),
                       transform=transform, dtype="uint8").astype(bool)
    mapped_ha = float(mapped.sum()) * res * res / 1e4

    # Seed the flood fill at the water furthest from any shore. The polygon
    # centroid would not do -- this lake's centroid lands on an island.
    seed = np.unravel_index(int(np.argmax(ndimage.distance_transform_edt(mapped))),
                            mapped.shape)
    # A band straddling the mapped shoreline, where land and water are present in
    # comparable amounts. The per-flight threshold is estimated here and applied
    # to the whole window.
    shore_band = ((ndimage.distance_transform_edt(mapped) * res <= STAGE_BAND_M)
                  | (ndimage.distance_transform_edt(~mapped) * res <= STAGE_BAND_M)) & window
    print(f"stage grid {W} x {H} @ {res} m ({W*H/1e6:.1f} M px); mapped lake "
          f"{mapped_ha:.0f} ha, measured inside a {STAGE_COLLAR_M:.0f} m collar")
    print(f"threshold band: {shore_band.sum() * res * res / 1e4:.0f} ha straddling "
          f"the shoreline ({100 * (mapped & shore_band).sum() / max(1, shore_band.sum()):.0f}% "
          f"of it inside the mapped lake)")

    areas, cover, thresholds = {}, {}, {}
    for y in years:
        ndwi = flight_ndwi(by_year[y], transform, H, W, crs)
        seen = np.isfinite(ndwi) & window
        cover[y] = float(seen.sum() / max(1, window.sum()))
        thr = otsu(ndwi[seen & shore_band])
        thresholds[y] = round(thr, 4)
        wet = (ndwi > thr) & seen
        lab, _ = ndimage.label(wet)
        home = lab[seed]
        if home == 0:
            raise SystemExit(f"{y}: the lake's deepest point did not classify as water")
        wet = lab == home
        # Scale to full coverage so a partly-clouded flight is not read as low water.
        areas[y] = float(wet.sum()) * res * res / 1e4 / max(cover[y], 1e-6)
        print(f"  {y} ({dates[y]}): {areas[y]:7.1f} ha water "
              f"(NDWI split {thr:+.3f}), "
              f"{100*areas[y]/mapped_ha:.1f}% of the mapped lake")

    # Reject flights whose area is a function of the threshold rather than the
    # lake. Without the sweep on disk everything is kept, and the ladder is only
    # as trustworthy as the run that produced it -- so say which happened.
    usable, rejected = list(years), {}
    if STABILITY.exists():
        sweep = json.loads(STABILITY.read_text())["area_ha"]
        for y in years:
            row = np.array(sweep.get(str(y), []), "float64")
            row = row[np.isfinite(row)]
            if row.size < 3:
                continue
            swing = float((row.max() - row.min()) / max(np.median(row), 1e-9))
            if swing > STAGE_SENSITIVITY_MAX:
                rejected[str(y)] = round(swing, 4)
                usable.remove(y)
        for y, sw in rejected.items():
            print(f"  DROPPED {y}: area moves {100*sw:.0f}% across the threshold "
                  f"sweep -- that is the method moving, not the lake")
    else:
        print(f"  (no {STABILITY.name} on disk -- keeping all flights unvetted)")
    if len(usable) < MIN_FLIGHTS:
        raise SystemExit(f"only {len(usable)} flights survive the stability check; "
                         f"need >= {MIN_FLIGHTS}")

    lo = min(areas[y] for y in usable)
    hi = max(areas[y] for y in usable)
    rank = {str(y): round((areas[y] - lo) / (hi - lo), 4) for y in usable}
    STAGE_OUT.write_text(json.dumps({
        "res_m": res,
        "dates": {str(y): dates[y] for y in usable},
        "area_ha": {str(y): round(areas[y], 1) for y in usable},
        "coverage": {str(y): round(cover[y], 4) for y in usable},
        "rejected": rejected,
        "sensitivity_max": STAGE_SENSITIVITY_MAX,
        "rank": rank,
        "spread_ha": round(hi - lo, 1),
        "mapped_ha": round(mapped_ha, 1),
        "collar_m": STAGE_COLLAR_M,
        "ndwi_split": thresholds,
        "ndwi_sane": list(STAGE_NDWI_SANE),
        "band_m": STAGE_BAND_M,
        "note": "rank 0 = lowest water observed, 1 = highest; area is a monotone "
                "proxy for stage on a fixed basin",
    }, indent=1))
    print(f"\nspread {hi - lo:.1f} ha across {len(usable)} usable flights")
    print("low water -> high water: " +
          " < ".join(str(y) for y in sorted(usable, key=lambda y: areas[y])))
    print(f"wrote {STAGE_OUT}")


if __name__ == "__main__":
    main()
