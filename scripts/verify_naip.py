"""Verify every candidate against 0.3 m NAIP aerial imagery, with no human in the loop.

The whole classification problem in this project comes from Sentinel-2's 10 m
pixel: a rock smaller than a pixel cannot be resolved as land, so it can only
ever look like bright water. NAIP settles it. At 0.3 m one Sentinel pixel covers
roughly 1,100 NAIP pixels, so a boulder that was sub-pixel in the source data is
hundreds of pixels here -- and NAIP carries a NIR band, so the same physics that
separated rock from shoal at 10 m works far better at 0.3 m.

This is an independent instrument, a different sensor, a different platform and a
different year from the detections it is checking. It is not the detector
grading its own homework.

Verdicts per candidate:
  rock_confirmed   Resolved dry land at 0.3 m inside the candidate footprint.
  shoal_confirmed  No dry land, but the bottom is measurably brighter than the
                   surrounding water -- a real submerged shallow.
  open_water       Neither. Indistinguishable from the water around it, so the
                   original detection is not supported.

Caveats kept honest: NAIP here was flown 2023-09-01, one date, at a lake level
that is not today's, and September sun is lower than ideal. A single date cannot
disprove a hazard that was under a wave that morning -- so `open_water` demotes
confidence, it does not delete the candidate.
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
from rasterio.windows import from_bounds
from shapely.geometry import shape

sys.path.insert(0, str(Path(__file__).resolve().parent))
from shoalrun_config import lake_crs

ROOT = Path(__file__).resolve().parent.parent
ROCKS = ROOT / "data" / "rocks.geojson"
LAKE = ROOT / "data" / "lake.geojson"
OUT = ROOT / "data" / "verified.geojson"

STAC = "https://planetarycomputer.microsoft.com/api/stac/v1"

# Half-width of the chip cut around each candidate. Wide enough to contain local
# water for a background estimate, tight enough that the background is genuinely
# local and not another feature 200 m away.
CHIP_HALF_M = 40.0

# NAIP NDWI split. Unlike the Sentinel path this is not Otsu'd per scene: a chip
# may legitimately contain no land at all, and Otsu on a unimodal histogram
# invents a boundary. A fixed threshold with a physical basis is safer here.
NDWI_LAND = 0.0

# Fraction of the inner core that must read as land before we call it dry rock.
# 3% of a 15 m core at 0.3 m is still ~70 pixels, well above noise.
LAND_FRAC_MIN = 0.03
CORE_RADIUS_M = 7.5

# Bottom brightness needed to confirm a submerged shoal, in local water sigmas.
SHOAL_SIGMA = 1.5


def main():
    rocks = json.loads(ROCKS.read_text())["features"]
    lake_ll = shape(json.loads(LAKE.read_text())["geometry"])
    crs = lake_crs()
    fwd = Transformer.from_crs("EPSG:4326", crs, always_xy=True)

    catalog = pystac_client.Client.open(STAC, modifier=planetary_computer.sign_inplace)
    items = list(
        catalog.search(collections=["naip"], bbox=lake_ll.bounds, datetime="2018-01-01/2026-12-31").items()
    )
    if not items:
        raise SystemExit("no NAIP coverage for this lake")

    # Newest first, so each candidate is checked against the most recent flight
    # that actually covers it.
    items.sort(key=lambda i: i.datetime, reverse=True)
    year = items[0].datetime.date()
    print(f"{len(items)} NAIP scenes, newest {year}, gsd {items[0].properties.get('gsd')} m")

    # Bucket candidates by the scene that contains them, so each COG opens once.
    assigned = defaultdict(list)
    unassigned = 0
    for idx, f in enumerate(rocks):
        p = f["properties"]
        for it in items:
            w, s, e, n = it.bbox
            if w <= p["lon"] <= e and s <= p["lat"] <= n:
                assigned[it.id].append(idx)
                break
        else:
            unassigned += 1
    print(f"{sum(len(v) for v in assigned.values())} candidates mapped to "
          f"{len(assigned)} scenes ({unassigned} outside NAIP coverage)")

    by_id = {i.id: i for i in items}
    results = {}

    for n_scene, (scene_id, idxs) in enumerate(assigned.items(), 1):
        item = by_id[scene_id]
        href = item.assets["image"].href
        try:
            src = rasterio.open(href)
        except Exception as exc:
            print(f"  [{n_scene}/{len(assigned)}] {scene_id} open failed: {exc}")
            continue

        with src:
            to_img = Transformer.from_crs("EPSG:4326", src.crs, always_xy=True)
            for idx in idxs:
                p = rocks[idx]["properties"]
                cx, cy = to_img.transform(p["lon"], p["lat"])
                try:
                    win = from_bounds(
                        cx - CHIP_HALF_M, cy - CHIP_HALF_M,
                        cx + CHIP_HALF_M, cy + CHIP_HALF_M,
                        src.transform,
                    )
                    chip = src.read((1, 2, 3, 4), window=win, boundless=True, fill_value=0).astype("float32")
                except Exception:
                    continue
                if chip.size == 0 or chip.shape[1] < 20 or chip.shape[2] < 20:
                    continue

                g = chip[1]
                nir = chip[3]
                valid = (chip.sum(axis=0) > 0)
                if valid.mean() < 0.5:
                    continue

                with np.errstate(divide="ignore", invalid="ignore"):
                    ndwi = np.where(g + nir > 0, (g - nir) / (g + nir), np.nan)

                h, w = ndwi.shape
                yy, xx = np.mgrid[0:h, 0:w]
                res = 2 * CHIP_HALF_M / max(h, w)
                dist = np.hypot((yy - h / 2) * res, (xx - w / 2) * res)
                core = (dist <= CORE_RADIUS_M) & valid
                ring = (dist > CORE_RADIUS_M * 2) & valid  # local water background
                if core.sum() < 50 or ring.sum() < 200:
                    continue

                land = (ndwi < NDWI_LAND) & valid
                land_frac = float(land[core].mean())

                # Bottom brightness: green in the core against the water ring,
                # using only ring pixels that are actually water.
                ring_water = ring & ~land
                if ring_water.sum() < 100:
                    continue
                bg_mean = float(np.nanmean(g[ring_water]))
                bg_std = float(np.nanstd(g[ring_water])) or 1.0
                core_water = core & ~land
                core_g = float(np.nanmean(g[core_water])) if core_water.sum() > 20 else np.nan
                sigma = (core_g - bg_mean) / bg_std if np.isfinite(core_g) else np.nan

                if land_frac >= LAND_FRAC_MIN:
                    verdict = "rock_confirmed"
                elif np.isfinite(sigma) and sigma >= SHOAL_SIGMA:
                    verdict = "shoal_confirmed"
                else:
                    verdict = "open_water"

                results[idx] = {
                    "verdict": verdict,
                    "naip_land_frac": round(land_frac, 4),
                    "naip_bottom_sigma": None if not np.isfinite(sigma) else round(float(sigma), 2),
                    "naip_scene": scene_id,
                    "naip_date": item.datetime.date().isoformat(),
                }
        print(f"  [{n_scene}/{len(assigned)}] {scene_id}: {len(idxs)} candidates")

    # --- report ------------------------------------------------------------
    print(f"\nverified {len(results)}/{len(rocks)} candidates against NAIP\n")
    table = defaultdict(lambda: defaultdict(int))
    for idx, r in results.items():
        table[rocks[idx]["properties"]["class"]][r["verdict"]] += 1

    verdicts = ("rock_confirmed", "shoal_confirmed", "open_water")
    print(f"{'sentinel class':16s} {'n':>4s} " + " ".join(f"{v:>16s}" for v in verdicts))
    for cls in ("shoal", "rock", "exposed", "island"):
        row = table.get(cls)
        if not row:
            continue
        n = sum(row.values())
        cells = " ".join(f"{row.get(v,0):>7d} ({row.get(v,0)/n*100:3.0f}%)" for v in verdicts)
        print(f"{cls:16s} {n:4d} {cells}")

    out = {"type": "FeatureCollection", "features": []}
    for idx, f in enumerate(rocks):
        props = dict(f["properties"])
        r = results.get(idx)
        if r:
            props.update(r)
            props["status"] = r["verdict"]
        else:
            props["verdict"] = "unchecked"
        out["features"].append({**f, "properties": props})
    OUT.write_text(json.dumps(out))
    print(f"\nwrote {OUT}")


if __name__ == "__main__":
    main()
