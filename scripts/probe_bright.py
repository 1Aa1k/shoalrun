"""Sanity-probe the brightness rule on chips containing known rocks.

The full run takes ~30 minutes and writes nothing until it finishes, so a bug in
the rule would cost the whole run before showing itself. This applies the exact
same test -- same sigma, same weights, same NIR split -- to small chips centred
on hand-mapped OSM rocks, and reports whether it fires there.

Not a recall measurement. It answers only: is the rule alive, and does it light
up on rock rather than on nothing or on everything?
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
from scipy import ndimage

sys.path.insert(0, str(Path(__file__).resolve().parent))
from detect_bright import BRIGHT_SIGMA, MIN_WEIGHTED_FRAC, NIR_DRY_SIGMA, local_stats
from shoalrun_config import lake_crs

ROOT = Path(__file__).resolve().parent.parent
RES = 1.0
CHIP_M = 300.0
N_PROBE = 5


def main():
    crs = lake_crs()
    fwd = Transformer.from_crs("EPSG:4326", crs, always_xy=True)
    quality = json.loads((ROOT / "data" / "flight_quality.json").read_text())
    weights = {int(y): q["weight"] for y, q in quality.items()}

    refs = json.loads((ROOT / "data" / "reference_rocks.geojson").read_text())["features"]
    refs = [r for r in refs if r["properties"].get("source") == "osm"][:N_PROBE]
    print(f"probing {len(refs)} known rocks, {CHIP_M:g} m chips @ {RES:g} m\n")

    cat = pystac_client.Client.open(
        "https://planetarycomputer.microsoft.com/api/stac/v1",
        modifier=planetary_computer.sign_inplace,
    )
    n = int(CHIP_M / RES)
    win = max(11, int(100.0 / RES) | 1)
    fired = 0

    for r in refs:
        lon, lat = r["geometry"]["coordinates"]
        cx, cy = fwd.transform(lon, lat)
        t = from_origin(cx - CHIP_M / 2, cy + CHIP_M / 2, RES, RES)
        items = list(cat.search(collections=["naip"],
                                bbox=[lon - 0.01, lat - 0.01, lon + 0.01, lat + 0.01]).items())
        by_year = defaultdict(list)
        for i in items:
            by_year[i.datetime.year].append(i)

        bright_w = np.zeros((n, n), "float32")
        total_w = np.zeros((n, n), "float32")
        dry_w = np.zeros((n, n), "float32")

        for year, its in sorted(by_year.items()):
            w = weights.get(year, 0.15)
            rgb = np.zeros((3, n, n), "float32")
            nir = np.zeros((n, n), "float32")
            got = np.zeros((n, n), bool)
            for it in its:
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
            wet = got & (ndwi > 0)
            if wet.sum() < 500:
                continue
            lum = rgb.mean(axis=0)
            lmean, lsd = local_stats(lum, wet, win)
            nmean, nsd = local_stats(nir, wet, win)
            with np.errstate(invalid="ignore"):
                zl = (lum - lmean) / lsd
                zn = (nir - nmean) / nsd
            is_bright = np.isfinite(zl) & (zl > BRIGHT_SIGMA) & got
            bright_w += w * is_bright
            dry_w += w * (is_bright & np.isfinite(zn) & (zn > NIR_DRY_SIGMA))
            total_w += w * got

        frac = np.where(total_w > 0, bright_w / np.maximum(total_w, 1e-6), 0.0)
        hit = frac >= MIN_WEIGHTED_FRAC
        lbl, nb = ndimage.label(hit)

        # Distance from the chip centre (the mapped rock) to the nearest hit.
        if hit.any():
            ys, xs = np.nonzero(hit)
            d = np.hypot(ys - n / 2, xs - n / 2).min() * RES
        else:
            d = float("inf")
        pct = hit.mean() * 100
        ok = d <= 30.0
        fired += ok
        print(f"  {lat:.5f},{lon:.5f}  blobs {nb:4d}  {pct:5.2f}% of chip lit  "
              f"nearest hit {d:6.1f} m  {'HIT' if ok else 'miss'}")

    print(f"\nfired on {fired}/{len(refs)} known rocks within 30 m")
    print("chip-lit % is the guard against the other failure: a rule that")
    print("lights everything scores hits for free and is worthless.")


if __name__ == "__main__":
    main()
