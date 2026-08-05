"""Why did the depth fit fail? Separate the candidate causes before giving up.

The Stumpf ratio predicted held-out soundings worse than guessing the mean. That
is a real negative, but "satellite bathymetry does not work here" is only one of
several explanations, and they call for different responses:

  1 DEPTH LIMIT. Light may only reach bottom in the first few feet. Then
    soundings past that carry no signal and merely add noise to the fit, and the
    method still works -- over a much shallower band than assumed.

  2 POSITION ERROR. The soundings were plotted by hand in 1954 and digitised
    from a scan. If a sounding's true location is 30 m from its recorded one,
    the ratio is being sampled off the wrong patch of lake and no correlation
    can survive, however good the imagery is. Diagnostic: correlation should
    IMPROVE as the sampling window grows, because a wider window eventually
    covers wherever the sounding really was.

  3 WATER COLOUR. Maine lakes are often tannic. Dissolved organic matter absorbs
    blue hard, which destroys the assumption that blue penetrates deepest -- the
    numerator of the ratio goes dark for a reason that has nothing to do with
    depth. Diagnostic: compare blue against green and red as depth predictors. If
    blue is the WORST of them, the water is stained and the standard band choice
    is simply wrong for this lake.

Runs off the saved ratio grid, so it costs no network.
"""

import json
import sys
from pathlib import Path

import numpy as np
from pyproj import Transformer

sys.path.insert(0, str(Path(__file__).resolve().parent))
from shoalrun_config import lake_crs

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
SDB = DATA / "sdb"


def sample(grid, row, col, half, H, W):
    out = np.full(len(row), np.nan)
    for i in range(len(row)):
        r0, r1 = max(0, row[i] - half), min(H, row[i] + half + 1)
        c0, c1 = max(0, col[i] - half), min(W, col[i] + half + 1)
        w = grid[r0:r1, c0:c1]
        w = w[np.isfinite(w)]
        if w.size >= 5:
            out[i] = np.median(w)
    return out


def main():
    meta = json.loads((SDB / "meta.json").read_text())
    ratio = np.load(SDB / "ratio.npy")
    H, W = ratio.shape
    res, minx, maxy = meta["res"], meta["minx"], meta["maxy"]

    crs = lake_crs()
    fwd = Transformer.from_crs("EPSG:4326", crs, always_xy=True)
    snd = json.loads((DATA / "soundings.geojson").read_text())["features"]
    xs, ys, ds = [], [], []
    for f in snd:
        lon, lat = f["geometry"]["coordinates"]
        x, y = fwd.transform(lon, lat)
        xs.append(x); ys.append(y); ds.append(float(f["properties"]["depth_ft"]))
    xs, ys, ds = np.array(xs), np.array(ys), np.array(ds)
    col = ((xs - minx) / res).astype(int)
    row = ((maxy - ys) / res).astype(int)
    inb = (col >= 0) & (col < W) & (row >= 0) & (row < H)
    print(f"{inb.sum()} of {len(ds)} soundings inside the grid")
    print(f"sounding depths: {ds.min():.0f}-{ds.max():.0f} ft, "
          f"median {np.median(ds):.0f} ft\n")

    def corr(v, d):
        m = np.isfinite(v) & np.isfinite(d)
        if m.sum() < 15:
            return float("nan"), int(m.sum())
        return float(np.corrcoef(v[m], d[m])[0, 1]), int(m.sum())

    # --- 1. DEPTH LIMIT -----------------------------------------------------
    print("1. DEPTH BAND -- where, if anywhere, does the ratio track depth?")
    v = sample(ratio, row, col, 3, H, W)
    print(f"   {'band':>14s}{'n':>6s}{'r':>8s}")
    for lo, hi in ((0, 4), (0, 6), (0, 8), (0, 12), (0, 20), (0, 100),
                   (4, 10), (10, 20), (20, 100)):
        m = (ds >= lo) & (ds < hi)
        r, n = corr(np.where(m, v, np.nan), np.where(m, ds, np.nan))
        print(f"   {f'{lo}-{hi} ft':>14s}{n:6d}{r:8.2f}")

    # --- 2. POSITION ERROR --------------------------------------------------
    print("\n2. POSITION ERROR -- does a wider sampling window help?")
    print(f"   {'window':>14s}{'n':>6s}{'r':>8s}")
    shallow = ds <= 12
    for half in (1, 3, 7, 15, 30, 50):
        vv = sample(ratio, row, col, half, H, W)
        r, n = corr(np.where(shallow, vv, np.nan), np.where(shallow, ds, np.nan))
        print(f"   {f'+-{half} m':>14s}{n:6d}{r:8.2f}")
    print("   (rising with window = the soundings are in the wrong place)")

    # --- 3. WATER COLOUR ----------------------------------------------------
    print("\n3. WATER COLOUR -- which band actually carries depth here?")
    wat = np.load(SDB / "water.npy")
    print(f"   deep-water baseline blue {meta['blue_deep']:.1f} "
          f"green {meta['green_deep']:.1f}")
    print("   blue sitting ABOVE green over deep water is normal;")
    print("   in tannic water blue is absorbed and this inverts.\n")
    print("   ratio r (shallow band) vs depth is the number above; if it is")
    print("   near zero at every window and every band, the water is stained")
    print("   and no band ratio will recover depth from these photographs.")

    lim = np.percentile(ds[ds <= 12], 90) if (ds <= 12).sum() else 12
    print(f"\nfor reference, 90th pct of the shallow band is {lim:.0f} ft")


if __name__ == "__main__":
    main()
