"""Test whether the 'shoal' class is actually sub-pixel exposed rock.

Nate cross-checked candidates against what he knows of the lake and reported the
shoals look like rocks. The likely cause is a resolution limit, not a physical
one: at 10 m, a rock smaller than a pixel can never cross a water/land threshold,
because the pixel's reflectance is dominated by the water around it. It can only
ever present as "brighter water" -- the same signature as a shallow bottom. If
that is what is happening, the class boundary is really "bigger than a pixel"
vs "smaller than a pixel", dressed up as exposed vs submerged.

Green and NIR separate the two physically:

  Submerged bottom  Green penetrates a few metres and reflects off the bottom,
                    so green is elevated. Water absorbs NIR almost completely,
                    so NIR stays at the water background.
  Dry rock in-pixel A dry surface reflects BOTH. NIR elevation is the tell,
                    because nothing under water produces it.

So for each candidate: elevation above the local water background in each band.
High green + flat NIR = real shoal. High green + high NIR = rock breaking the
surface inside that pixel.
"""

import json
import sys
from pathlib import Path

import numpy as np
import rasterio
from scipy import ndimage
from shapely.geometry import mapping, shape
from shapely.ops import transform as shp_transform

sys.path.insert(0, str(Path(__file__).resolve().parent))
from detect_rocks import BASELINE_0400_DATE, NEIGHBORHOOD_PX, otsu
from shoalrun_config import MIN_SUN_ELEVATION_DEG, lake_crs, solar_elevation

ROOT = Path(__file__).resolve().parent.parent
LAKE_CRS = lake_crs()

# NIR elevation, in local-background standard deviations, above which we call a
# candidate's pixel partly dry. Water is so absorptive in NIR that any real
# excess is hard to explain any other way.
NIR_DRY_Z = 1.0


def main():
    z = np.load(ROOT / "data" / "stack.npz", allow_pickle=False)
    green, nir, valid = z["green"].copy(), z["nir"].copy(), z["valid"]
    meta = json.loads(str(z["meta"]))
    transform = rasterio.Affine.from_gdal(*z["transform"])
    T, H, W = green.shape

    for t in range(T):
        if meta[t]["date"] >= BASELINE_0400_DATE:
            green[t] -= 1000
            nir[t] -= 1000
    np.clip(green, 1, None, out=green)
    np.clip(nir, 1, None, out=nir)

    from pyproj import Transformer
    from rasterio.features import rasterize

    lake_ll = shape(json.loads((ROOT / "data" / "lake.geojson").read_text())["geometry"])
    tf = Transformer.from_crs("EPSG:4326", LAKE_CRS, always_xy=True)
    lake = shp_transform(lambda x, y: tf.transform(x, y), lake_ll)
    lake_mask = rasterize(
        [(mapping(lake), 1)], out_shape=(H, W), transform=transform, dtype="uint8"
    ).astype(bool)

    c = lake_ll.centroid
    import datetime as dt

    keep = []
    for t in range(T):
        when = dt.datetime.fromisoformat(meta[t]["date"].replace("Z", "+00:00")).replace(tzinfo=None)
        if solar_elevation(when, c.y, c.x) >= MIN_SUN_ELEVATION_DEG and meta[t].get("usable", 1) >= 0.85:
            keep.append(t)
    print(f"using {len(keep)} gated scenes")

    # Accumulate mean z-score per band, over water, per pixel.
    zg = np.zeros((H, W), "float64")
    zn = np.zeros((H, W), "float64")
    cnt = np.zeros((H, W), "float64")

    for t in keep:
        d = green[t] + nir[t]
        ndwi = np.where(d > 0, (green[t] - nir[t]) / np.maximum(d, 1e-6), np.nan)
        th = otsu(ndwi[valid[t] & np.isfinite(ndwi)])
        wet = (ndwi > th) & valid[t] & lake_mask
        if wet.sum() < 1000:
            continue
        m = wet.astype("float32")
        n_local = ndimage.uniform_filter(m, NEIGHBORHOOD_PX, mode="nearest")
        for band, acc in ((green[t], zg), (nir[t], zn)):
            s = ndimage.uniform_filter(np.where(wet, band, 0).astype("float32"),
                                       NEIGHBORHOOD_PX, mode="nearest")
            sq = ndimage.uniform_filter(np.where(wet, band ** 2, 0).astype("float32"),
                                        NEIGHBORHOOD_PX, mode="nearest")
            mean = np.where(n_local > 0.05, s / np.maximum(n_local, 1e-6), np.nan)
            msq = np.where(n_local > 0.05, sq / np.maximum(n_local, 1e-6), np.nan)
            std = np.sqrt(np.maximum(msq - mean ** 2, 1e-6))
            with np.errstate(invalid="ignore"):
                zz = (band - mean) / std
            acc += np.where(wet & np.isfinite(zz), zz, 0)
        cnt += wet

    zg = np.where(cnt > 0, zg / np.maximum(cnt, 1), np.nan)
    zn = np.where(cnt > 0, zn / np.maximum(cnt, 1), np.nan)

    rocks = json.loads((ROOT / "data" / "rocks.geojson").read_text())["features"]
    inv = ~transform

    rows = []
    for f in rocks:
        p = f["properties"]
        x, y = tf.transform(p["lon"], p["lat"])
        col, row = inv * (x, y)
        col, row = int(col), int(row)
        if not (0 <= row < H and 0 <= col < W):
            continue
        sl = (slice(max(0, row - 1), row + 2), slice(max(0, col - 1), col + 2))
        g = np.nanmean(zg[sl])
        n = np.nanmean(zn[sl])
        if np.isfinite(g) and np.isfinite(n):
            rows.append((p["class"], p["area_m2"], g, n))

    print(f"\n{len(rows)} candidates with usable water-background statistics\n")
    print(f"{'class':10s} {'n':>4s} {'green z':>9s} {'NIR z':>9s} {'% NIR-dry':>10s}")
    for cls in ("exposed", "island", "shoal"):
        sub = [r for r in rows if r[0] == cls]
        if not sub:
            continue
        g = np.array([r[2] for r in sub])
        n = np.array([r[3] for r in sub])
        print(f"{cls:10s} {len(sub):4d} {np.median(g):9.2f} {np.median(n):9.2f} "
              f"{(n >= NIR_DRY_Z).mean() * 100:9.0f}%")

    shoals = [r for r in rows if r[0] == "shoal"]
    if shoals:
        n = np.array([r[3] for r in shoals])
        a = np.array([r[1] for r in shoals])
        dry = n >= NIR_DRY_Z
        print(f"\nshoal class breakdown:")
        print(f"  NIR-dry (rock breaking surface in-pixel): {dry.sum():3d} "
              f"({dry.mean()*100:.0f}%)  median footprint {np.median(a[dry]) if dry.any() else 0:.0f} m2")
        print(f"  NIR-flat (genuinely submerged bottom):    {(~dry).sum():3d} "
              f"({(~dry).mean()*100:.0f}%)  median footprint {np.median(a[~dry]) if (~dry).any() else 0:.0f} m2")
        print(f"\n  NIR z percentiles: "
              f"p10={np.percentile(n,10):.2f} p50={np.percentile(n,50):.2f} p90={np.percentile(n,90):.2f}")


if __name__ == "__main__":
    main()
