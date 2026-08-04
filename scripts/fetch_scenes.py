"""Pull a multi-date Sentinel-2 stack over Millinocket Lake onto one fixed grid.

The whole detector rests on temporal persistence, so every scene has to land on
the identical pixel grid -- otherwise "the same pixel across dates" is a lie and
sub-pixel drift reads as a rock. We define one UTM 19N / 10 m grid up front and
reproject every scene into it.

Bands: B03 (green) penetrates a couple of metres of clear water and is what
actually sees a submerged boulder. B08 (NIR) is absorbed by water almost
completely, which makes it the water/land discriminator. SCL is the L2A scene
classification, used to throw out cloud and cloud shadow per pixel rather than
per scene -- a scene with one cloud over the far shore is still good data.
"""

import json
from pathlib import Path

import numpy as np
import planetary_computer
import pystac_client
import rasterio
from pyproj import Transformer
from rasterio.warp import Resampling, reproject
from shapely.geometry import shape

ROOT = Path(__file__).resolve().parent.parent
LAKE = ROOT / "data" / "lake.geojson"
OUT = ROOT / "data" / "stack.npz"

STAC = "https://planetarycomputer.microsoft.com/api/stac/v1"
COLLECTION = "sentinel-2-l2a"
DATE_RANGE = "2019-01-01/2026-08-04"
MAX_CLOUD = 25
RES = 10.0  # metres, native for B03/B08
PAD_M = 300.0  # margin beyond the lake so the shoreline is never at the edge
MAX_SCENES = 250  # hard bound: never let a bad query allocate unbounded memory

# Ice and snow break every assumption here -- a frozen lake is not water and a
# snowbank is not a rock. Maine ice-out on this lake is typically early May and
# ice-in around late November, so only trust high summer.
OPEN_WATER_MONTHS = (6, 7, 8, 9, 10)

# SCL classes worth keeping: 4 vegetation, 5 bare soil, 6 water, 7 unclassified,
# 11 snow/ice is excluded deliberately.
SCL_GOOD = (4, 5, 6, 7)


def build_grid(lake_geom):
    """Fixed destination grid in UTM 19N covering the lake plus a margin."""
    minx, miny, maxx, maxy = lake_geom.bounds  # lon/lat
    tf = Transformer.from_crs("EPSG:4326", "EPSG:32619", always_xy=True)
    xs, ys = tf.transform([minx, maxx, minx, maxx], [miny, miny, maxy, maxy])

    left = np.floor((min(xs) - PAD_M) / RES) * RES
    right = np.ceil((max(xs) + PAD_M) / RES) * RES
    bottom = np.floor((min(ys) - PAD_M) / RES) * RES
    top = np.ceil((max(ys) + PAD_M) / RES) * RES

    width = int(round((right - left) / RES))
    height = int(round((top - bottom) / RES))
    transform = rasterio.transform.from_origin(left, top, RES, RES)
    return transform, width, height


def read_band(href, transform, width, height):
    """Read one COG asset reprojected onto the fixed grid."""
    dst = np.zeros((height, width), dtype="float32")
    with rasterio.open(href) as src:
        reproject(
            source=rasterio.band(src, 1),
            destination=dst,
            dst_transform=transform,
            dst_crs="EPSG:32619",
            resampling=Resampling.bilinear,
        )
    return dst


def read_scl(href, transform, width, height):
    """SCL is categorical -- nearest neighbour only, never interpolate classes."""
    dst = np.zeros((height, width), dtype="uint8")
    with rasterio.open(href) as src:
        reproject(
            source=rasterio.band(src, 1),
            destination=dst,
            dst_transform=transform,
            dst_crs="EPSG:32619",
            resampling=Resampling.nearest,
        )
    return dst


def main():
    lake = shape(json.loads(LAKE.read_text())["geometry"])
    transform, width, height = build_grid(lake)
    print(f"grid: {width} x {height} px @ {RES:g} m  (UTM 19N)")

    catalog = pystac_client.Client.open(STAC, modifier=planetary_computer.sign_inplace)
    search = catalog.search(
        collections=[COLLECTION],
        bbox=lake.bounds,
        datetime=DATE_RANGE,
        query={"eo:cloud_cover": {"lt": MAX_CLOUD}},
    )
    items = list(search.items())
    items = [i for i in items if i.datetime.month in OPEN_WATER_MONTHS]
    items.sort(key=lambda i: i.datetime)

    if len(items) > MAX_SCENES:
        print(f"capping {len(items)} scenes to {MAX_SCENES}")
        items = items[:MAX_SCENES]
    print(f"{len(items)} open-water scenes, {items[0].datetime.date()} .. {items[-1].datetime.date()}")

    greens, nirs, valids, meta = [], [], [], []
    for n, item in enumerate(items, 1):
        try:
            g = read_band(item.assets["B03"].href, transform, width, height)
            nir = read_band(item.assets["B08"].href, transform, width, height)
            scl = read_scl(item.assets["SCL"].href, transform, width, height)
        except Exception as exc:  # a single bad COG must not kill the run
            print(f"  [{n}/{len(items)}] {item.id} FAILED: {exc}")
            continue

        valid = np.isin(scl, SCL_GOOD)
        if valid.mean() < 0.5:
            print(f"  [{n}/{len(items)}] {item.datetime.date()} skipped, {valid.mean():.0%} usable")
            continue

        # Processing baseline 04.00 (Jan 2022) added a +1000 DN radiometric
        # offset. Recorded per scene so downstream code can tell the two eras
        # apart; the detector thresholds adaptively and does not depend on it,
        # but anything wanting true reflectance must subtract it.
        baseline = str(item.properties.get("s2:processing_baseline", "")) or "unknown"

        greens.append(g.astype("float32"))
        nirs.append(nir.astype("float32"))
        valids.append(valid)
        meta.append(
            {
                "id": item.id,
                "date": item.datetime.isoformat(),
                "usable": float(valid.mean()),
                "baseline": baseline,
                "boa_offset": 1000 if baseline >= "04.00" and baseline != "unknown" else 0,
            }
        )
        print(f"  [{n}/{len(items)}] {item.datetime.date()} ok  usable={valid.mean():.0%}")

    if not greens:
        raise SystemExit("no usable scenes -- refusing to write an empty stack")

    np.savez_compressed(
        OUT,
        green=np.stack(greens),
        nir=np.stack(nirs),
        valid=np.stack(valids),
        transform=np.array(transform.to_gdal()),
        crs="EPSG:32619",
        meta=json.dumps(meta),
    )
    print(f"wrote {OUT}  ({len(greens)} scenes, {OUT.stat().st_size / 1e6:.0f} MB)")


if __name__ == "__main__":
    main()
