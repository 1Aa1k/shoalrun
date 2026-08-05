"""Render NAIP true-colour with our detections on top, so claims can be checked by eye.

Nate can see rocks in a commercial basemap that our detector does not report.
Two possible explanations, and they call for opposite responses:

  a) the rocks are not in the NAIP pixels  -> the source is the problem
  b) the rocks ARE in the pixels, unflagged -> the detector is the problem

Arguing about source quality cannot distinguish these. Rendering the actual
pixels with the actual detections on top can. Anything visible here that has no
marker is a detector failure, full stop.
"""

import json
import sys
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

# The area screenshotted in Apple Maps: the rocky southwestern basin.
CENTER_LON, CENTER_LAT = -68.8180, 45.7300
SPAN_M = 900.0
RES = 0.3


def main():
    crs = lake_crs()
    fwd = Transformer.from_crs("EPSG:4326", crs, always_xy=True)
    cx, cy = fwd.transform(CENTER_LON, CENTER_LAT)
    n = int(SPAN_M / RES)
    t = from_origin(cx - SPAN_M / 2, cy + SPAN_M / 2, RES, RES)

    cat = pystac_client.Client.open(
        "https://planetarycomputer.microsoft.com/api/stac/v1",
        modifier=planetary_computer.sign_inplace,
    )
    items = [i for i in cat.search(collections=["naip"],
                                   bbox=[CENTER_LON - 0.02, CENTER_LAT - 0.02,
                                         CENTER_LON + 0.02, CENTER_LAT + 0.02],
                                   datetime="2023-01-01/2023-12-31").items()]
    print(f"{len(items)} NAIP 2023 scenes over the chip")

    rgb = np.zeros((3, n, n), "float32")
    nir = np.zeros((n, n), "float32")
    got = np.zeros((n, n), bool)
    for it in items:
        bands = np.zeros((4, n, n), "float32")
        try:
            with rasterio.open(it.assets["image"].href) as src:
                for bi in range(4):
                    reproject(rasterio.band(src, bi + 1), bands[bi], dst_transform=t,
                              dst_crs=crs, resampling=Resampling.average)
        except Exception as e:
            print("  read failed:", e)
            continue
        have = bands.sum(axis=0) > 0
        for bi in range(3):
            rgb[bi][have] = bands[bi][have]
        nir[have] = bands[3][have]
        got |= have

    print(f"coverage {got.mean():.0%}")
    raw = np.transpose(rgb, (1, 2, 0))
    # Display copy only. Keep `raw` for measurement: the stretch below clips
    # everything above the 99th percentile to 1.0, so measuring a 99.5th
    # percentile threshold on the stretched image compares saturated values
    # against a saturated threshold and reports zero bright pixels -- which is
    # what it did, and zero is impossible when rocks are plainly visible.
    img = np.clip(raw / max(1.0, np.percentile(raw[got], 99)), 0, 1)

    # Our hazards inside the chip.
    haz = json.loads((ROOT / "data" / "hazards.geojson").read_text())["features"]
    px, py, cls = [], [], []
    for f in haz:
        p = f["properties"]
        if p.get("lon") is None:
            continue
        x, y = fwd.transform(p["lon"], p["lat"])
        col = (x - (cx - SPAN_M / 2)) / RES
        row = ((cy + SPAN_M / 2) - y) / RES
        if 0 <= col < n and 0 <= row < n:
            px.append(col)
            py.append(row)
            cls.append(p.get("class"))
    print(f"{len(px)} of our hazards fall in this chip")

    fig, axes = plt.subplots(1, 2, figsize=(20, 10), dpi=110)
    for ax in axes:
        ax.imshow(img)
        ax.set_xticks([])
        ax.set_yticks([])
    axes[0].set_title("NAIP 0.3 m true colour - what the imagery actually shows", fontsize=13)

    colors = {"shoal": "#ffb02e", "rock": "#e0574a", "exposed": "#c3d0da", "island": "#5a6b78"}
    for c in set(cls):
        sel = [i for i, k in enumerate(cls) if k == c]
        axes[1].scatter([px[i] for i in sel], [py[i] for i in sel], s=90,
                        facecolors="none", edgecolors=colors.get(c, "w"),
                        linewidths=1.6, label=f"{c} ({len(sel)})")
    axes[1].legend(loc="upper right", fontsize=10)
    axes[1].set_title("same pixels + our detections - anything bright with no circle is a MISS",
                      fontsize=13)

    out = ROOT / "data" / "eyeball.png"
    fig.tight_layout()
    fig.savefig(out, bbox_inches="tight")
    print(f"wrote {out}")

    # Quantify: how bright are the unflagged bright things? Measured on `raw`.
    lum = raw.mean(axis=2)
    ndwi = np.where(rgb[1] + nir > 0, (rgb[1] - nir) / (rgb[1] + nir + 1e-6), np.nan)
    water = (ndwi > 0) & got
    if water.sum() > 1000:
        thr = np.percentile(lum[water], 99.5)
        bright = (lum > thr) & water
        print(f"\nbright-in-water pixels (top 0.5% luminance): {bright.sum():,}")
        from scipy import ndimage
        lbl, nb = ndimage.label(bright)
        sizes = ndimage.sum(bright, lbl, range(1, nb + 1))
        big = (sizes >= 4).sum()
        print(f"  clustered into {nb} blobs, {big} of >=4 px (>=0.36 m2)")
        print(f"  our detections in this chip: {len(px)}")


if __name__ == "__main__":
    main()
