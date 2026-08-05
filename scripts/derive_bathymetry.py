"""Turn the imagery into a depth map, calibrated and cross-validated on soundings.

This is the layering Nate asked for, and the sandy halo is what makes it work.
The halo is not merely a cue that rocks are nearby -- it is a measurement. Blue
light penetrates water several metres, green is absorbed sooner, and red within
about a metre. So the ratio between bands varies continuously with how much
bottom is showing through, which is to say with depth.

Stumpf's log ratio:

    ratio = ln(blue - blue_deep) / ln(green - green_deep)
    depth = m1 * ratio + m0

The deep-water subtraction matters. Some light never reaches the bottom at all --
it scatters back off the water column itself -- and leaving that in makes deep
water look like a shallow bottom. It is estimated from the darkest water in the
scene, which by construction is where no bottom is visible.

The ratio form is what makes this robust: bottom type divides out. A sand bottom
and a weed bottom at the same depth differ hugely in brightness, but both bands
change together, so the quotient stays put. That is why a ratio beats any single
band, and why the halo is legible at all.

WHY THIS IS THE HONEST PATH. Every previous score needed ground truth we do not
have -- 32 OSM rocks, too few to separate skill from density. This needs none.
The 260 MDIFW soundings are split: the fit sees 70% of them, and is scored on
the 30% it has never seen. That is a real generalisation test, automatic, with
error in feet. If the imagery genuinely carries depth information, held-out
soundings will be predicted; if it does not, they will not, and no amount of
threshold fiddling will hide it.

Only the 2021 flight is used. Stumpf calibration is per-image -- it assumes one
atmosphere, one sun angle, one water state -- and 2021 measured 2.8x the
rock-to-water contrast of any other flight. Mixing dates would mix radiometry
and corrupt the fit.
"""

import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import planetary_computer
import pystac_client
import rasterio
from pyproj import Transformer
from rasterio.features import rasterize
from rasterio.transform import from_origin
from rasterio.warp import Resampling, reproject
from scipy import ndimage
from shapely.geometry import Point, mapping, shape
from shapely.ops import transform as shp_transform

sys.path.insert(0, str(Path(__file__).resolve().parent))
from shoalrun_config import lake_crs

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
OUT = DATA / "sdb"

YEAR = 2021
RES = 1.0
TILE = 2048
PAD = 32
WORKERS = 6
TILE_WORKERS = 4

DEEP_PCT = 0.5      # percentile of water taken as "no bottom visible"
TRAIN_FRAC = 0.7
SEED = 17
MAX_FIT_DEPTH_FT = 25.0   # beyond this no light returns; see fit()


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    crs = lake_crs()
    fwd = Transformer.from_crs("EPSG:4326", crs, always_xy=True)
    lake_ll = shape(json.loads((DATA / "lake.geojson").read_text())["geometry"])
    lake = shp_transform(lambda x, y: fwd.transform(x, y), lake_ll)

    cat = pystac_client.Client.open(
        "https://planetarycomputer.microsoft.com/api/stac/v1",
        modifier=planetary_computer.sign_inplace,
    )
    items = [i for i in cat.search(collections=["naip"], bbox=lake_ll.bounds).items()
             if i.datetime.year == YEAR]
    print(f"{len(items)} NAIP {YEAR} scenes")

    minx, miny, maxx, maxy = lake.bounds
    W, H = int((maxx - minx) / RES), int((maxy - miny) / RES)
    print(f"grid {W} x {H} @ {RES} m")

    blue = np.full((H, W), np.nan, "float32")
    green = np.full((H, W), np.nan, "float32")
    water = np.zeros((H, W), bool)

    tiles = [(y0, x0) for y0 in range(0, H, TILE) for x0 in range(0, W, TILE)]

    def do_tile(t):
        y0, x0 = t
        y1, x1 = min(H, y0 + TILE), min(W, x0 + TILE)
        gy0, gy1 = max(0, y0 - PAD), min(H, y1 + PAD)
        gx0, gx1 = max(0, x0 - PAD), min(W, x1 + PAD)
        th, tw = gy1 - gy0, gx1 - gx0
        tr = from_origin(minx + gx0 * RES, maxy - gy0 * RES, RES, RES)
        wm = rasterize([(mapping(lake), 1)], out_shape=(th, tw), transform=tr,
                       dtype="uint8").astype(bool)
        if wm.sum() < 200:
            return None
        rgb = np.zeros((3, th, tw), "float32")
        nir = np.zeros((th, tw), "float32")
        got = np.zeros((th, tw), bool)

        def _read(it):
            b = np.zeros((4, th, tw), "float32")
            try:
                with rasterio.open(it.assets["image"].href) as src:
                    for bi in range(4):
                        reproject(rasterio.band(src, bi + 1), b[bi], dst_transform=tr,
                                  dst_crs=crs, resampling=Resampling.average)
                return b
            except Exception:
                return None

        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            for b in pool.map(_read, items):
                if b is None:
                    continue
                have = b.sum(axis=0) > 0
                for bi in range(3):
                    rgb[bi][have] = b[bi][have]
                nir[have] = b[3][have]
                got |= have
        if got.sum() < 200:
            return None
        with np.errstate(invalid="ignore", divide="ignore"):
            ndwi = np.where(rgb[1] + nir > 0, (rgb[1] - nir) / (rgb[1] + nir + 1e-6), np.nan)
        wet = got & wm & np.isfinite(ndwi) & (ndwi > 0)
        sy = slice(y0 - gy0, y0 - gy0 + (y1 - y0))
        sx = slice(x0 - gx0, x0 - gx0 + (x1 - x0))
        return (y0, y1, x0, x1,
                ndimage.median_filter(rgb[2], 3)[sy, sx],
                ndimage.median_filter(rgb[1], 3)[sy, sx],
                wet[sy, sx])

    done = 0
    with ThreadPoolExecutor(max_workers=TILE_WORKERS) as pool:
        for r in pool.map(do_tile, tiles):
            done += 1
            if r is None:
                continue
            y0, y1, x0, x1, b_, g_, w_ = r
            blue[y0:y1, x0:x1] = b_
            green[y0:y1, x0:x1] = g_
            water[y0:y1, x0:x1] = w_
            if done % 4 == 0:
                print(f"  {done}/{len(tiles)} tiles", flush=True)

    print(f"water pixels: {water.sum():,}")

    # Deep-water baseline: light that never reached the bottom. Subtracting it is
    # what stops open water from reading as a bright shallow bottom.
    b_deep = float(np.percentile(blue[water], DEEP_PCT))
    g_deep = float(np.percentile(green[water], DEEP_PCT))
    print(f"deep-water baseline: blue {b_deep:.1f}, green {g_deep:.1f}")

    bb = np.maximum(blue - b_deep, 1.0)
    gg = np.maximum(green - g_deep, 1.0)
    with np.errstate(invalid="ignore", divide="ignore"):
        ratio = np.log(bb) / np.log(gg)
    ratio = np.where(water, ratio, np.nan)

    np.save(OUT / "ratio.npy", ratio.astype("float32"))
    np.save(OUT / "water.npy", water)
    (OUT / "meta.json").write_text(json.dumps({
        "year": YEAR, "res": RES, "minx": minx, "maxy": maxy, "W": W, "H": H,
        "blue_deep": b_deep, "green_deep": g_deep, "crs": str(crs),
    }, indent=1))
    print(f"wrote {OUT}/ratio.npy")

    fit(ratio, water, minx, maxy, fwd)


def fit(ratio, water, minx, maxy, fwd):
    """Calibrate ratio -> depth on 70% of soundings, score on the other 30%."""
    snd = json.loads((DATA / "soundings.geojson").read_text())["features"]
    xs, ys, ds = [], [], []
    for f in snd:
        lon, lat = f["geometry"]["coordinates"]
        x, y = fwd.transform(lon, lat)
        xs.append(x); ys.append(y); ds.append(float(f["properties"]["depth_ft"]))
    xs, ys, ds = np.array(xs), np.array(ys), np.array(ds)

    col = ((xs - minx) / RES).astype(int)
    row = ((maxy - ys) / RES).astype(int)
    H, W = ratio.shape
    ok = (col >= 0) & (col < W) & (row >= 0) & (row < H)

    # Average the ratio in a small window: a sounding's recorded position is
    # good to boat-GPS-of-1954, not to one metre.
    vals = np.full(len(ds), np.nan)
    for i in np.nonzero(ok)[0]:
        r0, r1 = max(0, row[i] - 3), min(H, row[i] + 4)
        c0, c1 = max(0, col[i] - 3), min(W, col[i] + 4)
        win = ratio[r0:r1, c0:c1]
        win = win[np.isfinite(win)]
        if win.size >= 5:
            vals[i] = np.median(win)

    good = np.isfinite(vals) & np.isfinite(ds)
    # Past a certain depth no light returns from the bottom at all, so the ratio
    # carries no information and those soundings would only add noise to the
    # fit. The limit is a property of the water, not a convenience.
    good &= ds <= MAX_FIT_DEPTH_FT
    n = int(good.sum())
    print(f"\nsoundings usable for calibration: {n} of {len(ds)} "
          f"(<= {MAX_FIT_DEPTH_FT:g} ft, with imagery)")
    if n < 30:
        print("too few to calibrate honestly -- stopping")
        return

    v, d = vals[good], ds[good]
    rng = np.random.default_rng(SEED)
    idx = rng.permutation(n)
    ntr = int(TRAIN_FRAC * n)
    tr, te = idx[:ntr], idx[ntr:]

    m1, m0 = np.polyfit(v[tr], d[tr], 1)
    pred_tr = m1 * v[tr] + m0
    pred_te = m1 * v[te] + m0

    def score(p, a):
        rmse = float(np.sqrt(np.mean((p - a) ** 2)))
        r = float(np.corrcoef(p, a)[0, 1])
        return rmse, r, r * r

    rm_tr, r_tr, r2_tr = score(pred_tr, d[tr])
    rm_te, r_te, r2_te = score(pred_te, d[te])

    print(f"\ndepth = {m1:.2f} * ratio + {m0:.2f}")
    print(f"{'':10s}{'n':>5s}{'RMSE ft':>9s}{'r':>7s}{'R2':>7s}")
    print(f"{'train':10s}{len(tr):5d}{rm_tr:9.2f}{r_tr:7.2f}{r2_tr:7.2f}")
    print(f"{'HELD OUT':10s}{len(te):5d}{rm_te:9.2f}{r_te:7.2f}{r2_te:7.2f}")

    # Baseline: predicting the mean depth every time. Beating this is the bar --
    # an R2 near zero means the imagery told us nothing.
    base = float(np.sqrt(np.mean((np.mean(d[tr]) - d[te]) ** 2)))
    print(f"{'baseline':10s}{len(te):5d}{base:9.2f}{0.0:7.2f}{0.0:7.2f}"
          f"   <- always guess the average depth")
    if rm_te < base:
        print(f"\nimagery beats the naive guess by {base - rm_te:.2f} ft RMSE "
              f"({(1 - rm_te/base)*100:.0f}% better)")
    else:
        print("\nimagery does NOT beat guessing the average -- no depth signal")

    (OUT / "fit.json").write_text(json.dumps({
        "m1": float(m1), "m0": float(m0), "n_train": len(tr), "n_test": len(te),
        "rmse_train_ft": rm_tr, "rmse_heldout_ft": rm_te,
        "r2_train": r2_tr, "r2_heldout": r2_te, "rmse_baseline_ft": base,
        "max_fit_depth_ft": MAX_FIT_DEPTH_FT,
    }, indent=1))
    print(f"wrote {OUT}/fit.json")


if __name__ == "__main__":
    main()
