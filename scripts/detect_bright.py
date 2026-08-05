"""Rock detector keyed on what the eye actually uses: bright specks on dark water.

Two changes from the previous detector, both from Nate looking at pictures.

1. FLIGHTS ARE NOT EQUAL. The old detector counted flights, so a glary flight
   voted as loudly as a clear one. Measured lake-wide, the 2021 flight has 2.8x
   the rock-to-water contrast of the next best and ~7x the worst. Evidence is now
   weighted by flight quality (data/flight_quality.json) instead of counted.

2. BRIGHTNESS, NOT JUST COLOUR RATIOS. The old detector used green-vs-local-water
   and NIR. Those are the right physics for a shoal seen through the water column,
   but bare granite is simply one of the brightest things in the scene across ALL
   bands -- which is why the rocks are obvious to a person and were being smeared
   out by ratio arithmetic. Luminance anomaly is now the primary channel.

A pixel counts as rock-like when it is far brighter than the water immediately
around it. NIR then splits the result: dry rock returns NIR, submerged rock does
not, because water absorbs it.
"""

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
QUALITY = ROOT / "data" / "flight_quality.json"
OUT = ROOT / "data" / os.environ.get("SHOALRUN_OUT", "rocks_bright.geojson")

RES = float(os.environ.get("SHOALRUN_RES", "1.0"))
TILE = int(os.environ.get("SHOALRUN_TILE", "2048"))
WORKERS = int(os.environ.get("SHOALRUN_WORKERS", "8"))
PAD = 64

# Luminance sigmas above the local water background to call a pixel rock-like.
BRIGHT_SIGMA = 3.0
# Weighted fraction of available evidence that must agree. Weights come from
# flight quality, so this is "most of the good evidence", not "most flights".
MIN_WEIGHTED_FRAC = 0.45
NIR_DRY_SIGMA = 2.0
LOCAL_WINDOW_M = 100.0
SHORE_BUFFER_M = 8.0
MIN_BLOB_M2 = 4.0
MAX_BLOB_M2 = 5000.0


def local_stats(band, mask, win):
    """Mean and sd of `band` over `mask` pixels, in a local window."""
    m = mask.astype("float32")
    cnt = ndimage.uniform_filter(m, win, mode="nearest")
    s = ndimage.uniform_filter(np.where(mask, band, 0).astype("float32"), win, mode="nearest")
    sq = ndimage.uniform_filter(np.where(mask, band ** 2, 0).astype("float32"), win, mode="nearest")
    mean = np.where(cnt > 0.05, s / np.maximum(cnt, 1e-6), np.nan)
    msq = np.where(cnt > 0.05, sq / np.maximum(cnt, 1e-6), np.nan)
    sd = np.sqrt(np.maximum(msq - mean ** 2, 1e-6))
    return mean, sd


def main():
    crs = lake_crs()
    fwd = Transformer.from_crs("EPSG:4326", crs, always_xy=True)
    back = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
    lake_ll = shape(json.loads(LAKE.read_text())["geometry"])
    lake = shp_transform(lambda x, y: fwd.transform(x, y), lake_ll)
    inner = lake.buffer(-SHORE_BUFFER_M)

    quality = json.loads(QUALITY.read_text())
    weights = {int(y): q["weight"] for y, q in quality.items() if "weight" in q}
    if not weights:
        best = max(q["contrast"] for q in quality.values())
        weights = {int(y): max(0.15, q["contrast"] / best) for y, q in quality.items()}
    print("flight weights: " + ", ".join(f"{y}={w:.2f}" for y, w in sorted(weights.items())))

    cat = pystac_client.Client.open(
        "https://planetarycomputer.microsoft.com/api/stac/v1",
        modifier=planetary_computer.sign_inplace,
    )
    items = list(cat.search(collections=["naip"], bbox=lake_ll.bounds).items())
    by_year = defaultdict(list)
    for i in items:
        by_year[i.datetime.year].append(i)
    years = sorted(by_year)

    minx, miny, maxx, maxy = lake.bounds
    W = int((maxx - minx) / RES)
    H = int((maxy - miny) / RES)
    print(f"grid {W} x {H} @ {RES} m ({W*H/1e6:.0f} M px), tiles {TILE}")

    bright_w = np.zeros((H, W), "float32")   # weighted evidence for "bright"
    total_w = np.zeros((H, W), "float32")    # weighted evidence available
    dry_w = np.zeros((H, W), "float32")      # weighted evidence for "dry"

    n_tiles = ((H + TILE - 1) // TILE) * ((W + TILE - 1) // TILE)
    tno = 0
    win = max(11, int(LOCAL_WINDOW_M / RES) | 1)

    for y0 in range(0, H, TILE):
        for x0 in range(0, W, TILE):
            tno += 1
            y1, x1 = min(H, y0 + TILE), min(W, x0 + TILE)
            gy0, gy1 = max(0, y0 - PAD), min(H, y1 + PAD)
            gx0, gx1 = max(0, x0 - PAD), min(W, x1 + PAD)
            th, tw = gy1 - gy0, gx1 - gx0
            transform = from_origin(minx + gx0 * RES, maxy - gy0 * RES, RES, RES)

            water_poly = rasterize([(mapping(inner), 1)], out_shape=(th, tw),
                                   transform=transform, dtype="uint8").astype(bool)
            if water_poly.sum() < 500:
                continue

            for year in years:
                w = weights.get(year, 0.15)
                rgb = np.zeros((3, th, tw), "float32")
                nir = np.zeros((th, tw), "float32")
                got = np.zeros((th, tw), bool)

                def _read(it):
                    b = np.zeros((4, th, tw), "float32")
                    try:
                        with rasterio.open(it.assets["image"].href) as src:
                            for bi in range(4):
                                reproject(rasterio.band(src, bi + 1), b[bi],
                                          dst_transform=transform, dst_crs=crs,
                                          resampling=Resampling.average)
                        return b
                    except Exception:
                        return None

                with ThreadPoolExecutor(max_workers=WORKERS) as pool:
                    for b in pool.map(_read, by_year[year]):
                        if b is None:
                            continue
                        have = b.sum(axis=0) > 0
                        for bi in range(3):
                            rgb[bi][have] = b[bi][have]
                        nir[have] = b[3][have]
                        got |= have

                valid = got & water_poly
                if valid.sum() < 500:
                    continue

                with np.errstate(invalid="ignore", divide="ignore"):
                    ndwi = np.where(rgb[1] + nir > 0, (rgb[1] - nir) / (rgb[1] + nir + 1e-6), np.nan)
                wet = valid & (ndwi > 0)
                if wet.sum() < 500:
                    continue

                lum = rgb.mean(axis=0)
                lmean, lsd = local_stats(lum, wet, win)
                with np.errstate(invalid="ignore"):
                    zl = (lum - lmean) / lsd
                nmean, nsd = local_stats(nir, wet, win)
                with np.errstate(invalid="ignore"):
                    zn = (nir - nmean) / nsd

                is_bright = np.isfinite(zl) & (zl > BRIGHT_SIGMA) & valid
                is_dry = is_bright & np.isfinite(zn) & (zn > NIR_DRY_SIGMA)

                sy = slice(y0 - gy0, y0 - gy0 + (y1 - y0))
                sx = slice(x0 - gx0, x0 - gx0 + (x1 - x0))
                bright_w[y0:y1, x0:x1] += w * is_bright[sy, sx]
                dry_w[y0:y1, x0:x1] += w * is_dry[sy, sx]
                total_w[y0:y1, x0:x1] += w * valid[sy, sx]

            print(f"  tile {tno}/{n_tiles}", flush=True)

    frac = np.where(total_w > 0, bright_w / np.maximum(total_w, 1e-6), 0.0)
    enough = total_w >= 0.6 * sum(weights.values())
    hit = (frac >= MIN_WEIGHTED_FRAC) & enough
    dry_frac = np.where(bright_w > 0, dry_w / np.maximum(bright_w, 1e-6), 0.0)
    print(f"\nrock-like pixels: {hit.sum():,}")

    transform = from_origin(minx, maxy, RES, RES)
    feats = []
    lbl, n = ndimage.label(hit)
    if n:
        sizes = ndimage.sum(hit, lbl, range(1, n + 1)) * RES * RES
        keep = np.where(sizes >= MIN_BLOB_M2)[0] + 1
        clean = np.isin(lbl, keep)
        conf = ndimage.mean(frac, lbl, keep)
        dryv = ndimage.mean(dry_frac, lbl, keep)
        cmap = dict(zip(keep.tolist(), np.atleast_1d(conf).tolist()))
        dmap = dict(zip(keep.tolist(), np.atleast_1d(dryv).tolist()))
        lblc = np.where(clean, lbl, 0).astype("int32")
        for geom, val in shapes(lblc, mask=clean, transform=transform):
            g = shape(geom)
            area = g.area
            v = int(val)
            dry = dmap.get(v, 0.0)
            cls = "island" if area >= MAX_BLOB_M2 else ("rock" if dry >= 0.5 else "shoal")
            c = g.centroid
            lon, lat = back.transform(c.x, c.y)
            feats.append({
                "type": "Feature",
                "properties": {
                    "class": cls,
                    "area_m2": round(area, 1),
                    "confidence": round(float(cmap.get(v, 0)), 3),
                    "dry_frac": round(float(dry), 3),
                    "lat": round(lat, 6), "lon": round(lon, 6),
                    "source": f"naip-bright-{RES:g}m",
                    "verdict": "naip_weighted",
                },
                "geometry": mapping(shp_transform(lambda x, y: back.transform(x, y), g)),
            })

    counts = defaultdict(int)
    for f in feats:
        counts[f["properties"]["class"]] += 1
    print(f"features: {dict(counts)}  (total {len(feats)})")
    OUT.write_text(json.dumps({"type": "FeatureCollection", "features": feats}))
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
