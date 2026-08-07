"""Reconstruct the lake's water level over 40 years, so every aerial flight has a stage.

Why this exists. The detector's discriminator was temporal *persistence*: a rock
repeats across six NAIP flights, a wave crest does not. That treats the flights
as replicates of one scene. They are not. This lake is dam-regulated and its
surface moves 417 ha between low and high water -- 11% of its own area, measured
off the 70-scene Sentinel stack. So the six flights are six different lakes, and
"persistent across flights" quietly penalises the single most dangerous class of
hazard there is: a rock that stands dry at low water and lurks a foot under at
full pool. It shows land in two flights, water in four, and gets scored down for
being inconsistent -- when that inconsistency *is* the depth measurement.

To read exposure as depth you need to know which flight was the low one. Nothing
publishes that for this lake, so it gets measured from orbit: water surface area
per date, across every clear Landsat and Sentinel scene since 1984. Area is a
monotone function of stage on a fixed basin, which is all the ordering needs.

Two proxies, deliberately, because each fails differently:

  area_ha    binary water count. Robust, but 30 m Landsat pixels quantise a 76 km
             shoreline into ~230 ha steps, so it is coarse.
  band_ndwi  mean NDWI inside the fixed drawdown ring. Shoreline pixels get wetter
             continuously as stage rises, so this reads sub-pixel. Sensitive, but
             it drifts with sensor and sun angle.

They are computed independently and cross-checked. If they disagree on the
ordering of two dates, that pair is not ordered -- see `rank_confidence`.

Output: data/lake_stage.json
"""

import json
import os
import sys
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
OUT = ROOT / "data" / "lake_stage.json"

STAC = "https://planetarycomputer.microsoft.com/api/stac/v1"
RES = 30.0        # Landsat native; Sentinel is resampled down to share one grid
PAD_M = 150.0     # margin past the shoreline so the drawdown ring is never clipped

# Sanity bounds on a scene's measured water area, as a fraction of the mapped
# lake. A scene outside them is not a low lake, it is a broken read -- an asset
# with no geotransform reprojects through the identity matrix and lands nowhere,
# which showed up as several scenes reporting exactly 0 ha.
AREA_MIN_FRAC = 0.60
AREA_MAX_FRAC = 1.25
MAX_SCENES = 1500  # hard bound: a bad query must fail fast, not allocate forever
WORKERS = int(os.environ.get("SHOALRUN_WORKERS", "8"))

# Ice breaks every assumption -- a frozen lake is not water and a snowbank is not
# a shoreline. Maine ice-out here is early May, ice-in late November. Stay inside.
MONTHS = (6, 7, 8, 9, 10)
MAX_CLOUD = 35

# NDWI = (green - nir) / (green + nir). Water sits well above zero, land well
# below. Zero is the textbook split and holds on this lake; the drawdown ring is
# defined by pixels that cross it, so the threshold defines the ring, not the
# other way round.
NDWI_WATER = 0.0

COLLECTIONS = {
    "sentinel-2-l2a": {"green": "B03", "nir": "B08", "scale": 1e-4, "off": 0.0},
    "landsat-c2-l2": {"green": "green", "nir": "nir08", "scale": 2.75e-5, "off": -0.2},
}


def build_grid(lake_utm):
    minx, miny, maxx, maxy = lake_utm.bounds
    minx, miny = minx - PAD_M, miny - PAD_M
    maxx, maxy = maxx + PAD_M, maxy + PAD_M
    W = int(np.ceil((maxx - minx) / RES))
    H = int(np.ceil((maxy - miny) / RES))
    return from_origin(minx, maxy + (H * RES - (maxy - miny)), RES, RES), W, H


def read_band(href, asset_scale, asset_off, transform, W, H, crs):
    out = np.full((H, W), np.nan, "float32")
    with rasterio.open(href) as src:
        reproject(rasterio.band(src, 1), out, dst_transform=transform, dst_crs=crs,
                  resampling=Resampling.average, src_nodata=0, dst_nodata=np.nan)
    return out * asset_scale + asset_off


def scene_ndwi(item, cfg, transform, W, H, crs):
    """NDWI grid for one scene, NaN where the sensor saw nothing usable."""
    g = read_band(item.assets[cfg["green"]].href, cfg["scale"], cfg["off"],
                  transform, W, H, crs)
    n = read_band(item.assets[cfg["nir"]].href, cfg["scale"], cfg["off"],
                  transform, W, H, crs)
    bad = ~np.isfinite(g) | ~np.isfinite(n) | ((g + n) <= 0)
    with np.errstate(invalid="ignore", divide="ignore"):
        ndwi = (g - n) / (g + n)
    ndwi[bad] = np.nan
    return ndwi


def main():
    lake_ll = shape(json.loads(LAKE.read_text())["geometry"])
    crs = lake_crs()
    fwd = Transformer.from_crs("EPSG:4326", crs, always_xy=True)
    lake = shp_transform(lambda x, y: fwd.transform(x, y), lake_ll)
    transform, W, H = build_grid(lake)
    print(f"grid {W} x {H} @ {RES} m")

    # The analysis window: the lake plus a collar. Water outside it (the river,
    # the next pond over) must not enter the area count or a different basin's
    # weather reads as this lake's stage.
    window = rasterize([(mapping(lake.buffer(PAD_M * 0.75)), 1)], out_shape=(H, W),
                       transform=transform, dtype="uint8").astype(bool)
    mapped = rasterize([(mapping(lake), 1)], out_shape=(H, W),
                       transform=transform, dtype="uint8").astype(bool)
    mapped_ha = float(mapped.sum()) * RES * RES / 1e4

    # A seed known to be open water in every scene: the water pixel furthest from
    # any shore. Picking the polygon centroid would fail -- this lake's centroid
    # lands on an island.
    depth_px = ndimage.distance_transform_edt(mapped)
    seed_row, seed_col = np.unravel_index(int(np.argmax(depth_px)), depth_px.shape)
    print(f"mapped lake {mapped_ha:.0f} ha; seed at row {seed_row} col {seed_col}, "
          f"{depth_px[seed_row, seed_col] * RES:.0f} m from the nearest shore")

    catalog = pystac_client.Client.open(STAC, modifier=planetary_computer.sign_inplace)
    scenes = []
    for coll, cfg in COLLECTIONS.items():
        found = catalog.search(
            collections=[coll],
            bbox=lake_ll.bounds,
            datetime="1984-01-01/2026-12-31",
            query={"eo:cloud_cover": {"lt": MAX_CLOUD}},
        ).items()
        for it in found:
            if it.datetime.month not in MONTHS:
                continue
            scenes.append((it, cfg, coll))
            if len(scenes) > MAX_SCENES:
                raise SystemExit(f"query returned >{MAX_SCENES} scenes; refusing to allocate")
    scenes.sort(key=lambda s: s[0].datetime)
    print(f"{len(scenes)} candidate scenes "
          f"({sum(1 for s in scenes if s[2] == 'landsat-c2-l2')} landsat, "
          f"{sum(1 for s in scenes if s[2] == 'sentinel-2-l2a')} sentinel)")

    def work(job):
        it, cfg, coll = job
        try:
            ndwi = scene_ndwi(it, cfg, transform, W, H, crs)
        except Exception as exc:  # a single unreadable COG must not sink the run
            return {"id": it.id, "error": str(exc)[:120]}
        seen = np.isfinite(ndwi) & window
        cover = seen.sum() / max(1, window.sum())
        if cover < 0.92:
            return {"id": it.id, "error": f"only {cover:.0%} of the window observed"}
        water = (ndwi > NDWI_WATER) & seen
        # Keep only the basin the lake is in. Maine bog reads above NDWI 0 at 30 m,
        # and the outlet river and the next pond over are not this lake's stage.
        lab, _ = ndimage.label(water)
        home = lab[seed_row, seed_col]
        if home == 0:
            return {"id": it.id, "error": "lake centre did not classify as water"}
        water = lab == home
        area_ha = float(water.sum()) * RES * RES / 1e4
        if not (AREA_MIN_FRAC * mapped_ha <= area_ha <= AREA_MAX_FRAC * mapped_ha):
            return {"id": it.id,
                    "error": f"{area_ha:.0f} ha is outside {AREA_MIN_FRAC:.0%}-"
                             f"{AREA_MAX_FRAC:.0%} of the mapped {mapped_ha:.0f} ha"}
        return {
            "id": it.id,
            "collection": coll,
            "date": it.datetime.date().isoformat(),
            "platform": it.properties.get("platform", ""),
            "cloud": round(float(it.properties.get("eo:cloud_cover", -1)), 1),
            "cover": round(float(cover), 4),
            "area_ha": round(area_ha, 1),
            "_water": water,
            "_ndwi": ndwi,
        }

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        results = list(pool.map(work, scenes))

    ok = [r for r in results if "error" not in r]
    bad = [r for r in results if "error" in r]
    print(f"{len(ok)} scenes usable, {len(bad)} rejected")
    if len(ok) < 10:
        raise SystemExit("too few usable scenes to define a stage series")

    # The drawdown ring: pixels that are water in some scenes and land in others.
    # Everything the sub-pixel proxy measures happens inside it.
    stack = np.stack([r.pop("_water") for r in ok])
    wet = stack.mean(axis=0)
    ring = (wet > 0.02) & (wet < 0.98) & window
    # Drop specks -- isolated flicker is cloud edge, not shoreline.
    lab, _ = ndimage.label(ring)
    sizes = np.bincount(lab.ravel())
    ring &= np.isin(lab, np.flatnonzero(sizes >= 4)) & (lab > 0)
    print(f"drawdown ring: {ring.sum()} px ({ring.sum() * RES * RES / 1e4:.0f} ha)")

    for r in ok:
        nd = r.pop("_ndwi")
        vals = nd[ring]
        vals = vals[np.isfinite(vals)]
        r["band_ndwi"] = round(float(vals.mean()), 4) if vals.size else None

    areas = np.array([r["area_ha"] for r in ok])
    bands = np.array([r["band_ndwi"] if r["band_ndwi"] is not None else np.nan for r in ok])
    good = np.isfinite(bands)
    corr = float(np.corrcoef(areas[good], bands[good])[0, 1]) if good.sum() > 3 else float("nan")
    print(f"area_ha vs band_ndwi correlation: r = {corr:+.3f}  (n={int(good.sum())})")

    # Rank each date on both proxies. Where the two ranks agree the ordering is
    # trustworthy; where they fight, it is not, and the consumer must not pretend.
    ar = areas.argsort().argsort() / max(1, len(areas) - 1)
    br = np.where(good, bands, -np.inf).argsort().argsort() / max(1, len(bands) - 1)
    for i, r in enumerate(ok):
        r["stage_rank_area"] = round(float(ar[i]), 4)
        r["stage_rank_ndwi"] = round(float(br[i]), 4)
        r["stage_rank"] = round(float((ar[i] + br[i]) / 2), 4)
        r["rank_confidence"] = round(1.0 - abs(float(ar[i] - br[i])), 4)

    ok.sort(key=lambda r: r["date"])
    payload = {
        "lake": json.loads(LAKE.read_text()).get("properties", {}).get("name"),
        "res_m": RES,
        "months": list(MONTHS),
        "ndwi_water": NDWI_WATER,
        "ring_px": int(ring.sum()),
        "ring_ha": round(float(ring.sum()) * RES * RES / 1e4, 1),
        "area_vs_ndwi_r": round(corr, 4),
        "n_scenes": len(ok),
        "n_rejected": len(bad),
        "area_ha_min": float(areas.min()),
        "area_ha_max": float(areas.max()),
        "area_ha_median": float(np.median(areas)),
        "scenes": ok,
        "rejected": bad[:50],
    }
    OUT.write_text(json.dumps(payload, indent=1))
    print(f"wrote {OUT}")
    lo = sorted(ok, key=lambda r: r["stage_rank"])[:5]
    hi = sorted(ok, key=lambda r: r["stage_rank"])[-5:]
    print("lowest water:  " + ", ".join(f"{r['date']} ({r['area_ha']:.0f} ha)" for r in lo))
    print("highest water: " + ", ".join(f"{r['date']} ({r['area_ha']:.0f} ha)" for r in hi))


if __name__ == "__main__":
    main()
