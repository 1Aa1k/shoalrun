"""Measure what the 32 known rocks actually look like in 0.3 m imagery.

Recall is 16% and the obvious assumption is that the detector is too blunt. Before
tuning anything, this asks a prior question: are these rocks even present in the
pixels? A threshold cannot be tuned to find something that is not there.

For each hand-mapped reference rock, sample its footprint in the 2023 NAIP flight
and compare against a local water annulus:

  dry     Dry surface in the footprint (NIR well above the water background).
          Any competent detector should get these; missing one is a real bug.
  bottom  No dry surface, but green measurably above local water -- a submerged
          shallow visible through the column. Detectable, needs the right
          threshold.
  invisible  Neither. Statistically indistinguishable from the water around it.
          No imagery method at any resolution will find these, because the
          information is not in the image. This is the population that buoys and
          sonar exist for, and pretending otherwise would be dishonest.
"""

import json
import sys
from pathlib import Path

import numpy as np
import planetary_computer
import pystac_client
import rasterio
from pyproj import Transformer
from rasterio.transform import from_origin
from rasterio.warp import Resampling, reproject

sys.path.insert(0, str(Path(__file__).resolve().parent))
from shoalrun_config import lake_crs

ROOT = Path(__file__).resolve().parent.parent
REFS = ROOT / "data" / "reference_rocks.geojson"
OUT = ROOT / "data" / "reference_signatures.json"

RES = 0.3
CHIP_M = 60.0             # chip side
CORE_R = 6.0              # footprint radius sampled as "the rock"
RING_IN, RING_OUT = 14.0, 28.0   # local water annulus

NIR_DRY_SIGMA = 3.0       # NIR above water background => dry surface present
GREEN_BOTTOM_SIGMA = 2.0  # green above water background => visible bottom


def main():
    refs = json.loads(REFS.read_text())["features"]
    crs = lake_crs()
    tr = Transformer.from_crs("EPSG:4326", crs, always_xy=True)

    cat = pystac_client.Client.open(
        "https://planetarycomputer.microsoft.com/api/stac/v1",
        modifier=planetary_computer.sign_inplace,
    )
    items = [
        i for i in cat.search(collections=["naip"], bbox=[-68.87, 45.71, -68.71, 45.80],
                              datetime="2023-01-01/2023-12-31").items()
    ]
    print(f"{len(items)} NAIP 2023 scenes; {len(refs)} reference rocks\n")

    n = int(CHIP_M / RES)
    rows = []
    for f in refs:
        lon, lat = f["geometry"]["coordinates"]
        cov = [i for i in items if i.bbox[0] <= lon <= i.bbox[2] and i.bbox[1] <= lat <= i.bbox[3]]
        if not cov:
            rows.append((f["properties"], "no_coverage", None, None))
            continue

        x, y = tr.transform(lon, lat)
        t = from_origin(x - n / 2 * RES, y + n / 2 * RES, RES, RES)
        g = np.zeros((n, n), "float32")
        nir = np.zeros((n, n), "float32")
        got = np.zeros((n, n), bool)
        for it in cov:
            bg = np.zeros((n, n), "float32")
            bn = np.zeros((n, n), "float32")
            try:
                with rasterio.open(it.assets["image"].href) as src:
                    reproject(rasterio.band(src, 2), bg, dst_transform=t, dst_crs=crs,
                              resampling=Resampling.average)
                    reproject(rasterio.band(src, 4), bn, dst_transform=t, dst_crs=crs,
                              resampling=Resampling.average)
            except Exception:
                continue
            have = (bg + bn) > 0
            g[have] = bg[have]
            nir[have] = bn[have]
            got |= have

        if got.mean() < 0.3:
            rows.append((f["properties"], "no_coverage", None, None))
            continue

        yy, xx = np.mgrid[0:n, 0:n]
        d = np.hypot((yy - n / 2) * RES, (xx - n / 2) * RES)
        core = (d <= CORE_R) & got
        ring = (d >= RING_IN) & (d <= RING_OUT) & got
        if core.sum() < 50 or ring.sum() < 200:
            rows.append((f["properties"], "no_coverage", None, None))
            continue

        # Water background from the annulus, excluding any dry pixels in it.
        nd_ring = np.where(g[ring] + nir[ring] > 0, (g[ring] - nir[ring]) / (g[ring] + nir[ring]), -1)
        wet_ring = nd_ring > 0
        if wet_ring.sum() < 100:
            rows.append((f["properties"], "no_coverage", None, None))
            continue

        gb, gs = g[ring][wet_ring].mean(), g[ring][wet_ring].std() or 1.0
        nb, ns = nir[ring][wet_ring].mean(), nir[ring][wet_ring].std() or 1.0

        z_nir = (nir[core].mean() - nb) / ns
        z_green = (g[core].mean() - gb) / gs

        if z_nir >= NIR_DRY_SIGMA:
            verdict = "dry"
        elif z_green >= GREEN_BOTTOM_SIGMA:
            verdict = "bottom"
        else:
            verdict = "invisible"
        rows.append((f["properties"], verdict, float(z_green), float(z_nir)))

    from collections import Counter

    c = Counter(r[1] for r in rows)
    print(f"{'verdict':12s} {'n':>4s}   what it means")
    meanings = {
        "dry": "dry surface present -- detector SHOULD find these",
        "bottom": "submerged but visible -- detectable with right threshold",
        "invisible": "indistinguishable from water -- NO imagery method can find these",
        "no_coverage": "no usable NAIP chip",
    }
    for k in ("dry", "bottom", "invisible", "no_coverage"):
        if c.get(k):
            print(f"{k:12s} {c[k]:4d}   {meanings[k]}")

    detectable = c.get("dry", 0) + c.get("bottom", 0)
    total = sum(v for k, v in c.items() if k != "no_coverage")
    if total:
        print(f"\nCEILING: at best {detectable}/{total} = {detectable/total*100:.0f}% "
              f"of known rocks are findable from imagery at any resolution.")

    print(f"\n{'kind':12s} {'verdict':11s} {'z_green':>8s} {'z_nir':>7s}")
    for props, verdict, zg, zn in sorted(rows, key=lambda r: r[1]):
        zgs = f"{zg:8.2f}" if zg is not None else "       -"
        zns = f"{zn:7.2f}" if zn is not None else "      -"
        print(f"{props.get('kind','?'):12s} {verdict:11s} {zgs} {zns}")

    OUT.write_text(json.dumps([
        {"kind": p.get("kind"), "lat": p.get("lat"), "lon": p.get("lon"),
         "verdict": v, "z_green": zg, "z_nir": zn} for p, v, zg, zn in rows
    ], indent=1))
    print(f"\nwrote {OUT}")


if __name__ == "__main__":
    main()
