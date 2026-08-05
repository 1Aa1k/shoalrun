"""Render offshore imagery into a click-to-mark-rocks page.

Every automated measure has hit the same wall: the only rock-scale ground truth
is 32 hand-mapped OSM points, and 32 points cannot distinguish a good detector
from a dense one. The depth survey is independent but far too coarse to resolve a
boulder -- one sounding per 32 acres.

Nate knows this lake. Him clicking rocks is not a fallback, it is the highest
resolution ground truth available, and it is the only kind measured at the same
scale the detector operates at.

So: cut the offshore water into sections, render each from the clearest flights,
and let him click. Output is a plain list of rock positions that recall_check.py
and tune_edge.py can both consume.

Design decisions worth stating:

  Offshore only. Nate does not care about shoreline rock and neither does the
  metric -- near shore everything is shallow, and detectors get credit there
  they did not earn.

  Multiple flights per section, flipped with a keypress. A wave crest appears in
  one flight; a rock appears in all of them. Flipping between years is exactly
  how a person separates the two, and it is the same persistence logic the
  detector uses, run on a better classifier.

  Sections ordered by detection density, so the informative water comes first
  and stopping early still yields a usable sample.

  Work saved to localStorage after every click. 244 sections is more than one
  sitting, and losing an hour of clicking to a closed tab would be unforgivable.
"""

import json
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import planetary_computer
import pystac_client
import rasterio
from PIL import Image
from pyproj import Transformer
from rasterio.transform import from_origin
from rasterio.warp import Resampling, reproject
from scipy import ndimage
from shapely.geometry import Point, box, shape
from shapely.ops import transform as shp_transform

sys.path.insert(0, str(Path(__file__).resolve().parent))
from shoalrun_config import lake_crs

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
OUT = ROOT / "dist" / "annotate"
CHIPS = OUT / "chips"

SECTION_M = 400.0
RES = 0.5              # 800 px per section; a 2 m rock is 4 px, clickable
OFFSHORE_M = 50.0
MIN_WATER_FRAC = 0.08  # skip sections that are nearly all land
N_FLIGHTS = 3          # best three by measured contrast
JPEG_Q = 82
MAX_WATER_GAIN = 3.0   # ceiling on the equalisation gain; see stretch()
EQ_MIX = 0.5           # equalised vs linear blend; 1.0 was pure confetti
WORKERS = 4            # reads within one section
SECTION_WORKERS = 10   # sections in flight at once


def stretch(rgb, valid, water=None, land_gain=0.55):
    """Percentile stretch per band, scaled to the WATER histogram.

    Stretching across the whole chip is what made the first render useless.
    Sunlit trees and sand are several times brighter than water, so they claimed
    the entire range and every water pixel landed in the bottom couple of
    percent -- rendering as near-black with visible sensor noise. The submerged
    detail was in the data the whole time and was discarded at display time.

    So the range comes from water pixels only, and land is allowed to blow out.
    Land is then pulled down by `land_gain` so it still reads as land instead of
    a white sheet, without ever competing for the range that matters.
    """
    out = np.zeros(rgb.shape[1:] + (3,), "uint8")
    has_water = water is not None and water.sum() > 200
    ref = water if has_water else valid
    if ref.sum() < 50:
        return out

    if not has_water:
        for i in range(3):
            lo, hi = np.percentile(rgb[i][ref], (0.5, 99.5))
            out[..., i] = np.clip((rgb[i] - lo) / (max(hi - lo, 1)) * 255, 0, 255)
        return out

    # A percentile stretch is not enough on its own. Over deep water the whole
    # histogram is narrow AND close to zero, and its top end gets set by the
    # bright shallow shelf and the rocks -- which are the very things we are
    # hunting -- so deep water stays crushed no matter which percentiles are
    # picked. Equalising against the water's own cumulative distribution spreads
    # whatever range exists across the full display instead.
    # Kill single-pixel sensor noise BEFORE boosting contrast. At 0.5 m a rock is
    # 4-10 px across and speckle is 1 px, so a 3x3 median removes the noise and
    # leaves every real target intact. Without this, equalisation multiplies the
    # noise along with the signal and the water renders as confetti.
    rgb = np.stack([ndimage.median_filter(rgb[i], size=3) for i in range(3)])

    lum = rgb.mean(axis=0)
    wl = lum[water]
    qs = np.linspace(0, 100, 129)
    knots = np.percentile(wl, qs)
    knots = np.maximum.accumulate(knots)          # monotone for np.interp
    eq = np.interp(lum, knots, qs / 100.0)

    # Equalisation is a per-pixel gain on luminance; applying it to the bands
    # keeps hue intact instead of washing everything grey. Capped, because in
    # near-black water the gain would otherwise run away and turn sensor noise
    # into confetti -- which is what the first render looked like.
    # Full equalisation is too aggressive on its own -- it spends the whole
    # display range on the flattest water and exaggerates every ripple. Blending
    # it with a plain linear stretch keeps the picture looking like a lake while
    # still lifting the dark end where the shoals live.
    top = max(np.percentile(wl, 99.9), 1e-6)
    linear = lum / top
    mixed = EQ_MIX * eq + (1.0 - EQ_MIX) * np.clip(linear, 0, 1)
    gain = np.clip(mixed / np.maximum(linear, 1e-3), 0.0, MAX_WATER_GAIN)
    for i in range(3):
        v = rgb[i] / top * gain
        v = np.where(water, v, (rgb[i] / max(np.percentile(lum[~water], 99.0)
                                             if (~water).sum() > 50 else 1.0, 1e-6)) * land_gain)
        out[..., i] = np.clip(v * 255, 0, 255).astype("uint8")
    return out


def shallow_index(rgb, nir, water):
    """False-colour view keyed on bottom reflectance -- the sandy halo.

    Nate's observation: groups of rocks sit inside a yellowish patch. That is a
    gravel or sand shoal, and it is a far bigger target than the rock on it --
    tens of metres across instead of a few. Finding the halo finds the rock.

    Blue light penetrates water furthest and red is absorbed within a metre or
    two, so the ratio of the two tracks depth over a uniform bottom. This is the
    Stumpf log-ratio used for satellite-derived bathymetry:

        index = ln(blue) / ln(green)

    The raw ratio FALLS as water shallows -- over sand the green band climbs
    faster than blue, so the quotient drops -- which renders deep water bright
    and the shoal dark. That is backwards for this job, so it is inverted here:
    bright means shallow, bright means danger.

    Equalised rather than percentile-stretched, because most of the lake is deep
    and a linear scale puts nearly every pixel at one end.
    """
    b = np.maximum(rgb[2], 1.0)
    g = np.maximum(rgb[1], 1.0)
    with np.errstate(invalid="ignore", divide="ignore"):
        idx = np.log(b) / np.log(g)
    idx = np.nan_to_num(idx, nan=0.0, posinf=0.0, neginf=0.0)
    out = np.zeros(idx.shape, "uint8")
    if water.sum() < 200:
        return out
    idx = ndimage.median_filter(idx, size=3)
    qs = np.linspace(0, 100, 129)
    knots = np.maximum.accumulate(np.percentile(idx[water], qs))
    v = 1.0 - np.interp(idx, knots, qs / 100.0)   # invert: shallow = bright
    # Land carries no depth meaning here; flatten it so it cannot be misread as
    # a very shallow shoal.
    v = np.where(water, v, 0.0)
    return (np.clip(v, 0, 1) * 255).astype("uint8")


def main():
    CHIPS.mkdir(parents=True, exist_ok=True)
    crs = lake_crs()
    fwd = Transformer.from_crs("EPSG:4326", crs, always_xy=True)
    back = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
    lake_ll = shape(json.loads((DATA / "lake.geojson").read_text())["geometry"])
    lake = shp_transform(lambda x, y: fwd.transform(x, y), lake_ll)
    off = lake.buffer(-OFFSHORE_M)

    quality = json.loads((DATA / "flight_quality.json").read_text())
    best = sorted(quality.items(), key=lambda kv: -kv[1]["contrast"])[:N_FLIGHTS]
    years = [int(y) for y, _ in best]
    print("flights: " + ", ".join(f"{y} ({quality[str(y)]['date']}, "
                                 f"contrast {quality[str(y)]['contrast']})" for y in years))

    haz = json.loads((DATA / "hazards.geojson").read_text())["features"]
    hz = [(f["properties"]["lon"], f["properties"]["lat"], f["properties"].get("class"))
          for f in haz if f["properties"].get("lon") is not None
          and f["properties"].get("offshore")]
    hz_xy = [(*fwd.transform(lon, lat), c) for lon, lat, c in hz]

    snd = json.loads((DATA / "soundings.geojson").read_text())["features"]
    snd_xy = [(*fwd.transform(*f["geometry"]["coordinates"]),
               f["properties"]["depth_ft"]) for f in snd]

    minx, miny, maxx, maxy = lake.bounds
    cells = []
    x = minx
    while x < maxx:
        y = miny
        while y < maxy:
            c = box(x, y, x + SECTION_M, y + SECTION_M)
            if off.intersects(c):
                inter = off.intersection(c)
                if inter.area > SECTION_M * SECTION_M * MIN_WATER_FRAC:
                    n_haz = sum(1 for hx, hy, _ in hz_xy
                                if x <= hx < x + SECTION_M and y <= hy < y + SECTION_M)
                    cells.append({"x": x, "y": y, "n_haz": n_haz,
                                  "water": inter.area / (SECTION_M * SECTION_M)})
            y += SECTION_M
        x += SECTION_M
    # Densest first: if Nate stops after 40 sections, those 40 should be the
    # ones that discriminate between detectors rather than empty open water.
    cells.sort(key=lambda c: -c["n_haz"])

    # Optional whitelist of section ids. Section ids are positions in THIS
    # sorted list, so the sort above must stay deterministic for a whitelist
    # written by a previous run to still mean the same squares.
    wl = DATA / "section_whitelist.json"
    if wl.exists():
        keep = set(json.loads(wl.read_text()))
        cells = [c for i, c in enumerate(cells) if i in keep]
        # Ids are carried explicitly so filenames and existing marks line up.
        cells = [dict(c, _id=i) for i, c in zip(sorted(keep), cells)]
        print(f"whitelist: {len(cells)} of {len(keep)} requested sections")
    else:
        cells = [dict(c, _id=i) for i, c in enumerate(cells)]
    print(f"{len(cells)} sections of {SECTION_M:g} m to render")

    cat = pystac_client.Client.open(
        "https://planetarycomputer.microsoft.com/api/stac/v1",
        modifier=planetary_computer.sign_inplace,
    )
    items = list(cat.search(collections=["naip"], bbox=lake_ll.bounds).items())
    by_year = defaultdict(list)
    for i in items:
        by_year[i.datetime.year].append(i)

    n = int(SECTION_M / RES)

    # Parallel ACROSS sections, not within one. Each read is a round trip to
    # Azure for a small window, so the job is latency-bound, not bandwidth-bound
    # -- the link sits idle while a section waits its turn. Serially this was
    # 33 s per section, 2.2 hours for the lake. Overlapping the waits fixes it.
    def render(args):
        _, c = args
        si = c["_id"]
        t = from_origin(c["x"], c["y"] + SECTION_M, RES, RES)
        imgs = []
        for year in years:
            rgb = np.zeros((3, n, n), "float32")
            nir = np.zeros((n, n), "float32")
            got = np.zeros((n, n), bool)

            def _read(it):
                b = np.zeros((4, n, n), "float32")
                try:
                    with rasterio.open(it.assets["image"].href) as src:
                        for bi in range(4):
                            reproject(rasterio.band(src, bi + 1), b[bi], dst_transform=t,
                                      dst_crs=crs, resampling=Resampling.bilinear)
                    return b
                except Exception:
                    return None

            with ThreadPoolExecutor(max_workers=WORKERS) as pool:
                for b in pool.map(_read, by_year[year]):
                    if b is None:
                        continue
                    have = b.sum(axis=0) > 0
                    for bi in range(3):
                        rgb[bi][have] = b[bi][have]
                    nir[have] = b[3][have]
                    got |= have
            if got.mean() < 0.2:
                continue  # this flight missed the section; others may still cover it
            # Water mask drives every stretch below. NDWI (green vs NIR) is the
            # reliable split here because water absorbs NIR and vegetation
            # reflects it strongly.
            with np.errstate(invalid="ignore", divide="ignore"):
                ndwi = np.where(rgb[1] + nir > 0,
                                (rgb[1] - nir) / (rgb[1] + nir + 1e-6), np.nan)
            water = got & np.isfinite(ndwi) & (ndwi > 0)

            fn = f"s{si:04d}_{year}.jpg"
            Image.fromarray(stretch(rgb, got, water)).save(CHIPS / fn, quality=JPEG_Q)

            # Bottom-reflectance view: Nate's sandy halo, made explicit.
            shn = f"s{si:04d}_{year}_sh.jpg"
            Image.fromarray(shallow_index(rgb, nir, water)).save(
                CHIPS / shn, quality=JPEG_Q)

            # Infrared, greyscale. Water absorbs NIR almost completely, so open
            # water goes black and anything breaking the surface glows -- it is
            # the single most direct way to see a rock that is actually above
            # the waterline, and it separates dry rock from a shallow bottom
            # (which stays dark, because there is still water over it).
            # Stretched on its own histogram: NIR over water is so uniformly low
            # that sharing the visible bands' scaling would flatten it to black.
            irn = f"s{si:04d}_{year}_ir.jpg"
            Image.fromarray(stretch(np.stack([nir] * 3), got, water, land_gain=1.0)[..., 0]).save(
                CHIPS / irn, quality=JPEG_Q)
            imgs.append({"year": year, "file": f"chips/{fn}",
                         "ir": f"chips/{irn}", "sh": f"chips/{shn}"})
        if not imgs:
            return None

        def to_px(px, py):
            return [round((px - c["x"]) / RES, 1), round((c["y"] + SECTION_M - py) / RES, 1)]

        return {
            "id": si,
            "imgs": imgs,
            "n": n,
            "res": RES,
            "origin": [c["x"], c["y"] + SECTION_M],
            "center_ll": list(back.transform(c["x"] + SECTION_M / 2, c["y"] + SECTION_M / 2)),
            "haz": [{"p": to_px(hx, hy), "c": cl} for hx, hy, cl in hz_xy
                    if c["x"] <= hx < c["x"] + SECTION_M and c["y"] <= hy < c["y"] + SECTION_M],
            "snd": [{"p": to_px(sx, sy), "d": d} for sx, sy, d in snd_xy
                    if c["x"] <= sx < c["x"] + SECTION_M and c["y"] <= sy < c["y"] + SECTION_M],
        }

    sections = []
    done = 0
    with ThreadPoolExecutor(max_workers=SECTION_WORKERS) as pool:
        for r in pool.map(render, list(enumerate(cells))):
            done += 1
            if r:
                sections.append(r)
            if done % 10 == 0:
                print(f"  {done}/{len(cells)} sections, {len(sections)} kept", flush=True)
    # pool.map preserves input order, but sort anyway so the JSON order is the
    # click order Nate sees regardless of scheduling.
    sections.sort(key=lambda s_: s_["id"])

    meta = {
        "crs": str(crs), "res": RES, "section_m": SECTION_M,
        "sections": sections,
    }
    (OUT / "sections.json").write_text(json.dumps(meta))
    tpl = (ROOT / "web" / "annotate.template.html").read_text()
    (OUT / "index.html").write_text(tpl.replace("/*DATA*/", json.dumps(meta)))
    total = sum(len(s["imgs"]) for s in sections)
    print(f"\n{len(sections)} sections, {total} chips -> {OUT}")
    print(f"open {OUT / 'index.html'}")


if __name__ == "__main__":
    main()
