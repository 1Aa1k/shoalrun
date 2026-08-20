"""Fetch NAIP aerial imagery for the lake and write it as one georeferenced JPEG.

The map draws inference: magenta candidate marks that came off a satellite and,
for three quarters of them, mean nothing more than "this pixel kept looking odd
across six flights". The overlay is the photograph underneath that inference, so
somebody at the helm can look at a mark and see whether there is actually a rock
there.

Not inlined into index.html the way everything else is. At a resolution where a
boulder is more than one pixel the image is several megabytes, and index.html is
what the app needs before it can draw anything at all -- making the first paint
wait on the photograph would trade the thing that matters for the thing that is
nice to have. It ships beside the app and the service worker precaches it, so it
is on the phone before the lake but never in the critical path.

Resolution is a real trade, not a default: NAIP is 0.6-1 m, and every halving of
the pixel size quadruples the file. RES_M below is the knob.
"""

import json
import math
import sys
from pathlib import Path

import numpy as np
import planetary_computer
import pystac_client
import rasterio
from PIL import Image
from rasterio.enums import Resampling
from rasterio.warp import Resampling as WarpResampling
from rasterio.warp import reproject
from shapely.geometry import shape

ROOT = Path(__file__).resolve().parent.parent
STAC = "https://planetarycomputer.microsoft.com/api/stac/v1"

# Metres per output pixel. At 2 m a 3 m boulder is a pixel and a half -- enough
# to say "there is something there", which is the question being asked. At 1 m
# the file roughly quadruples for detail the phone screen cannot show at the
# zoom levels this gets used at.
RES_M = 2.0

# JPEG, not PNG: this is a photograph, and PNG of a photograph is ~5x the size
# for a fidelity nobody can see through a 40% opacity blend.
QUALITY = 72

# Written into data/, not dist/. dist/ is a build output and gets regenerated;
# the photograph costs a long download over a metered connection and must
# survive a rebuild. build_app.py copies it across.
OUT = ROOT / "data" / "sat.jpg"
META = ROOT / "web" / "sat-meta.json"


# Padding around the lake polygon, in degrees. The shoreline is the most useful
# part of the photograph -- it is where the rocks are -- so it must not sit on
# the very edge of the image.
PAD_LON = 0.004
PAD_LAT = 0.003

# The same earth radius `web/geo.js` uses, so "2 m" here means what the app
# means by 2 m rather than what some other spheroid does.
R_EARTH = 6378137.0
M_PER_DEG_LAT = math.pi / 180.0 * R_EARTH


def aoi_bounds():
    """The lake's extent in plain lon/lat, padded.

    Deliberately NOT the UTM grid the detection pipeline runs on. The app does
    not use UTM: `web/geo.js` projects with a local flat-earth transform whose
    forward map is affine in lon and lat. That means a lon/lat-aligned image is
    still an axis-aligned rectangle in the app's world metres, so the overlay
    reduces to one `drawImage` with no per-frame resampling. Handing it UTM
    instead would put the image in a slightly rotated, slightly sheared frame
    and the marks would sit next to the rocks rather than on them.
    """
    lake = shape(json.loads((ROOT / "data" / "lake.geojson").read_text())["geometry"])
    minx, miny, maxx, maxy = lake.bounds
    return (minx - PAD_LON, miny - PAD_LAT, maxx + PAD_LON, maxy + PAD_LAT)


def main():
    minx, miny, maxx, maxy = aoi_bounds()
    crs = "EPSG:4326"
    mid_lat = (miny + maxy) / 2.0
    m_per_deg_lon = M_PER_DEG_LAT * math.cos(math.radians(mid_lat))
    dlon = RES_M / m_per_deg_lon
    dlat = RES_M / M_PER_DEG_LAT
    width = int(round((maxx - minx) / dlon))
    height = int(round((maxy - miny) / dlat))
    dst_transform = rasterio.transform.from_bounds(minx, miny, maxx, maxy, width, height)
    print(f"AOI {minx:.6f},{miny:.6f} .. {maxx:.6f},{maxy:.6f}")
    print(f"output {width} x {height} px at ~{RES_M} m/px "
          f"({width * height / 1e6:.1f} Mpx)")

    lake = shape(json.loads((ROOT / "data" / "lake.geojson").read_text())["geometry"])
    catalog = pystac_client.Client.open(STAC, modifier=planetary_computer.sign_inplace)
    items = list(catalog.search(collections=["naip"], bbox=lake.bounds).items())
    if not items:
        raise SystemExit("no NAIP items for this lake")

    # One flight, not a blend. Mixing years across a mosaic seam puts two
    # different water levels and two different sun angles next to each other,
    # and the seam reads as a shoreline that is not there.
    years = sorted({it.properties["naip:year"] for it in items}, reverse=True)
    year = years[0]
    items = [it for it in items if it.properties["naip:year"] == year]
    print(f"NAIP flights available: {years} -- using {year} ({len(items)} tiles)")

    dst = np.zeros((3, height, width), dtype=np.uint8)
    covered = np.zeros((height, width), dtype=bool)

    for n, it in enumerate(items, 1):
        href = it.assets["image"].href
        with rasterio.open(href) as src:
            # Read decimated at roughly the target resolution. The COG serves it
            # from an overview, so this transfers a fraction of the full tile
            # rather than the whole 1 m scene we would then throw away.
            factor = max(1, int(RES_M / abs(src.transform.a)))
            out_h = max(1, src.height // factor)
            out_w = max(1, src.width // factor)
            band = src.read(
                indexes=[1, 2, 3],
                out_shape=(3, out_h, out_w),
                resampling=Resampling.average,
            )
            src_transform = src.transform * src.transform.scale(
                src.width / out_w, src.height / out_h
            )
            tmp = np.zeros((3, height, width), dtype=np.uint8)
            reproject(
                source=band,
                destination=tmp,
                src_transform=src_transform,
                src_crs=src.crs,
                dst_transform=dst_transform,
                dst_crs=crs,
                resampling=WarpResampling.bilinear,
            )
        # Only write where this tile actually had data, so a later tile's black
        # margin cannot punch a hole in an earlier tile's imagery.
        hit = tmp.any(axis=0) & ~covered
        dst[:, hit] = tmp[:, hit]
        covered |= hit
        print(f"  [{n}/{len(items)}] {it.id}  covered {covered.mean() * 100:.1f}%")

    img = Image.fromarray(np.transpose(dst, (1, 2, 0)), mode="RGB")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    img.save(OUT, "JPEG", quality=QUALITY, optimize=True, progressive=True)
    size = OUT.stat().st_size

    # The app needs the extent to place the image, and it must come from the
    # same numbers that produced it rather than a constant copied by hand.
    META.write_text(json.dumps({
        "crs": crs,
        "west": minx, "south": miny, "east": maxx, "north": maxy,
        "width": width, "height": height,
        "res_m": RES_M,
        "year": year,
        "source": "USDA NAIP via Microsoft Planetary Computer",
    }, indent=2) + "\n")

    print(f"wrote {OUT}  ({size / 1e6:.1f} MB, {covered.mean() * 100:.1f}% covered)")
    print(f"wrote {META}")
    if covered.mean() < 0.98:
        print("WARNING: coverage under 98% -- some of the AOI has no NAIP", file=sys.stderr)


if __name__ == "__main__":
    main()
