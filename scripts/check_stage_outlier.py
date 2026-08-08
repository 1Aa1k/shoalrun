"""Is the 2018 flight high water, or is the flood fill swallowing a neighbour?

`measure_stage` counts the connected water body containing the lake's deepest
point. That is the right guard against the outlet river and the next pond over
being counted as this lake -- until stage rises far enough to *join* them, at
which point the component grows by a whole extra water body in one step and the
measurement reads it as a lake that got 285 ha bigger.

The six flights have exactly that shape: five inside 85 ha, and 2018 alone 285 ha
above the next highest. Both explanations predict it, so the number cannot
settle it. The geometry can.

  Real high water spreads as a thin skin over 76 km of shoreline. Every extra
  pixel is close to the mapped shore, and the extra area is spread all round it.

  A leak is a lobe. The extra pixels are concentrated in one direction and reach
  far past the mapped shoreline, connected through a narrow channel.

So: take the extra water, and look at how far it is from the mapped lake and how
much of it hangs together in one piece.
"""

import json
import sys
from pathlib import Path

import numpy as np
from rasterio.features import rasterize
from rasterio.transform import from_origin
from scipy import ndimage
from shapely.geometry import mapping, shape
from shapely.ops import transform as shp_transform
from pyproj import Transformer
import planetary_computer
import pystac_client

sys.path.insert(0, str(Path(__file__).resolve().parent))
from exposure_stack import (NDWI_LAND, STAGE_COLLAR_M, STAGE_RES, flight_ndwi,
                            naip_items)
from shoalrun_config import lake_crs

ROOT = Path(__file__).resolve().parent.parent
LAKE = ROOT / "data" / "lake.geojson"
STAGE = ROOT / "data" / "naip_stage.json"


def main():
    stage = json.loads(STAGE.read_text())
    areas = {int(k): v for k, v in stage["area_ha"].items()}
    high = max(areas, key=areas.get)
    rest = sorted((y for y in areas if y != high), key=lambda y: -areas[y])
    ref = rest[0]
    print(f"outlier flight {high} ({areas[high]:.0f} ha) vs next highest "
          f"{ref} ({areas[ref]:.0f} ha) -- gap {areas[high] - areas[ref]:.0f} ha")

    lake_ll = shape(json.loads(LAKE.read_text())["geometry"])
    crs = lake_crs()
    fwd = Transformer.from_crs("EPSG:4326", crs, always_xy=True)
    lake = shp_transform(lambda x, y: fwd.transform(x, y), lake_ll)

    res = STAGE_RES
    window_poly = lake.buffer(STAGE_COLLAR_M)
    minx, miny, maxx, maxy = window_poly.bounds
    W = int(np.ceil((maxx - minx) / res))
    H = int(np.ceil((maxy - miny) / res))
    transform = from_origin(minx, maxy, res, res)
    window = rasterize([(mapping(window_poly), 1)], out_shape=(H, W),
                       transform=transform, dtype="uint8").astype(bool)
    mapped = rasterize([(mapping(lake), 1)], out_shape=(H, W),
                       transform=transform, dtype="uint8").astype(bool)
    seed = np.unravel_index(int(np.argmax(ndimage.distance_transform_edt(mapped))),
                            mapped.shape)
    outside_m = ndimage.distance_transform_edt(~mapped) * res

    catalog = pystac_client.Client.open(STAC_URL, modifier=planetary_computer.sign_inplace)
    by_year = naip_items(catalog, lake_ll)

    masks = {}
    for y in (high, ref):
        ndwi = flight_ndwi(by_year[y], transform, H, W, crs)
        wet = (ndwi > NDWI_LAND) & np.isfinite(ndwi) & window
        lab, _ = ndimage.label(wet)
        masks[y] = lab == lab[seed]
        print(f"  {y}: {masks[y].sum() * res * res / 1e4:.0f} ha in the lake's component")

    extra = masks[high] & ~masks[ref]
    if not extra.any():
        print("no extra water -- nothing to explain")
        return
    d = outside_m[extra]
    ha = extra.sum() * res * res / 1e4
    print(f"\nextra water in {high}: {ha:.0f} ha")
    print(f"  distance outside the mapped shoreline: median {np.median(d):.0f} m, "
          f"90th pct {np.percentile(d, 90):.0f} m, max {d.max():.0f} m")
    print(f"  share sitting within 25 m of the mapped shore: "
          f"{100 * np.mean(d <= 25):.0f}%")

    lab, n = ndimage.label(extra)
    sizes = np.bincount(lab.ravel())[1:] * res * res / 1e4
    order = np.argsort(sizes)[::-1]
    print(f"  {n} separate pieces; largest {sizes[order[0]]:.0f} ha "
          f"({100 * sizes[order[0]] / ha:.0f}% of the extra), "
          f"next {sizes[order[1]] if n > 1 else 0:.0f} ha")

    lobe = sizes[order[0]] / ha > 0.5 and np.median(d) > 40
    print("\nVERDICT: " + (
        f"LEAK. {100*sizes[order[0]]/ha:.0f}% of the extra water is one blob sitting "
        f"a median {np.median(d):.0f} m outside the mapped lake. That is another "
        f"water body joined through a channel, not this lake rising. Drop {high} "
        f"from the stage ladder."
        if lobe else
        f"REAL HIGH WATER. The extra area is spread over {n} pieces hugging the "
        f"shoreline (median {np.median(d):.0f} m outside it). {high} is genuinely "
        f"the high rung."))


STAC_URL = "https://planetarycomputer.microsoft.com/api/stac/v1"

if __name__ == "__main__":
    main()
