"""Find navigation buoys in 0.3 m NAIP, and use them as proxies for invisible rocks.

Measured on this lake: 25 of 32 known rocks are statistically indistinguishable
from open water in aerial imagery. They sit just under the surface -- obvious to
someone in a boat, absent from the pixels. No resolution fixes that, because the
information is not in the image.

But the buoy marking such a rock floats ON the surface. It is small, bright,
isolated, and sitting in the middle of dark water: close to the easiest thing
aerial imagery can detect. So while the rock is invisible, the warning somebody
bolted above it is not. A detected buoy is a strong prior that a hazard exists
within a few metres of it -- placed there by a human who knew.

Signature used:
  - well above local water in BOTH green and NIR (a floating object, not a
    bottom feature seen through the water column -- water kills NIR, so NIR
    excess means something is on or above the surface)
  - small: buoys are ~0.5 m, so 2-30 px at 0.3 m
  - isolated: surrounded by water, not attached to shore or an island
  - away from shore, so moored floats and docks are excluded

This does NOT distinguish a hazard buoy from a mooring ball, a swim float or a
fishing marker. It produces candidates for a human to read, and it is labelled
as such.
"""

import json
import sys
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
OUT = ROOT / "data" / "buoy_candidates.geojson"

RES = 0.3
TILE = 4096
PAD = 64

MIN_PX = 2                # ~0.2 m2
MAX_PX = 40               # ~3.6 m2 -- above this it is a boat, raft or rock
GREEN_SIGMA = 4.0         # bright against local water
NIR_SIGMA = 4.0           # on/above the surface, not seen through water
SHORE_BUFFER_M = 25.0     # keep clear of docks, moored boats and shoreline clutter
LOCAL_WINDOW_M = 60.0


def main():
    lake_ll = shape(json.loads(LAKE.read_text())["geometry"])
    crs = lake_crs()
    fwd = Transformer.from_crs("EPSG:4326", crs, always_xy=True)
    back = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
    lake = shp_transform(lambda x, y: fwd.transform(x, y), lake_ll)
    inner = lake.buffer(-SHORE_BUFFER_M)

    cat = pystac_client.Client.open(
        "https://planetarycomputer.microsoft.com/api/stac/v1",
        modifier=planetary_computer.sign_inplace,
    )
    items = [i for i in cat.search(collections=["naip"], bbox=lake_ll.bounds,
                                   datetime="2023-01-01/2023-12-31").items()]
    print(f"{len(items)} NAIP 2023 scenes @ {RES} m")

    minx, miny, maxx, maxy = lake.bounds
    W = int((maxx - minx) / RES)
    H = int((maxy - miny) / RES)
    print(f"grid {W} x {H} ({W*H/1e6:.0f} M px), tiles of {TILE}")

    feats = []
    n_tiles = ((H + TILE - 1) // TILE) * ((W + TILE - 1) // TILE)
    tno = 0

    for y0 in range(0, H, TILE):
        for x0 in range(0, W, TILE):
            tno += 1
            y1, x1 = min(H, y0 + TILE), min(W, x0 + TILE)
            gy0, gy1 = max(0, y0 - PAD), min(H, y1 + PAD)
            gx0, gx1 = max(0, x0 - PAD), min(W, x1 + PAD)
            th, tw = gy1 - gy0, gx1 - gx0
            transform = from_origin(minx + gx0 * RES, maxy - gy0 * RES, RES, RES)

            water = rasterize([(mapping(inner), 1)], out_shape=(th, tw),
                              transform=transform, dtype="uint8").astype(bool)
            if water.sum() < 5000:
                continue

            g = np.zeros((th, tw), "float32")
            nir = np.zeros((th, tw), "float32")
            got = np.zeros((th, tw), bool)
            for it in items:
                bg = np.zeros((th, tw), "float32")
                bn = np.zeros((th, tw), "float32")
                try:
                    with rasterio.open(it.assets["image"].href) as src:
                        reproject(rasterio.band(src, 2), bg, dst_transform=transform,
                                  dst_crs=crs, resampling=Resampling.average)
                        reproject(rasterio.band(src, 4), bn, dst_transform=transform,
                                  dst_crs=crs, resampling=Resampling.average)
                except Exception:
                    continue
                have = (bg + bn) > 0
                g[have] = bg[have]
                nir[have] = bn[have]
                got |= have

            valid = got & water
            if valid.sum() < 5000:
                continue

            nb = max(11, int(LOCAL_WINDOW_M / RES) | 1)
            wf = valid.astype("float32")
            cnt = ndimage.uniform_filter(wf, nb, mode="nearest")

            def zmap(band):
                s = ndimage.uniform_filter(np.where(valid, band, 0).astype("float32"), nb, mode="nearest")
                sq = ndimage.uniform_filter(np.where(valid, band ** 2, 0).astype("float32"), nb, mode="nearest")
                m = np.where(cnt > 0.05, s / np.maximum(cnt, 1e-6), np.nan)
                q = np.where(cnt > 0.05, sq / np.maximum(cnt, 1e-6), np.nan)
                sd = np.sqrt(np.maximum(q - m ** 2, 1e-6))
                with np.errstate(invalid="ignore"):
                    return (band - m) / sd

            zg = zmap(g)
            zn = zmap(nir)

            # Floating object: bright in green AND returning NIR. Water absorbs
            # NIR, so an NIR excess cannot come from the bottom.
            cand = (zg > GREEN_SIGMA) & (zn > NIR_SIGMA) & valid
            if not cand.any():
                print(f"  tile {tno}/{n_tiles}: 0", flush=True)
                continue

            lbl, n = ndimage.label(cand)
            sizes = ndimage.sum(cand, lbl, range(1, n + 1))
            keep = np.where((sizes >= MIN_PX) & (sizes <= MAX_PX))[0] + 1
            if len(keep) == 0:
                print(f"  tile {tno}/{n_tiles}: 0", flush=True)
                continue

            clean = np.isin(lbl, keep)
            lblc = np.where(clean, lbl, 0).astype("int32")
            found = 0
            for geom, val in shapes(lblc, mask=clean, transform=transform):
                gm = shape(geom)
                c = gm.centroid
                # Only keep the core region so tile overlap does not duplicate.
                col = (c.x - minx) / RES
                row = (maxy - c.y) / RES
                if not (y0 <= row < y1 and x0 <= col < x1):
                    continue
                lon, lat = back.transform(c.x, c.y)
                feats.append({
                    "type": "Feature",
                    "properties": {
                        "class": "buoy_candidate",
                        "area_m2": round(gm.area, 2),
                        "source": "naip-0.3m",
                        "note": "floating object; may be a hazard buoy, mooring or float",
                    },
                    "geometry": {"type": "Point", "coordinates": [round(lon, 6), round(lat, 6)]},
                })
                found += 1
            print(f"  tile {tno}/{n_tiles}: {found}", flush=True)

    print(f"\n{len(feats)} floating-object candidates")
    OUT.write_text(json.dumps({"type": "FeatureCollection", "features": feats}))
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
