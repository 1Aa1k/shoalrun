"""Does satellite-derived bathymetry work on this lake? Measure, do not assume.

Satellite-derived bathymetry (SDB) infers depth from how much light the water
column absorbs: the deeper the water, the darker the bottom reads. The standard
empirical forms are Lyzenga (log of a single band, linear in depth) and Stumpf
(the ratio of two bands' logs, which cancels bottom albedo). Both need labels to
fit against, and this lake has 260 of them from 1954.

This script does not build anything. It answers one question -- is there a usable
depth signal in the imagery we already hold -- so the decision to invest in SDB
is made on a number rather than on the fact that the technique exists.

Two things bound the answer before it starts:

  * We only fetched green and NIR. Stumpf needs blue, so only the weaker
    single-band Lyzenga form can be tested here. If green alone shows signal,
    blue/green would show more.
  * SDB reaches roughly one to one-and-a-half Secchi depths. A tannic Maine lake
    is not the Bahamas, so the honest expectation is that this works in the
    shallows and fails in the basin -- which happens to be where the hazards
    are.

Validation is against HELD-OUT soundings. Fitting two parameters to 260 points
and reporting the fit quality on the same points would measure nothing.
"""

import json
import sys
from pathlib import Path

import numpy as np
from pyproj import Transformer

sys.path.insert(0, str(Path(__file__).resolve().parent))
from shoalrun_config import lake_crs

ROOT = Path(__file__).resolve().parent.parent
STACK = ROOT / "data" / "stack.npz"
SOUND = ROOT / "data" / "soundings.geojson"

# The README's finding: only July-August are radiometrically stable here. Autumn
# sun angles at 45.7N put specular response over the water that swamps the
# signal, and one October scene read the lake as half dry.
TRUSTED_MONTHS = (7, 8)


def main():
    d = np.load(STACK, allow_pickle=True)
    meta = json.loads(str(d["meta"]))
    green = d["green"]
    nir = d["nir"]
    valid = d["valid"]
    tr = d["transform"]

    keep = [
        i for i, m in enumerate(meta)
        if int(m["date"][5:7]) in TRUSTED_MONTHS and m["usable"] > 0.8
    ]
    print(f"{len(keep)} of {len(meta)} scenes in the trusted window")
    if not keep:
        raise SystemExit("no trusted scenes")

    # Median across scenes: a single date carries sun glint and wave texture that
    # a depth model would happily fit and then fail on.
    g = np.where(valid[keep], green[keep], np.nan)
    gmed = np.nanmedian(g, axis=0)
    nmed = np.nanmedian(np.where(valid[keep], nir[keep], np.nan), axis=0)

    # Soundings to pixel coordinates.
    fwd = Transformer.from_crs("EPSG:4326", lake_crs(), always_xy=True)
    x0, px, _, y0, _, py = tr
    feats = json.loads(SOUND.read_text())["features"]

    rows, cols, depth = [], [], []
    for f in feats:
        lon, lat = f["geometry"]["coordinates"]
        ux, uy = fwd.transform(lon, lat)
        c = int(round((ux - x0) / px))
        r = int(round((uy - y0) / py))
        if 0 <= r < gmed.shape[0] and 0 <= c < gmed.shape[1]:
            rows.append(r)
            cols.append(c)
            depth.append(f["properties"]["depth_ft"])

    rows = np.array(rows)
    cols = np.array(cols)
    depth = np.array(depth, dtype=float)

    # 3x3 mean around each sounding. A 1954 sounding's position is good to tens
    # of metres at best, so pinning it to one 10 m pixel asserts a precision the
    # label does not have.
    def patch(arr):
        out = np.full(len(rows), np.nan)
        for i, (r, c) in enumerate(zip(rows, cols)):
            w = arr[max(0, r - 1):r + 2, max(0, c - 1):c + 2]
            w = w[np.isfinite(w)]
            if w.size:
                out[i] = w.mean()
        return out

    gv = patch(gmed)
    nv = patch(nmed)
    ok = np.isfinite(gv) & np.isfinite(nv) & (gv > 0)
    gv, nv, dv = gv[ok], nv[ok], depth[ok]
    print(f"{ok.sum()} soundings landed on valid pixels, {dv.min():.0f}-{dv.max():.0f} ft")

    # Lyzenga: ln(radiance above deep-water floor) is linear in depth. The deep
    # water value is the asymptote the signal decays toward, estimated as a low
    # percentile of the whole lake rather than assumed to be zero.
    water = gmed[np.isfinite(gmed)]
    deep = np.percentile(water, 2)
    X = np.log(np.maximum(gv - deep, 1e-6))

    def report(name, x, y):
        # Fit on half, score on the other half. Repeated over shuffles so the
        # number is not an artefact of one lucky split.
        rng = np.random.default_rng(0)
        r2s, maes = [], []
        for _ in range(200):
            idx = rng.permutation(len(y))
            h = len(y) // 2
            tr_i, te_i = idx[:h], idx[h:]
            A = np.vstack([x[tr_i], np.ones(h)]).T
            coef, *_ = np.linalg.lstsq(A, y[tr_i], rcond=None)
            pred = coef[0] * x[te_i] + coef[1]
            resid = y[te_i] - pred
            ss = 1 - (resid ** 2).sum() / ((y[te_i] - y[te_i].mean()) ** 2).sum()
            r2s.append(ss)
            maes.append(np.abs(resid).mean())
        print(f"  {name:28s} held-out R2 {np.mean(r2s):+.3f}   MAE {np.mean(maes):.1f} ft")
        return np.mean(r2s)

    print("\nSingle-band Lyzenga, green:")
    report("whole lake", X, dv)

    # The shallows are the part that matters and the part SDB can physically
    # reach. Reported separately because a whole-lake number is dominated by
    # deep water the method cannot see into, and would understate it.
    for lim in (10, 15, 20, 30):
        m = dv <= lim
        if m.sum() > 40:
            report(f"<= {lim} ft  (n={m.sum()})", X[m], dv[m])

    # Correlation with NIR is the control. Water absorbs NIR almost completely,
    # so NIR must NOT predict depth. If it does, the "signal" is something on the
    # surface -- glint, cloud, wave texture -- and not the water column at all.
    print("\nNIR control (must be near zero -- water absorbs NIR):")
    Xn = np.log(np.maximum(nv - np.percentile(nmed[np.isfinite(nmed)], 2), 1e-6))
    report("NIR, whole lake", Xn, dv)

    print(f"\nraw correlation  green vs depth: {np.corrcoef(X, dv)[0,1]:+.3f}")
    print(f"raw correlation  NIR   vs depth: {np.corrcoef(Xn, dv)[0,1]:+.3f}")


if __name__ == "__main__":
    main()

# RESULT (24 trusted Jul/Aug scenes, 260 soundings, held-out validation):
#
#   whole lake      R2 -0.043   MAE 13.4 ft
#   <= 10 ft        R2 -0.086   MAE  2.5 ft
#   <= 15 ft        R2 -0.446   MAE  3.3 ft
#   NIR control     R2 -0.012
#
#   raw correlation  green vs depth  +0.090
#   raw correlation  NIR   vs depth  +0.178
#
# Every R2 is NEGATIVE -- the fit is worse than predicting the mean depth. And
# the control fails in the diagnostic direction: NIR, which water absorbs almost
# completely and which therefore CANNOT carry depth, correlates twice as hard as
# green. Whatever weak association exists is something on the surface, not in the
# water column.
#
# Binned medians say the same thing without any model:
#
#     0-5 ft  n=19   green 1116.7
#    10-15 ft n=28   green 1109.6
#    45-80 ft n=28   green 1120.0
#
# Flat, and if anything deep water reads slightly BRIGHTER, which is backwards.
# The whole spread across depth is ~4 DN against a per-sounding sd of 22.
#
# Conclusion: the water column here is optically opaque at 10 m. This is a
# tannic Maine lake -- dissolved organic carbon absorbs the short wavelengths
# that would carry bottom return, so blue would be worse than green, not better,
# and no amount of model capacity recovers a signal that is not in the photons.
# SDB is not available on this lake. Do not revisit without a reason to think the
# water clarity assumption has changed.
