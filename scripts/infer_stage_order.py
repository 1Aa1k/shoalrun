"""Let the imagery say which flights were low water, instead of measuring it.

Measuring stage from water area failed, and failed in a way that a threshold
sweep could not catch. Sweeping moves one cutoff across all flights together, so
it detects a badly chosen threshold but is blind to a flight whose *radiometry*
is off -- and 2021 is exactly that flight (contrast 25.74 against 2015's 3.87).
The verdict was blunt: a permuted stage order found 49 blobs where the measured
order found 1. An ordering that loses that badly to a shuffle is not noisy, it
is wrong.

So invert the problem. The physical constraint says a rock cannot be dry at high
water and drowned at low water, which means the years a pixel stood dry must be
the *lowest* years. Read backwards, that constrains the ordering: the true one is
whichever makes the most pixels behave lawfully.

The cheap way to ask. A pixel is lawful under an ordering exactly when its set of
dry years is a prefix of that ordering. So count, once, how many pixels have each
possible dry-set -- there are only 2^n - 2 of them worth anything -- and every
ordering's score becomes a sum of n-1 lookups. No re-running, no sampling.

That also hands over the null for free. A pixel dry in k of n years is a prefix
of exactly k!(n-k)!/n! = 1/C(n,k) of the orderings, so the expected score under a
random ordering is a closed form, not a simulation. A real signal has to beat it
by a margin that 120 tries cannot manufacture.

The stronger evidence is the shape rather than the score. If lake level really
drives this, the common dry-sets must NEST -- {a} inside {a,b} inside {a,b,c} --
because each year of lower water exposes everything the year above it exposed and
more. Sets that overlap without nesting are not a water level; they are six
different days of weather, glint and sun angle.
"""

import argparse
import json
import sys
from collections import defaultdict
from itertools import permutations
from math import comb
from pathlib import Path

import numpy as np
import planetary_computer
import pystac_client
from pyproj import Transformer
from rasterio.features import rasterize
from rasterio.transform import from_origin
from shapely.geometry import mapping, shape
from shapely.ops import transform as shp_transform

sys.path.insert(0, str(Path(__file__).resolve().parent))
from exposure_stack import (DEEP_REF_MIN_PX, DRY_SIGMA, MIN_OBS, MIN_WATER_SIGMA,
                            NDWI_LAND, PAD, SHORE_BUFFER_M, STAGE_OUT, WET_SIGMA,
                            flight_ndwi, naip_items)
from shoalrun_config import lake_crs

ROOT = Path(__file__).resolve().parent.parent
LAKE = ROOT / "data" / "lake.geojson"
PRIORITY = ROOT / "data" / "priority.geojson"
OUT = ROOT / "data" / "stage_order_inferred.json"
STAC = "https://planetarycomputer.microsoft.com/api/stac/v1"


def dry_set_histogram(stack, water, n_flights):
    """How many pixels were dry in each possible subset of the flights.

    Only pixels that pass the same dry/wet sigma gates the detector uses are
    counted, so this measures the population the detector actually works on
    rather than every shoreline pixel on the lake.
    """
    valid = np.isfinite(stack) & water
    enough = valid.sum(axis=0) >= MIN_OBS
    if enough.sum() < 100:
        return None
    always_wet = np.all(np.where(valid, stack, 1.0) > NDWI_LAND, axis=0) & enough
    if always_wet.sum() < DEEP_REF_MIN_PX:
        return None

    ref = np.array([np.nanmedian(g[always_wet]) for g in stack], "float32")
    anom = np.where(valid, stack - ref[:, None, None], np.nan)
    sigma = max(float(np.nanstd(anom[:, always_wet])), MIN_WATER_SIGMA)

    dry = valid & (stack < NDWI_LAND)
    n_dry = dry.sum(axis=0)
    n_obs = valid.sum(axis=0)
    wet = valid & ~dry
    n_wet = wet.sum(axis=0)

    with np.errstate(invalid="ignore"):
        dry_mean = np.where(n_dry > 0, np.nansum(np.where(dry, anom, 0), axis=0)
                            / np.maximum(n_dry, 1), np.nan)
        wet_mean = np.where(n_wet > 0, np.nansum(np.where(wet, anom, 0), axis=0)
                            / np.maximum(n_wet, 1), np.nan)

    keep = (
        enough
        & (n_dry >= 1) & (n_dry < n_obs)
        & np.isfinite(dry_mean) & ((-dry_mean / sigma) >= DRY_SIGMA)
        & np.isfinite(wet_mean) & ((np.abs(wet_mean) / sigma) <= WET_SIGMA)
        & (n_obs == n_flights)   # a partly seen pixel has an ambiguous dry-set
    )
    if not keep.any():
        return np.zeros(1 << n_flights, "int64")

    code = np.zeros(stack.shape[1:], "int64")
    for i in range(n_flights):
        code |= (dry[i].astype("int64") << i)
    return np.bincount(code[keep], minlength=1 << n_flights).astype("int64")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=120,
                    help="tiles to sample; they are the highest-priority ones")
    ap.add_argument("--res", type=float, default=1.0,
                    help="metres; 1.0 samples far more lake per unit of time than 0.3")
    args = ap.parse_args()

    stage = json.loads(STAGE_OUT.read_text())
    lake_ll = shape(json.loads(LAKE.read_text())["geometry"])
    crs = lake_crs()
    fwd = Transformer.from_crs("EPSG:4326", crs, always_xy=True)
    lake = shp_transform(lambda x, y: fwd.transform(x, y), lake_ll)
    inner = lake.buffer(-SHORE_BUFFER_M)

    catalog = pystac_client.Client.open(STAC, modifier=planetary_computer.sign_inplace)
    by_year = naip_items(catalog, lake_ll)
    years = [y for y in sorted(by_year) if str(y) in stage["rank"]]
    n = len(years)
    print(f"{n} flights in the ladder: {years}")
    print(f"measured order (low water first): "
          f"{[years[j] for j in np.argsort([stage['rank'][str(y)] for y in years])]}")

    tiles = json.loads(PRIORITY.read_text())["features"]
    tiles.sort(key=lambda f: -f["properties"]["water_ha"])
    tiles = tiles[:args.limit]
    print(f"sampling {len(tiles)} tiles at {args.res} m\n")

    total = np.zeros(1 << n, "int64")
    scored = 0
    for i, tf in enumerate(tiles, 1):
        p = tf["properties"]
        res = args.res
        left, bottom, right, top = p["utm"]
        pad = PAD * res
        left, bottom, right, top = left - pad, bottom - pad, right + pad, top + pad
        tw = int(round((right - left) / res))
        th = int(round((top - bottom) / res))
        transform = from_origin(left, top, res, res)
        water = rasterize([(mapping(inner), 1)], out_shape=(th, tw),
                          transform=transform, dtype="uint8").astype(bool)
        if water.sum() < 100:
            continue
        try:
            stack = np.stack([flight_ndwi(by_year[y], transform, th, tw, crs)
                              for y in years])
        except Exception as exc:
            print(f"  tile {i}: read failed ({str(exc)[:60]})")
            continue
        h = dry_set_histogram(stack, water, n)
        if h is None:
            continue
        total += h
        scored += 1
        if i % 20 == 0:
            print(f"  {i}/{len(tiles)} tiles | {total.sum():,} qualifying pixels")

    usable = total.copy()
    usable[0] = 0
    usable[(1 << n) - 1] = 0
    if usable.sum() == 0:
        raise SystemExit("no qualifying pixels; nothing to infer an ordering from")

    def name(mask):
        return "{" + ",".join(str(years[i]) for i in range(n) if mask >> i & 1) + "}"

    print(f"\n{scored} tiles scored, {usable.sum():,} qualifying pixels\n")
    print("most common dry-sets (the years a pixel stood dry):")
    for mask in np.argsort(usable)[::-1][:10]:
        if usable[mask] == 0:
            break
        print(f"  {name(int(mask)):28s} {usable[mask]:8,}  "
              f"{100*usable[mask]/usable.sum():5.1f}%")

    # Every ordering's score is a sum over its prefixes. The null is exact.
    expected = sum(usable[m] / comb(n, bin(m).count("1"))
                   for m in range(1 << n) if usable[m])
    scores = {}
    for perm in permutations(range(n)):
        mask, s = 0, 0
        for idx in perm[:-1]:
            mask |= 1 << idx
            s += usable[mask]
        scores[perm] = int(s)

    ranked = sorted(scores.items(), key=lambda kv: -kv[1])
    best, best_score = ranked[0]
    vals = np.array(list(scores.values()), "float64")
    print(f"\nexpected score for a random ordering: {expected:,.0f}")
    print(f"spread over all {len(scores)} orderings: median {np.median(vals):,.0f}, "
          f"90th pct {np.percentile(vals, 90):,.0f}, best {best_score:,}")
    print("\ntop orderings (low water first):")
    for perm, s in ranked[:5]:
        print(f"  {' < '.join(str(years[j]) for j in perm):34s} {s:8,}  "
              f"{s/max(expected,1):5.2f}x expected")

    runner_up = ranked[1][1]
    margin = best_score / max(runner_up, 1)
    lift = best_score / max(expected, 1)

    # Nesting is the real test. A water level produces a chain of sets, each
    # containing the last; weather produces overlapping sets that do not nest.
    top_masks = [int(m) for m in np.argsort(usable)[::-1][:4] if usable[m] > 0]
    nested = sum(1 for a in top_masks for b in top_masks
                 if a != b and (a & b) in (a, b))
    possible = len(top_masks) * (len(top_masks) - 1)
    nest_frac = nested / possible if possible else 0.0
    print(f"\nnesting among the top {len(top_masks)} dry-sets: "
          f"{nested}/{possible} pairs nest ({100*nest_frac:.0f}%)")

    verdict = (
        "SIGNAL" if lift > 2.0 and margin > 1.15 and nest_frac > 0.6 else
        "NO SIGNAL")
    print(f"\nVERDICT: {verdict}")
    if verdict == "SIGNAL":
        print(f"  one ordering wins clearly ({lift:.1f}x the random expectation, "
              f"{margin:.2f}x its runner-up) and the dry-sets nest, which is what a "
              f"lake level looks like and what weather does not.")
    else:
        print(f"  best ordering is {lift:.1f}x expected and only {margin:.2f}x its "
              f"runner-up, with {100*nest_frac:.0f}% nesting. Picking the best of "
              f"{len(scores)} always yields a winner; this one is not separated "
              f"enough to be a lake level rather than the luck of the draw.")

    OUT.write_text(json.dumps({
        "years": years,
        "tiles_scored": scored,
        "res_m": args.res,
        "qualifying_px": int(usable.sum()),
        "dry_set_counts": {name(m): int(usable[m]) for m in range(1 << n) if usable[m]},
        "expected_random": expected,
        "best_order": [years[j] for j in best],
        "best_score": best_score,
        "runner_up_score": runner_up,
        "lift_over_random": lift,
        "margin_over_runner_up": margin,
        "nesting_fraction": nest_frac,
        "verdict": verdict,
    }, indent=1))
    print(f"\nwrote {OUT}")


if __name__ == "__main__":
    main()
