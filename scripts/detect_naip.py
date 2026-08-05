"""Detect rocks directly on NAIP aerial imagery at 1 m, across the whole lake.

This replaces the original architecture, which was backwards. Detection ran on
Sentinel-2 at 10 m and NAIP was used only to *verify* the results -- so anything
smaller than roughly one 10 m pixel was never proposed as a candidate at all, and
NAIP was never given the chance to confirm it. Nate said the map was missing
rocks he knows are there, and that is exactly the failure mode: the bottleneck
was the detector's resolution, not the verifier's.

At 1 m a 3 m boulder is ~7 pixels across instead of a tenth of one. Six NAIP
flights (2011, 2013, 2015, 2018, 2021, 2023) give the same temporal-persistence
discrimination that made the Sentinel approach work -- a wave crest does not
repeat across six flights years apart, a rock does -- but at 100x the areal
resolution.

Memory: a 1 m grid over this lake's bounding box is ~87 M pixels per band, so the
whole stack will not fit in RAM at once. Processed in tiles, accumulating only
the per-pixel persistence counters, which are what the classification needs.
"""

import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from collections import defaultdict
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
OUT = ROOT / "data" / os.environ.get("SHOALRUN_OUT", "rocks_naip.geojson")

STAC = "https://planetarycomputer.microsoft.com/api/stac/v1"

# Resolution and flight window are configurable because the two useful modes have
# very different hardware needs:
#   RES=1.0, all six flights   -- 75 M px, runs on a laptop, best wave rejection
#   RES=0.3, 2018+ flights     -- 832 M px, needs ~25 GB, resolves metre-scale rock
# At 0.3 m only the 2023 flight is native; 2018/2021 are 0.6 m and get upsampled,
# which adds no detail but still contributes independent dates for persistence.
RES = float(os.environ.get("SHOALRUN_RES", "1.0"))
MIN_YEAR = int(os.environ.get("SHOALRUN_MIN_YEAR", "0"))
TILE = int(os.environ.get("SHOALRUN_TILE", "2048"))
WORKERS = int(os.environ.get("SHOALRUN_WORKERS", "8"))
PAD = 64                  # tile overlap so blobs on a seam are not cut in half

NDWI_LAND = 0.0           # NAIP green-vs-NIR; water sits well below zero
MIN_FLIGHTS_LAND = 2      # land in >= this many flights to call it rock
SHOAL_SIGMA = 1.5         # bottom brightness over local water, in sigmas
MIN_FLIGHTS_SHOAL = 3     # bright bottom in >= this many flights
NEIGHBORHOOD_M = 100.0    # local water background window, in metres
SHORE_BUFFER_M = 8.0      # tighter than the Sentinel path -- 1 m pixels mix less
MIN_BLOB_M2 = 4.0         # below this, NAIP compression artefacts creep in
MAX_BLOB_M2 = 5000.0      # above this it is an island, handled as its own class


def main():
    lake_ll = shape(json.loads(LAKE.read_text())["geometry"])
    crs = lake_crs()
    fwd = Transformer.from_crs("EPSG:4326", crs, always_xy=True)
    back = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
    lake = shp_transform(lambda x, y: fwd.transform(x, y), lake_ll)
    inner = lake.buffer(-SHORE_BUFFER_M)

    catalog = pystac_client.Client.open(STAC, modifier=planetary_computer.sign_inplace)
    items = list(catalog.search(collections=["naip"], bbox=lake_ll.bounds).items())
    by_year = defaultdict(list)
    for it in items:
        if it.datetime.year >= MIN_YEAR:
            by_year[it.datetime.year].append(it)
    years = sorted(by_year)
    if not years:
        raise SystemExit(f"no NAIP flights at or after {MIN_YEAR}")
    print(f"{len(years)} NAIP flights: {years}   RES={RES} m  TILE={TILE}  WORKERS={WORKERS}")

    minx, miny, maxx, maxy = lake.bounds
    minx, miny = np.floor(minx / RES) * RES, np.floor(miny / RES) * RES
    maxx, maxy = np.ceil(maxx / RES) * RES, np.ceil(maxy / RES) * RES
    W = int((maxx - minx) / RES)
    H = int((maxy - miny) / RES)
    print(f"grid {W} x {H} @ {RES} m ({W*H/1e6:.0f} M px) -- tiled at {TILE}")

    land_count = np.zeros((H, W), dtype="uint8")
    bright_count = np.zeros((H, W), dtype="uint8")
    obs_count = np.zeros((H, W), dtype="uint8")

    n_tiles = ((H + TILE - 1) // TILE) * ((W + TILE - 1) // TILE)
    tile_no = 0

    for y0 in range(0, H, TILE):
        for x0 in range(0, W, TILE):
            tile_no += 1
            y1 = min(H, y0 + TILE)
            x1 = min(W, x0 + TILE)
            # Expand by PAD for context, then write back only the core.
            gy0, gy1 = max(0, y0 - PAD), min(H, y1 + PAD)
            gx0, gx1 = max(0, x0 - PAD), min(W, x1 + PAD)
            th, tw = gy1 - gy0, gx1 - gx0

            top = maxy - gy0 * RES
            left = minx + gx0 * RES
            transform = from_origin(left, top, RES, RES)

            lake_tile = rasterize([(mapping(inner), 1)], out_shape=(th, tw),
                                  transform=transform, dtype="uint8").astype(bool)
            if lake_tile.sum() < 500:
                continue  # tile barely touches water; nothing to detect

            for year in years:
                g = np.zeros((th, tw), "float32")
                nir = np.zeros((th, tw), "float32")
                got = np.zeros((th, tw), bool)

                def _read(it):
                    """Fetch one scene into the tile grid. Network-bound, so these
                    overlap; each call allocates its own arrays and returns them
                    rather than touching shared state."""
                    try:
                        with rasterio.open(it.assets["image"].href) as src:
                            bg = np.zeros((th, tw), "float32")
                            bn = np.zeros((th, tw), "float32")
                            reproject(rasterio.band(src, 2), bg, dst_transform=transform,
                                      dst_crs=crs, resampling=Resampling.average)
                            reproject(rasterio.band(src, 4), bn, dst_transform=transform,
                                      dst_crs=crs, resampling=Resampling.average)
                        return bg, bn
                    except Exception:
                        return None

                with ThreadPoolExecutor(max_workers=WORKERS) as pool:
                    for res_ in pool.map(_read, by_year[year]):
                        if res_ is None:
                            continue
                        band_g, band_n = res_
                        have = (band_g + band_n) > 0
                        g[have] = band_g[have]
                        nir[have] = band_n[have]
                        got |= have

                valid = got & lake_tile
                if valid.sum() < 500:
                    continue

                with np.errstate(divide="ignore", invalid="ignore"):
                    ndwi = np.where(g + nir > 0, (g - nir) / (g + nir), np.nan)

                land = (ndwi < NDWI_LAND) & valid

                # Bottom brightness against a local water background.
                water = valid & ~land
                wf = water.astype("float32")
                # Window sized in metres so the background statistic means the
                # same thing at 1 m and at 0.3 m.
                nb = max(11, int(NEIGHBORHOOD_M / RES) | 1)
                cnt = ndimage.uniform_filter(wf, nb, mode="nearest")
                s = ndimage.uniform_filter(np.where(water, g, 0).astype("float32"),
                                           nb, mode="nearest")
                sq = ndimage.uniform_filter(np.where(water, g ** 2, 0).astype("float32"),
                                            nb, mode="nearest")
                mean = np.where(cnt > 0.05, s / np.maximum(cnt, 1e-6), np.nan)
                msq = np.where(cnt > 0.05, sq / np.maximum(cnt, 1e-6), np.nan)
                std = np.sqrt(np.maximum(msq - mean ** 2, 1e-6))
                with np.errstate(invalid="ignore"):
                    sig = (g - mean) / std
                bright = (sig > SHOAL_SIGMA) & water

                sy = slice(y0 - gy0, y0 - gy0 + (y1 - y0))
                sx = slice(x0 - gx0, x0 - gx0 + (x1 - x0))
                dy = slice(y0, y1)
                dx = slice(x0, x1)
                land_count[dy, dx] += land[sy, sx].astype("uint8")
                bright_count[dy, dx] += bright[sy, sx].astype("uint8")
                obs_count[dy, dx] += valid[sy, sx].astype("uint8")

            print(f"  tile {tile_no}/{n_tiles} done", flush=True)

    print(f"\nobserved pixels: {(obs_count > 0).sum():,}")
    rock = (land_count >= MIN_FLIGHTS_LAND) & (obs_count >= 3)
    shoal = (bright_count >= MIN_FLIGHTS_SHOAL) & (obs_count >= 3) & ~rock
    print(f"rock px {rock.sum():,}   shoal px {shoal.sum():,}")

    transform = from_origin(minx, maxy, RES, RES)
    feats = []
    for name, mask in (("rock", rock), ("shoal", shoal)):
        lbl, n = ndimage.label(mask)
        if n == 0:
            continue
        sizes = ndimage.sum(mask, lbl, range(1, n + 1))
        keep = np.where(sizes * RES * RES >= MIN_BLOB_M2)[0] + 1
        clean = np.isin(lbl, keep)
        counts_src = land_count if name == "rock" else bright_count
        conf = ndimage.mean(counts_src, lbl, keep)
        conf_by = dict(zip(keep.tolist(), np.atleast_1d(conf).tolist()))

        lbl_clean = np.where(clean, lbl, 0).astype("int32")
        for geom, val in shapes(lbl_clean, mask=clean, transform=transform):
            g = shape(geom)
            area = g.area
            cls = "island" if area >= MAX_BLOB_M2 and name == "rock" else name
            c = g.centroid
            lon, lat = back.transform(c.x, c.y)
            feats.append({
                "type": "Feature",
                "properties": {
                    "class": cls,
                    "area_m2": round(area, 1),
                    "flights": round(float(conf_by.get(int(val), 0)), 2),
                    "lat": round(lat, 6),
                    "lon": round(lon, 6),
                    "source": "naip-1m",
                    "verdict": "naip_multiyear",
                },
                "geometry": mapping(shp_transform(lambda x, y: back.transform(x, y), g)),
            })

    counts = defaultdict(int)
    for f in feats:
        counts[f["properties"]["class"]] += 1
    print(f"\nfeatures: {dict(counts)}  (total {len(feats)})")

    OUT.write_text(json.dumps({"type": "FeatureCollection", "features": feats}))
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()


# --- NOTE ON SIGNATURE, added after visually inspecting Apple Maps imagery ----
#
# Looking at this lake in a commercial satellite basemap, the rocks are obvious:
# small BRIGHT WHITE specks scattered around the islands and shorelines, brighter
# than the surrounding forest. Bare granite has very high albedo, so a rock that
# breaks or nearly breaks the surface is one of the highest-reflectance things in
# the scene -- across ALL bands, not just green.
#
# The detector here keys on green-vs-local-water (bottom brightness) and NIR
# (dry surface). That is the right physics for a submerged shoal seen through the
# water column, and it is what produced 88% recall. But it under-weights the
# easiest signal available: a bare rock is simply BRIGHT. An all-band brightness
# channel would sharpen small-rock detection and, more importantly, tighten
# centroids -- the current 12 m positional scatter is the live complaint.
#
# Not implemented yet; recorded here so the reasoning is not lost. The 0.3 m run
# should be evaluated first, since finer pixels address the same scatter.
