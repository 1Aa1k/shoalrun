"""Can NAIP water area order these six flights by lake level at all?

Four threshold rules produced four different orderings, and the 2015 flight moved
from lowest to highest between two of them. Either the lake is being measured or
the method is, and there is a direct test: sweep the threshold across the whole
plausible range and watch each pair of flights. A pair whose order flips inside
that range is not ordered by the data -- it is ordered by the choice.

The stage ladder is the foundation of stage-exposure detection. If the rungs are
not reliably ordered, the monotonicity constraint has nothing to be monotone
against and the rung number means nothing. Better to find that here than to ship
a hazard map resting on it.
"""

import json
import sys
from itertools import combinations
from pathlib import Path

import numpy as np
import planetary_computer
import pystac_client
from pyproj import Transformer
from rasterio.features import rasterize
from rasterio.transform import from_origin
from scipy import ndimage
from shapely.geometry import mapping, shape
from shapely.ops import transform as shp_transform

sys.path.insert(0, str(Path(__file__).resolve().parent))
from exposure_stack import STAGE_COLLAR_M, STAGE_RES, flight_ndwi, naip_items
from shoalrun_config import lake_crs

ROOT = Path(__file__).resolve().parent.parent
LAKE = ROOT / "data" / "lake.geojson"
OUT = ROOT / "data" / "stage_stability.json"
STAC = "https://planetarycomputer.microsoft.com/api/stac/v1"
SWEEP = np.round(np.arange(0.15, 0.451, 0.025), 3)


def main():
    lake_ll = shape(json.loads(LAKE.read_text())["geometry"])
    crs = lake_crs()
    fwd = Transformer.from_crs("EPSG:4326", crs, always_xy=True)
    lake = shp_transform(lambda x, y: fwd.transform(x, y), lake_ll)

    res = STAGE_RES
    wp = lake.buffer(STAGE_COLLAR_M)
    minx, miny, maxx, maxy = wp.bounds
    W, H = int(np.ceil((maxx - minx) / res)), int(np.ceil((maxy - miny) / res))
    tr = from_origin(minx, maxy, res, res)
    window = rasterize([(mapping(wp), 1)], out_shape=(H, W), transform=tr,
                       dtype="uint8").astype(bool)
    mapped = rasterize([(mapping(lake), 1)], out_shape=(H, W), transform=tr,
                       dtype="uint8").astype(bool)
    seed = np.unravel_index(int(np.argmax(ndimage.distance_transform_edt(mapped))),
                            mapped.shape)

    catalog = pystac_client.Client.open(STAC, modifier=planetary_computer.sign_inplace)
    by_year = naip_items(catalog, lake_ll)
    years = sorted(by_year)

    areas = {}
    for y in years:
        ndwi = flight_ndwi(by_year[y], tr, H, W, crs)
        seen = np.isfinite(ndwi) & window
        row = []
        for t in SWEEP:
            wet = (ndwi > t) & seen
            lab, _ = ndimage.label(wet)
            home = lab[seed]
            row.append(float((lab == home).sum()) * res * res / 1e4 if home else np.nan)
        areas[y] = row
        print(f"  {y}: " + " ".join(f"{a:6.0f}" for a in row))

    print("\nthresholds: " + " ".join(f"{t:6.3f}" for t in SWEEP))
    flips, stable = [], []
    for a, b in combinations(years, 2):
        d = np.array(areas[a]) - np.array(areas[b])
        d = d[np.isfinite(d)]
        if d.size == 0:
            continue
        if (d > 0).any() and (d < 0).any():
            flips.append((a, b, float(d.min()), float(d.max())))
        else:
            stable.append((a, b, float(np.median(d))))

    n_pairs = len(flips) + len(stable)
    print(f"\n{len(flips)}/{n_pairs} flight pairs FLIP order inside the sweep:")
    for a, b, lo, hi in flips:
        print(f"  {a} vs {b}: difference runs {lo:+.0f} ha to {hi:+.0f} ha")
    print(f"{len(stable)}/{n_pairs} hold their order:")
    for a, b, med in stable:
        print(f"  {a} {'>' if med > 0 else '<'} {b} throughout (median {abs(med):.0f} ha apart)")

    OUT.write_text(json.dumps({
        "sweep": SWEEP.tolist(),
        "area_ha": {str(y): areas[y] for y in years},
        "pairs_total": n_pairs,
        "pairs_flipped": len(flips),
        "flips": [{"a": a, "b": b, "min_ha": lo, "max_ha": hi} for a, b, lo, hi in flips],
        "stable": [{"a": a, "b": b, "median_ha": m} for a, b, m in stable],
    }, indent=1))
    print(f"\nwrote {OUT}")
    print("\nVERDICT: " + (
        "the ordering is a property of the threshold, not the lake. NAIP water "
        "area cannot rank these flights by stage."
        if len(flips) > n_pairs / 3 else
        f"{len(stable)} of {n_pairs} pairs are ordered robustly; a partial ladder "
        f"is available from those."))


if __name__ == "__main__":
    main()
