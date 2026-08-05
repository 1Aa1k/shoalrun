"""Rank the NAIP flights by how well rocks stand out from water.

The detector had been treating all six flights as equal evidence. They are not.
Measured on one patch, the 2021 flight has ~15x the rock-to-water contrast of the
2023 flight, because 2023 was shot on 1 September when low sun turns the lake
into glare. Averaging a clear flight together with a glary one smears out exactly
the signal we want.

Score, per flight, sampled across the whole lake rather than one lucky spot:

    contrast = (99.9th percentile of water luminance - median water luminance)
               / (interquartile spread of water luminance)

In words: how far the brightest specks sit above ordinary water, measured in
units of ordinary water variation. High = rocks pop out. Low = glare has
flattened everything to the same grey.

The denominator matters. A hazy flight can have bright pixels everywhere; what
distinguishes a rock is standing out from its surroundings, not raw brightness.
"""

import json
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np
import planetary_computer
import pystac_client
import rasterio
from pyproj import Transformer
from rasterio.transform import from_origin
from rasterio.warp import Resampling, reproject
from shapely.geometry import Point, shape
from shapely.ops import transform as shp_transform

sys.path.insert(0, str(Path(__file__).resolve().parent))
from shoalrun_config import lake_crs

ROOT = Path(__file__).resolve().parent.parent
LAKE = ROOT / "data" / "lake.geojson"
OUT = ROOT / "data" / "flight_quality.json"

RES = 1.0
CHIP_M = 400.0
N_CHIPS = 12          # sampled across the lake, so one glary bay cannot decide it
SEED = 7


def main():
    crs = lake_crs()
    fwd = Transformer.from_crs("EPSG:4326", crs, always_xy=True)
    lake_ll = shape(json.loads(LAKE.read_text())["geometry"])
    lake = shp_transform(lambda x, y: fwd.transform(x, y), lake_ll)

    # Sample chip centres well inside the lake so shoreline does not dominate.
    inner = lake.buffer(-120)
    rng = np.random.default_rng(SEED)
    minx, miny, maxx, maxy = inner.bounds
    centres = []
    while len(centres) < N_CHIPS:
        p = Point(rng.uniform(minx, maxx), rng.uniform(miny, maxy))
        if inner.contains(p):
            centres.append((p.x, p.y))
    print(f"sampling {len(centres)} chips of {CHIP_M:g} m across the lake")

    cat = pystac_client.Client.open(
        "https://planetarycomputer.microsoft.com/api/stac/v1",
        modifier=planetary_computer.sign_inplace,
    )
    items = list(cat.search(collections=["naip"], bbox=lake_ll.bounds).items())
    by_year = defaultdict(list)
    for i in items:
        by_year[i.datetime.year].append(i)
    years = sorted(by_year)

    n = int(CHIP_M / RES)
    results = {}

    for y in years:
        scores, meds = [], []
        date = str(by_year[y][0].datetime.date())
        for (cx, cy) in centres:
            t = from_origin(cx - CHIP_M / 2, cy + CHIP_M / 2, RES, RES)
            rgb = np.zeros((3, n, n), "float32")
            nir = np.zeros((n, n), "float32")
            got = np.zeros((n, n), bool)
            for it in by_year[y]:
                b = np.zeros((4, n, n), "float32")
                try:
                    with rasterio.open(it.assets["image"].href) as src:
                        for bi in range(4):
                            reproject(rasterio.band(src, bi + 1), b[bi], dst_transform=t,
                                      dst_crs=crs, resampling=Resampling.average)
                except Exception:
                    continue
                have = b.sum(axis=0) > 0
                for bi in range(3):
                    rgb[bi][have] = b[bi][have]
                nir[have] = b[3][have]
                got |= have
            if got.mean() < 0.5:
                continue

            with np.errstate(invalid="ignore", divide="ignore"):
                ndwi = np.where(rgb[1] + nir > 0, (rgb[1] - nir) / (rgb[1] + nir + 1e-6), np.nan)
            water = (ndwi > 0) & got
            if water.sum() < 5000:
                continue
            lum = rgb.mean(axis=0)
            wl = lum[water]
            med = float(np.median(wl))
            iqr = float(np.percentile(wl, 75) - np.percentile(wl, 25))
            top = float(np.percentile(wl, 99.9))
            scores.append((top - med) / (iqr + 1e-6))
            meds.append(med)

        if not scores:
            continue
        results[y] = {
            "date": date,
            "contrast": round(float(np.median(scores)), 2),
            "contrast_mean": round(float(np.mean(scores)), 2),
            "water_median": round(float(np.median(meds)), 1),
            "chips": len(scores),
        }
        print(f"  {y} ({date}): contrast {results[y]['contrast']:7.2f}  "
              f"(n={len(scores)} chips)")

    ranked = sorted(results.items(), key=lambda kv: -kv[1]["contrast"])
    best = ranked[0][1]["contrast"]

    print(f"\n{'rank':<5}{'year':<7}{'date':<13}{'contrast':>9}  {'weight':>7}  verdict")
    for i, (y, r) in enumerate(ranked, 1):
        # Weight relative to the best flight, floored so a poor flight still
        # contributes a little rather than vanishing -- an extra date has value
        # even when it is hazy, it just should not outvote a clear one.
        w = round(max(0.15, r["contrast"] / best), 3)
        r["weight"] = w
        verdict = "CLEAR" if w >= 0.5 else ("usable" if w >= 0.25 else "GLARY - low weight")
        print(f"{i:<5}{y:<7}{r['date']:<13}{r['contrast']:9.2f}  {w:7.3f}  {verdict}")

    OUT.write_text(json.dumps(results, indent=1, sort_keys=True))
    print(f"\nwrote {OUT}")


if __name__ == "__main__":
    main()
