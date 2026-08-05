"""Compute per-pixel rock evidence and SAVE IT, so thresholds can be tuned offline.

Two problems with the previous detectors, one measured and one structural.

MEASURED. Offshore, where hazards actually matter, the brightness channel sits at
a median 7.6 ft depth against an 8.8 ft shore-matched null -- almost no skill. It
is finding sun glint about as often as rock. Brightness alone cannot separate the
two, because a glint and a wet rock are both simply bright.

Shape can separate them. Glint is a BROAD, SMOOTH swell of light spread over tens
of metres. A rock is a SMALL, HARD-EDGED blob a few metres across. So two shape
channels go in beside brightness:

  dog   Difference of Gaussians at rock scale. Responds to compact bright blobs
        and actively cancels broad smooth gradients -- it subtracts the local
        background swell, which is precisely what glare is.
  grad  Gradient magnitude. A rock has a hard boundary against water; glint
        fades out with no edge to find.

STRUCTURAL. Every threshold experiment so far meant re-downloading 75 megapixels
across six flights over a 46 Mbit link, ~30 minutes per attempt. That budget is
why thresholds were guessed rather than fitted. So this script separates the two
halves of the job: it downloads once and writes the weighted evidence grids to
disk as .npy, and tune_edge.py then thresholds them in seconds, as many times as
wanted, with no network at all.

The grids are weighted by flight quality (see rank_flights.py) -- a glary flight
should not vote as loudly as a clear one.
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
from rasterio.features import rasterize
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
EVID = ROOT / "data" / "evidence"

RES = float(os.environ.get("SHOALRUN_RES", "1.0"))
TILE = int(os.environ.get("SHOALRUN_TILE", "2048"))
WORKERS = int(os.environ.get("SHOALRUN_WORKERS", "8"))
PAD = 64

LOCAL_WINDOW_M = 100.0
SHORE_BUFFER_M = 8.0

# Rock scale. Hand-mapped rocks here have a median detected blob around 7 m2,
# so a few metres across. DoG between these two sigmas is tuned to that size:
# small enough to keep a 3 m rock, large enough that a 30 m glare patch cancels.
DOG_SMALL_M = 1.5
DOG_LARGE_M = 6.0


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


def zscore(band, mask, win):
    mean, sd = local_stats(band, mask, win)
    with np.errstate(invalid="ignore", divide="ignore"):
        return (band - mean) / sd


def main():
    EVID.mkdir(parents=True, exist_ok=True)
    crs = lake_crs()
    fwd = Transformer.from_crs("EPSG:4326", crs, always_xy=True)
    lake_ll = shape(json.loads(LAKE.read_text())["geometry"])
    lake = shp_transform(lambda x, y: fwd.transform(x, y), lake_ll)
    inner = lake.buffer(-SHORE_BUFFER_M)

    quality = json.loads(QUALITY.read_text())
    weights = {int(y): q["weight"] for y, q in quality.items()}
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
    print(f"grid {W} x {H} @ {RES} m ({W*H/1e6:.0f} M px)")

    # Weighted sums of per-flight z-scores. Storing the SUM rather than a
    # thresholded count is the whole point: a count bakes in a threshold that
    # then cannot be changed without re-downloading. A sum stays tunable.
    acc = {k: np.zeros((H, W), "float32") for k in ("bright", "dog", "grad", "nir")}
    total_w = np.zeros((H, W), "float32")

    n_tiles = ((H + TILE - 1) // TILE) * ((W + TILE - 1) // TILE)
    tno = 0
    win = max(11, int(LOCAL_WINDOW_M / RES) | 1)
    s_small, s_large = DOG_SMALL_M / RES, DOG_LARGE_M / RES

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
                    ndwi = np.where(rgb[1] + nir > 0,
                                    (rgb[1] - nir) / (rgb[1] + nir + 1e-6), np.nan)
                wet = valid & (ndwi > 0)
                if wet.sum() < 500:
                    continue

                lum = rgb.mean(axis=0)
                zb = zscore(lum, wet, win)

                # DoG: small-scale detail minus local background swell. Broad
                # glare appears in both blurs and cancels; a metre-scale rock
                # survives in the small blur only.
                dog = (ndimage.gaussian_filter(lum, s_small)
                       - ndimage.gaussian_filter(lum, s_large))
                zd = zscore(dog, wet, win)

                gy_, gx_ = np.gradient(ndimage.gaussian_filter(lum, s_small))
                zg = zscore(np.hypot(gy_, gx_), wet, win)

                zn = zscore(nir, wet, win)

                sy = slice(y0 - gy0, y0 - gy0 + (y1 - y0))
                sx = slice(x0 - gx0, x0 - gx0 + (x1 - x0))
                for key, z in (("bright", zb), ("dog", zd), ("grad", zg), ("nir", zn)):
                    zz = np.nan_to_num(z[sy, sx], nan=0.0, posinf=0.0, neginf=0.0)
                    acc[key][y0:y1, x0:x1] += w * zz
                total_w[y0:y1, x0:x1] += w * valid[sy, sx]

            print(f"  tile {tno}/{n_tiles}", flush=True)

    # Store the weighted MEAN z-score per channel: comparable between pixels
    # regardless of how many flights happened to cover each one.
    for key, arr in acc.items():
        mean_z = np.where(total_w > 0, arr / np.maximum(total_w, 1e-6), 0.0)
        np.save(EVID / f"{key}.npy", mean_z.astype("float32"))
        print(f"  wrote {key}.npy  (mean z {mean_z[total_w>0].mean():+.3f}, "
              f"p99.9 {np.percentile(mean_z[total_w>0], 99.9):+.2f})")
    np.save(EVID / "total_w.npy", total_w)
    (EVID / "meta.json").write_text(json.dumps({
        "res": RES, "minx": minx, "maxy": maxy, "W": W, "H": H,
        "crs": str(crs), "weights": {str(k): v for k, v in weights.items()},
        "total_weight": sum(weights.values()),
    }, indent=1))
    print(f"\nwrote evidence grids to {EVID}\nnow run tune_edge.py -- no network needed")


if __name__ == "__main__":
    main()
