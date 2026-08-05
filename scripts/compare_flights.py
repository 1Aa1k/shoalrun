"""Show all six NAIP flights over the same rocky spot, side by side.

The claim to check by eye: some flights are clear (dark water, rocks obvious)
and some are washed out by sun glare, and averaging them all together smears the
good ones into the bad. Renders the identical patch of water from every flight so
the difference is visible rather than argued about.
"""

import sys
from collections import defaultdict
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
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

# Rocky area in the southwestern basin, the same one shown in Apple Maps.
LON, LAT = -68.8180, 45.7300
SPAN_M = 500.0
RES = 0.5


def main():
    crs = lake_crs()
    fwd = Transformer.from_crs("EPSG:4326", crs, always_xy=True)
    cx, cy = fwd.transform(LON, LAT)
    n = int(SPAN_M / RES)
    t = from_origin(cx - SPAN_M / 2, cy + SPAN_M / 2, RES, RES)

    cat = pystac_client.Client.open(
        "https://planetarycomputer.microsoft.com/api/stac/v1",
        modifier=planetary_computer.sign_inplace,
    )
    items = list(cat.search(collections=["naip"],
                            bbox=[LON - 0.02, LAT - 0.02, LON + 0.02, LAT + 0.02]).items())
    by_year = defaultdict(list)
    for i in items:
        by_year[i.datetime.year].append(i)
    years = sorted(by_year)
    print(f"flights: {years}")

    panels = []
    for y in years:
        rgb = np.zeros((3, n, n), "float32")
        nir = np.zeros((n, n), "float32")
        got = np.zeros((n, n), bool)
        date = None
        for it in by_year[y]:
            b = np.zeros((4, n, n), "float32")
            try:
                with rasterio.open(it.assets["image"].href) as src:
                    for bi in range(4):
                        reproject(rasterio.band(src, bi + 1), b[bi], dst_transform=t,
                                  dst_crs=crs, resampling=Resampling.average)
            except Exception:
                continue
            have = b.sum(axis=0) > 0
            for bi in range(3):
                rgb[bi][have] = b[bi][have]
            nir[have] = b[3][have]
            got |= have
            date = it.datetime.date()
        if got.mean() < 0.5:
            continue

        img = np.transpose(rgb, (1, 2, 0))
        hi = np.percentile(img[got], 99.5) or 1.0
        disp = np.clip(img / hi, 0, 1)

        # Water = green above NIR. Contrast score: how much brighter the
        # brightest things in the water are than typical water. High score means
        # rocks stand out; low means glare has flattened everything.
        with np.errstate(invalid="ignore", divide="ignore"):
            ndwi = np.where(rgb[1] + nir > 0, (rgb[1] - nir) / (rgb[1] + nir + 1e-6), np.nan)
        water = (ndwi > 0) & got
        lum = img.mean(axis=2)
        if water.sum() > 5000:
            wl = lum[water]
            med = float(np.median(wl))
            p999 = float(np.percentile(wl, 99.9))
            spread = float(np.percentile(wl, 75) - np.percentile(wl, 25))
            # Contrast of the brightest specks against typical water, in units of
            # ordinary water variation. Bigger = rocks pop.
            score = (p999 - med) / (spread + 1e-6)
        else:
            med = p999 = score = float("nan")
        panels.append((y, date, disp, score, med, p999))
        print(f"  {y} ({date}): contrast score {score:5.1f}   water median {med:.3f}")

    panels.sort(key=lambda p: -p[3])
    cols = 3
    rows = (len(panels) + cols - 1) // cols
    fig, axes = plt.subplots(rows, cols, figsize=(6 * cols, 6 * rows), dpi=100)
    axes = np.atleast_1d(axes).ravel()
    for ax in axes:
        ax.axis("off")
    for ax, (y, date, disp, score, med, p999) in zip(axes, panels):
        ax.imshow(disp)
        ax.axis("off")
        verdict = "CLEAR - rocks visible" if score > np.nanmedian([p[3] for p in panels]) else "GLARY - washed out"
        ax.set_title(f"{y}  ({date})\ncontrast {score:.1f}  -  {verdict}", fontsize=12)
    fig.suptitle("Same patch of water, every NAIP flight. Sorted best contrast first.\n"
                 "Bright white specks in the water are rocks.", fontsize=15)
    out = ROOT / "data" / "flight_comparison.png"
    fig.tight_layout()
    fig.savefig(out, bbox_inches="tight")
    print(f"\nwrote {out}")


if __name__ == "__main__":
    main()
